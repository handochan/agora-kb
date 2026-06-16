"""Canonical content hashing (DATA-MODEL §11.2).

``content_sha256`` is computed over a canonically-normalized form of an inbox item's **body** so
that byte-equivalent knowledge captured by different writers/sources/platforms collapses to one
tier-2 candidate reproducibly across implementations, while distinct provenance tuples are still
preserved and unioned by the curator (DATA-MODEL §1, §7).

Normalization (in order):
1. body text only — frontmatter is excluded (the caller passes the body);
2. UTF-8, NFC Unicode normalization;
3. LF newlines (CRLF/CR → LF);
4. trailing whitespace stripped per line;
5. single trailing newline.
"""

from __future__ import annotations

import hashlib
import unicodedata

__all__ = ["normalize_body", "content_sha256"]


def normalize_body(body: str) -> str:
    """Return the canonical normalized body text per DATA-MODEL §11.2 (steps 2–5)."""
    text = unicodedata.normalize("NFC", body)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # "trailing whitespace" == the Unicode whitespace set (Python str.rstrip()), pinned in
    # DATA-MODEL §11.2 so independent reimplementations agree byte-for-byte.
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    # Collapse any run of trailing newlines (incl. trailing blank lines) to exactly one.
    return text.rstrip("\n") + "\n"


def content_sha256(body: str) -> str:
    """Hex SHA-256 of the canonically-normalized body (DATA-MODEL §11.2)."""
    return hashlib.sha256(normalize_body(body).encode("utf-8")).hexdigest()
