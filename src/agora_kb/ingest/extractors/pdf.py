"""PDF extractor — extract text from uploaded PDF bytes (pdfminer.six).

pdfminer.six (MIT) is used in preference to pymupdf (AGPL — see DESIGN §8 purity notes). The
optional dependency is imported lazily so the core stays dependency-light (ADR-0005); a missing
install raises :class:`ExtractorUnavailable`. PDFs are untrusted input — a malformed file raises
:class:`ExtractorError` rather than leaking a pdfminer traceback.
"""

from __future__ import annotations

import io
import os

from agora_kb.core.hashing import content_sha256

from .base import ExtractedDoc, ExtractorError, ExtractorUnavailable

__all__ = ["extract_pdf"]


def extract_pdf(data: bytes, *, filename: str | None = None) -> ExtractedDoc:
    """Extract text from PDF ``data`` (the raw uploaded bytes).

    Returns an :class:`ExtractedDoc` with ``extractor="pdf"``, ``mime="application/pdf"``, and
    ``title`` = the filename stem when ``filename`` is given (pdfminer yields plain text with no
    reliable title).

    Raises :class:`ExtractorUnavailable` if ``pdfminer.six`` is not installed, and
    :class:`ExtractorError` for malformed/garbage PDF bytes.
    """
    if not isinstance(data, bytes | bytearray):
        raise ValueError("pdf data must be bytes")

    try:
        from pdfminer.high_level import extract_text  # lazy: optional `ingest` extra (ADR-0005)
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise ExtractorUnavailable(
            "PDF extraction requires `pdfminer.six`. Install the ingest extra: "
            "`pip install agora-kb[ingest]` or `uv sync --extra ingest`."
        ) from exc

    try:
        # NOTE: no page/size cap here — pdfminer's extract_text is unbounded. PDF resource limits
        # (maxpages / max bytes against a crafted bomb) are deferred to the web upload handler /
        # Phase 4, mirroring the SSRF note in url.py. Phase 3 is localhost single-user.
        text = extract_text(io.BytesIO(bytes(data)))
    except Exception as exc:  # untrusted/malformed PDF → wrap, never leak a raw traceback
        raise ExtractorError(f"failed to extract PDF: {exc}") from exc

    if not text or not text.strip():
        # Consistent with url.py: empty/whitespace-only output is a clean extractor failure, not an
        # opaque downstream ValueError from Inbox.write (which rejects empty text).
        raise ExtractorError(f"no extractable text in PDF {filename!r}")

    return ExtractedDoc(
        markdown=text,
        title=_filename_stem(filename),
        source_url=None,
        content_sha256=content_sha256(text),
        mime="application/pdf",
        extractor="pdf",
    )


def _filename_stem(filename: str | None) -> str | None:
    """Return the basename stem of ``filename`` (no directory, no extension), or ``None``."""
    if not filename:
        return None
    stem = os.path.splitext(os.path.basename(filename))[0]
    return stem or None
