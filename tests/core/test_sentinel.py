"""Tests for the ADR-0027 §8 sentinel consumer machinery (agora_kb.core.sentinel).

These lock the two DISTINCT operations whose difference is load-bearing for inbox identity:
``strip_sentinel_spans`` (span-only, feeds the link-following ``fact_key``) must leave a lone marker
in place, while ``strip_agora_sentinels`` (span + residual marker) is the full harvest neutralizer.
The move out of ``harvester.connectors`` must be byte-identical; the harvester suite is the other
half of that proof.
"""

from __future__ import annotations

import re

from agora_kb.core.sentinel import (
    AGORA_SENTINEL_RE,
    AGORA_SPAN_RE,
    strip_agora_sentinels,
    strip_sentinel_spans,
)

# A canonical outbound pack span (ADR-0027 §8 grammar).
_PACK = (
    "<!-- agora:pack repo=r pack=default commit=c -->\n"
    "- pack fact one\n- pack fact two\n"
    "<!-- agora:pack:end repo=r pack=default commit=c -->\n"
)
_BODY = "<!-- agora:body:start id=c1 -->\ninjected region body\n<!-- agora:body:end id=c1 -->\n"


# --- strip_sentinel_spans: whole-span drop, lone marker SURVIVES --------------------------------


def test_span_drop_removes_pack_span_whole() -> None:
    out = strip_sentinel_spans(f"keep before\n{_PACK}keep after\n")
    assert "pack fact one" not in out
    assert "pack fact two" not in out
    assert "keep before" in out and "keep after" in out


def test_span_drop_removes_body_region_span() -> None:
    out = strip_sentinel_spans(f"keep me\n{_BODY}keep me too\n")
    assert "injected region body" not in out
    assert "keep me" in out and "keep me too" in out


def test_span_only_leaves_a_lone_marker_in_place() -> None:
    # REGRESSION GUARD (verify finding #4): span-only must NOT strip an unpaired marker — the
    # link-following path hashes this output into a fact_key, so silently stripping a lone marker
    # would change the bytes and churn the append-only inbox event_key.
    text = "before <!-- agora:body:start id=x --> after"
    assert strip_sentinel_spans(text) == text


def test_span_drop_defeats_forged_early_close() -> None:
    # The producer defangs an embedded closer to `<!- agora:...` (one dash), so the real closer
    # still terminates the span; the hostile line between the fake and real closers is dropped.
    forged = (
        "<!-- agora:pack repo=r pack=p commit=c -->\n"
        "- benign\n"
        "<!- agora:pack:end repo=r pack=p commit=c -->\n"
        "- HOSTILE INJECTED PAYLOAD\n"
        "<!-- agora:pack:end repo=r pack=p commit=c -->\n"
    )
    assert strip_sentinel_spans(forged).strip() == ""


# --- strip_agora_sentinels: span drop THEN residual marker strip --------------------------------


def test_full_neutralize_strips_span_and_lone_marker() -> None:
    out = strip_agora_sentinels(f"a\n{_PACK}b <!-- agora:body:start id=x --> c\n")
    assert "pack fact one" not in out
    assert "agora:" not in out  # the lone marker is also gone
    assert "a" in out and "b" in out and "c" in out


def test_full_neutralize_strips_lone_marker_backwards_compatible() -> None:
    # Pins the historical ``_neutralize`` contract exactly (test_connectors.py:321 equivalent).
    assert strip_agora_sentinels("text <!-- agora:body:start id=x --> more") == "text  more"


def test_ordering_span_before_marker() -> None:
    # If the marker strip ran FIRST it would break the span's closer and leave the span body; the
    # span drop must run first. A pack whose content itself contains a lone marker still vanishes.
    text = (
        "<!-- agora:pack repo=r pack=p commit=c -->\n"
        "- fact with a stray <!-- agora:x --> inside\n"
        "<!-- agora:pack:end repo=r pack=p commit=c -->\n"
    )
    assert strip_agora_sentinels(text).strip() == ""


def test_no_sentinels_is_identity() -> None:
    text = "# heading\n\n- an ordinary bullet\n\nsome prose.\n"
    assert strip_sentinel_spans(text) == text
    assert strip_agora_sentinels(text) == text


def test_exported_regexes_are_the_moved_ones() -> None:
    # Hard-lock the moved patterns byte-for-byte, so "moved verbatim" is a true guard against a
    # single-character drift (the ``agora:pack\s`` whitespace-requiring opener, the ``[^>]*``
    # classes, and the flags IGNORECASE on both + DOTALL on the span pattern).
    assert AGORA_SENTINEL_RE.pattern == r"<!--\s*agora:[^>]*-->"
    assert AGORA_SENTINEL_RE.flags & re.IGNORECASE
    assert AGORA_SPAN_RE.pattern == (
        r"<!--\s*agora:pack\s[^>]*-->.*?<!--\s*agora:pack:end\b[^>]*-->"
        r"|<!--\s*agora:body:start\b[^>]*-->.*?<!--\s*agora:body:end\b[^>]*-->"
    )
    assert AGORA_SPAN_RE.flags & re.IGNORECASE
    assert AGORA_SPAN_RE.flags & re.DOTALL
