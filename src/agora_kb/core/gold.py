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
untouched** (ADR-0027 decision 4). *Eligibility:* a note enters a pack only if it is a ``theme``
(the unit of atomic knowledge), ``status: active``, ungated (``confidence`` != ``low``), and NOT
harvest-origin (``origin: harvest:*`` is DEFAULT-EXCLUDED — without this exclusion gold is a
prompt-injection amplifier: attacker mail/message → harvest → curated summary → injected into every
session). *Score:* ``0.35 ×`` structural centrality (reusing the ADR-0012 §5 ``d_moc`` / in-degree
machinery — but query-independent: every MOC/index root seeds a full-graph BFS) ``+ 0.25 ×`` recency
exp-decay (half-life 30 d, commit-anchored) ``+ 0.20 ×`` status/confidence bucket ``+ 0.20 ×``
provenance density ``min(1, len(sources)/5)``. Greedy fill to a CJK-aware token budget.

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
from .hashing import content_sha256
from .wiki import STRUCT_ALPHA, STRUCT_BETA, _is_moc_path

if TYPE_CHECKING:
    from agora_kb.core.layout import RepoLayout
    from agora_kb.core.repo import Repo

__all__ = [
    "GOLD_SCHEMA_VERSION",
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
GOLD_SCHEMA_VERSION = 1

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

# CJK codepoint ranges: Hangul (syllables + jamo + compatibility), CJK unified ideographs (+Ext A),
# Hiragana/Katakana, CJK symbols/punctuation, and fullwidth forms. A CJK codepoint counts ≈ 1
# token/char; other text at bytes/4. The owner's KB is Korean-heavy and plain bytes/4 underestimates
# CJK by 1.5–3×, so this ships in Phase A (not later) with a Korean-corpus budget-test fixture.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
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


def _is_cjk(codepoint: int) -> bool:
    """True iff ``codepoint`` falls in a CJK range (1 token/char in the estimator)."""
    for lo, hi in _CJK_RANGES:
        if lo <= codepoint <= hi:
            return True
    return False


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

    ``notes`` is ``[(rel_path, basename, frontmatter, body)]``. Seeds: every ``<domain>-moc.md`` and
    its direct children at ``d_moc=0``; root ``index.md`` and its direct children at ``d_moc=1``;
    then multi-source BFS records MIN hop distance (unreached → :data:`_UNREACHED`). In-degree
    counts resolved inbound links. This is the ADR-0012 §5 machinery, but seeded from EVERY
    navigation root rather than a query — a note's centrality is a property of the graph, not a
    question (ADR-0027 decision 4).
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
        if _is_moc_path(rel):
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
    budget-fills eligible theme notes, and renders the ``agora:pack`` span with assembly-time
    sentinel neutralization. Construct over a :class:`Repo`; call :meth:`assemble`.
    """

    def __init__(self, repo: Repo) -> None:
        self._repo = repo
        self._layout = repo.layout

    def assemble(self, spec: PackSpec | None = None, *, generated_at: datetime) -> AssembledPack:
        """Assemble the pack. ``generated_at`` is the caller's wall clock (recorded ONLY in meta —
        the pack BODY never depends on it, preserving byte-identical rebuild). Raises
        :class:`agora_kb.core.repo.GitError` if the curated tip / committer instant cannot be read.
        """
        from agora_kb.schema.notes import parse_all_notes

        spec = spec or PackSpec()
        curated_sha = self._repo.branch_commit()
        reference = self._repo.commit_committer_datetime(curated_sha)

        parsed = [
            (n.rel_path, n.basename, n.frontmatter, n.body) for n in parse_all_notes(self._layout)
        ]
        d_moc, indeg, max_indeg = _compute_centrality(parsed)

        scored: list[_Scored] = []
        for rel, basename, fm, body in parsed:
            if not self._eligible(fm):
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
    def _eligible(fm: dict[str, object]) -> bool:
        """ADR-0027 decision 4 gate: theme, status=active, confidence!=low, not harvest-provenance.

        ``contested``/``stub``/``deprecated`` are excluded by the status=active rule (ADR-0027
        §S2 default-excludes ``contested`` from a standing context slot). ``confidence`` absent →
        treated ``high`` (the non-gated APPLY default), so only an explicit ``low`` is excluded.
        Harvest content is excluded by BOTH decision-4 clauses via :func:`_is_harvest_provenance`
        (``origin: harvest:*`` OR harvest-only ``sources:``) — the anti-injection-amplifier posture.
        """
        if _fm_str(fm.get("type")) != "theme":
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


def read_meta(layout: RepoLayout, pack: str = DEFAULT_PACK) -> PackMeta | None:
    """Read + structurally validate the sidecar at ``_kb/gold/<pack>.meta.json``; ``None`` on any
    problem.

    Never raises: an absent / unreadable / non-JSON / wrong-shape / schema-mismatched sidecar (or an
    unsafe pack name) returns ``None`` so a status read degrades to "no pack" (mirrors
    :func:`agora_kb.core.index_cache.read_payload`). The caller compares ``curated_sha`` against the
    live curated tip to decide fresh vs stale.
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
    if not isinstance(doc, dict) or doc.get("schema_version") != GOLD_SCHEMA_VERSION:
        return None
    try:
        raw_inputs = doc["inputs"]
        inputs = tuple(
            PackInput(path=i["path"], content_sha256=i["content_sha256"], score=i["score"])
            for i in raw_inputs
        )
        return PackMeta(
            schema_version=doc["schema_version"],
            pack=doc["pack"],
            curated_sha=doc["curated_sha"],
            spec_hash=doc["spec_hash"],
            generated_at=doc["generated_at"],
            estimator=doc["estimator"],
            note_count=doc["note_count"],
            est_tokens=doc["est_tokens"],
            budget_tokens=doc["budget_tokens"],
            reference_instant=doc["reference_instant"],
            harvest_derived_share=doc["harvest_derived_share"],
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
