"""Agora structural sentinel grammar — the one canonical home of the ADR-0027 §8 consumer duty.

ADR-0027 §8 is *the* single normative outbound sentinel + loop-break contract: every Agora→agent
emission is wrapped in an ``<!-- agora:pack … -->`` … ``<!-- agora:pack:end … -->`` span, and every
consumer that reads agent memory back in (the harvester's :class:`~agora_kb.harvester.connectors.\
FileConnector`, the future ADR-0026 session distiller, the reserved ADR-0030 federation composer)
must drop those spans so Agora's own output never re-enters as a fact (the verbatim half of the
loop, ADR-0017 §5). This module owns that CONSUMER machinery so the grammar lives in exactly one
place and never forks (ADR-0027 §8: consumers "MUST cite this ADR §8 rather than restating it").

Two distinct operations — kept separate on purpose (the difference is load-bearing for identity):

* :func:`strip_sentinel_spans` removes whole SPANS only (opener → matching closer, inclusive) and
  leaves any *lone* residual marker in place. The harvester's link-following read path hashes the
  span-stripped sibling body into a ``fact_key`` (:data:`connectors._strip_sentinel_spans` call
  site), so this must stay span-only — folding in the marker strip would change those bytes and
  churn the append-only inbox's ``event_key``.
* :func:`strip_agora_sentinels` runs the span drop and THEN strips any residual lone marker — the
  full defense-in-depth neutralization applied to a harvested fact body before it becomes a
  candidate (== the harvester's historical ``_neutralize``). This is the phase-0 sanitizer the
  redaction module (:func:`agora_kb.core.redact.sanitize`) composes before the secret/PII scan.

The PRODUCER duty (assembly-time neutralization of an embedded opener) lives with the emitter in
:mod:`agora_kb.core.gold` (``_neutralize_sentinels``) and is intentionally *not* moved here.

This module ALSO owns the curator's **producer body-region grammar** (the strict, line-anchored
``agora:body:start/end`` matchers) and the vocabulary of an **unauthored region body** — the
placeholder set plus the :func:`has_unauthored_region` grader that both the curator's post-§4.2
``body_status`` clear and the schema linter's L2-6 check read (#119). Two grammars now live here on
purpose; see the section comment below for why they must never be conflated.
"""

from __future__ import annotations

import re

__all__ = [
    "AGORA_SENTINEL_RE",
    "AGORA_SPAN_RE",
    "BODY_END_LINE_RE",
    "BODY_PLACEHOLDER",
    "BODY_RESET_PLACEHOLDER",
    "BODY_START_LINE_RE",
    "UNAUTHORED_REGION_BODIES",
    "has_unauthored_region",
    "region_is_unauthored",
    "strip_sentinel_spans",
    "strip_agora_sentinels",
]

# An agora structural sentinel HTML comment (the curator's body-region markers, apply.py grammar).
# Stripped from harvested text so a poisoned memory bullet cannot inject a fake region into the
# candidate bundle the planning brain reads (defense-in-depth; the curator's diff gate is the real
# integrity boundary).
AGORA_SENTINEL_RE = re.compile(r"<!--\s*agora:[^>]*-->", re.IGNORECASE)
# A full agora sentinel SPAN — an opening marker THROUGH its matching closing marker, INCLUSIVE
# (ADR-0027 §8 consumer duty, the NET-NEW half of the outbound loop-break contract). The marker-only
# strip above leaves the span CONTENT in place, which is insufficient: a harvested gold pack
# (``agora:pack …`` … ``agora:pack:end …``) would re-enter as facts. So the whole span is removed
# BEFORE the marker strip runs. The opener requires whitespace after ``pack`` (``agora:pack\s``) so
# it matches only the true opener, never the ``:end`` closer; ``.*?`` reaches the FIRST real closer,
# the producer's assembly-time neutralization (``gold._neutralize_sentinels``) defangs any forged
# early closer so a hostile summary line cannot terminate the span early. The body-region family
# (``agora:body:start`` … ``agora:body:end``) is covered symmetrically.
AGORA_SPAN_RE = re.compile(
    r"<!--\s*agora:pack\s[^>]*-->.*?<!--\s*agora:pack:end\b[^>]*-->"
    r"|<!--\s*agora:body:start\b[^>]*-->.*?<!--\s*agora:body:end\b[^>]*-->",
    re.IGNORECASE | re.DOTALL,
)


def strip_sentinel_spans(text: str) -> str:
    """Remove whole agora sentinel SPANS (open marker → close marker inclusive) — ADR-0027 §8.

    The NET-NEW consumer duty: a harvested gold pack (or a body-region span) is removed ENTIRELY so
    it contributes zero facts, closing the verbatim half of the loop (ADR-0027 §8, ADR-0017 §5). A
    *lone* (unpaired) marker is deliberately left in place — this runs BEFORE the marker-only strip
    in :func:`strip_agora_sentinels`, and callers that hash the result into an identity key (the
    link-following ``fact_key`` path) depend on this span-only behavior staying byte-stable.
    """
    return AGORA_SPAN_RE.sub("", text)


