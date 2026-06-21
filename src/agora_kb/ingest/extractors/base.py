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
   **SSRF (Phase-4 concern).** :func:`extract_url` fetches a server-side URL. In a future multi-user
   deployment that is a Server-Side Request Forgery vector (fetching internal/cloud-metadata URLs).
   No auth/allowlisting is built here: Phase 3 is localhost single-user. See ADR-0004 and the
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
) -> ExtractedDoc:
    """Dispatch to the right extractor and return an :class:`ExtractedDoc`.

    Exactly one of ``url`` or ``data`` must be provided:

    - ``url`` given → the URL extractor (:func:`~agora_kb.ingest.extractors.url.extract_url`).
    - ``data`` given → choose PDF vs office by ``mime``/``filename`` extension: ``.pdf`` (or
      ``application/pdf``) → PDF; ``.docx``/``.xlsx``/``.pptx``/``.doc``… (or the matching office
      MIME) → office.

    Raises :class:`ValueError` when neither or both of ``url``/``data`` are given, or when the
    ``data`` type is unsupported/ambiguous (no recognizable PDF/office signal).
    """
    if (url is None) == (data is None):
        raise ValueError(
            "exactly one of `url` or `data` must be provided (got "
            f"url={'set' if url is not None else 'None'}, "
            f"data={'set' if data is not None else 'None'})"
        )

    if url is not None:
        from .url import extract_url

        return extract_url(url)

    # data path — choose pdf vs office.
    assert data is not None  # narrowed by the XOR check above; for type-checkers.
    ext = _ext(filename)
    norm_mime = mime.split(";", 1)[0].strip().lower() if mime else None

    is_pdf = norm_mime == _PDF_MIME or ext == ".pdf"
    is_office = norm_mime in _OFFICE_MIMES or ext in _OFFICE_EXTS

    if is_pdf and is_office:
        raise ValueError(
            f"ambiguous upload: both PDF and office signals (mime={mime!r}, filename={filename!r})"
        )
    if is_pdf:
        from .pdf import extract_pdf

        return extract_pdf(data, filename=filename)
    if is_office:
        from .office import extract_office

        if not filename:
            raise ValueError("office extraction requires a filename to determine the document type")
        return extract_office(data, filename=filename)

    raise ValueError(
        "unsupported or ambiguous upload: cannot determine extractor from "
        f"mime={mime!r}, filename={filename!r}. Supported: .pdf and "
        ".docx/.doc/.xlsx/.xls/.pptx/.ppt office documents."
    )
