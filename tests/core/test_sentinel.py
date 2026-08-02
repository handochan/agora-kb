"""Tests for the ADR-0027 §8 sentinel consumer machinery (agora_kb.core.sentinel).

These lock the two DISTINCT operations whose difference is load-bearing for inbox identity:
``strip_sentinel_spans`` (span-only, feeds the link-following ``fact_key``) must leave a lone marker
in place, while ``strip_agora_sentinels`` (span + residual marker) is the full harvest neutralizer.
The move out of ``harvester.connectors`` must be byte-identical; the harvester suite is the other
half of that proof.

The module ALSO hosts the curator's STRICT, line-anchored producer body-region grammar and the
:func:`has_unauthored_region` grader both halves of the #119 invariant read (the worker's
``body_status`` clear and the L2-6 lint check). The second half of this file pins that grader — and
a cross-test asserting the tolerant consumer stays a SUPERSET of the strict producer, so the two
grammars living in one module can never quietly drift into each other.
"""

from __future__ import annotations

import re

from agora_kb.core.sentinel import (
    AGORA_SENTINEL_RE,
    AGORA_SPAN_RE,
    BODY_END_LINE_RE,
    BODY_PLACEHOLDER,
    BODY_RESET_PLACEHOLDER,
    BODY_START_LINE_RE,
    UNAUTHORED_REGION_BODIES,
    has_unauthored_region,
    region_is_unauthored,
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


# --- the PRODUCER body-region grammar: has_unauthored_region (#119) -----------------------------
#
# The ONE grader behind both halves of the #119 invariant — the curator's post-§4.2 ``body_status``
# clear (``worker._clear_body_status``) and the L2-6 lint check (``lint._check_body_status``). Its
# verdict decides whether a published note keeps or drops the flag, so every shape it can see is
# pinned here: the placeholder vocabulary, the multi-region case, and the malformed-marker
# fail-safe.

_SID = "2026-06-13T03-00-00.000Z--7f31ab--c1"
_SID2 = "2026-06-13T03-00-00.000Z--7f31ab--c2"


def _region(sentinel_id: str, body: str) -> str:
    return (
        f"<!-- agora:body:start id={sentinel_id} -->\n"
        f"{body}\n"
        f"<!-- agora:body:end id={sentinel_id} -->"
    )


def test_region_is_unauthored_covers_the_whole_placeholder_vocabulary() -> None:
    # The three UNAUTHORED spellings are APPLY's initial fill, the §4.2 RESET form, and nothing at
    # all — plus whitespace-only, since the comparison strips before matching.
    assert region_is_unauthored("")
    assert region_is_unauthored("   \n\t\n ")
    assert region_is_unauthored(BODY_PLACEHOLDER)
    assert region_is_unauthored(f"\n{BODY_PLACEHOLDER}\n")
    assert region_is_unauthored(BODY_RESET_PLACEHOLDER)
    assert not region_is_unauthored("The single curator holds a per-repo flock.")


def test_placeholder_constants_are_derived_not_respelled() -> None:
    # The reset form is DERIVED from the initial fill, and the frozenset from both, so a change to
    # either placeholder cannot silently stop being detected by the grader.
    assert BODY_RESET_PLACEHOLDER == f"> {BODY_PLACEHOLDER}"
    assert UNAUTHORED_REGION_BODIES == frozenset({"", BODY_PLACEHOLDER, BODY_RESET_PLACEHOLDER})


def test_has_unauthored_region_empty_region_is_unauthored() -> None:
    assert has_unauthored_region(_region(_SID, ""))


def test_has_unauthored_region_apply_placeholder_is_unauthored() -> None:
    assert has_unauthored_region(_region(_SID, BODY_PLACEHOLDER))


def test_has_unauthored_region_reset_placeholder_is_unauthored() -> None:
    assert has_unauthored_region(_region(_SID, BODY_RESET_PLACEHOLDER))


def test_has_unauthored_region_whitespace_only_is_unauthored() -> None:
    assert has_unauthored_region(_region(_SID, "   \n\t"))


def test_has_unauthored_region_authored_region_is_authored() -> None:
    assert not has_unauthored_region(_region(_SID, "The single curator holds a per-repo flock."))


def test_has_unauthored_region_multiline_prose_is_authored() -> None:
    body = _region(_SID, "## Detail\n\n- one flock per repo\n- the curator is the only writer")
    assert not has_unauthored_region(body)


def test_has_unauthored_region_mixed_note_is_unauthored() -> None:
    # THE case the note-local rule exists for: one region authored THIS run, one an EARLIER run
    # left at the placeholder. The note still owes prose, so the flag must survive.
    body = _region(_SID, "Real prose.") + "\n\n" + _region(_SID2, BODY_PLACEHOLDER)
    assert has_unauthored_region(body)


def test_has_unauthored_region_all_regions_authored_is_authored() -> None:
    body = _region(_SID, "Real prose.") + "\n\n" + _region(_SID2, "More real prose.")
    assert not has_unauthored_region(body)


def test_has_unauthored_region_zero_regions_is_authored() -> None:
    # A note with no agora:body markers at all owes nothing — e.g. a needs_prose=False CREATE_THEME.
    assert not has_unauthored_region("Just a body with no sentinels at all.\n")
    assert not has_unauthored_region("")


def test_has_unauthored_region_prose_outside_a_region_does_not_count() -> None:
    # Only text BETWEEN the markers is the region body; surrounding prose is APPLY-owned structure.
    body = f"Leading structural prose.\n\n{_region(_SID, BODY_PLACEHOLDER)}\n\nTrailing prose.\n"
    assert has_unauthored_region(body)


# --- fail-safe: every malformed marker structure grades as UNAUTHORED ---------------------------
#
# One decision buys three properties: a tampered note KEEPS its flag, L2-6 stays silent about a note
# L1-20 already hard-rejects, and the clear never errs toward claiming prose it cannot verify.


def test_has_unauthored_region_unclosed_start_is_fail_safe() -> None:
    assert has_unauthored_region(f"<!-- agora:body:start id={_SID} -->\nreal prose\n")


def test_has_unauthored_region_unmatched_end_is_fail_safe() -> None:
    assert has_unauthored_region(f"real prose\n<!-- agora:body:end id={_SID} -->\n")


def test_has_unauthored_region_nested_start_is_fail_safe() -> None:
    body = (
        f"<!-- agora:body:start id={_SID} -->\nreal prose\n"
        f"<!-- agora:body:start id={_SID2} -->\nmore\n"
        f"<!-- agora:body:end id={_SID2} -->\n"
        f"<!-- agora:body:end id={_SID} -->\n"
    )
    assert has_unauthored_region(body)


def test_has_unauthored_region_mismatched_id_is_fail_safe() -> None:
    body = f"<!-- agora:body:start id={_SID} -->\nreal prose\n<!-- agora:body:end id={_SID2} -->\n"
    assert has_unauthored_region(body)


def test_has_unauthored_region_duplicated_id_is_fail_safe() -> None:
    """Two well-formed regions sharing ONE id — the corruption L1-20 exists for.

    A real dogfood run produced exactly this shape (a cross-run MERGE / daily-append whose bare
    per-run candidate_id collided), which is why the persisted id became run-scoped and L1-20 was
    added. ``apply._extract_sentinel_regions`` returns ``None`` for it, so this walk — which the
    docstring says mirrors apply EXACTLY — must fail safe too. Without this arm the note grades as
    fully authored and L2-6 reports "every region is authored" over a note lint simultaneously
    hard-rejects as tampered; the worker would also strip its flag, saved only by the §4.4 gate
    happening to run afterwards, i.e. by ordering luck rather than by the stated fail-safe.
    """
    body = f"{_region(_SID, 'real prose one')}\n\n{_region(_SID, 'real prose two')}\n"
    assert has_unauthored_region(body)


def test_has_unauthored_region_duplicate_arm_matches_apply_exactly() -> None:
    """The docstring's "mirrors apply exactly" claim, asserted against the REAL apply function.

    Re-spelling apply's verdict here would let the two drift silently — which is the whole failure
    mode #119 consolidated the grammar to prevent.
    """
    from agora_kb.curator.apply import _extract_sentinel_regions

    dup = f"{_region(_SID, 'real prose one')}\n\n{_region(_SID, 'real prose two')}\n"
    assert _extract_sentinel_regions(dup) is None
    assert has_unauthored_region(dup)


def test_has_unauthored_region_non_anchored_marker_is_ordinary_content() -> None:
    # The producer grammar is LINE-ANCHORED (\A…\Z): an inline marker is NOT a region boundary, so
    # this note has zero regions. Locks the strictness that makes this grammar distinct from the
    # tolerant AGORA_SPAN_RE consumer regex below.
    body = f"prefix <!-- agora:body:start id={_SID} --> suffix\n"
    assert not has_unauthored_region(body)


# --- the two grammars must not be conflated (grafted cross-test) --------------------------------


def test_tolerant_consumer_still_strips_a_span_the_strict_producer_renders() -> None:
    """The ADR-0027 §8 consumer must stay a SUPERSET of the strict producer grammar.

    ``core.sentinel`` now hosts BOTH the tolerant consumer regexes and the strict line-anchored
    producer matchers. If the producer's marker shape ever drifts out of ``AGORA_SPAN_RE``'s reach,
    a harvested Agora body region would stop being dropped and the §8 loop-break hole would quietly
    reopen — with no other test failing. This is that alarm.
    """
    from agora_kb.curator.apply import body_sentinels

    start, end = body_sentinels(_SID)
    # The producer's own renderer must parse under the strict grammar…
    assert BODY_START_LINE_RE.match(start) is not None
    assert BODY_END_LINE_RE.match(end) is not None
    # …and the tolerant consumer must still remove the whole span it forms.
    span = f"{start}\ndistilled agora prose\n{end}\n"
    out = strip_sentinel_spans(f"keep before\n{span}keep after\n")
    assert "distilled agora prose" not in out
    assert "keep before" in out and "keep after" in out


def test_grader_agrees_with_the_run_scoped_unauthored_regions_verdict() -> None:
    """``_unauthored_regions`` (run-scoped, #115) and ``has_unauthored_region`` (note-local, #119)
    must never drift on the placeholder set — they share ``UNAUTHORED_REGION_BODIES``, and this
    pins that sharing behaviorally rather than by inspection."""
    from agora_kb.curator.worker import _unauthored_regions

    rel = "wiki/ai-tech/themes/t.md"
    for region_body, expected_unauthored in (
        (BODY_PLACEHOLDER, True),  # APPLY's untouched initial fill
        (BODY_RESET_PLACEHOLDER, True),  # the §4.2 freshly-degraded note
        ("", True),  # an emptied region
        ("The single curator holds a per-repo flock.", False),  # real prose landed
    ):
        old = _region(_SID, BODY_PLACEHOLDER)
        new = _region(_SID, region_body)
        run_scoped = bool(_unauthored_regions({rel: [_SID]}, {rel: old}, {rel: new}))
        assert run_scoped is expected_unauthored
        assert has_unauthored_region(new) is expected_unauthored