def strip_agora_sentinels(text: str) -> str:
    """Strip agora structural sentinels from untrusted harvested text (defense-in-depth, ADR-0017).

    First removes whole agora SPANS (:func:`strip_sentinel_spans`, ADR-0027 §8 — a pack/body span
    is dropped content-and-all), THEN strips any residual lone marker comments (``<!-- agora: -->``)
    so a poisoned memory bullet cannot inject a fake region into the candidate bundle. The candidate
    text is already treated as untrusted DATA by the planning prompt (INGEST-CONTRACT §8) and the
    curator's deterministic diff gate is the real integrity boundary; this is a cheap extra layer.
    """
    return AGORA_SENTINEL_RE.sub("", strip_sentinel_spans(text))


# --- the curator's body-region grammar (producer side; ADR-0011 §3/§3.1) ----------------------
# DISTINCT FROM the tolerant consumer regexes above, and deliberately so: AGORA_SPAN_RE is
# DOTALL + id-agnostic + first-closer-wins because it defends against POISONED harvested text.
# The producer grammar below is STRICT and LINE-ANCHORED because it grades the curator's OWN
# output at the §4.2 integrity boundary. Never implement a region predicate on AGORA_SPAN_RE —
# it pairs marker shapes the strict gate rejects, and the two would silently disagree.
# These two patterns are the SINGLE spelling: apply.py, worker.py and schema/lint.py all import
# them, so the L1-20 gate, the §4.2 validator and the L2-6 check provably read the same markers.
BODY_START_LINE_RE = re.compile(r"\A<!-- agora:body:start id=(?P<cid>.+) -->\Z")
BODY_END_LINE_RE = re.compile(r"\A<!-- agora:body:end id=(?P<cid>.+) -->\Z")

# APPLY's initial fill (apply.py) and the §4.2 AUTHOR-failure RESET form (worker.py). They live
# HERE, below both `schema/` and `curator/`, because BOTH the curator (which sets and clears
# `body_status`) and schema/lint.py (which asserts it, L2-6) must grade "unauthored" with the
# SAME vocabulary — and schema/ may not import the curator (core <- curator, ADR-0001/0003).
BODY_PLACEHOLDER = "_summary pending_"
BODY_RESET_PLACEHOLDER = f"> {BODY_PLACEHOLDER}"
# A region body reading as one of these carries no prose, whatever the diff says. DERIVED from
# the REAL constants rather than re-spelled, so a change to either placeholder cannot silently
# stop being detected. Compared after strip() so trailing-newline churn is not mistaken for
# authored content.
UNAUTHORED_REGION_BODIES = frozenset({"", BODY_PLACEHOLDER, BODY_RESET_PLACEHOLDER})


def region_is_unauthored(region_body: str) -> bool:
    """True iff ``region_body`` still carries no prose (empty / either placeholder)."""
    return region_body.strip() in UNAUTHORED_REGION_BODIES


def has_unauthored_region(body: str) -> bool:
    """True iff ``body`` holds at least one agora:body region that is still unauthored (#119).

    The ONE grader behind both halves of the invariant: the curator's post-§4.2 clear
    (:func:`agora_kb.curator.worker._clear_body_status`) and the L2-6 lint check
    (:func:`agora_kb.schema.lint._check_body_status`) call this, so a state the worker just
    produced can never trip its own new lint rule.

    NOTE-LOCAL, never run-scoped: it walks EVERY region in the note, including one an EARLIER
    run left at the placeholder (a same-day APPEND_DAILY or a MERGE_INTO_THEME onto a
    prose-pending theme both produce that shape). A rule derived from only THIS run's region ids
    would clear a flag that is still owed.

    FAIL-SAFE on malformed markers (nested / unmatched / mismatched / DUPLICATED id): returns True,
    so a tampered note KEEPS its flag and L2-6 stays silent about a note L1-20 already hard-rejects.
    The line walk mirrors :func:`agora_kb.curator.apply._extract_sentinel_regions` exactly —
    including its duplicate-id arm, which is not decorative: a duplicated id is the very corruption
    L1-20 was added for after a real dogfood run produced two identical markers in one note. Without
    the ``seen`` set this function would grade such a note as fully authored and L2-6 would report
    "every region is authored" over a note lint simultaneously declares structurally tampered.
    """
    open_cid: str | None = None
    seen: set[str] = set()
    lines: list[str] = []
    for line in body.split("\n"):
        start = BODY_START_LINE_RE.match(line)
        if start is not None:
            if open_cid is not None:
                return True  # nested/overlapping start — malformed, fail safe
            open_cid = start.group("cid")
            lines = []
            continue
        end = BODY_END_LINE_RE.match(line)
        if end is not None:
            if open_cid is None or end.group("cid") != open_cid:
                return True  # unmatched/mismatched end — malformed, fail safe
            if open_cid in seen:
                return True  # duplicated id — malformed, fail safe (apply returns None here)
            if region_is_unauthored("\n".join(lines)):
                return True
            seen.add(open_cid)
            open_cid = None
            lines = []
            continue
        if open_cid is not None:
            lines.append(line)
    return open_cid is not None  # unclosed start — malformed, fail safe
