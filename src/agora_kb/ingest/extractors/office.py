"""markitdown extractor — convert office docs + several markup/data formats to markdown.

markitdown (MIT) converts Office Open XML documents (docx/xlsx/pptx) AND a range of other text-based
formats (html, csv, json, xml) to markdown. The optional dependency is imported lazily so the core
stays dependency-light (ADR-0005); a missing install raises :class:`ExtractorUnavailable`. Uploaded
documents are untrusted — a malformed file raises :class:`ExtractorError` rather than leaking a
markitdown traceback.

Two public entry points share one engine (:func:`_extract_markitdown`):

* :func:`extract_office` — the docx/xlsx/pptx office path (``extractor="office"``, ``mime`` from the
  office extension map). Unchanged contract (kept for back-compat / ADR-0020).
* :func:`extract_markitdown` — the WIDENED path (ADR-0025) for html/csv/json/xml/epub
  (``extractor="markitdown"``), reusing the SAME pinned ``markitdown`` (no new dep, invariant 4).

epub is ROUTED here (ADR-0025 rec D, issue #53): markitdown's ``EpubConverter`` needs only the
already-pinned core (zipfile + beautifulsoup4), no extra reader. OCR (images) and audio
transcription remain DEFERRED behind their own future extra + ADR-0005 vetting (extra deps +
privacy/cost concerns).

**Decompression-bomb guard (issue #53 — implemented; supersedes the former Phase-4 deferral).**
docx/xlsx/pptx/epub are zip archives; the per-file upload cap bounds only the COMPRESSED size, so
a crafted archive could balloon to GBs when markitdown decompresses it. :func:`_guard_zip_bomb`
runs two stdlib-only layers before markitdown touches the bytes: (1) a cheap DECLARED-total
pre-check rejects honest bombs without decompressing anything, and (2) because declared sizes are
attacker-controlled and can be forged smaller than the real deflate payload, each member is then
streamed through a size-capped reader that measures the ACTUAL decompressed length and aborts the
moment the running total crosses ``max_uncompressed_bytes`` — the guard's own transient buffer is
bounded to one read chunk. A rejection raises :class:`ExtractorError`.

Do NOT rely on the declared-size pre-check alone: ``zipfile``'s ``ZipExtFile`` truncates its
RETURNED bytes to the declared size, but only AFTER ``zlib.decompress`` has already expanded the
chunk in memory (up to ``ZipExtFile.MAX_N`` = 1 GiB per read on a read-all), so an under-declared
entry still balloons transiently. Layer (2) — measuring the true decompressed length by reading a
``file_size``-raised copy of each member — is what actually closes that vector.
"""

from __future__ import annotations

import copy
import io
import os
import zipfile
import zlib

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
    ".epub": "application/epub+zip",  # ADR-0025 rec D (issue #53) — zip-based, bomb-guarded below
}

# Default cap on the DECLARED uncompressed total of a zip-based upload (issue #53): 10× the web
# face's 25 MiB compressed per-file cap (a coherent expansion allowance for legitimate documents),
# kept under a 256 MiB ceiling. Overridable per call (the web face threads
# `web.upload.max_uncompressed_bytes`).
_DEFAULT_MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024

# Zip container magics: local-file header, empty archive, spanned archive.
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# Per-read chunk for the actual-decompression pass (issue #53). Bounds the guard's own transient
# buffer: ``ZipExtFile.read(n)`` caps zlib's ``max_length`` at ``n``, so no single read materializes
# more than this regardless of the entry's real (attacker-controlled) declared size.
_BOMB_READ_CHUNK = 1024 * 1024


def _guard_zip_bomb(data: bytes, *, filename: str, max_uncompressed_bytes: int) -> None:
    """Reject zip ``data`` that decompresses past ``max_uncompressed_bytes`` (issue #53).

    Two stdlib-only layers, run BEFORE markitdown touches the bytes:

    1. a cheap DECLARED-total pre-check rejects honest bombs without decompressing anything; and
    2. because declared sizes are attacker-controlled (they can be forged smaller than the real
       deflate payload), each member is then streamed through a size-capped reader that measures
       the ACTUAL decompressed length and aborts the moment the running total crosses the cap —
       the guard's own transient buffer is bounded to one ``_BOMB_READ_CHUNK``.

    Non-zip data passes through untouched (the guard only fires on a zip magic); an unreadable /
    corrupt / encrypted archive is left for the extractor's own error path (the guard must never
    mask its reporting). The rejection is raised OUTSIDE the tolerant ``except`` so a decompression
    error can never swallow it.
    """
    if bytes(data[:4]) not in _ZIP_MAGICS:
        return
    declared_over = 0
    actual_over = 0
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
            infos = zf.infolist()
            declared = sum(info.file_size for info in infos)
            if declared > max_uncompressed_bytes:
                declared_over = declared
            else:
                total = 0
                for info in infos:
                    if info.is_dir():
                        continue
                    # Defeat ZipExtFile's declared-size truncation (``self._left = file_size``,
                    # which the attacker can forge small): read a copy whose ``file_size`` is raised
                    # so the reader yields the entry's TRUE decompressed bytes (still bounded by the
                    # real ``compress_size``), and count them against the cap. It must sit ABOVE
                    # where our own counter aborts (``cap + one chunk``); otherwise the reader
                    # would truncate mid-stream, declare EOF early, and CRC-check the partial bytes
                    # against the full expected CRC — a spurious ``BadZipFile`` the tolerant
                    # ``except`` below would swallow, letting the bomb through. For an honest
                    # under-cap entry the reader still reaches its real EOF first, CRC verifying.
                    probe = copy.copy(info)
                    probe.file_size = max_uncompressed_bytes + 2 * _BOMB_READ_CHUNK + 2
                    with zf.open(probe) as member:
                        while True:
                            chunk = member.read(_BOMB_READ_CHUNK)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > max_uncompressed_bytes:
                                actual_over = total
                                break
                    if actual_over:
                        break
    except (zipfile.BadZipFile, ValueError, OSError, EOFError, RuntimeError, zlib.error):
        return  # not a readable archive — markitdown's own ExtractorError path reports it
    if declared_over:
        raise ExtractorError(
            f"zip archive {filename!r} declares {declared_over} uncompressed bytes > "
            f"{max_uncompressed_bytes} limit (decompression-bomb guard)"
        )
    if actual_over:
        raise ExtractorError(
            f"zip archive {filename!r} decompresses to over {max_uncompressed_bytes} bytes "
            f"(decompression-bomb guard)"
        )


