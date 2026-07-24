"""Ingest extractors — the input-adapter family of ADR-0004 (DESIGN §2.3).

Each extractor is a **pure transform**: it turns one upload (a URL, or uploaded ``bytes`` with a
filename) into an :class:`ExtractedDoc` carrying the extracted markdown/plain text plus best-effort
provenance metadata. These functions deliberately touch **nothing** on the filesystem destination
side — no ``raw/``, no inbox, no git, no DB. The web upload handler (a later unit) calls
:func:`extract` and then ``Inbox.write(text=doc.markdown, source="web:<user>")``; the curator
materializes ``raw/`` from the capture body. Keeping extraction pure makes it trivially testable and
keeps the integrity boundary (who may write the repo) entirely on the inbox/curator side.

Optional dependencies (``trafilatura``/``pdfminer.six``/``markitdown``) live in the ``ingest`` extra
(``pip install agora-kb[ingest]`` / ``uv sync --extra ingest``) and are imported **lazily inside the
functions** — never at module top level — so ``import agora_kb`` and ``import
agora_kb.ingest.extractors`` work in a dependency-light core install (invariant 4 / ADR-0005). A
missing dependency raises :class:`ExtractorUnavailable`, not a bare ``ImportError`` at import time.

Uploaded bytes and fetched URLs are **untrusted** input: every third-party call is wrapped so that
malformed/garbage input raises :class:`ExtractorError` rather than leaking an arbitrary traceback.

.. note::
   **SSRF guard (issue #66 — implemented).** :func:`extract_url` fetches a server-side URL behind a
   default-on SSRF guard: http/https only, every resolved address (and every redirect hop) must be
   public, and the connection is pinned to the validated IP (DNS-rebinding defence). A local caller
   may opt in to internal URLs with ``allow_private=True``; the web face never does. See the
   docstring of :func:`agora_kb.ingest.extractors.url.extract_url`.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ExtractedDoc",
    "Extractor",
    "ExtractorError",
    "ExtractorUnavailable",
    "extract",
]

# Filename extensions routed to the office (markitdown) extractor. ``.pdf`` is handled separately.
_OFFICE_EXTS = frozenset({".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"})
# MIME types that unambiguously indicate an office document.
_OFFICE_MIMES = frozenset(
    {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)
_PDF_MIME = "application/pdf"

# Dependency-free text passthrough (ADR-0025): markdown / plain text is already the body.
_TEXT_EXTS = frozenset({".txt", ".md", ".markdown"})
_TEXT_MIMES = frozenset({"text/plain", "text/markdown", "text/x-markdown"})

# WIDENED markitdown routing (ADR-0025): TEXT-based markup/data formats the ALREADY-pinned
# markitdown core handles (no new dependency, invariant 4). Routed to the generic markitdown path
# in office.py.
_MARKITDOWN_EXTS = frozenset({".html", ".htm", ".csv", ".json", ".xml", ".epub"})
_MARKITDOWN_MIMES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/csv",
        "application/json",
        "application/xml",
        "text/xml",
        "application/epub+zip",
    }
)

# .epub IS routed (ADR-0025 rec D, issue #53): markitdown's epub converter needs only the
# already-pinned core (zipfile + beautifulsoup4), and the zip decompression-bomb guard in
# office.py covers it. Image (OCR) and audio (transcription) remain DEFERRED: each needs its own
# optional extra + ADR-0005 vetting (extra deps, privacy/cost), so they are NOT routed here.


class ExtractorUnavailable(RuntimeError):
    """An optional dependency for the chosen extractor is not installed.

    Carries a message naming the missing package and the remedy
    (``pip install agora-kb[ingest]`` / ``uv sync --extra ingest``).
    """


class ExtractorError(RuntimeError):
    """Extraction of malformed/garbage/untrusted input failed.

    Raised when a wrapped third-party extraction call errors out, so callers never see an arbitrary
    library traceback for bad input.
    """


class ExtractedDoc(BaseModel):
    """The result of an extraction: markdown content + best-effort provenance metadata.

    Frozen/immutable — an extracted document is a value, never mutated in place.
    """

    model_config = ConfigDict(frozen=True)

    markdown: str
    title: str | None = None
    source_url: str | None = None
    content_sha256: str
    mime: str | None = None
    extractor: str


@runtime_checkable
class Extractor(Protocol):
    """The callable contract every concrete extractor satisfies.

    A concrete extractor takes its input (URL string, or ``bytes`` + ``filename``) and returns an
    :class:`ExtractedDoc`. The exact signature differs per source (see :func:`extract_url`,
    :func:`extract_pdf`, :func:`extract_office`); this Protocol documents the common return contract
    and lets the dispatcher / callers type against "an extractor".
    """

    def __call__(self, *args: object, **kwargs: object) -> ExtractedDoc: ...


def _ext(filename: str | None) -> str:
    """Lowercase file extension (incl. the dot) of ``filename``, or ``""`` if none."""
    if not filename:
        return ""
    return os.path.splitext(filename)[1].lower()


def extract(
    *,
    url: str | None = None,
    data: bytes | None = None,
    filename: str | None = None,
    mime: str | None = None,
    allow_private: bool = False,
    max_uncompressed_bytes: int | None = None,
) -> ExtractedDoc:
    """Dispatch to the right extractor and return an :class:`ExtractedDoc`.

    Exactly one of ``url`` or ``data`` must be provided:

    - ``url`` given → the URL extractor (:func:`~agora_kb.ingest.extractors.url.extract_url`).
    - ``data`` given → choose the extractor by ``mime``/``filename`` extension:
      ``.pdf`` (or ``application/pdf``) → PDF; ``.docx``/``.xlsx``/``.pptx``/``.doc``… (or the
      matching office MIME) → office; ``.html``/``.csv``/``.json``/``.xml``/``.epub`` (or the
      matching MIME) → markitdown (ADR-0025, reuses the office engine); ``.txt``/``.md``/
      ``.markdown`` (or ``text/plain``/``text/markdown``) → the dependency-free text passthrough.

    Two hardening knobs are forwarded to the extractor they apply to (and ignored by the others),
    and only when set — so the defaults live in ONE place (the concrete extractor) and existing
    :class:`Extractor`-shaped callables/mocks keep working:

    - ``allow_private`` (URL path; issue #66): opt OUT of the SSRF guard for a legitimately
      internal URL — local CLI callers only; the web face never sets it.
    - ``max_uncompressed_bytes`` (office/markitdown zip paths; issue #53): the declared
      uncompressed-total cap of the decompression-bomb guard.

    Raises :class:`ValueError` when neither or both of ``url``/``data`` are given, or when the
    ``data`` type is unsupported/ambiguous (no recognizable PDF/office/markitdown/text signal).
    """
    if (url is None) == (data is None):
        raise ValueError(
            "exactly one of `url` or `data` must be provided (got "
            f"url={'set' if url is not None else 'None'}, "
            f"data={'set' if data is not None else 'None'})"
        )

    if url is not None:
        from .url import extract_url

        if allow_private:
            return extract_url(url, allow_private=True)
        return extract_url(url)

    # data path — choose pdf vs office.
    assert data is not None  # narrowed by the XOR check above; for type-checkers.
    ext = _ext(filename)
    norm_mime = mime.split(";", 1)[0].strip().lower() if mime else None

    is_pdf = norm_mime == _PDF_MIME or ext == ".pdf"
    is_office = norm_mime in _OFFICE_MIMES or ext in _OFFICE_EXTS
    is_markitdown = norm_mime in _MARKITDOWN_MIMES or ext in _MARKITDOWN_EXTS

    # A generic ``text/plain``/``text/markdown`` MIME is a WEAK signal: browsers, OSes, and curl
    # routinely attach it to ``.csv``/``.json``/``.html``/``.xml`` exports (especially on the
    # drag-and-drop path). When a MORE-SPECIFIC pdf/office/markitdown EXTENSION is present, the
    # extension is the authoritative router and the generic text MIME must NOT veto it — otherwise
    # the common ``data.csv`` + ``text/plain`` upload would (wrongly) read as a conflict. So only
    # treat the generic-MIME case as a "text" signal when no stronger extension claims the file.
    has_specific_ext = ext == ".pdf" or ext in _OFFICE_EXTS or ext in _MARKITDOWN_EXTS
    is_text_mime_only = norm_mime in _TEXT_MIMES and ext not in _TEXT_EXTS
    is_text = ext in _TEXT_EXTS or (
        norm_mime in _TEXT_MIMES and not (is_text_mime_only and has_specific_ext)
    )

    # Disjoint-format guard: a contradictory pair of SPECIFIC signals (e.g. a .pdf filename with an
    # office MIME, or a .csv filename with application/pdf) is operator/upload confusion, not a
    # routable document — surface it rather than silently picking a winner. A generic text MIME
    # riding alongside a specific extension (handled above) is NOT a conflict.
    flagged = [
        name
        for name, flag in (
            ("PDF", is_pdf),
            ("office", is_office),
            ("markitdown", is_markitdown),
            ("text", is_text),
        )
        if flag
    ]
    if len(flagged) > 1:
        raise ValueError(
            f"ambiguous upload: conflicting {flagged} signals "
            f"(mime={mime!r}, filename={filename!r})"
        )

    if is_pdf:
        from .pdf import extract_pdf

        return extract_pdf(data, filename=filename)
    if is_office:
        from .office import extract_office

        if not filename:
            raise ValueError("office extraction requires a filename to determine the document type")
        if max_uncompressed_bytes is not None:
            return extract_office(
                data, filename=filename, max_uncompressed_bytes=max_uncompressed_bytes
            )
        return extract_office(data, filename=filename)
    if is_markitdown:
        from .office import extract_markitdown

        if not filename:
            raise ValueError(
                "markitdown extraction requires a filename to determine the document type"
            )
        if max_uncompressed_bytes is not None:
            return extract_markitdown(
                data, filename=filename, max_uncompressed_bytes=max_uncompressed_bytes
            )
        return extract_markitdown(data, filename=filename)
    if is_text:
        from .text import extract_text

        return extract_text(data, filename=filename, mime=mime)

    raise ValueError(
        "unsupported or ambiguous upload: cannot determine extractor from "
        f"mime={mime!r}, filename={filename!r}. Supported: .pdf; "
        ".docx/.doc/.xlsx/.xls/.pptx/.ppt office documents; "
        ".html/.htm/.csv/.json/.xml/.epub (markitdown); and .txt/.md/.markdown text."
    )
