"""Tests for canonical content hashing (DATA-MODEL §11.2)."""

from __future__ import annotations

import hashlib
import unicodedata

from agora_kb.core.hashing import content_sha256, normalize_body


def test_known_vector() -> None:
    # "foo bar" with a single trailing newline is the canonical form.
    expected = hashlib.sha256(b"foo bar\n").hexdigest()
    assert content_sha256("foo bar") == expected
    assert content_sha256("foo bar\n") == expected
    assert content_sha256("foo bar\n\n\n") == expected


def test_crlf_and_cr_normalized_to_lf() -> None:
    assert content_sha256("a\r\nb") == content_sha256("a\nb") == content_sha256("a\rb")


def test_trailing_whitespace_stripped_per_line() -> None:
    assert content_sha256("a   \nb\t\nc") == content_sha256("a\nb\nc")


def test_internal_blank_lines_preserved() -> None:
    # An internal blank line is significant; only *trailing* newlines collapse.
    assert content_sha256("a\n\nb") != content_sha256("a\nb")


def test_nfc_normalization() -> None:
    # 'é' as composed (NFC) vs decomposed (NFD) must hash identically.
    composed = "café"  # é
    decomposed = "café"  # e + combining acute
    assert unicodedata.normalize("NFC", decomposed) == composed
    assert content_sha256(composed) == content_sha256(decomposed)


def test_distinct_content_differs() -> None:
    assert content_sha256("alpha") != content_sha256("beta")


def test_normalize_body_shape() -> None:
    assert normalize_body("x  \r\n\r\n") == "x\n"
    assert normalize_body("") == "\n"
    assert normalize_body("line1\nline2") == "line1\nline2\n"


def test_output_is_64_hex() -> None:
    h = content_sha256("anything")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_trailing_unicode_whitespace_stripped() -> None:
    # DATA-MODEL §11.2 pins "trailing whitespace" to the Unicode set (str.rstrip()): a trailing NBSP
    # (U+00A0) or ideographic space (U+3000) must not change the hash.
    assert content_sha256("a \nb　") == content_sha256("a\nb")
