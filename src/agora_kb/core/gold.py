"""Deterministic gold context-pack assembler (ADR-0027, issue #37).

Gold is the DERIVED consumption tier of the medallion model (bronze = the ingress concept — the
append-only inbox spool + immutable ``raw/`` captures; silver = the curated ``wiki/`` SSOT; gold =
these packs). A pack is a PURE, DETERMINISTIC FUNCTION of ``(curated commit, pack spec)``:
reader-class code assembles verbatim summary lines from validator-gated wiki notes into a small,
token-budgeted, byte-stable slice suitable for injection at agent session start. No LLM runs here;
the curator's plan vocabulary gains no op; nothing writes ``wiki/``, the inbox, or indexes
(invariants #1/#2/#3 intact). Everything lands under git-ignored ``_kb/gold/``, rebuildable from
silver (DATA-MODEL §6) — exactly the posture of the harvest cursor (ADR-0017) and the reader cache
(ADR-0012 §2). The LLM ``DISTILL`` act (denser summaries → ``wiki/digests/``) is a SEPARATE,
evidence-triggered decision reserved as ADR-0028; gold v1 is assembly, not authorship.

**Byte-identical rebuild at a fixed (curated commit, spec) is a REGRESSION-TESTED CONTRACT** —
prompt-cache economics depend on stable bytes — so the pack BODY carries ONLY the ``curated_sha`` in
its header; the wall-clock ``generated_at`` / age lives ONLY in the ``meta.json`` sidecar
(:class:`PackMeta`). The recency decay is likewise anchored to the curated commit's committer
timestamp, never a wall clock (:meth:`Repo.commit_committer_datetime`).

**Selection — a NEW deterministic gold-score contract; the frozen ADR-0012 §0 query contract is
untouched** (ADR-0027 decision 4). *Eligibility:* a note enters a pack only if its KIND is one of
:data:`GOLD_KINDS` (``concept`` / ``summary`` — the claim-bearing tiers; ADR-0041 D2.5 maps a
schema-1 ``type: theme`` onto ``concept``, so a v1 repo's eligible population is unchanged),
``status: active``, ungated (``confidence`` != ``low``), NOT ``derived: true`` (ADR-0041 D2.4 —
proposal-plane output is not a curated claim), and NOT harvest-origin (``origin: harvest:*`` is
DEFAULT-EXCLUDED — without this exclusion gold is a prompt-injection amplifier: attacker
mail/message → harvest → curated summary → injected into every session). *Score:* ``0.35 ×``
structural centrality (reusing the ADR-0012 §5 ``d_moc`` / in-degree machinery — but
query-independent: every map/index root seeds a full-graph BFS) ``+ 0.25 ×`` recency
exp-decay (half-life 30 d, commit-anchored) ``+ 0.20 ×`` status/confidence bucket ``+ 0.20 ×``
provenance density ``min(1, len(sources)/5)``. Greedy fill to a CJK-aware token budget.

**``wiki/people/**`` is EXCLUDED from every pack, and the exclusion is a POPULATION filter, not a
score (ADR-0041 D3.3, day 1).** A human-owned note is dropped before centrality is computed, so it
neither enters a pack nor influences one through its in-degree or its BFS edges. Two reasons it is
the population and not merely the eligibility gate: a pack is assembled from *validator-gated*
notes and ``lint()`` permanently excludes ``wiki/people/**`` from its graded population, so a
people note is by construction not gated; and the outbound redaction boundary for a human-owned
read corpus is undesigned, which makes any push-shaped emission of it an unreviewed egress. This is
a **default, not a permanent rule** — lifting it requires that boundary design, not a config flag —
and it is the control for the PUSH surface only: the MCP read tools (``kb_read`` / ``kb_neighbors``
/ ``kb_query``) still serve people notes on demand (D3.3, residual R1).

**The outbound sentinel + loop-break contract (ADR-0027 §8, the single normative spec every
Agora→agent emission path cites).** Every pack is wrapped in ``agora:pack`` / ``agora:pack:end``
markers, and the assembler NEUTRALIZES any embedded ``agora:`` sentinel inside assembled content so
a hostile summary line cannot forge an early span close (the forged-early-close attack). The
consumer half — span-drop of the whole sentinel span — lives in the harvester's ``FileConnector``.
The §8 loop telemetry (near-duplicate shingle counter + harvest-derived-share cap) also lives here.

*Stated residual (ADR-0027 decision 4 / §8):* recency uses the note's ``updated`` frontmatter. A
harvest-ORIGIN note is wholesale excluded, closing the primary amplification path; the finer refusal
to credit a NON-harvest note whose ``updated`` was bumped by a harvest-sourced MERGE would need a
curator-stamped "last non-harvest update" field (no curator change lands in Phase A) and is deferred
— consistent with the ADR's honest-residual posture (the reworded loop stays open, ADR-0017 §5).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .atomicio import atomic_write_text
from .cjk import is_cjk as _is_cjk
from .hashing import content_sha256
from .layout import CLAIM_BEARING_KINDS
from .wiki import STRUCT_ALPHA, STRUCT_BETA, _is_map_path

if TYPE_CHECKING:
    from agora_kb.core.layout import RepoLayout
    from agora_kb.core.repo import Repo
    from agora_kb.schema.notes import Note

__all__ = [
    "GOLD_SCHEMA_VERSION",
    "GOLD_KINDS",
    "DEFAULT_PACK",
    "DEFAULT_BUDGET_TOKENS",
    "ESTIMATOR",
    "PackSpec",
    "PackInput",
    "PackMeta",
    "AssembledPack",
    "GoldBuildResult",
    "PackAssembler",
    "estimate_tokens",
    "build_gold",
    "read_meta",
    "read_meta_schema_version",
    "serialize_meta",
    "pack_fact_lines",
    "shingles",
    "shingle_similarity",
    "count_near_duplicates",
]

# Bump on ANY change to the assembly algorithm, the gold-score, the estimator, or the rendered pack
# grammar: it is folded into :meth:`PackSpec.spec_hash`, so a bump invalidates every pack's
# ``(curated_sha, spec_hash)`` freshness key and forces a rebuild (mirrors index_cache's
# CACHE_SCHEMA_VERSION, ADR-0012 §2). ``schema_version`` of the in-repo schema contract is UNTOUCHED
# — gold state is all git-ignored ``_kb/`` (ADR-0027 decision 9).
#
# 1 → 2 (ADR-0041 D5/D3.3, wave W2.3): the ELIGIBILITY predicate moved from ``type: theme`` to
# :data:`GOLD_KINDS`, ``derived: true`` and ``wiki/people/**`` became exclusions, and the ``d_moc``
# seed moved from ``<domain>-moc.md`` to the shared map predicate. All four change which notes a
# pack contains at an UNCHANGED ``(curated_sha, spec)``, which is exactly the state a freshness key
# must invalidate — without the bump a pack built by the previous build would be served as fresh.
GOLD_SCHEMA_VERSION = 2

#: The kinds a pack may contain (ADR-0041 D2.5, the claim-bearing tiers). Deliberately written as
#: ONE schema-agnostic predicate rather than the v1/v2 branch ``schema.lint._is_sourced_kind``
#: carries: :attr:`Note.kind <agora_kb.schema.notes.Note.kind>` is derived for BOTH schemas, a
#: schema-1 ``type: theme`` maps to ``concept``, and schema 1 has no ``summary`` antecedent at all
#: — so on a v1 repo this set selects exactly the v1 themes and nothing else. ``entity`` is absent
#: (no day-1 producer, OD-8, and an entity page is "registered, with gated filling" — the husk
#: shape a standing context slot must not carry); ``map`` / ``index`` / ``note`` are navigation and
#: journal tiers, not atomic claims; ``person`` is excluded by the D3.3 population filter above.
#: It IS :data:`~agora_kb.core.layout.CLAIM_BEARING_KINDS` (the same object), so this set can never
#: drift from ``schema.lint._V2_SOURCED_KINDS`` or ``faces.mcp_server.ORPHAN_KINDS``.
GOLD_KINDS: frozenset[str] = CLAIM_BEARING_KINDS

DEFAULT_PACK = "default"
# ADR-0027 §S1 (A): a standing include stays cheap in every prompt; 4000 is one config line away
# once real packs measure short (revisit after the V4 curator-economics measurement).
DEFAULT_BUDGET_TOKENS = 2000
# The estimator NAME recorded in meta so ``est_tokens`` is unambiguous about which one produced
# it (swappable later without ambiguity, ADR-0027 decision 5).
ESTIMATOR = "cjk-v1"

# gold-score weights (ADR-0027 decision 4) — a NEW contract; folded into spec_hash.
_W_STRUCT = 0.35
_W_RECENCY = 0.25
_W_BUCKET = 0.20
_W_PROVENANCE = 0.20
_RECENCY_HALF_LIFE_DAYS = 30.0
_PROVENANCE_SATURATION = 5  # min(1, len(sources)/5)
# ADR-0027 §8 Phase-B acceptance criterion: a pack may be at most this fraction harvest-derived
# (pinned harvest content). v1 has no pins and harvest-origin is default-excluded, so the realized
# share is 0.0 — the cap is a live guardrail that binds once pins land (see _cap_harvest_share).
_HARVEST_SHARE_CAP = 0.5

# A d_moc sentinel for a note unreachable from any MOC/index navigation root: 1/(1+d) → ~0, so a
# disconnected note contributes ~no MOC-distance centrality (only its in-degree term).
_UNREACHED = 1_000_000

# An embedded agora sentinel comment OPENER inside assembled content. The producer NEUTRALIZES it
# (ADR-0027 §8 assembly-time neutralization) by breaking the ``<!--`` opener so a hostile summary
# line carrying a literal ``<!-- agora:pack:end … -->`` cannot terminate the span early: the closer
# no longer matches ``core.sentinel.AGORA_SENTINEL_RE`` (``<!--\s*agora:…``), so the REAL closer
# still terminates the span and span-drop removes the whole span cleanly.
_EMBEDDED_SENTINEL_OPEN = re.compile(r"<!--(\s*agora:)", re.IGNORECASE)


# --- token estimator (CJK-aware, ADR-0027 decision 5) -------------------------------------------

# The CJK codepoint range table lives in :mod:`core.cjk` (the SINGLE SOURCE shared with the
# ADR-0012-addendum query tokenizer, issue #56; imported above as ``_is_cjk``). A CJK codepoint
# counts ≈ 1 token/char; other text at bytes/4. The owner's KB is Korean-heavy and plain bytes/4
# underestimates CJK by 1.5–3×, so this ships in Phase A (not later) with a Korean-corpus
# budget-test fixture.


def estimate_tokens(text: str) -> int:
    """CJK-aware token estimate (ADR-0027 decision 5): CJK codepoints ≈ 1 token/char, other at
    bytes/4.

    Deterministic and locale-free (fixed codepoint ranges, no tokenizer model). CJK chars each count
    as one token; every non-CJK char contributes its UTF-8 byte length to a running byte count that
    is divided by 4 (rounded up). The two are summed. An empty string is 0.
    """
    cjk = 0
    other_bytes = 0
    for ch in text:
        if _is_cjk(ord(ch)):
            cjk += 1
        else:
            other_bytes += len(ch.encode("utf-8"))
    return cjk + math.ceil(other_bytes / 4)


# --- pack spec ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PackSpec:
    """The half of the ``(curated commit, spec)`` pair that is NOT the git tree (ADR-0027).

    v1 ships an implicit zero-config ``default`` pack; a git-tracked ``_meta/gold.yaml`` policy file
    (pins, per-audience packs, budgets) is the future home, DEFERRED until pins/team packs land
    (ADR-0027 §S3), so this is a plain value with sensible defaults. :meth:`spec_hash` captures
    everything that affects the pack bytes so a spec change (or a :data:`GOLD_SCHEMA_VERSION` bump)
    invalidates the pack's freshness key.
    """

    name: str = DEFAULT_PACK
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    estimator: str = ESTIMATOR

    def spec_hash(self) -> str:
        """Hex SHA-256 over the normative spec fields + the module scoring constants + the schema
        version — the ``spec_hash`` half of the ``(curated_sha, spec_hash)`` invalidation key."""
        payload = {
            "schema_version": GOLD_SCHEMA_VERSION,
            "name": self.name,
            "budget_tokens": self.budget_tokens,
            "estimator": self.estimator,
            "weights": [_W_STRUCT, _W_RECENCY, _W_BUCKET, _W_PROVENANCE],
            "half_life_days": _RECENCY_HALF_LIFE_DAYS,
            "provenance_saturation": _PROVENANCE_SATURATION,
            "harvest_share_cap": _HARVEST_SHARE_CAP,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --- pack meta sidecar (ADR-0027 decision 3) ----------------------------------------------------


@dataclass(frozen=True)
class PackInput:
    """One note that fed a pack: its path, content hash, and gold score (ADR-0027 decision 3)."""

    path: str
    content_sha256: str
    score: float


@dataclass(frozen=True)
class PackMeta:
    """The ``_kb/gold/<pack>.meta.json`` sidecar (ADR-0027 decision 3).

    Carries everything the byte-identical pack BODY must NOT carry: the wall-clock ``generated_at``
    (age), the ``(curated_sha, spec_hash)`` invalidation key, the ``reference_instant`` (the
    committer timestamp that anchored recency decay — recorded for determinism auditing), the
    realized ``harvest_derived_share`` (the §8 telemetry), and the per-input provenance. Read
    tolerantly (:func:`read_meta` returns ``None`` on any problem) so a corrupt sidecar degrades to
    "no pack" rather than crashing a status read.
    """

    schema_version: int
    pack: str
    curated_sha: str
    spec_hash: str
    generated_at: str  # ISO-Z wall clock — ONLY here, never in the pack body
    estimator: str
    note_count: int
    est_tokens: int
    budget_tokens: int
    reference_instant: str  # the committer instant anchoring recency decay (determinism record)
    harvest_derived_share: float
    inputs: tuple[PackInput, ...]


@dataclass(frozen=True)
class AssembledPack:
    """The pure result of :meth:`PackAssembler.assemble`: the pack ``text`` + its :class:`PackMeta`.

    The byte-identical-rebuild contract is a property of ``text`` alone (``meta.generated_at`` is
    wall clock and legitimately differs between rebuilds).
    """

    text: str
    meta: PackMeta


@dataclass(frozen=True)
class GoldBuildResult:
    """Outcome of :func:`build_gold` (mirrors :class:`agora_kb.core.wiki.IndexBuildResult`)."""

    pack: str
    note_count: int
    est_tokens: int
    curated_sha: str
    pack_path: Path
    meta_path: Path


# --- internal scoring model ---------------------------------------------------------------------


@dataclass
class _Scored:
    """Working state for one eligible note during assembly."""

    path: str
    basename: str
    title: str
    summary: str
    body: str
    score: float
    harvest_derived: bool


def _fm_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _fm_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _collapse_ws(text: str) -> str:
    """Collapse all runs of whitespace (incl. newlines) to single spaces and trim.

    Keeps a rendered pack line to one bullet line (a multi-line frontmatter summary can never
    inject a second bullet or a spurious blank line) — this is a structural safety AND a
    byte-stability aid.
    """
    return re.sub(r"\s+", " ", text).strip()


def _neutralize_sentinels(text: str) -> str:
    """Defang any embedded agora sentinel opener in assembled text (ADR-0027 §8 producer duty)."""
    return _EMBEDDED_SENTINEL_OPEN.sub(r"<!-\1", text)


def _parse_note_datetime(fm: dict[str, object]) -> datetime | None:
    """Best-effort UTC datetime of a note's last update: ``updated`` → ``timestamp`` → ``created``.

    ``updated``/``created`` are ``YYYY-MM-DD`` strings (midnight UTC); ``timestamp`` is a full
    ``…T00:00:00Z`` instant. A ``datetime.date`` (some YAML loaders coerce a bare date) is accepted.
    Returns ``None`` when nothing parses — a note with no date gets zero recency credit rather
    than crashing the pack (tolerant-consumer posture, ADR-0014 D1)."""
    for key in ("updated", "timestamp", "created"):
        raw = fm.get(key)
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
        if isinstance(raw, date):
            return datetime(raw.year, raw.month, raw.day, tzinfo=UTC)
        if isinstance(raw, str) and raw.strip():
            text = raw.strip()
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                try:  # a bare YYYY-MM-DD
                    d = date.fromisoformat(text[:10])
                    return datetime(d.year, d.month, d.day, tzinfo=UTC)
                except ValueError:
                    continue
    return None


def _recency(note_dt: datetime | None, reference: datetime) -> float:
    """Exp-decay recency in [0,1], half-life 30 d, anchored to ``reference`` (committer instant).

    ``0.5 ** (age_days / 30)``. Age is clamped to ≥ 0 (a note dated after the commit — clock skew /
    a future-dated import — gets the max 1.0, never a >1 credit). ``None`` → 0.0.
    """
    if note_dt is None:
        return 0.0
    age_days = max(0.0, (reference - note_dt).total_seconds() / 86400.0)
    return 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)


def _status_confidence_bucket(confidence: str) -> float:
    """Status/confidence bucket in [0,1] (eligibility pins status=active, confidence!=low)."""
    if confidence == "high":
        return 1.0
    if confidence == "medium":
        return 0.7
    return 0.5  # defensive: an eligible note is high|medium (absent → high), never reaches here


def _provenance_density(sources: list[object]) -> float:
    """``min(1, len(sources)/5)`` — provenance density (ADR-0027 decision 4)."""
    return min(1.0, len(sources) / _PROVENANCE_SATURATION)


def _is_harvest_provenance(fm: dict[str, object]) -> bool:
    """True iff a note is harvest-ORIGIN or its provenance is HARVEST-ONLY (ADR-0027 decision 4).

    Decision 4 default-excludes a note via EITHER clause: an explicit ``origin: harvest:*`` (the
    curator's stamp when a harvested candidate CREATED the note), OR a ``sources:`` list whose every
    entry is a ``harvest:<agent>`` source with no non-harvest source (harvest-only provenance — e.g.
    a harvested candidate merged into a note that carried a PRE-EXISTING non-harvest ``origin`` the
    curator's set-union stamp refused to overwrite, leaving ``origin: manual`` +
    ``sources: [harvest:…]``). Both are excluded so gold can never become a prompt-injection
    amplifier. A note with MIXED provenance (≥1 non-harvest source) is NOT excluded: it has genuine
    curated provenance and v1 packs render only its curator-authored summary line, not the merged
    body. (In v1 the summary path means neither clause admits attacker text to an emitted line; the
    exclusion is the ADR-mandated posture that stays correct once pins render full bodies.)
    """
    origin = (_fm_str(fm.get("origin")) or "").strip().lower()
    if origin.startswith("harvest:"):
        return True
    sources = [s for s in _fm_list(fm.get("sources")) if isinstance(s, str)]
    harvest_sources = [s for s in sources if s.strip().lower().startswith("harvest:")]
    return bool(harvest_sources) and len(harvest_sources) == len(sources)


def _structural(d_moc: int, indeg: int, max_indeg: int) -> float:
    """The ADR-0012 §5 degree-surrogate structural score, reusing the frozen §5 constants.

    Byte-identical to :func:`agora_kb.core.wiki._structural`; kept here (over the private import) so
    the reuse of the ADR-0012 §5 formula/constants is explicit and self-documenting.
    """
    indeg_norm = indeg / max(1, max_indeg)
    return STRUCT_ALPHA * (1.0 / (1 + d_moc)) + STRUCT_BETA * indeg_norm


# --- graph (query-independent centrality) -------------------------------------------------------


def _note_outlinks(body: str, fm: dict[str, object]) -> list[str]:
    """Resolved outlink basenames of a note: BODY markdown links + frontmatter related/children.

    Reuses the frozen schema grammars (:func:`body_link_basenames` / :func:`wikilinks`) so the gold
    graph is the SAME link surface the read path and lint seed on (ADR-0014 D3). Order-preserving,
    de-duplicated.
    """
    from agora_kb.schema.notes import body_link_basenames, wikilinks

    out: list[str] = []
    seen: set[str] = set()
    for base in body_link_basenames(body):
        if base not in seen:
            seen.add(base)
            out.append(base)
    for key in ("related", "children"):
        for item in _fm_list(fm.get(key)):
            if isinstance(item, str):
                for base in wikilinks(item):
                    if base not in seen:
                        seen.add(base)
                        out.append(base)
    return out


def _compute_centrality(
    notes: list[tuple[str, str, dict[str, object], str]],
) -> tuple[dict[str, int], dict[str, int], int]:
    """Return ``(d_moc, indeg, max_indeg)`` over the whole note graph (query-independent).

    ``notes`` is ``[(rel_path, basename, frontmatter, body)]``. Seeds: every MAP and its direct
    children at ``d_moc=0``; root ``index.md`` and its direct children at ``d_moc=1``; then
    multi-source BFS records MIN hop distance (unreached → :data:`_UNREACHED`). In-degree counts
    resolved inbound links. This is the ADR-0012 §5 machinery, but seeded from EVERY navigation
    root rather than a query — a note's centrality is a property of the graph, not a question
    (ADR-0027 decision 4).

    The map predicate is :func:`agora_kb.core.wiki._is_map_path`, IMPORTED rather than restated:
    ADR-0041 D5 moves it from ``wiki/<domain>/<domain>-moc.md`` to the ``wiki/maps/`` directory as
    ONE shared edit with the query path, precisely so gold and ``Wiki.query`` can never seed
    ``d_moc`` from two different definitions of what a map is.
    """
    by_basename = {b: (rel, fm, body) for rel, b, fm, body in notes}
    outlinks: dict[str, list[str]] = {b: _note_outlinks(body, fm) for rel, b, fm, body in notes}

    indeg: dict[str, int] = {b: 0 for _rel, b, _fm, _body in notes}
    for targets in outlinks.values():
        for t in targets:
            if t in indeg:
                indeg[t] += 1
    max_indeg = max(indeg.values(), default=0)

    # Level-0 seeds: MOC notes + their direct children. Level-1 seeds: root index.md + its children.
    level0: set[str] = set()
    level1: set[str] = set()
    for rel, b, _fm, _body in notes:
        if _is_map_path(rel):
            level0.add(b)
            level0.update(t for t in outlinks[b] if t in by_basename)
    idx = by_basename.get("index")
    if idx is not None and idx[0] == "index.md":
        level1.add("index")
        level1.update(t for t in outlinks.get("index", []) if t in by_basename)

    d_moc: dict[str, int] = {}
    queue: deque[str] = deque()
    for b in sorted(level0):  # enqueue all d=0 before any d=1 → nondecreasing BFS order
        d_moc[b] = 0
        queue.append(b)
    for b in sorted(level1):
        if b not in d_moc:
            d_moc[b] = 1
            queue.append(b)
    while queue:
        b = queue.popleft()
        for t in outlinks.get(b, []):
            if t in by_basename and t not in d_moc:
                d_moc[t] = d_moc[b] + 1
                queue.append(t)
    for _rel, b, _fm, _body in notes:
        d_moc.setdefault(b, _UNREACHED)
    return d_moc, indeg, max_indeg


# --- the assembler ------------------------------------------------------------------------------


class PackAssembler:
    """Assemble a gold pack as a pure, deterministic function of ``(curated commit, spec)``.

    Reader-class code — never the sandboxed model. Reads the wiki via the tolerant
    :func:`parse_all_notes` (the SAME scanner the browse face + lint build on), scores + orders +
    budget-fills the eligible :data:`GOLD_KINDS` notes, and renders the ``agora:pack`` span with
    assembly-time sentinel neutralization. Construct over a :class:`Repo`; call :meth:`assemble`.
    """

    def __init__(self, repo: Repo) -> None:
        self._repo = repo
        self._layout = repo.layout

    def assemble(self, spec: PackSpec | None = None, *, generated_at: datetime) -> AssembledPack:
        """Assemble the pack. ``generated_at`` is the caller's wall clock (recorded ONLY in meta —
        the pack BODY never depends on it, preserving byte-identical rebuild). Raises
        :class:`agora_kb.core.repo.GitError` if the curated tip / committer instant cannot be read.
        """
        from agora_kb.schema.notes import is_ungraded_people_note, parse_all_notes

        spec = spec or PackSpec()
        curated_sha = self._repo.branch_commit()
        reference = self._repo.commit_committer_datetime(curated_sha)

        # The D3.3 people exclusion is applied HERE, at the population boundary, so it is
        # structural: a human-owned note is not scored, not rendered, and — because the filter
        # precedes _compute_centrality — contributes no in-degree and no BFS edge to any note that
        # IS in the pack. An eligibility-only exclusion would still let `wiki/people/**` move the
        # pack's contents through the graph. (:meth:`_eligible` re-checks it as a second layer.)
        # `is_ungraded_people_note` — not a bare path test — because D3.3's tree exists only on
        # schema 2; on a schema-1 repo `wiki/people/` is an ordinary DOMAIN that lint grades, so an
        # unconditional exclusion would make gold answer differently from lint and the dashboard.
        population = [n for n in parse_all_notes(self._layout) if not is_ungraded_people_note(n)]
        parsed = [(n.rel_path, n.basename, n.frontmatter, n.body) for n in population]
        d_moc, indeg, max_indeg = _compute_centrality(parsed)

        scored: list[_Scored] = []
        for note in population:
            rel, basename, fm, body = note.rel_path, note.basename, note.frontmatter, note.body
            if not self._eligible(note):
                continue
            struct = round(
                _structural(d_moc.get(basename, _UNREACHED), indeg.get(basename, 0), max_indeg), 6
            )
            recency = round(_recency(_parse_note_datetime(fm), reference), 6)
            confidence = (_fm_str(fm.get("confidence")) or "high").strip().lower()
            bucket = round(_status_confidence_bucket(confidence), 6)
            provenance = round(_provenance_density(_fm_list(fm.get("sources"))), 6)
            score = round(
                _W_STRUCT * struct
                + _W_RECENCY * recency
                + _W_BUCKET * bucket
                + _W_PROVENANCE * provenance,
                6,
            )
            title = _collapse_ws(_fm_str(fm.get("title")) or basename.replace("-", " "))
            summary = _collapse_ws(_fm_str(fm.get("summary")) or "")
            scored.append(
                _Scored(
                    path=rel,
                    basename=basename,
                    title=title,
                    summary=summary,
                    body=body,
                    score=score,
                    # Eligibility already excludes harvest-provenance notes, so this is False for
                    # every SELECTED note in v1; kept consistent with the exclusion so the §8
                    # share cap stays correct once pins (which bypass eligibility) land.
                    harvest_derived=_is_harvest_provenance(fm),
                )
            )

        # Total order (no ties survive): score desc, then path asc — a deterministic tie-break.
        scored.sort(key=lambda s: (-s.score, s.path))
        scored, harvest_share = _cap_harvest_share(scored, _HARVEST_SHARE_CAP)

        selected = self._budget_fill(spec, scored)

        text = self._render(spec, curated_sha, selected)
        # est_tokens is the EXACT script-aware estimate of the RENDERED pack bytes — so a consumer
        # that recomputes estimate_tokens(pack.text) gets the same value (the greedy fill uses a
        # conservative per-line over-estimate to DECIDE inclusion; this records the true total).
        est_tokens = estimate_tokens(text)
        inputs = tuple(
            PackInput(path=s.path, content_sha256=content_sha256(s.body), score=s.score)
            for s in selected
        )
        meta = PackMeta(
            schema_version=GOLD_SCHEMA_VERSION,
            pack=spec.name,
            curated_sha=curated_sha,
            spec_hash=spec.spec_hash(),
            generated_at=_iso_z(generated_at),
            estimator=spec.estimator,
            note_count=len(selected),
            est_tokens=est_tokens,
            budget_tokens=spec.budget_tokens,
            reference_instant=_iso_z(reference),
            harvest_derived_share=round(harvest_share, 6),
            inputs=inputs,
        )
        return AssembledPack(text=text, meta=meta)

    # --- internals ------------------------------------------------------------------------------
    @staticmethod
    def _eligible(note: Note) -> bool:
        """ADR-0027 decision 4 gate, on the ADR-0041 kind axis.

        ``kind ∈`` :data:`GOLD_KINDS`, not ``wiki/people/**``, not ``derived: true``,
        ``status: active``, ``confidence != low``, and not harvest-provenance.

        The kind is derived under the repo's own ``schema_version`` — from the DIRECTORY on
        schema 2 (D2.1, with NO frontmatter fallback: see the comment below), from the frozen D2.5
        ``type:`` table on schema 1 — so this ONE predicate is correct on both schemas and the
        assembler never branches on a layout. ``contested``/``stub``/``deprecated`` are excluded by
        the status=active rule (ADR-0027 §S2 default-excludes ``contested`` from a standing context
        slot). ``confidence`` absent → treated ``high`` (the non-gated APPLY default), so only an
        explicit ``low`` is excluded. Harvest content is excluded by BOTH decision-4 clauses via
        :func:`_is_harvest_provenance` (``origin: harvest:*`` OR harvest-only ``sources:``) — the
        anti-injection-amplifier posture. The ``wiki/people/**`` clause is the SECOND layer of the
        D3.3 exclusion (:meth:`assemble` filters the population before scoring); it is restated
        here so a future caller that scores a note directly cannot reopen the egress, and it is
        the same schema-gated predicate the population filter uses.
        """
        from agora_kb.schema.notes import is_ungraded_people_note, path_kind

        fm = note.frontmatter
        # On schema 2 the kind is read off the PATH here, never off ``Note.kind``. The two agree
        # everywhere except the one place that matters: ``_derive_kind`` falls back to the
        # frontmatter ``kind:`` MIRROR whenever the path declares no kind — i.e. for every
        # OFF-LAYOUT note (``wiki/scratch/x.md``, ``wiki/x.md``, a variant-cased
        # ``wiki/People/x.md``), which is exactly the L1-22 population. ``assemble`` never runs
        # lint, so that fallback let anyone who can drop a file into ``wiki/`` (a human, a synced
        # vault, a teammate's push) put arbitrary prose into every agent's standing context just by
        # declaring ``kind: concept`` under a directory the schema does not know — defeating the
        # D3.1 closed-directory guarantee and, for a variant-cased ``people/``, the D3.3 exclusion.
        # Schema 1 has no directory authority to read, so ``note.kind`` (the frozen D2.5 ``type:``
        # table) stays the answer there and ``test_schema_1_repo_still_assembles`` is unaffected.
        kind = path_kind(note.rel_path) if note.schema_version >= 2 else note.kind
        if kind not in GOLD_KINDS:
            return False
        if is_ungraded_people_note(note):
            return False
        if note.derived:  # ADR-0041 D2.4: a derivation-plane note is not a curated claim.
            return False
        if (_fm_str(fm.get("status")) or "").strip().lower() != "active":
            return False
        if (_fm_str(fm.get("confidence")) or "high").strip().lower() == "low":
            return False
        return not _is_harvest_provenance(fm)

    def _budget_fill(self, spec: PackSpec, scored: list[_Scored]) -> list[_Scored]:
        """Greedy fill to the token budget in score order (ADR-0027 decision 4).

        Include a note iff its rendered line fits the REMAINING budget, then keep scanning
        (skip-and-continue) so a single oversized early note cannot starve the rest. The running
        total is a CONSERVATIVE (budget-safe) over-estimate — ``estimate_tokens`` rounds bytes/4 up
        PER line, so the per-line sum is an upper bound on ``estimate_tokens`` of the concatenated
        pack; the emitted pack therefore never exceeds ``budget_tokens``. Returns the selected notes
        in score order; the caller records the EXACT ``estimate_tokens(rendered pack)`` in meta.
        """
        selected: list[_Scored] = []
        running = estimate_tokens(self._render(spec, "0" * 40, []))  # frame cost (header + markers)
        for s in scored:
            line_tokens = estimate_tokens(_render_line(s) + "\n")
            if running + line_tokens > spec.budget_tokens:
                continue
            selected.append(s)
            running += line_tokens
        return selected

    def _render(self, spec: PackSpec, curated_sha: str, selected: list[_Scored]) -> str:
        """Render the ``agora:pack`` span. Byte-stable at a fixed (commit, spec): no wall clock."""
        repo = self._layout.root.name
        head = f"<!-- agora:pack repo={repo} pack={spec.name} commit={curated_sha} -->"
        tail = f"<!-- agora:pack:end repo={repo} pack={spec.name} commit={curated_sha} -->"
        lines = [
            head,
            f"# gold: {spec.name}",
            (
                f"> Derived context pack — assembled from curated commit `{curated_sha}`; "
                f"rebuildable, do not edit (ADR-0027)."
            ),
            "",
        ]
        lines.extend(_render_line(s) for s in selected)
        if selected:
            lines.append("")
        lines.append(tail)
        return "\n".join(lines) + "\n"


def _render_line(s: _Scored) -> str:
    """Render one note as a single bullet, sentinel-neutralized (ADR-0027 §8 producer duty)."""
    title = _neutralize_sentinels(s.title)
    summary = _neutralize_sentinels(s.summary)
    body = f"- **{title}**"
    if summary:
        body += f" — {summary}"
    body += f"  [{s.path}]"
    return body


def _cap_harvest_share(selected: list[_Scored], cap: float) -> tuple[list[_Scored], float]:
    """Enforce the ADR-0027 §8 harvest-derived-share cap; return ``(kept, realized_share)``.

    A pack may be at most ``cap`` fraction harvest-derived. Drops the LOWEST-score harvest-derived
    entries (``selected`` is pre-sorted score desc, so the last harvest entries) until the share is
    within the cap, preserving relative order otherwise. **v1:** harvest-ORIGIN notes are
    default-excluded at eligibility and v1 ships no pins, so nothing is harvest-derived and the
    realized share is 0.0 — the cap is a live guardrail that binds once human pins (which CAN
    introduce harvest content) land (ADR-0027 decision 4). Kept as pure + unit-tested so the
    guardrail is real, not aspirational.
    """
    total = len(selected)
    if total == 0:
        return selected, 0.0
    harvest_idx = [i for i, s in enumerate(selected) if s.harvest_derived]

    def share(dropped: set[int]) -> float:
        remaining_total = total - len(dropped)
        if remaining_total == 0:
            return 0.0
        remaining_harvest = sum(1 for i in harvest_idx if i not in dropped)
        return remaining_harvest / remaining_total

    dropped: set[int] = set()
    # Drop lowest-score harvest entries first (highest index, since sorted score desc).
    for i in reversed(harvest_idx):
        if share(dropped) <= cap:
            break
        dropped.add(i)
    kept = [s for i, s in enumerate(selected) if i not in dropped]
    return kept, share(dropped)


# --- meta serialization (mirrors index_cache) ---------------------------------------------------


def serialize_meta(meta: PackMeta) -> str:
    """Serialize a :class:`PackMeta` to canonical JSON (sort_keys, trailing ``\\n``)."""
    doc = {
        "schema_version": meta.schema_version,
        "pack": meta.pack,
        "curated_sha": meta.curated_sha,
        "spec_hash": meta.spec_hash,
        "generated_at": meta.generated_at,
        "estimator": meta.estimator,
        "note_count": meta.note_count,
        "est_tokens": meta.est_tokens,
        "budget_tokens": meta.budget_tokens,
        "reference_instant": meta.reference_instant,
        "harvest_derived_share": meta.harvest_derived_share,
        "inputs": [
            {"path": i.path, "content_sha256": i.content_sha256, "score": i.score}
            for i in meta.inputs
        ],
    }
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _read_meta_doc(layout: RepoLayout, pack: str) -> dict[str, object] | None:
    """Return the sidecar's raw JSON object, or ``None`` when there is no readable object at all.

    Split out of :func:`read_meta` so that "absent / unreadable / not a JSON object" (a CORRUPT
    sidecar) and "a JSON object declaring some OTHER ``schema_version``" (a sidecar written by a
    different build) stay distinguishable — :func:`read_meta` collapses them both to ``None``,
    which is right for a freshness read and wrong for a serve gate. Never raises.
    """
    from agora_kb.core.layout import InvalidWriterError

    try:
        path = layout.gold_meta_path(pack)
    except InvalidWriterError:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def read_meta_schema_version(layout: RepoLayout, pack: str = DEFAULT_PACK) -> int | None:
    """Return the :data:`GOLD_SCHEMA_VERSION` the sidecar DECLARES, or ``None`` if it declares none.

    The question :func:`read_meta` cannot answer, because it returns ``None`` for a corrupt sidecar
    and for a version-mismatched one alike. A serve path needs the difference: a corrupt sidecar
    says nothing about the PACK BYTES (which are the contract — the meta is advisory), while a
    sidecar declaring an older version says the bytes beside it were assembled under a different
    eligibility rule, and a :data:`GOLD_SCHEMA_VERSION` bump exists precisely to invalidate those.

    ``None`` for an absent / unreadable / non-object / version-less / non-integer sidecar; a
    ``bool`` is rejected too (``True`` is an ``int`` in Python and would read as version 1).
    """
    doc = _read_meta_doc(layout, pack)
    if doc is None:
        return None
    version = doc.get("schema_version")
    return version if isinstance(version, int) and not isinstance(version, bool) else None


def _meta_str(doc: dict[str, object], key: str) -> str:
    """Extract ``doc[key]`` as a ``str``; raises :class:`TypeError` on any other JSON shape."""
    value = doc[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a str")
    return value


def _meta_int(doc: dict[str, object], key: str) -> int:
    """Extract ``doc[key]`` as an ``int``; raises :class:`TypeError` on any other JSON shape.

    ``bool`` is rejected too — it is an ``int`` subtype in Python and would otherwise pass silently.
    """
    value = doc[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key} must be an int")
    return value


def _meta_float(doc: dict[str, object], key: str) -> float:
    """Extract ``doc[key]`` as a ``float``; a JSON int is accepted and widened, ``bool`` is not."""
    value = doc[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{key} must be a float")
    return float(value)


def _parse_pack_inputs(raw_inputs: object) -> tuple[PackInput, ...]:
    """Parse the sidecar's ``inputs`` array into :class:`PackInput` rows.

    Raises :class:`TypeError` on any shape other than a list of ``{path, content_sha256, score}``
    objects — the caller's ``except`` turns that into the same ``None`` a corrupt sidecar always
    produced.
    """
    if not isinstance(raw_inputs, list):
        raise TypeError("inputs must be a list")
    parsed: list[PackInput] = []
    for item in raw_inputs:
        if not isinstance(item, dict):
            raise TypeError("each input must be an object")
        parsed.append(
            PackInput(
                path=_meta_str(item, "path"),
                content_sha256=_meta_str(item, "content_sha256"),
                score=_meta_float(item, "score"),
            )
        )
    return tuple(parsed)


def read_meta(layout: RepoLayout, pack: str = DEFAULT_PACK) -> PackMeta | None:
    """Read + structurally validate the sidecar at ``_kb/gold/<pack>.meta.json``; ``None`` on any
    problem.

    Never raises: an absent / unreadable / non-JSON / wrong-shape / schema-mismatched sidecar (or an
    unsafe pack name) returns ``None`` so a status read degrades to "no pack" (mirrors
    :func:`agora_kb.core.index_cache.read_payload`). The caller compares ``curated_sha`` against the
    live curated tip to decide fresh vs stale. A caller that must tell a version mismatch from a
    corrupt sidecar asks :func:`read_meta_schema_version` as well.
    """
    doc = _read_meta_doc(layout, pack)
    if doc is None or doc.get("schema_version") != GOLD_SCHEMA_VERSION:
        return None
    try:
        inputs = _parse_pack_inputs(doc["inputs"])
        return PackMeta(
            schema_version=_meta_int(doc, "schema_version"),
            pack=_meta_str(doc, "pack"),
            curated_sha=_meta_str(doc, "curated_sha"),
            spec_hash=_meta_str(doc, "spec_hash"),
            generated_at=_meta_str(doc, "generated_at"),
            estimator=_meta_str(doc, "estimator"),
            note_count=_meta_int(doc, "note_count"),
            est_tokens=_meta_int(doc, "est_tokens"),
            budget_tokens=_meta_int(doc, "budget_tokens"),
            reference_instant=_meta_str(doc, "reference_instant"),
            harvest_derived_share=_meta_float(doc, "harvest_derived_share"),
            inputs=inputs,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _iso_z(value: datetime) -> str:
    """Render a UTC instant as ``2026-07-05T03:00:12Z`` (matches the state-file / cursor form)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- build (assemble + durable write) -----------------------------------------------------------


def build_gold(
    repo: Repo, spec: PackSpec | None = None, *, generated_at: datetime
) -> GoldBuildResult:
    """Assemble a pack and atomically write ``_kb/gold/<pack>.md`` + ``.meta.json`` (ADR-0027).

    Reader/worker-class code ONLY — never the sandboxed backend, never in the ADR-0008 INGEST
    allowlist (invariant #2). The two writes are each temp+rename atomic (the ``atomic_write_text``
    posture); concurrent writers are safe because the bytes are a pure function of
    ``(curated commit, spec)`` — last-writer-wins converges. ``generated_at`` is the caller's wall
    clock, recorded ONLY in the meta sidecar (the pack body stays byte-identical). Raises
    :class:`agora_kb.core.repo.GitError` if the repo has no curated tip / committer instant.
    """
    spec = spec or PackSpec()
    assembled = PackAssembler(repo).assemble(spec, generated_at=generated_at)
    layout = repo.layout
    pack_path = layout.gold_pack_path(spec.name)
    meta_path = layout.gold_meta_path(spec.name)
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(pack_path, assembled.text, exclusive=False)
    atomic_write_text(meta_path, serialize_meta(assembled.meta), exclusive=False)
    return GoldBuildResult(
        pack=spec.name,
        note_count=assembled.meta.note_count,
        est_tokens=assembled.meta.est_tokens,
        curated_sha=assembled.meta.curated_sha,
        pack_path=pack_path,
        meta_path=meta_path,
    )


# --- §8 loop telemetry: near-duplicate (shingle) counter ----------------------------------------


def pack_fact_lines(pack_text: str) -> list[str]:
    """Extract the bullet fact lines of a rendered pack (the ``- `` lines between the markers).

    Used by the harvester to compare EMITTED pack content against INCOMING harvest candidates for
    the §8 near-duplicate loop counter. Tolerant of a hand-edited / partial pack (it just scans for
    ``- `` bullets); the surrounding sentinel markers and the ``# gold:`` heading are ignored.
    """
    out: list[str] = []
    for line in pack_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            out.append(stripped[2:].strip())
    return out


def shingles(text: str, k: int = 8) -> set[str]:
    """Character k-shingles of ``text`` (lowercased, whitespace-collapsed).

    CHARACTER n-grams (not word tokens) so the near-duplicate signal is language-agnostic — it works
    for the Korean-heavy corpus where a ``[a-z0-9]+`` word tokenizer would see almost nothing. A
    string shorter than ``k`` yields the whole normalized string as a single shingle.
    """
    norm = _collapse_ws(text.lower())
    if not norm:
        return set()
    if len(norm) <= k:
        return {norm}
    return {norm[i : i + k] for i in range(len(norm) - k + 1)}


def shingle_similarity(a: str, b: str, k: int = 8) -> float:
    """Jaccard similarity of the character k-shingle sets of ``a`` and ``b`` (0..1)."""
    sa, sb = shingles(a, k), shingles(b, k)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def count_near_duplicates(
    pack_lines: list[str], candidate_texts: list[str], *, threshold: float = 0.6, k: int = 8
) -> int:
    """Count ``candidate_texts`` that near-duplicate ANY ``pack_line`` (ADR-0027 §8 loop telemetry).

    The reworded loop (ADR-0017 §5) is NOT claimed closed — an agent restating pack content in its
    own words defeats any marker — so this instruments it: how many incoming harvest candidates look
    like reworded emissions of the gold pack. A count > 0 is a loop signal, not an error.
    """
    pack_shingles = [shingles(line, k) for line in pack_lines if line.strip()]
    if not pack_shingles:
        return 0
    count = 0
    for cand in candidate_texts:
        cs = shingles(cand, k)
        if not cs:
            continue
        for ps in pack_shingles:
            union = len(cs | ps)
            if union and len(cs & ps) / union >= threshold:
                count += 1
                break
    return count
