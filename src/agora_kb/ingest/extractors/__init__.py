"""Ingest extractors — input adapters (ADR-0004 / DESIGN §2.3).

Pure transforms turning an upload (url / pdf / office) into markdown + provenance metadata, with no
filesystem-destination side effects (no ``raw/``, no inbox, no git). The web upload handler (a later
unit) calls these and then writes the result through ``Inbox.write``.

The third-party extraction libraries (``trafilatura``/``pdfminer.six``/``markitdown``) live in the
optional ``ingest`` extra and are imported lazily, so importing this package never requires them.
"""

from .base import (
    ExtractedDoc,
    Extractor,
    ExtractorError,
    ExtractorUnavailable,
    extract,
)
from .office import extract_office
from .pdf import extract_pdf
from .url import extract_url

__all__ = [
    "ExtractedDoc",
    "Extractor",
    "ExtractorError",
    "ExtractorUnavailable",
    "extract",
    "extract_office",
    "extract_pdf",
    "extract_url",
]