def extract_office(
    data: bytes, *, filename: str, max_uncompressed_bytes: int | None = None
) -> ExtractedDoc:
    """Convert office-document ``data`` (docx/xlsx/pptx bytes) to markdown.

    Returns an :class:`ExtractedDoc` with ``extractor="office"``, ``mime`` derived from the
    ``filename`` extension, and ``title`` = markitdown's title if it yields one, else the filename
    stem.

    Raises :class:`ExtractorUnavailable` if ``markitdown`` is not installed, and
    :class:`ExtractorError` for malformed/garbage document bytes — or for a zip archive whose
    declared uncompressed total exceeds ``max_uncompressed_bytes`` (default
    ``_DEFAULT_MAX_UNCOMPRESSED_BYTES``; the decompression-bomb guard, issue #53).
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
        max_uncompressed_bytes=max_uncompressed_bytes,
    )


def extract_markitdown(
    data: bytes, *, filename: str, max_uncompressed_bytes: int | None = None
) -> ExtractedDoc:
    """Convert markup/data ``data`` (html/csv/json/xml/epub bytes) to markdown (ADR-0025).

    The WIDENED counterpart of :func:`extract_office`: it reuses the SAME pinned ``markitdown``
    engine (no new dependency, invariant 4) for the additional formats markitdown handles. Returns
    an :class:`ExtractedDoc` with ``extractor="markitdown"`` and ``mime`` from the ``filename``
    extension. Same untrusted-input posture as the office path (``ExtractorUnavailable`` /
    ``ExtractorError``), including the zip decompression-bomb guard for ``.epub``
    (``max_uncompressed_bytes``, issue #53).
    """
    if not filename or not filename.strip():
        raise ValueError("markitdown extraction requires a filename")
    ext = os.path.splitext(filename)[1].lower()
    return _extract_markitdown(
        data,
        filename=filename,
        extractor="markitdown",
        mime=_MARKITDOWN_EXT_MIME.get(ext),
        max_uncompressed_bytes=max_uncompressed_bytes,
    )


def _extract_markitdown(
    data: bytes,
    *,
    filename: str,
    extractor: str,
    mime: str | None,
    max_uncompressed_bytes: int | None = None,
) -> ExtractedDoc:
    """Shared markitdown engine for the office + widened paths (DRY; one untrusted-input wrapper).

    Converts ``data`` to markdown via ``markitdown``, classifying a missing per-format reader as
    :class:`ExtractorUnavailable` (install remedy) and any other failure / empty output as
    :class:`ExtractorError`. The caller supplies the ``extractor`` label + recorded ``mime`` so the
    office and widened entry points produce distinctly-tagged docs from one code path.
    """
    if not isinstance(data, bytes | bytearray):
        raise ValueError("markitdown data must be bytes")

    # Decompression-bomb guard (issue #53) — stdlib-only, so it protects (and is testable) even
    # when the `ingest` extra is not installed; only fires on zip-magic bytes.
    if max_uncompressed_bytes is None:
        max_uncompressed_bytes = _DEFAULT_MAX_UNCOMPRESSED_BYTES
    _guard_zip_bomb(bytes(data), filename=filename, max_uncompressed_bytes=max_uncompressed_bytes)

    try:
        from markitdown import MarkItDown  # lazy: optional `ingest` extra (ADR-0005)
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise ExtractorUnavailable(
            "This format requires `markitdown`. Install the ingest extra: "
            "`pip install agora-kb[ingest]` or `uv sync --extra ingest`."
        ) from exc

    ext = os.path.splitext(filename)[1].lower()
    try:
        # docx/xlsx/pptx/epub are zip archives markitdown decompresses; _guard_zip_bomb above has
        # already bounded the ACTUAL decompressed total by streaming each member (issue #53 — the
        # former Phase-4 deferral is closed; see the module docstring for the defence layering).
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
