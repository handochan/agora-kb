"""Office extractor — convert docx/xlsx/pptx uploads to markdown (markitdown).

markitdown (MIT) converts Office Open XML documents (and several other formats) to markdown. The
optional dependency is imported lazily so the core stays dependency-light (ADR-0005); a missing
install raises :class:`ExtractorUnavailable`. Uploaded documents are untrusted — a malformed file
raises :class:`ExtractorError` rather than leaking a markitdown traceback.
"""

from __future__ import annotations

import io
import os

from agora_kb.core.hashing import content_sha256

from .base import ExtractedDoc, ExtractorError, ExtractorUnavailable

__all__ = ["extract_office"]

# Filename extension → MIME type recorded on the ExtractedDoc.
_EXT_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
}


def extract_office(data: bytes, *, filename: str) -> ExtractedDoc:
    """Convert office-document ``data`` (docx/xlsx/pptx bytes) to markdown.

    Returns an :class:`ExtractedDoc` with ``extractor="office"``, ``mime`` derived from the
    ``filename`` extension, and ``title`` = markitdown's title if it yields one, else the filename
    stem.

    Raises :class:`ExtractorUnavailable` if ``markitdown`` is not installed, and
    :class:`ExtractorError` for malformed/garbage document bytes.
    """
    if not isinstance(data, bytes | bytearray):
        raise ValueError("office data must be bytes")
    if not filename or not filename.strip():
        raise ValueError("office extraction requires a filename")

    try:
        from markitdown import MarkItDown  # lazy: optional `ingest` extra (ADR-0005)
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise ExtractorUnavailable(
            "Office (docx/xlsx/pptx) extraction requires `markitdown`. Install the ingest extra: "
            "`pip install agora-kb[ingest]` or `uv sync --extra ingest`."
        ) from exc

    ext = os.path.splitext(filename)[1].lower()
    try:
        # NOTE: docx/xlsx/pptx are zip archives; markitdown decompresses untrusted bytes with no
        # size cap (a decompression-bomb surface). Archive-size limits are deferred to the web
        # upload handler / Phase 4, mirroring url.py's SSRF note (Phase 3 is localhost single-user).
        result = MarkItDown().convert_stream(io.BytesIO(bytes(data)), file_extension=ext)
    except Exception as exc:  # untrusted/malformed document → wrap, never leak a raw traceback
        # markitdown signals a missing per-format reader (mammoth/openpyxl/python-pptx) with its own
        # MissingDependencyException (NOT an ImportError subclass), raised at convert time. Classify
        # that as a missing optional dependency (install remedy), not malformed input. The `ingest`
        # extra pins markitdown[docx,xlsx,pptx], so this only fires on a partial install.
        if type(exc).__name__ == "MissingDependencyException":
            raise ExtractorUnavailable(
                f"Office extraction of {ext or 'this format'} requires the markitdown format "
                "readers. Install the ingest extra: `pip install agora-kb[ingest]` or "
                "`uv sync --extra ingest`."
            ) from exc
        raise ExtractorError(f"failed to extract office document {filename!r}: {exc}") from exc

    markdown = getattr(result, "text_content", None) or getattr(result, "markdown", None) or ""
    if not markdown or not markdown.strip():
        # Consistent with url.py/pdf.py: empty output is a clean extractor failure rather than an
        # opaque downstream ValueError. (markitdown may also degrade unrecognized bytes to nonsense
        # non-empty text, e.g. the literal "None"; that residual quirk is out of scope for Phase 3.)
        raise ExtractorError(f"no extractable content in office document {filename!r}")
    title = _result_title(result) or _filename_stem(filename)

    return ExtractedDoc(
        markdown=markdown,
        title=title,
        source_url=None,
        content_sha256=content_sha256(markdown),
        mime=_EXT_MIME.get(ext),
        extractor="office",
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
