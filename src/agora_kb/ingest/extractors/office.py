"""markitdown extractor — convert office docs + several markup/data formats to markdown.

markitdown (MIT) converts Office Open XML documents (docx/xlsx/pptx) AND a range of other text-based
formats (html, csv, json, xml) to markdown. The optional dependency is imported lazily so the core
stays dependency-light (ADR-0005); a missing install raises :class:`ExtractorUnavailable`. Uploaded
documents are untrusted — a malformed file raises :class:`ExtractorError` rather than leaking a
markitdown traceback.

Two public entry points share one engine (:func:`_extract_markitdown`):

* :func:`extract_office` — the docx/xlsx/pptx office path (``extractor="office"``, ``mime`` from the
  office extension map). Unchanged contract (kept for back-compat / ADR-0020).
* :func:`extract_markitdown` — the WIDENED path (ADR-0025) for html/csv/json/xml
  (``extractor="markitdown"``), reusing the SAME pinned ``markitdown`` (no new dep, invariant 4).

epub, OCR (images), and audio transcription are DEFERRED behind their own future extra + ADR-0005
vetting (extra deps + privacy/cost concerns): the `ingest` extra pins only
markitdown[docx,xlsx,pptx] so they would fail confusingly at runtime, and so they are NOT routed.
"""

from __future__ import annotations

import io
import os

from agora_kb.core.hashing import content_sha256

from .base import ExtractedDoc, ExtractorError, ExtractorUnavailable

__all__ = ["extract_office", "extract_markitdown"]

# Filename extension → MIME type recorded on the office ExtractedDoc.
_EXT_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
}

# WIDENED markitdown formats (ADR-0025): markup/data the same pinned markitdown converts.
# Extension → MIME recorded for provenance; the engine is identical to office (no new dependency).
_MARKITDOWN_EXT_MIME = {
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xml": "application/xml",
}


def extract_office(data: bytes, *, filename: str) -> ExtractedDoc:
    """Convert office-document ``data`` (docx/xlsx/pptx bytes) to markdown.

    Returns an :class:`ExtractedDoc` with ``extractor="office"``, ``mime`` derived from the
    ``filename`` extension, and ``title`` = markitdown's title if it yields one, else the filename
    stem.

    Raises :class:`ExtractorUnavailable` if ``markitdown`` is not installed, and
    :class:`ExtractorError` for malformed/garbage document bytes.
    """
    # Back-compat: keep the original public error ordering/message — the bytes-type check fired
    # FIRST on main (before the filename check), so preserve it here even though _extract_markitdown
    # re-validates. (The widened extract_markitdown delegates straight to the shared engine.)
    if not isinstance(data, bytes | bytearray):
        raise ValueError("office data must be bytes")
    if not filename or not filename.strip():
        raise ValueError("office extraction requires a filename")
    ext = os.path.splitext(filename)[1].lower()
    return _extract_markitdown(
        data,
        filename=filename,
        extractor="office",
        mime=_EXT_MIME.get(ext),
    )


def extract_markitdown(data: bytes, *, filename: str) -> ExtractedDoc:
    """Convert markup/data ``data`` (html/csv/json/epub/xml bytes) to markdown (ADR-0025).

    The WIDENED counterpart of :func:`extract_office`: it reuses the SAME pinned ``markitdown``
    engine (no new dependency, invariant 4) for the additional formats markitdown handles. Returns
    an :class:`ExtractedDoc` with ``extractor="markitdown"`` and ``mime`` from the ``filename``
    extension. Same untrusted-input posture as the office path (``ExtractorUnavailable`` /
    ``ExtractorError``).
    """
    if not filename or not filename.strip():
        raise ValueError("markitdown extraction requires a filename")
    ext = os.path.splitext(filename)[1].lower()
    return _extract_markitdown(
        data,
        filename=filename,
        extractor="markitdown",
        mime=_MARKITDOWN_EXT_MIME.get(ext),
    )


def _extract_markitdown(
    data: bytes, *, filename: str, extractor: str, mime: str | None
) -> ExtractedDoc:
    """Shared markitdown engine for the office + widened paths (DRY; one untrusted-input wrapper).

    Converts ``data`` to markdown via ``markitdown``, classifying a missing per-format reader as
    :class:`ExtractorUnavailable` (install remedy) and any other failure / empty output as
    :class:`ExtractorError`. The caller supplies the ``extractor`` label + recorded ``mime`` so the
    office and widened entry points produce distinctly-tagged docs from one code path.
    """
    if not isinstance(data, bytes | bytearray):
        raise ValueError("markitdown data must be bytes")

    try:
        from markitdown import MarkItDown  # lazy: optional `ingest` extra (ADR-0005)
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise ExtractorUnavailable(
            "This format requires `markitdown`. Install the ingest extra: "
            "`pip install agora-kb[ingest]` or `uv sync --extra ingest`."
        ) from exc

    ext = os.path.splitext(filename)[1].lower()
    try:
        # NOTE: docx/xlsx/pptx/epub are zip archives; markitdown decompresses untrusted bytes with
        # no size cap (a decompression-bomb surface). Archive-size limits are deferred to the web
        # upload handler / Phase 4, mirroring url.py's SSRF note (Phase 3 is localhost single-user).
        result = MarkItDown().convert_stream(io.BytesIO(bytes(data)), file_extension=ext)
    except Exception as exc:  # untrusted/malformed document → wrap, never leak a raw traceback
        # markitdown signals a missing per-format reader (mammoth/openpyxl/python-pptx) with its own
        # MissingDependencyException (NOT an ImportError subclass), raised at convert time. Classify
        # that as a missing optional dependency (install remedy), not malformed input. The `ingest`
        # extra pins markitdown[docx,xlsx,pptx], so this only fires on a partial install.
        if type(exc).__name__ == "MissingDependencyException":
            raise ExtractorUnavailable(
                f"Extraction of {ext or 'this format'} requires the markitdown format "
                "readers. Install the ingest extra: `pip install agora-kb[ingest]` or "
                "`uv sync --extra ingest`."
            ) from exc
        raise ExtractorError(f"failed to extract {filename!r}: {exc}") from exc

    markdown = getattr(result, "text_content", None) or getattr(result, "markdown", None) or ""
    if not markdown or not markdown.strip():
        # Consistent with url.py/pdf.py: empty output is a clean extractor failure rather than an
        # opaque downstream ValueError. (markitdown may also degrade unrecognized bytes to nonsense
        # non-empty text, e.g. the literal "None"; that residual quirk is out of scope for Phase 3.)
        raise ExtractorError(f"no extractable content in {filename!r}")
    title = _result_title(result) or _filename_stem(filename)

    return ExtractedDoc(
        markdown=markdown,
        title=title,
        source_url=None,
        content_sha256=content_sha256(markdown),
        mime=mime,
        extractor=extractor,
    )


def _result_title(result: object) -> str | None:
    """Best-effort title from a markitdown result object; ``None`` if unavailable."""
    title = getattr(result, "title", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


def _filename_stem(filename: str) -> str | None:
    """Return the basename stem of ``filename`` (no directory, no extension), or ``None``."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    return stem or None
