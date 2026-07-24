"""Shared CJK codepoint ranges — the SINGLE SOURCE for every CJK-aware component (issue #56).

The range table was born in :mod:`core.gold` (ADR-0027 decision 5) to power the token-budget
estimator; issue #56 (ADR-0012 addendum) makes the query tokenizer CJK-aware too, and the two must
never drift, so the table lives here and BOTH import it. This module is pure data plus a range
test — no tokenization policy: gold counts codepoints (≈1 token/char), the wiki tokenizer forms
character-bigram runs; each policy stays at its call site.

Deterministic by construction: fixed codepoint ranges, no locale, no external table.
"""

from __future__ import annotations

__all__ = ["CJK_RANGES", "is_cjk"]

# CJK codepoint ranges: Hangul (syllables + jamo + compatibility), CJK unified ideographs (+Ext A),
# Hiragana/Katakana, CJK symbols/punctuation, and fullwidth forms.
CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x3000, 0x303F),  # CJK symbols and punctuation
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x3130, 0x318F),  # Hangul compatibility jamo
    (0x3400, 0x4DBF),  # CJK unified ideographs extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xAC00, 0xD7A3),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF00, 0xFFEF),  # Halfwidth and fullwidth forms
)


def is_cjk(codepoint: int) -> bool:
    """True iff ``codepoint`` falls in a CJK range (see :data:`CJK_RANGES`)."""
    for lo, hi in CJK_RANGES:
        if lo <= codepoint <= hi:
            return True
    return False
