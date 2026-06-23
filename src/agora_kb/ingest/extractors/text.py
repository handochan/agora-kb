"""Text passthrough extractor — plain ``.txt`` / markdown uploads (dependency-free; ADR-0025).

Markdown / plain text need no conversion: the upload *is* already the markdown body. This extractor
is therefore a **pure, dependency-free** transform — it has NO third-party import (unlike the
url/pdf/office extractors behind the lazy ``ingest`` extra, ADR-0005) — so a core install can always
ingest a pasted note or a ``.md`` file even with no extras installed. Uploaded bytes are untrusted
but text is benign: they are decoded as UTF-8 with ``errors="replace"`` (a malformed byte becomes
the replacement char, never a raise), and the canonical :func:`content_sha256` (DATA-MODEL §11.2) is
reused so the hash matches what the inbox would stamp for the same body.
"""

from __future__ import annotations

from agora_kb.core.hashing import content_sha256

from .base import ExtractedDoc

__all__ = ["extract_text"]


def extract_text(
    data: bytes, *, filename: str | None = None, mime: str | None = None
) -> ExtractedDoc:
    """Decode ``data`` as UTF-8 text and wrap it as an :class:`ExtractedDoc` verbatim.

    Used for ``.txt`` / ``.md`` / ``.markdown`` uploads (and ``text/plain`` / ``text/markdown``
    MIME): the bytes ARE the markdown body, so there is nothing to convert. Decoding uses
    ``errors="replace"`` (untrusted bytes never raise), ``extractor="text"``, no title/source_url,
    and the canonical :func:`content_sha256`. ``filename``/``mime`` are accepted for a uniform
    extractor signature; ``mime`` (when given) is recorded on the doc for provenance.
    """
    if not isinstance(data, bytes | bytearray):
        raise ValueError("text data must be bytes")
    text = bytes(data).decode("utf-8", errors="replace")
    norm_mime = mime.split(";", 1)[0].strip().lower() if mime else None
    return ExtractedDoc(
        markdown=text,
        title=None,
        source_url=None,
        content_sha256=content_sha256(text),
        mime=norm_mime,
        extractor="text",
    )
