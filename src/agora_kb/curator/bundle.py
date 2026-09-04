"""The read-only backend input bundle + tier-2 content-equivalence dedup (ADR-0011 §1, §5 tier-2).

This is the deterministic, MODEL-FREE step that sits between the FIFO claim (Stage A,
:mod:`agora_kb.curator.claim`) and the PLAN pass: the worker reads the immutable claimed events
from ``processing/<run-id>/events/``, collapses byte-equivalent knowledge into ONE candidate each
(tier-2, ADR-0011 §5 / DATA-MODEL §11.2), pre-fetches the deterministic MODEL-FREE related view
per candidate (§1.1 — what makes a small local model reliable), and materializes the read-only
``processing/<run-id>/bundle/`` tree the sandboxed backend later reads (§1). Nothing here invokes a
model and nothing here writes the wiki — it is pure orchestration that produces the model's *input*.

Two pieces live here:

* :class:`Candidate` — one tier-2-deduped knowledge unit: its deterministic ``candidate_id``
  (``c1``, ``c2`` … in FIFO order), the manifest ``event_ids`` that collapsed into it, the UNIONed
  per-event ``provenance`` tuples (so the worker — never the model — owns ``sources:``, §2), the
  verbatim ``text``, the ``domain`` hint, the WORST-CASE ``kind``/``confidence`` across the merged
  events, and the worker-computed ``is_gated`` flag (``kind == 'candidate'`` OR
  ``confidence == 'low'``, §6 — computed here, never trusted to the model).
* :func:`build_bundle` — reads the claimed events, applies tier-2 dedup, runs the model-free
  lexical oracle ``core.Wiki.query_lexical`` (ADR-0012 §0a) for each candidate's ``related/`` view
  — deliberately NOT ``core.Wiki.query``, the read face ``kb_query`` owns, which MAY one day grow a
  ranking tier the write path must not follow (issue #144) — writes ``candidates.json`` +
  ``related/<cand-id>.json``, copies the read-only schema doc +
  ``_meta/taxonomy.yaml`` into the bundle, and returns a :class:`BundleResult`.

Tier-2 (ADR-0011 §5): group claimed events by canonical-normalized ``content_sha256`` (the worker
RE-derives it from the body via :func:`agora_kb.core.content_sha256`, never trusting the event's own
``content_sha256:`` field), collapsing identical content from different writers/sources into ONE
candidate while UNIONing every distinct ``{event_id, source, writer, cwd, raw_ref, created, body}``
tuple present in THIS run's manifest into ``provenance[]`` (the immutable event ``body`` is carried
so deterministic APPLY can materialize the cited ``raw/`` source, ADR-0010 D3). The union is bounded
to the manifest universe
(§5 "Tier-2 union universe"), so the §4.1 coverage check stays exact: every manifest ``event_id``
is reachable through exactly one candidate. Determinism is the contract — the same claimed events
always produce the same bytes, so two implementations agree.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from ..core import frontmatter
from ..core.hashing import content_sha256
from ..core.layout import RepoLayout
from ..core.repo import Repo
from ..core.wiki import QueryResult, Wiki
from .constants import DEFAULT_RELATED_K
from .manifest import RunManifest

__all__ = ["Candidate", "BundleResult", "build_bundle"]

# Worst-case ordering for the per-candidate kind/confidence roll-up (§1: "kind/confidence are the
# worst-case across merged events"). A LOWER rank is worse, so a tier-2 group's value is the min.
# kind: a `candidate` (harvested, gated) is worse than a `capture`; confidence: low < medium < high.
# An ABSENT confidence is the most permissive (highest rank), so it never makes a group "low" on its
# own — only a real `low` does (matching the §6 gate: is_gated iff kind==candidate OR conf==low).
_KIND_RANK = {"candidate": 0, "capture": 1}
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


# How much deeper the oracle is asked to look when the ineligible hits will be filtered out again
# (#152 follow-up). The filter runs AFTER retrieval, so without an over-fetch a candidate whose top
# `related_k` hits are all human notes hands the brain an EMPTY view — and an empty related view is
# exactly what tells PASS 1 "genuinely new -> CREATE_THEME", i.e. a duplicate theme beside the
# curator note it should have merged into. Over-fetching and trimming back to `related_k` restores
# the K eligible hits the model was promised. A constant multiplier, not "fetch everything": the
# view is the brain's prompt, `limit` only truncates an already-ranked list (`Wiki.query_lexical`),
# and the trim is a pure suffix drop — so this changes RECALL, never ORDER (ADR-0012 §0a).
_MERGEABLE_OVERFETCH = 4


def _only_mergeable(
    result: QueryResult, mergeable: frozenset[str] | None, *, limit: int
) -> QueryResult:
    """Drop ``related/`` hits that may not be a MERGE/CONTEST target (#152); ``None`` keeps all.

    A pure path-set filter over an ALREADY-computed :class:`QueryResult`, then a truncation to
    ``limit`` — no ranking input is recomputed here and no hit is reordered (ADR-0012 §0a). An
    emptied view reports ``not_found``, the same status the oracle itself uses for "no eligible
    evidence", so the brain reads one consistent shape.
    """
    if mergeable is None:
        return result
    kept = tuple(h for h in result.hits if h.path in mergeable)[: max(0, limit)]
    if kept == result.hits:
        return result
    return result.model_copy(update={"hits": kept, "status": "ok" if kept else "not_found"})


@dataclass(frozen=True)
class Candidate:
    """One tier-2-deduped candidate — the unit the PLAN pass adjudicates (ADR-0011 §1, §5 tier-2).

    A candidate is ONE canonical ``content_sha256`` group: byte-equivalent knowledge captured by
    different writers/sources collapses here into a single entry, but every distinct provenance
    tuple in THIS run's manifest is preserved in :attr:`provenance` (§5 "always preserve
    provenance"). All fields are deterministic functions of the claimed events; ``is_gated`` is
    computed by the worker (§6), never read from or trusted to the model.
    """

    candidate_id: str
    event_ids: tuple[str, ...]
    provenance: tuple[dict[str, object], ...]
    text: str
    domain: str | None
    kind: str
    confidence: str | None
    is_gated: bool


@dataclass(frozen=True)
class BundleResult:
    """The materialized read-only bundle (ADR-0011 §1) + the worker-side handles for later stages.

    :attr:`candidates` is the ordered tier-2 candidate set; :attr:`bundle_dir` is
    ``processing/<run-id>/bundle/`` (the read-only mount the backend reads);
    :attr:`gated_candidate_ids` is the set passed to :func:`agora_kb.curator.plan.validate_plan` for
    the §6 gate; :attr:`provenance` maps each ``candidate_id`` to its UNIONed provenance list (the
    input :func:`apply_plan` materializes into ``sources:`` AND the cited ``raw/`` artifact from
    each tuple's ``body``, so the worker — never the model — owns both provenance and the canonical
    raw/ source, §2 / ADR-0010 D3).
    """

    candidates: tuple[Candidate, ...]
    bundle_dir: Path
    gated_candidate_ids: set[str]
    provenance: dict[str, list[dict[str, object]]]


def build_bundle(
    layout: RepoLayout,
    repo: Repo,
    manifest: RunManifest,
    *,
    related_k: int = DEFAULT_RELATED_K,
    mergeable_paths: Collection[str] | None = None,
) -> BundleResult:
    """Build the read-only input bundle for ``manifest``'s claimed events (ADR-0011 §1, §5 tier-2).

    DETERMINISTIC and pure-read: parses the immutable ``processing/<run-id>/events/`` files, applies
    tier-2 ``content_sha256`` dedup (union provenance over the manifest universe, §5), runs
    :meth:`agora_kb.core.Wiki.query_lexical` over the live wiki for each candidate's ``related/``
    view (§1.1, the model-free lexical oracle — ZERO network, no search tool — NOT the read face's
    :meth:`~agora_kb.core.Wiki.query`, #144), and writes the bundle tree. No model is invoked and no
    wiki file is written.

    ``mergeable_paths`` (issue #152) is the set of repo-relative note paths that MAY legally be a
    MERGE/CONTEST target — the curator's own CONCEPT (and, when ADR-0041 OD-7's producer lands,
    SUMMARY) notes: the two SOURCED kinds, exactly what the plan gate's ``theme_basenames`` accepts
    (a curator journal, a map, ``index.md`` or a human-owned ``wiki/people/`` note is as illegal a
    target as any other human note). The KIND is the DIRECTORY under KB wiki schema 2 (ADR-0041
    D1/D2.1), so the caller derives this set from the same ``kind`` the APPLY-side resolver filters
    on. ``None`` (the default) filters nothing and is byte-identical to before. When given,
    a ``related/`` hit outside the set is dropped from the view the planning brain reads: PASS 1 is
    told to pick ``target_basename`` out of these hits, so offering an ineligible one is handing the
    model a menu with a poisoned item — the plan gate rejects the pick, which fails the WHOLE run.
    Dropping the hit is a pure PATH-SET filter followed by a truncation — it computes no
    ``lex``/``struct``/``fm``/``score``/``match_reason`` and reorders nothing, so ADR-0012 §0a is
    untouched.

    The bundle (§1):

    * ``bundle/candidates.json`` — ONE entry per tier-2 candidate (the backend's PLAN read input);
    * ``bundle/related/<cand-id>.json`` — the ordered top-k related ``QueryResult`` per candidate;
    * ``bundle/schema.md`` — a read-only copy of the repo schema doc (``AGENTS.md``), the rules the
      model must obey;
    * ``bundle/taxonomy.yaml`` — a read-only copy of ``_meta/taxonomy.yaml`` (the closed tag/domain
      vocabulary the model MAY NOT expand, §6.1).

    ``repo`` is accepted as part of the worker's run context (it owns the worktree at
    ``base_commit`` for the per-run ``wiki_index.json`` rebuild, §1.2, and is consumed by later
    stages); the §1.1 related view is read deterministically from the live wiki via :class:`Wiki`,
    so this function does not require the worktree.
    """
    events = _read_events(layout, manifest)
    candidates = _dedup_tier2(events)

    bundle_dir = layout.processing_dir / manifest.run_id / "bundle"
    related_dir = bundle_dir / "related"
    related_dir.mkdir(parents=True, exist_ok=True)

    wiki = Wiki(layout)
    mergeable = None if mergeable_paths is None else frozenset(mergeable_paths)
    candidate_entries: list[dict[str, object]] = []
    provenance: dict[str, list[dict[str, object]]] = {}
    gated_candidate_ids: set[str] = set()

    for cand in candidates:
        # §1.1: retrieve-then-decide. Pre-fetch the deterministic core.read view of the current
        # wiki; the backend never holds a search tool (sandbox, ADR-0008).
        #
        # PERMANENT DETERMINISM PIN (issue #144, ADR-0012 §0a): this is `query_lexical`, the
        # model-free lexical oracle, NOT `query`. `query` is the read face and MAY one day gain a
        # ranking tier above the lexical floor; the write path may not follow it there. This
        # `related/` view is the planning brain's tier-3 input and decides MERGE_INTO_THEME
        # targets, and a wrong merge is permanent — the closed ADR-0011 op vocabulary has no
        # DELETE, apply.py stamps the merged fact with real raw/ provenance, the inbox event is
        # drained to processed/ and tier-2 content_sha256 dedup never re-proposes it. Do not
        # "unify" this back onto query().
        #
        # The fetch is DEEPER than `related_k` exactly when the ineligible hits are about to be
        # filtered out again, so the brain still gets up to `related_k` LEGAL targets in a repo the
        # human also writes in (see `_MERGEABLE_OVERFETCH`). `mergeable is None` keeps the single
        # `related_k` call byte-identical to before.
        over = max(related_k, related_k * _MERGEABLE_OVERFETCH)
        fetch_k = related_k if mergeable is None else over
        result = wiki.query_lexical(cand.text, limit=fetch_k)
        # #152: never offer a target the plan gate will reject (see `mergeable_paths` above), and
        # trim the over-fetch back to the `related_k` the view is contracted to carry.
        result = _only_mergeable(result, mergeable, limit=related_k)
        related_path = related_dir / f"{cand.candidate_id}.json"
        related_path.write_text(_to_json(result.model_dump(mode="json")), encoding="utf-8")

        provenance[cand.candidate_id] = [dict(tup) for tup in cand.provenance]
        if cand.is_gated:
            gated_candidate_ids.add(cand.candidate_id)

        candidate_entries.append(
            {
                "candidate_id": cand.candidate_id,
                "content_sha256": content_sha256(cand.text),
                "text": cand.text,
                "kind": cand.kind,
                "confidence": cand.confidence,
                "is_gated": cand.is_gated,
                "domain": cand.domain,
                "provenance": list(cand.provenance),
                "related_ref": f"related/{cand.candidate_id}.json",
            }
        )

    candidates_doc: dict[str, object] = {
        "run_id": manifest.run_id,
        "candidates": candidate_entries,
    }
    (bundle_dir / "candidates.json").write_text(_to_json(candidates_doc), encoding="utf-8")

    _copy_read_only_inputs(layout, bundle_dir)

    return BundleResult(
        candidates=candidates,
        bundle_dir=bundle_dir,
        gated_candidate_ids=gated_candidate_ids,
        provenance=provenance,
    )


@dataclass(frozen=True)
class _Event:
    """One parsed claimed event: its id, body, and provenance tuple (ADR-0011 §1)."""

    event_id: str
    body: str
    sha: str
    domain: str | None
    kind: str
    confidence: str | None
    provenance: dict[str, object]


def _read_events(layout: RepoLayout, manifest: RunManifest) -> list[_Event]:
    """Parse the claimed ``events/<event-id>.md`` files in manifest (FIFO) order (ADR-0011 §1).

    The manifest ``event_ids`` are already FIFO/chronological (Stage A claim), so iterating them
    preserves order. Each event's ``content_sha256`` is RE-derived from the body (DATA-MODEL §11.2)
    rather than trusted from frontmatter, so tier-2 grouping is reproducible and tamper-independent.
    The provenance tuple is exactly the ``{event_id, source, writer, cwd, raw_ref, created, body}``
    shape :func:`agora_kb.curator.apply_plan` materializes into ``sources:`` (§2): the immutable
    event BODY is threaded in so deterministic APPLY (the ENGINE, never the model) can materialize
    the cited ``raw/<domain>/<event_id>.md`` free-text capture from it (ADR-0010 D3).
    """
    events_dir = layout.processing_dir / manifest.run_id / "events"
    out: list[_Event] = []
    for event_id in manifest.event_ids:
        fm, body = frontmatter.parse((events_dir / f"{event_id}.md").read_text(encoding="utf-8"))
        out.append(
            _Event(
                event_id=event_id,
                body=body,
                sha=content_sha256(body),
                domain=_opt_str(fm.get("domain")),
                kind=str(fm.get("kind")) if isinstance(fm.get("kind"), str) else "capture",
                confidence=_opt_str(fm.get("confidence")),
                provenance={
                    "event_id": event_id,
                    "source": fm.get("source"),
                    "writer": fm.get("writer"),
                    "cwd": fm.get("cwd"),
                    "raw_ref": fm.get("raw_ref"),
                    "created": fm.get("created"),
                    "body": body,
                },
            )
        )
    return out


def _dedup_tier2(events: list[_Event]) -> tuple[Candidate, ...]:
    """Collapse byte-equivalent events into ONE candidate each (ADR-0011 §5 tier-2, DATA-MODEL §11).

    Group events by canonical ``content_sha256``; the candidate carries the UNION of every member's
    provenance tuple (manifest universe only, §5) in FIFO order. ``candidate_id`` is assigned
    ``c1``, ``c2`` … in the FIFO order of each group's FIRST event, so ids are a deterministic
    function of the claimed set. ``text`` is the verbatim body of the FIRST member (byte-equivalent
    by construction); ``domain`` is the first member's hint; ``kind``/``confidence`` are the
    WORST-CASE across members (§1); ``is_gated`` (§6) is computed here from those worst-cases —
    never trusted to the model.
    """
    groups: dict[str, list[_Event]] = {}
    order: list[str] = []  # first-seen sha order == FIFO candidate order
    for ev in events:
        if ev.sha not in groups:
            groups[ev.sha] = []
            order.append(ev.sha)
        groups[ev.sha].append(ev)

    candidates: list[Candidate] = []
    for index, sha in enumerate(order, start=1):
        members = groups[sha]
        first = members[0]
        kind = _worst_kind(members)
        confidence = _worst_confidence(members)
        candidates.append(
            Candidate(
                candidate_id=f"c{index}",
                event_ids=tuple(m.event_id for m in members),
                provenance=tuple(m.provenance for m in members),
                text=first.body,
                domain=first.domain,
                kind=kind,
                confidence=confidence,
                # §6: gated iff harvested-shaped — a candidate kind OR an explicit low confidence.
                is_gated=(kind == "candidate" or confidence == "low"),
            )
        )
    return tuple(candidates)


def _worst_kind(members: list[_Event]) -> str:
    """Worst-case kind across a tier-2 group (§1): a ``candidate`` member makes the group gated.

    ``candidate < capture``; an unrecognized kind is ignored (defaults to ``capture``), so the group
    is treated as a candidate iff at least one merged event declares ``kind: candidate``.
    """
    known = [m.kind for m in members if m.kind in _KIND_RANK]
    if not known:
        return "capture"
    return min(known, key=lambda k: _KIND_RANK[k])


def _worst_confidence(members: list[_Event]) -> str | None:
    """Worst-case confidence across a tier-2 group, or ``None`` if no member declares one (§1).

    ``low < medium < high``; an absent confidence does not make a group ``low`` (it stays the most
    permissive), so ``is_gated`` flips on a real ``low`` only — matching the §6 gate exactly.
    """
    declared = [m.confidence for m in members if m.confidence in _CONFIDENCE_RANK]
    if not declared:
        return None
    return min(declared, key=lambda c: _CONFIDENCE_RANK[c])  # type: ignore[index]


def _copy_read_only_inputs(layout: RepoLayout, bundle_dir: Path) -> None:
    """Copy the read-only schema doc + ``_meta/taxonomy.yaml`` into the bundle (ADR-0011 §1).

    ``bundle/schema.md`` = the repo ``AGENTS.md`` (the rules the model must obey);
    ``bundle/taxonomy.yaml`` = ``_meta/taxonomy.yaml`` (the closed tag/domain vocabulary the model
    MAY NOT expand, §6.1). Both are copies so the bundle is a self-contained read-only mount; the
    originals are never mutated. A missing source is skipped (a not-yet-emitted schema is a
    caller/setup concern, not a bundle error).
    """
    schema_src = layout.schema_file
    if schema_src.is_file():
        (bundle_dir / "schema.md").write_text(
            schema_src.read_text(encoding="utf-8"), encoding="utf-8"
        )
    taxonomy_src = layout.root / "_meta" / "taxonomy.yaml"
    if taxonomy_src.is_file():
        (bundle_dir / "taxonomy.yaml").write_text(
            taxonomy_src.read_text(encoding="utf-8"), encoding="utf-8"
        )


def _opt_str(value: object) -> str | None:
    """Return ``value`` as a ``str`` iff it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


def _to_json(doc: object) -> str:
    """Render a plain-dict document as pretty JSON with a trailing newline (state.json form)."""
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
