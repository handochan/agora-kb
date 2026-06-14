"""Deterministic §3 APPLY + §4.2 AUTHOR-diff validation + §4.6 stray-link strip (ADR-0011).

This is the *second* deterministic stage of PLAN-APPLY-AUTHOR (ADR-0011). After PASS-1's
``plan.json`` clears the §4.1 PLAN gate (:mod:`agora_kb.curator.plan`), the WORKER — not the model
— materializes EVERYTHING that bears integrity: files, the full ADR-0010 C2 frontmatter, the
``sources:`` provenance union, wikilinks, MOC/index entries, the contested callout, daily sections,
and the candidate-id-keyed body sentinels. The model later authors ONLY prose between those
sentinels (PASS 2), graded by :func:`validate_author_diff`.

Three pure pieces live here:

* :func:`apply_plan` — the §3 deterministic APPLY. Given a validated
  :class:`~agora_kb.curator.plan.Plan`,
  a worktree path, the injected ``run_date`` (so APPLY reads NO wall clock — ADR-0010 D1), and the
  per-candidate ``provenance`` (so the WORKER writes ``sources:``, never the model — ADR-0011 §2),
  it performs each disposition's op and updates the touched ``<domain>-moc.md`` files and root
  ``index.md``. It writes under the §4.0 curated allowlist (``wiki/**``, ``index.md``,
  ``<domain>-moc.md``, ``log.md``, ``assets/**``) PLUS the immutable canonical sources under
  ``raw/<domain>/<event_id>.md`` for every cited free-text capture, and RETURNS the exact
  ``{raw_ref: content}`` set it materialized so the worker's final-diff gate admits ONLY those exact
  engine-written sources (a brain-planted or brain-overwritten ``raw/`` file is rejected).

  ADR cross-reference (design divergence, ADR-0010 D3): the ADR's literal wording names
  ``core.ingest`` (the WRITE path) as the ONLY writer of ``raw/``, persisting each capture at
  capture time. Until a dedicated ``core.ingest`` ``raw/`` persister exists, this deterministic
  APPLY engine materializes free-text ``raw/<domain>/<event_id>.md`` sources from the immutable
  claimed-event body instead. The relocation is sound (APPLY is also deterministic engine code,
  never the sandboxed brain, and copies the body verbatim from the immutable event, so the baseline
  is faithful); a future ``core.ingest`` persister and this engine must agree on the exact path or
  the new final-diff exact-set gate would reject one of the two producers.
* :func:`validate_author_diff` — the §4.2 frontmatter-aware AUTHOR diff check: accept ONLY edits
  inside the candidate-id-keyed body-sentinel regions of ``needs_prose`` notes; reject any
  frontmatter change, any other file, any line outside a sentinel pair, sentinel tampering, a
  ``log.md`` byte change, an over-bound body, or a NEW ``[[wikilink]]`` beyond the plan's links.
* :func:`strip_stray_wikilinks` — the §4.6 byte-deterministic stray-link strip: any ``[[X]]`` whose
  key is not in the allowed set is replaced by its inner text (delimiters removed, meaning kept).

Determinism is the contract: ``apply_plan`` is a pure function of ``(plan, run_date, provenance)``
over the worktree at ``base_commit``, and the two validators are pure functions of their arguments —
so the same inputs always produce the same bytes / the same verdict, with ZERO model in the loop.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from agora_kb.core import frontmatter
from agora_kb.curator.plan import Disposition, Plan
from agora_kb.schema.notes import wikilinks

__all__ = [
    "apply_plan",
    "validate_author_diff",
    "strip_stray_wikilinks",
    "ApplyError",
    "body_sentinels",
    "region_sentinel_id",
    "DEFAULT_MAX_BODY_BYTES",
]

# --- sentinels (ADR-0011 §3 / §3.1) -----------------------------------------------------------

# The PERSISTED body sentinel id is RUN-SCOPED — ``{run_id}--{candidate_id}`` (see
# :func:`region_sentinel_id`) — NOT the bare candidate_id. The bare candidate_id ("c1","c2",…) is
# reassigned per RUN by :func:`agora_kb.curator.bundle._dedup_tier2`, so it is NOT unique across
# runs: a MERGE_INTO_THEME / cross-run APPEND_DAILY appends a NEW region to a note that may ALREADY
# hold a region with the same bare candidate_id from a PRIOR run, producing two identical
# ``id=c1`` markers in one note. ``run_id`` is globally unique per run (and regex-safe for the
# sentinel grammar), so prefixing with it makes every persisted region id globally unique while
# multiple APPEND_DAILY dispositions in ONE run still get distinct ids (their candidate_ids differ).
# PASS 2 writes ONLY between the start/end markers; APPLY places them empty (CREATE_THEME wraps the
# whole body; MERGE_INTO_THEME wraps a NEW augmentation sub-region appended below prior prose).
_SENTINEL_START = "<!-- agora:body:start id={cid} -->"
_SENTINEL_END = "<!-- agora:body:end id={cid} -->"

# The INITIAL-fill placeholder line the worker writes inside a fresh body region (PASS 2 replaces
# it). Kept on its own line so a clean sentinel region is never byte-empty. This is DISTINCT from
# the §4.2 AUTHOR-failure RESET placeholder, which ADR-0011 §4.2 pins as the blockquote ``>
# _summary pending_`` derived from the plan summary; the reset path lives in the worker, not here,
# so the two must not be conflated.
_BODY_PLACEHOLDER = "_summary pending_"

# §4.2 default per-region body byte bound (tunable via repo.yaml curator.limits, §1.3).
DEFAULT_MAX_BODY_BYTES = 8 * 1024

# The confidence APPLY mirrors when a candidate's worst-case value is not supplied. ``confidence``
# is the candidate's worst-case value (ADR-0011 §2 / DATA-MODEL §1) the WORKER passes in; ``high``
# is the conservative non-gated default for a candidate omitted from the per-candidate map.
_DEFAULT_CONFIDENCE = "high"

# The §2.1 contested callout first-line detector (mirrors lint's _CONTESTED_CALLOUT_RE) — used only
# to keep the rendered template consistent; APPLY renders the block, the lint/dashboard parse it.
_CONTESTED_CALLOUT_PREFIX = "> [!contested]"

# §4.6 stray-wikilink regex: a [[...]] token with no nested brackets / newlines, inner captured.
_WIKILINK_TOKEN_RE = re.compile(r"\[\[([^\[\]\r\n]*)\]\]")

# A sentinel start/end line matcher for the AUTHOR-diff parser (§4.2): captures the candidate id so
# the validator can pair start/end and confirm only declared needs_prose regions were touched.
_SENTINEL_START_RE = re.compile(r"\A<!-- agora:body:start id=(?P<cid>.+) -->\Z")
_SENTINEL_END_RE = re.compile(r"\A<!-- agora:body:end id=(?P<cid>.+) -->\Z")


class ApplyError(ValueError):
    """Raised by :func:`apply_plan` when a disposition cannot be materialized deterministically.

    This is a worker-side precondition failure (e.g. a CREATE_THEME missing the domain/basename the
    §4.1 PLAN gate is supposed to have guaranteed), NOT a model verdict — a valid plan never raises.
    """


def region_sentinel_id(run_id: str, candidate_id: str) -> str:
    """Return the globally-unique PERSISTED body-sentinel id for a region (ADR-0011 §3 / §3.1).

    The id is ``{run_id}--{candidate_id}``. ``candidate_id`` ("c1","c2",…) is reassigned per RUN by
    :func:`agora_kb.curator.bundle._dedup_tier2`, so it is NOT unique across runs; ``run_id`` is
    globally unique per run, so the composite is globally unique across runs while two regions
    placed in ONE run still differ (their candidate_ids differ). This is the SINGLE SOURCE OF TRUTH
    for the persisted id — APPLY (placement) and the worker's ``_needs_prose_map`` (the §4.2
    ``sentinels`` set) BOTH call it, so they can never drift. ``run_id`` is regex-safe for the
    sentinel grammar (no ``" -->"`` substring, no newline) so the composite parses unambiguously.
    """
    return f"{run_id}--{candidate_id}"


def body_sentinels(sentinel_id: str) -> tuple[str, str]:
    """Return the ``(start, end)`` body-sentinel marker lines for ``sentinel_id`` (§3 / §3.1).

    ``sentinel_id`` is the FINAL persisted region id — for placed regions the run-scoped
    :func:`region_sentinel_id` value, never the bare per-run candidate_id. This formatter is
    generic: it wraps whatever id string it is given, so it is reused by tests/validators too.
    """
    return (
        _SENTINEL_START.format(cid=sentinel_id),
        _SENTINEL_END.format(cid=sentinel_id),
    )


def _str_list(value: object) -> list[str]:
    """Return the string elements of a raw frontmatter value as a ``list[str]`` (else ``[]``).

    Frontmatter values are typed ``object`` (a parsed YAML mapping); a ``sources``/``related``/
    ``contested_by`` entry is a list of strings. This narrows a present-but-untyped value to
    ``list[str]`` so the set-union edits below stay typed; a missing/non-list value yields ``[]``,
    matching :func:`agora_kb.schema.lint._str_items`.
    """
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


# --- provenance -> sources union (ADR-0011 §2 / ADR-0010 D3, L1-7/L1-8) ------------------------


def _sources_union(
    domain: str | None,
    provenance: list[dict[str, object]],
    *,
    worktree: Path,
    raw_writes: dict[str, str],
) -> list[str]:
    """Return the ordered, de-duplicated ``sources:`` list, MATERIALIZING each cited ``raw/`` (§2).

    The WORKER — never the model — writes ``sources:`` so provenance can never be lost (§2, "the
    worker writes/extends this, NOT the model"). Each provenance tuple cites a ``raw/`` artifact:

    * a tuple WITH an explicit ``raw_ref`` (an uploaded file) cites that ref and is NOT (re)written
      here — ``core.ingest`` already persisted the upload at capture time (ADR-0010 D3);
    * a tuple WITHOUT a ``raw_ref`` (a free-text ``kb_remember`` capture) cites
      ``raw/<domain>/<event_id>.md`` (basename == inbox event id, ADR-0010 D3) AND the engine
      materializes that file in the worktree from the tuple's immutable ``body`` so the curator's
      commit contains ``raw/`` + ``wiki/`` consistently and lint L1-8 passes. The write is the
      DETERMINISTIC engine's job (never the sandboxed brain, which can never touch ``raw/``); the
      file is immutable — written once, NEVER overwritten if it already exists. A tuple with neither
      a ``raw_ref`` nor a ``body`` (e.g. a hand-authored unit-test provenance fixture) keeps citing
      the path but skips the file write, preserving today's behavior.

    Every engine-WRITTEN ``raw/`` ref (and its exact on-disk content) is recorded in ``raw_writes``
    so the worker's final-diff gate can admit ONLY these exact paths-with-content — a brain that
    overwrites or plants a ``raw/`` file during PASS 2 is then NOT in ``raw_writes`` (a new path) or
    has mismatched content (an overwrite), so it falls through to the off-allowlist rejection.

    Order is provenance order; duplicates collapse while preserving first-seen order so the rendered
    list is a deterministic function of the provenance input.
    """
    sources: list[str] = []
    seen: set[str] = set()
    for tup in provenance:
        raw_ref = tup.get("raw_ref")
        if isinstance(raw_ref, str) and raw_ref:
            ref = raw_ref
        else:
            event_id = tup.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                continue
            ref = f"raw/{domain}/{event_id}.md" if domain else f"raw/{event_id}.md"
            _materialize_raw_source(worktree, ref, tup.get("body"), raw_writes=raw_writes)
        if ref not in seen:
            seen.add(ref)
            sources.append(ref)
    return sources


def _materialize_raw_source(
    worktree: Path, ref: str, body: object, *, raw_writes: dict[str, str]
) -> None:
    """Write the cited ``raw/`` free-text capture into the worktree, immutably (ADR-0010 D3).

    The DETERMINISTIC engine (this APPLY pass, NOT the sandboxed brain) persists each free-text
    capture as an immutable ``raw/<domain>/<event_id>.md`` so the cited source EXISTS in the
    curator's commit and lint L1-8 ("sources path does not exist") passes. ``body`` is the immutable
    claimed-event body threaded through the provenance tuple by
    :func:`agora_kb.curator.bundle.build_bundle`. Skips the write when ``body`` is absent (a
    hand-authored provenance fixture has no body — keep today's cite-only behavior) or when the file
    already exists (immutable: written once, never overwritten — so a re-run / cross-domain merge
    citing the same ref never clobbers it).

    Records ``raw_writes[ref] = <exact bytes written>`` for every ref the engine OWNS in this run —
    whether it wrote the file now or found it already materialized (an immutable re-cite). This is
    the EXACT-PATH-AND-CONTENT allowlist the final-diff gate enforces against: a PASS-2 overwrite of
    one of these files (same path, different content) and any brain-planted ``raw/`` file (a path
    absent from ``raw_writes``) are both rejected, so the brain still never writes ``raw/``.
    """
    if not isinstance(body, str):
        return
    path = worktree / ref
    if path.exists():
        # Immutable: never overwrite. The engine still OWNS this ref this run (an immutable
        # re-cite), so record its current bytes — a later PASS-2 overwrite changes them, fails gate.
        raw_writes[ref] = path.read_text(encoding="utf-8")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    raw_writes[ref] = body


def _is_harvest_origin(provenance: list[dict[str, object]]) -> str | None:
    """Return the ``harvest:<agent>`` origin to stamp, or ``None`` (ADR-0011 §2 / ADR-0010 D4).

    A kept region whose provenance includes ANY ``source == harvest:<agent>`` is tagged ``origin:
    harvest:<agent>`` by the worker for loop-prevention (DATA-MODEL §7) — the model never writes
    this tag. The FIRST harvest source in provenance order wins (deterministic).
    """
    for tup in provenance:
        source = tup.get("source")
        if (
            isinstance(source, str)
            and source.startswith("harvest:")
            and len(source) > len("harvest:")
        ):
            return source
    return None


def _canonicalize_dates(fm: dict[str, object]) -> None:
    """Coerce ``date``-typed frontmatter values to canonical ``YYYY-MM-DD`` strings in place.

    A foreign/externally-authored note may carry an UNQUOTED ``created: 2026-06-13``, which
    ``yaml.safe_load`` returns as a :class:`datetime.date` and ``frontmatter.render`` re-emits
    unquoted — while the worker-written ``updated`` is a quoted string, leaving the SAME note with
    mixed YAML quoting on re-render. Coercing every ``date``/``datetime`` value to its ISO string
    makes the rendered YAML quoting uniform regardless of how the note was originally authored, so
    MERGE/MARK_CONTESTED output stays uniform without depending on the source note's quoting style.
    Notes CREATED by APPLY already pass string dates, so this only normalizes foreign notes.
    """
    for key, value in list(fm.items()):
        if isinstance(value, datetime.date):  # datetime.datetime is a subclass — both handled
            fm[key] = value.isoformat()


def _stamp_harvest_origin(fm: dict[str, object], provenance: list[dict[str, object]]) -> None:
    """Set ``fm['origin'] = harvest:<agent>`` on a re-rendered note whose provenance is harvested.

    Used by MERGE/MARK_CONTESTED, which fold a candidate into an EXISTING note (ADR-0011 §2 / §6 /
    DATA-MODEL §7 loop-prevention). Set-union semantics: only ADD a harvest origin when the note has
    no ``origin`` yet, so a pre-existing origin is never overwritten and a non-harvest provenance
    leaves ``origin`` untouched. The model never writes this tag.
    """
    origin = _is_harvest_origin(provenance)
    if origin is not None and not fm.get("origin"):
        fm["origin"] = origin


# --- theme frontmatter (ADR-0010 C2 / §2) -----------------------------------------------------


def _theme_frontmatter(
    disp: Disposition,
    *,
    run_date: str,
    sources: list[str],
    origin: str | None,
    confidence: str,
) -> dict[str, object]:
    """Build the FULL ADR-0010 C2 theme frontmatter from the plan + worker-computed facts.

    The model DECIDES ``title``/``summary``/``status``/``tags``/``aliases``/``links``; the WORKER
    MATERIALIZES ``created``/``updated`` (== ``run_date``, D1), ``sources`` (the provenance union,
    never the model), ``related`` (the plan ``links`` as ``"[[basename]]"`` tokens), ``origin``
    (harvest only), ``confidence`` (MIRRORED from the candidate's worst-case value — NEVER decided
    by the model, ADR-0011 §2, so the backend can never inflate it), and ``body_status: pending``
    when the note needs prose. Key order matches the ADR-0010 §2 documented shape so the rendered
    YAML is stable.
    """
    status = disp.status or "active"
    fm: dict[str, object] = {
        "title": disp.title or disp.basename or disp.candidate_id,
        "type": "theme",
        "aliases": list(disp.aliases),
        "tags": list(disp.tags),
        "created": run_date,
        "updated": run_date,
        "status": status,
        "summary": disp.summary or "",
        "sources": list(sources),
        "related": [f"[[{link}]]" for link in disp.links],
    }
    if origin is not None:
        fm["origin"] = origin
    fm["confidence"] = confidence
    if disp.needs_prose:
        fm["body_status"] = "pending"
    return fm


def _daily_frontmatter(
    disp: Disposition,
    *,
    run_id: str,
    run_date: str,
    sources: list[str],
) -> dict[str, object]:
    """Build the ADR-0010 C2 daily frontmatter (``date``/``run_id`` from the injected run, §3.1)."""
    fm: dict[str, object] = {
        "title": disp.title or f"Daily {run_date}",
        "type": "daily",
        "aliases": list(disp.aliases),
        "tags": list(disp.tags),
        "created": run_date,
        "updated": run_date,
        "status": disp.status or "active",
        "summary": disp.summary or "",
        "date": run_date,
        "run_id": run_id,
        "sources": list(sources),
    }
    if disp.needs_prose:
        fm["body_status"] = "pending"
    return fm


# --- region rendering -------------------------------------------------------------------------


def _empty_body_region(sentinel_id: str) -> str:
    """Render a fresh, sentinel-wrapped body region with a placeholder line (PASS 2 fills it).

    ``sentinel_id`` is the FINAL persisted region id (the run-scoped :func:`region_sentinel_id`
    value at every placement site), never the bare per-run candidate_id — so a MERGE / cross-run
    APPEND_DAILY into a note already holding a prior-run region never collides on the bare id.
    """
    start, end = body_sentinels(sentinel_id)
    return f"{start}\n{_BODY_PLACEHOLDER}\n{end}"


def _contested_callout(disp: Disposition, *, run_date: str, sources: list[str]) -> str:
    """Render the §2.1 ``> [!contested]`` callout block byte-for-byte.

    One block per contesting claim. ``competing-basename`` is the FIRST plan link (a DIFFERENT note
    whose claim disagrees, §2.1); ``sources`` lists the new claim's event refs. The summary supplies
    the verbatim competing claim text. The first line matches the lint/dashboard detector
    ``^> \\[!contested\\]`` exactly. The caller (:func:`_apply_contested`) guarantees ``links`` is
    non-empty, so the competing basename is never a self-referential fallback.
    """
    competing = disp.links[0]
    claim_text = disp.summary or ""
    sources_line = ", ".join(sources)
    return (
        f"{_CONTESTED_CALLOUT_PREFIX} Competing claim (recorded {run_date})\n"
        f"> {claim_text}\n"
        f"> — see [[{competing}]] · sources: {sources_line}"
    )


# --- MOC / index maintenance (ADR-0010 §2 children + §3.2 child-bullet grammar) ----------------


def _moc_basename(domain: str) -> str:
    return f"{domain}-moc"


# --- the deterministic APPLY (ADR-0011 §3) -----------------------------------------------------


def apply_plan(
    plan: Plan,
    *,
    worktree: Path,
    run_date: str,
    provenance: dict[str, list[dict[str, object]]],
    confidence: dict[str, str] | None = None,
) -> dict[str, str]:
    """Materialize a validated ``plan`` into the ``worktree`` deterministically (ADR-0011 §3).

    Returns ``{raw_ref: exact_content}`` for every engine-WRITTEN canonical ``raw/`` source this run
    materialized (ADR-0010 D3) — the EXACT path-and-content set the worker's final-diff gate admits
    into the curated diff. Any OTHER ``raw/`` change in the committed tree (a brain-planted file, or
    a PASS-2 overwrite of one of these — same path, different content) is therefore rejected, so the
    brain still never writes ``raw/`` and the immutable verification baseline cannot be forged.

    ``confidence`` maps ``candidate_id -> worst-case confidence`` (``high|medium|low``, ADR-0011
    §2 / DATA-MODEL §1). APPLY MIRRORS it onto the materialized note (it is NOT a plan field, so the
    backend can never inflate it); a candidate omitted from the map falls back to the conservative
    :data:`_DEFAULT_CONFIDENCE`. The WORKER computes this worst-case value across merged events —
    the model never supplies it.

    The WORKER performs ALL structural mutation so correctness is by construction, not by post-hoc
    rejection. For each disposition:

    * ``CREATE_THEME`` — create ``wiki/<domain>/themes/<basename>.md`` with the FULL ADR-0010 C2
      theme frontmatter (``title``, ``type: theme``, ``aliases``, ``tags``, ``created``/``updated``
      == ``run_date``, ``status`` from the plan, ``summary``, ``sources`` = the provenance union,
      ``related`` from ``links``, ``origin`` if harvested, ``confidence``) + an ``agora:body``
      sentinel pair keyed by ``candidate_id`` when ``needs_prose``; add ``[[basename]]`` to the
      domain MOC and root index.
    * ``APPEND_DAILY`` — create-or-append ``wiki/<domain>/daily/<domain>-<run_date>.md``, adding one
      dated ``## <run_date>`` section per disposition (in stable manifest-event order) wrapped in
      its own ``candidate_id``-keyed sentinel pair (§3.1).
    * ``MERGE_INTO_THEME`` — union this run's provenance into the target theme's ``sources:`` (never
      drop prior), bump ``updated``, insert ``links``, and append a NEW sentinel augmentation
      sub-region below existing prose (never rewrite prior prose).
    * ``MARK_CONTESTED`` — set ``status: contested`` + ``contested_by`` + ``contested_at`` ==
      ``run_date`` and render the ``> [!contested]`` callout (§2.1).
    * ``DROP`` / ``NOOP`` — no wiki edit.

    Reads NO wall clock (``run_date`` is injected). Writes under the §4.0 curated allowlist PLUS the
    immutable canonical ``raw/<domain>/<event_id>.md`` source for every cited free-text capture (the
    engine — never the model — persists ``raw/``, ADR-0010 D3; materialized from each provenance
    tuple's ``body`` by :func:`_sources_union`). ``plan`` is assumed §4.1-valid; a precondition
    violation raises :class:`ApplyError`.
    """
    run_id = plan.run_id
    conf_map = confidence or {}

    # The EXACT set of engine-written canonical raw/ sources (ref -> exact content) materialized
    # this run, accumulated across every disposition's _sources_union. Returned so the worker's
    # final-diff gate admits ONLY these exact paths-with-content (ADR-0010 D3); anything else under
    # raw/ in the committed tree is a brain write and is rejected off-allowlist.
    raw_writes: dict[str, str] = {}

    # Track domain -> theme basenames added this run, so MOC/index maintenance is a single pass at
    # the end (idempotent set-union with whatever themes already live in the worktree tree).
    created_themes_by_domain: dict[str, set[str]] = {}

    # Group APPEND_DAILY dispositions per (domain) daily file so multiple sections land in stable
    # manifest-event order (§3.1: sort by each disposition's first event_id).
    daily_dispositions: list[Disposition] = []

    for disp in plan.dispositions:
        prov = provenance.get(disp.candidate_id, [])
        conf = conf_map.get(disp.candidate_id, _DEFAULT_CONFIDENCE)
        if disp.op == "CREATE_THEME":
            _apply_create_theme(
                disp,
                worktree=worktree,
                run_id=run_id,
                run_date=run_date,
                provenance=prov,
                confidence=conf,
                raw_writes=raw_writes,
            )
            if disp.domain and disp.basename:
                created_themes_by_domain.setdefault(disp.domain, set()).add(disp.basename)
        elif disp.op == "APPEND_DAILY":
            daily_dispositions.append(disp)
        elif disp.op == "MERGE_INTO_THEME":
            _apply_merge(
                disp,
                worktree=worktree,
                run_id=run_id,
                run_date=run_date,
                provenance=prov,
                raw_writes=raw_writes,
            )
        elif disp.op == "MARK_CONTESTED":
            _apply_contested(
                disp, worktree=worktree, run_date=run_date, provenance=prov, raw_writes=raw_writes
            )
        elif disp.op in ("DROP", "NOOP"):
            continue
        else:  # pragma: no cover — §4.1 CLOSED-VOCAB makes this unreachable for a valid plan
            raise ApplyError(f"candidate {disp.candidate_id!r}: unknown op {disp.op!r}")

    # APPEND_DAILY in stable manifest-event order: sort by the first event_id of each disposition
    # (§3.1). Sections are appended into the per-domain daily file in that order.
    daily_dispositions.sort(key=lambda d: (d.event_ids[0] if d.event_ids else "", d.candidate_id))
    for disp in daily_dispositions:
        _apply_append_daily(
            disp,
            worktree=worktree,
            run_id=run_id,
            run_date=run_date,
            provenance=provenance.get(disp.candidate_id, []),
            raw_writes=raw_writes,
        )

    # MOC + root index maintenance for every domain that gained a theme this run.
    for domain, basenames in created_themes_by_domain.items():
        _update_moc(domain, basenames, worktree=worktree, run_date=run_date)
    if created_themes_by_domain:
        _update_index(set(created_themes_by_domain), worktree=worktree, run_date=run_date)

    return raw_writes


def _apply_create_theme(
    disp: Disposition,
    *,
    worktree: Path,
    run_id: str,
    run_date: str,
    provenance: list[dict[str, object]],
    confidence: str,
    raw_writes: dict[str, str],
) -> None:
    """Create ``wiki/<domain>/themes/<basename>.md`` with full C2 frontmatter + a body sentinel."""
    if not disp.domain or not disp.basename:
        raise ApplyError(
            f"candidate {disp.candidate_id!r}: CREATE_THEME requires domain + basename"
        )
    sources = _sources_union(disp.domain, provenance, worktree=worktree, raw_writes=raw_writes)
    origin = _is_harvest_origin(provenance)
    fm = _theme_frontmatter(
        disp, run_date=run_date, sources=sources, origin=origin, confidence=confidence
    )
    if disp.needs_prose:
        body = _empty_body_region(region_sentinel_id(run_id, disp.candidate_id))
    else:
        body = ""
    path = worktree / "wiki" / disp.domain / "themes" / f"{disp.basename}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def _apply_append_daily(
    disp: Disposition,
    *,
    worktree: Path,
    run_id: str,
    run_date: str,
    provenance: list[dict[str, object]],
    raw_writes: dict[str, str],
) -> None:
    """Create-or-append the per-domain daily, adding one dated section per disposition (§3.1)."""
    if not disp.domain:
        raise ApplyError(f"candidate {disp.candidate_id!r}: APPEND_DAILY requires a domain")
    basename = disp.basename or f"{disp.domain}-{run_date}"
    path = worktree / "wiki" / disp.domain / "daily" / f"{basename}.md"
    region = _empty_body_region(region_sentinel_id(run_id, disp.candidate_id))
    section = f"## {run_date}\n\n{region}"
    new_sources = _sources_union(disp.domain, provenance, worktree=worktree, raw_writes=raw_writes)

    if path.is_file():
        # Append a new dated section, unioning the day's sources into frontmatter (keep prior).
        existing = path.read_text(encoding="utf-8")
        fm, prior_body = frontmatter.parse(existing)
        _canonicalize_dates(fm)
        merged_sources = _str_list(fm.get("sources"))
        for s in new_sources:
            if s not in merged_sources:
                merged_sources.append(s)
        fm["sources"] = merged_sources
        fm["updated"] = run_date
        if disp.needs_prose:
            fm["body_status"] = "pending"
        body = f"{prior_body}\n\n{section}" if prior_body else section
        path.write_text(frontmatter.render(fm, body), encoding="utf-8")
    else:
        fm = _daily_frontmatter(disp, run_id=run_id, run_date=run_date, sources=new_sources)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frontmatter.render(fm, section), encoding="utf-8")


def _resolve_target_path(domain_target: str, worktree: Path, *, theme_only: bool = False) -> Path:
    """Return the live-tree path of an existing note basename, else raise.

    MERGE/MARK_CONTESTED edit an EXISTING note whose path is derived from the live tree at APPLY
    (not the plan, §4.1 / _implied_note_path). We search ``wiki/**`` for ``<basename>.md`` — the
    §4.1 BASENAME/PROVENANCE checks already guaranteed it exists and is unique.

    ``theme_only`` restricts the match to ``wiki/<domain>/themes/<basename>.md``: MERGE_INTO_THEME
    and MARK_CONTESTED are theme-scoped ops (§2 op table), so resolving to a daily/moc/index must be
    rejected here as a precondition violation rather than mutating a non-theme note (the §4.1
    BASENAME check only verifies existence, not type).
    """
    matches = sorted(p for p in (worktree / "wiki").rglob(f"{domain_target}.md") if p.is_file())
    if theme_only:
        matches = [p for p in matches if p.parent.name == "themes"]
    if not matches:
        raise ApplyError(
            f"target basename {domain_target!r} not found as a theme in the live worktree tree"
            if theme_only
            else f"target basename {domain_target!r} not found in the live worktree tree"
        )
    return matches[0]


def _apply_merge(
    disp: Disposition,
    *,
    worktree: Path,
    run_id: str,
    run_date: str,
    provenance: list[dict[str, object]],
    raw_writes: dict[str, str],
) -> None:
    """Union provenance into the target theme's ``sources:`` + append an augmentation sub-region."""
    if not disp.target_basename:
        raise ApplyError(f"candidate {disp.candidate_id!r}: MERGE_INTO_THEME requires target")
    path = _resolve_target_path(disp.target_basename, worktree, theme_only=True)
    fm, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    _canonicalize_dates(fm)

    # Derive the target's domain from its live path (wiki/<domain>/...) so the source refs cite the
    # right raw/ folder; the worker — never the model — writes the union (§2, idempotent set-union).
    rel_parts = path.relative_to(worktree).as_posix().split("/")
    target_domain = rel_parts[1] if len(rel_parts) >= 2 and rel_parts[0] == "wiki" else disp.domain
    new_sources = _sources_union(
        target_domain, provenance, worktree=worktree, raw_writes=raw_writes
    )
    merged = _str_list(fm.get("sources"))
    for s in new_sources:
        if s not in merged:
            merged.append(s)
    fm["sources"] = merged
    fm["updated"] = run_date

    # MERGE_INTO_THEME is the ONLY op a gated/harvested candidate may use to ADD content (§6), so it
    # is the primary loop-prevention site: a kept-via-merge harvested region tags the target
    # ``origin: harvest:<agent>`` (§2 / §6 / DATA-MODEL §7). Set-union: only ADD a harvest origin
    # when the note has none yet, never overwrite a pre-existing origin (the model never writes it).
    _stamp_harvest_origin(fm, provenance)

    # Insert plan links into related (set-union, materialized as "[[basename]]"), never dropping.
    if disp.links:
        related = _str_list(fm.get("related"))
        for link in disp.links:
            token = f"[[{link}]]"
            if token not in related:
                related.append(token)
        fm["related"] = related

    if disp.needs_prose:
        fm["body_status"] = "pending"
        augmentation = _empty_body_region(region_sentinel_id(run_id, disp.candidate_id))
        body = f"{body}\n\n{augmentation}" if body else augmentation
    path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def _apply_contested(
    disp: Disposition,
    *,
    worktree: Path,
    run_date: str,
    provenance: list[dict[str, object]],
    raw_writes: dict[str, str],
) -> None:
    """Set the §2.1 contested frontmatter + render the ``> [!contested]`` callout on the target."""
    if not disp.target_basename:
        raise ApplyError(f"candidate {disp.candidate_id!r}: MARK_CONTESTED requires target")
    # The §2.1 contested shape requires ≥1 competing basename: ``contested_by`` must be non-empty
    # (ADR-0010 L1-10) and the callout cites a DIFFERENT note. A §4.1-valid MARK_CONTESTED carries
    # it in ``links``; an empty ``links`` is a precondition violation (it would produce a self-link
    # and an empty ``contested_by``), so surface it at APPLY rather than fabricating a self-link.
    if not disp.links:
        raise ApplyError(
            f"candidate {disp.candidate_id!r}: MARK_CONTESTED requires at least one competing "
            f"basename in links (the contested shape needs a non-empty contested_by)"
        )
    path = _resolve_target_path(disp.target_basename, worktree, theme_only=True)
    fm, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    _canonicalize_dates(fm)

    rel_parts = path.relative_to(worktree).as_posix().split("/")
    target_domain = rel_parts[1] if len(rel_parts) >= 2 and rel_parts[0] == "wiki" else disp.domain
    new_sources = _sources_union(
        target_domain, provenance, worktree=worktree, raw_writes=raw_writes
    )

    # Union the new claim's provenance into sources (keep BOTH; contested needs >=2 sources, §2.1).
    merged = _str_list(fm.get("sources"))
    for s in new_sources:
        if s not in merged:
            merged.append(s)
    fm["sources"] = merged

    # status: contested + contested_by (set-union, never replaced) + contested_at == run_date.
    fm["status"] = "contested"
    # contested_by is the set-union of any prior value with this run's competing basenames (the
    # plan links); never replaced (§2.1). An empty list would later fail lint L1-10 (the full
    # contested-shape conjunction) rather than be silently accepted here.
    contested_by = _str_list(fm.get("contested_by"))
    for link in disp.links:
        if link not in contested_by:
            contested_by.append(link)
    fm["contested_by"] = contested_by
    fm["contested_at"] = run_date
    fm["updated"] = run_date

    # A harvested contradicting claim folded into the target tags it origin: harvest:<agent> for
    # loop-prevention (same rule as MERGE; §2 / §6 / DATA-MODEL §7). Set-union; never overwrite.
    _stamp_harvest_origin(fm, provenance)

    callout = _contested_callout(disp, run_date=run_date, sources=new_sources)
    body = f"{body}\n\n{callout}" if body else callout
    path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def _update_moc(
    domain: str,
    new_basenames: set[str],
    *,
    worktree: Path,
    run_date: str,
) -> None:
    """Create-or-update ``wiki/<domain>/<domain>-moc.md`` so children == the theme-basename set.

    The children set is the UNION of the themes already living in ``wiki/<domain>/themes/`` and the
    ones created this run. ``children:`` frontmatter is kept exactly equal to the body child-bullet
    set (L1-6) and both are sorted, so the MOC is a deterministic function of its child set.
    """
    themes_dir = worktree / "wiki" / domain / "themes"
    existing = (
        {p.stem for p in themes_dir.glob("*.md") if p.is_file()} if themes_dir.is_dir() else set()
    )
    children = sorted(existing | new_basenames)

    moc_path = worktree / "wiki" / domain / f"{_moc_basename(domain)}.md"
    if moc_path.is_file():
        fm, _ = frontmatter.parse(moc_path.read_text(encoding="utf-8"))
        _canonicalize_dates(fm)
        fm["children"] = [f"[[{b}]]" for b in children]
        fm["updated"] = run_date
        body = "\n".join(f"- [[{b}]]" for b in children)
        moc_path.write_text(frontmatter.render(fm, body), encoding="utf-8")
    else:
        fm = {
            "title": f"{domain} MOC",
            "type": "moc",
            "aliases": [],
            "tags": [],
            "created": run_date,
            "updated": run_date,
            "status": "active",
            "summary": f"Map of content for the {domain} domain.",
            "children": [f"[[{b}]]" for b in children],
        }
        body = "\n".join(f"- [[{b}]]" for b in children)
        moc_path.parent.mkdir(parents=True, exist_ok=True)
        moc_path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def _update_index(
    domains: set[str],
    *,
    worktree: Path,
    run_date: str,
) -> None:
    """Create-or-update root ``index.md`` so its children list every domain MOC (ADR-0010 §1).

    The index's children are the domain MOC basenames (``<domain>-moc``) — the UNION of every MOC
    already present in the worktree and the ones touched this run. ``children:`` == the body
    child-bullet set (L1-6); both sorted, deterministic.
    """
    wiki = worktree / "wiki"
    existing_mocs = (
        {p.stem for p in wiki.rglob("*-moc.md") if p.is_file()} if wiki.is_dir() else set()
    )
    moc_children = sorted(existing_mocs | {_moc_basename(d) for d in domains})

    index_path = worktree / "index.md"
    if index_path.is_file():
        fm, _ = frontmatter.parse(index_path.read_text(encoding="utf-8"))
        _canonicalize_dates(fm)
        fm["children"] = [f"[[{b}]]" for b in moc_children]
        fm["updated"] = run_date
        body = "\n".join(f"- [[{b}]]" for b in moc_children)
        index_path.write_text(frontmatter.render(fm, body), encoding="utf-8")
    else:
        fm = {
            "title": "Knowledge base index",
            "type": "index",
            "aliases": [],
            "tags": [],
            "created": run_date,
            "updated": run_date,
            "status": "active",
            "summary": "Top map of content; links every domain MOC.",
            "children": [f"[[{b}]]" for b in moc_children],
        }
        body = "\n".join(f"- [[{b}]]" for b in moc_children)
        index_path.write_text(frontmatter.render(fm, body), encoding="utf-8")


# --- §4.6 stray-wikilink stripping -------------------------------------------------------------


def strip_stray_wikilinks(text: str, allowed: set[str]) -> str:
    """Strip every ``[[X]]`` whose key is not in ``allowed``, keeping the inner text (§4.6).

    Byte-deterministic: each ``[[X]]`` token (no nested brackets / newlines) whose RESOLVED key —
    the substring left of any ``|``, ASCII-stripped, matching
    :func:`agora_kb.schema.notes.wikilinks` normalization — is absent from ``allowed`` is replaced
    by its inner text (delimiters removed, meaning preserved), so PASS 2 can never introduce a
    dangling link (links are structure, owned by APPLY). A token whose key IS allowed is kept.

    The substitution is iterated to a FIXED POINT because nested/doubled delimiters can SYNTHESIZE a
    surviving link from a single pass: stripping the inner token of ``[[[[victim]]]]`` would leave
    ``[[victim]]`` — a brand-new stray link a single non-recursive ``re.sub`` never re-scans.
    Looping until the text stops changing guarantees no stray ``[[X]]`` survives. As a final
    invariant we then assert (via :func:`agora_kb.schema.notes.wikilinks`, the frozen grammar) that
    every surviving link key is in ``allowed``; this is the §4.6 "no dangling link is created"
    guarantee, and it uses the SAME matcher as the §4.2 detector so the two provably agree.

    The token grammar here intentionally matches :func:`agora_kb.schema.notes.wikilinks` (no nested
    brackets / newlines) rather than the looser ``\\[\\[([^\\]]*)\\]\\]`` literal once written in
    ADR-0011 §4.6, so stripping and detection share one definition (a divergence would let a stray
    link past one but not the other).
    """
    prev = None
    out = text
    while prev != out:
        prev = out
        out = _WIKILINK_TOKEN_RE.sub(lambda m: _strip_one(m, allowed), out)
    # Invariant: after reaching the fixed point, no surviving link key is outside ``allowed`` (the
    # §4.6 guarantee). A stray that somehow survives is re-stripped rather than smuggled through.
    while set(wikilinks(out)) - allowed:
        prev = out
        out = _WIKILINK_TOKEN_RE.sub(lambda m: _strip_one(m, allowed), out)
        if out == prev:  # pragma: no cover — the fixed-point loop already removes every stray token
            break
    return out


def _strip_one(m: re.Match[str], allowed: set[str]) -> str:
    """Replace ONE ``[[X]]`` token: keep it if its key is allowed, else drop the delimiters.

    The resolved key matches :func:`agora_kb.schema.notes.wikilinks` normalization (the substring
    left of any ``|``, ASCII-stripped) so the strip grammar and the §4.2 detector share one rule.
    """
    inner = m.group(1)
    key = inner.split("|", 1)[0].strip(" \t\r\n\f\v")
    if key in allowed:
        return m.group(0)  # a planned link — keep delimiters intact
    return inner  # stray — drop the [[ ]] delimiters, keep the inner text verbatim


# --- §4.2 AUTHOR-diff validation ---------------------------------------------------------------


def _extract_sentinel_regions(text: str) -> dict[str, str] | None:
    """Return ``{candidate_id: region_body}`` for the matched sentinel pairs in ``text``, or None.

    The region body is the text BETWEEN a ``start``/``end`` pair (exclusive of the marker lines).
    Returns ``None`` on any sentinel tampering: an unmatched start, an unmatched end, a duplicated
    id, or a nested/overlapping pair — so :func:`validate_author_diff` rejects the note. Lines are
    matched against the exact ``agora:body:start/end id=<cid>`` grammar; a malformed marker line is
    treated as ordinary content (and will surface as an out-of-sentinel edit if it differs).
    """
    regions: dict[str, str] = {}
    open_cid: str | None = None
    open_lines: list[str] = []
    for line in text.split("\n"):
        start = _SENTINEL_START_RE.match(line)
        end = _SENTINEL_END_RE.match(line)
        if start is not None:
            if open_cid is not None:
                return None  # nested/overlapping start before the prior end — tampering
            open_cid = start.group("cid")
            open_lines = []
            continue
        if end is not None:
            cid = end.group("cid")
            if open_cid is None or cid != open_cid:
                return None  # end with no matching open, or mismatched id — tampering
            if cid in regions:
                return None  # duplicated candidate-id region — tampering
            regions[cid] = "\n".join(open_lines)
            open_cid = None
            open_lines = []
            continue
        if open_cid is not None:
            open_lines.append(line)
    if open_cid is not None:
        return None  # an unmatched start — tampering
    return regions


def _split_frontmatter_and_body(text: str) -> tuple[str, str] | None:
    """Split a note into ``(frontmatter_block_including_fences, body)``, or None if no frontmatter.

    The frontmatter block is everything up to and including the closing ``---`` fence (so a §4.2
    frontmatter-change check is a byte comparison of that exact slice); the body is the remainder.
    Mirrors :func:`agora_kb.core.frontmatter.parse`'s fence rules without coercing the YAML, so the
    comparison is purely textual.
    """
    nl = text.find("\n")
    first = text if nl == -1 else text[:nl]
    if first.strip() != "---":
        return None
    rest = text[nl + 1 :] if nl != -1 else ""
    closing = re.search(r"^---[ \t]*$", rest, re.MULTILINE)
    if closing is None:
        return None
    fm_block = text[: nl + 1 + closing.end()]
    body = rest[closing.end() :]
    return fm_block, body


def validate_author_diff(
    *,
    changed_paths: list[str],
    per_file_old: dict[str, str],
    per_file_new: dict[str, str],
    sentinels: dict[str, set[str]],
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> list[str]:
    """Validate the PASS-2 AUTHOR diff (ADR-0011 §4.2); return failure messages (``[]`` iff clean).

    Accept ONLY edits inside the candidate-id-keyed body-sentinel regions of ``needs_prose`` notes
    and reject everything else. Pure + deterministic.

    Parameters
    ----------
    changed_paths:
        POSIX repo-relative paths the PASS-2 diff touched (git status ``A``/``M``/``D``). Any path
        not in ``sentinels`` is rejected (the model may only edit declared needs_prose notes).
    per_file_old / per_file_new:
        Pre-/post-PASS-2 full text of each changed file (``base``-state vs worktree-state).
    sentinels:
        ``{rel_path: {candidate_id, ...}}`` — the COMPLETE set of candidate-id body-sentinel regions
        currently present in each needs_prose note at base-state, NOT just this run's new regions.
        The validator enforces EXACT set equality (``set(regions) == sentinels[rel_path]``), so a
        multi-region note (e.g. a CREATE_THEME body from a prior run plus a MERGE augmentation
        appended this run) must list ALL live candidate ids here. Prior-run regions are expected to
        retain their sentinels (ADR-0011 §3) so re-authoring cannot drift them; a note may only
        change WITHIN these regions, and only these candidate ids may exist.
    max_body_bytes:
        Per-region UTF-8 byte bound (default :data:`DEFAULT_MAX_BODY_BYTES`).

    Checks (failures reported in ``changed_paths`` order, then a stable per-file order):

    1. only declared needs_prose notes changed; NO other file (incl. ``log.md`` — byte-identical to
       base, asserted explicitly);
    2. NO frontmatter change (byte-identical frontmatter block);
    3. ONLY sentinel-region bodies changed — the body OUTSIDE every sentinel region is
       byte-identical to base, no sentinel tampering, and only the DECLARED candidate-id regions
       exist;
    4. each region body within ``max_body_bytes``; valid UTF-8;
    5. NO new ``[[wikilink]]`` introduced beyond what the base region already contained (links are
       structure, owned by APPLY; stray links are stripped by :func:`strip_stray_wikilinks`).
    """
    errors: list[str] = []

    # log.md must be byte-identical to base throughout PASS 2 (§4.2 check 2 / §4.3 ordering).
    if "log.md" in changed_paths:
        old = per_file_old.get("log.md", "")
        new = per_file_new.get("log.md", "")
        if old != new:
            errors.append("log.md changed during PASS 2 (must be byte-identical to base_commit)")

    for path in changed_paths:
        if path == "log.md":
            continue  # handled above
        if path not in sentinels:
            errors.append(
                f"{path}: file changed during PASS 2 but is not a declared needs_prose note "
                f"(only sentinel body regions may change)"
            )
            continue

        old_text = per_file_old.get(path, "")
        new_text = per_file_new.get(path, "")

        # check 2 — frontmatter byte-identical.
        old_split = _split_frontmatter_and_body(old_text)
        new_split = _split_frontmatter_and_body(new_text)
        if old_split is None or new_split is None:
            errors.append(f"{path}: missing/malformed frontmatter block in the PASS-2 diff")
            continue
        old_fm, old_body = old_split
        new_fm, new_body = new_split
        if old_fm != new_fm:
            errors.append(
                f"{path}: frontmatter changed during PASS 2 (frontmatter is owned by APPLY)"
            )

        # check 3 — sentinel structure intact + only declared regions; out-of-region body unchanged.
        expected_cids = sentinels[path]
        old_regions = _extract_sentinel_regions(old_body)
        new_regions = _extract_sentinel_regions(new_body)
        if old_regions is None or new_regions is None:
            errors.append(f"{path}: sentinel tampering (unmatched/duplicated agora:body markers)")
            continue
        if set(new_regions) != set(old_regions) or set(new_regions) != expected_cids:
            errors.append(
                f"{path}: sentinel region set {sorted(new_regions)} != "
                f"expected {sorted(expected_cids)}"
            )
            continue

        # The body OUTSIDE every sentinel region must be byte-identical (replace each region body
        # with a fixed token so only out-of-region text is compared).
        if _strip_region_bodies(old_body) != _strip_region_bodies(new_body):
            errors.append(
                f"{path}: content outside the sentinel body regions changed during PASS 2"
            )

        # checks 4 + 5 — per region: byte bound, UTF-8, no NEW wikilinks beyond the base region.
        for cid in sorted(expected_cids):
            new_region = new_regions[cid]
            old_region = old_regions[cid]
            if len(new_region.encode("utf-8")) > max_body_bytes:
                errors.append(f"{path}: body region id={cid} exceeds {max_body_bytes} bytes")
            try:
                new_region.encode("utf-8").decode("utf-8")
            except UnicodeDecodeError:  # pragma: no cover — a Python str is always valid UTF-8
                errors.append(f"{path}: body region id={cid} is not valid UTF-8")
            old_links = set(wikilinks(old_region))
            new_links = set(wikilinks(new_region))
            stray = sorted(new_links - old_links)
            if stray:
                errors.append(
                    f"{path}: body region id={cid} introduced new wikilink(s) {stray} "
                    f"(links are owned by APPLY; strip stray links via strip_stray_wikilinks)"
                )

    return errors


# A token that cannot appear in a sentinel region body, used to blank out region bodies so the
# out-of-region byte comparison in check 3 ignores intentional in-region prose edits.
_REGION_BLANK = "\x00AGORA_REGION\x00"


def _strip_region_bodies(body: str) -> str:
    """Replace each sentinel region BODY with a fixed token; keep markers + out-of-region text.

    So two notes with identical structure but different in-region prose compare EQUAL outside the
    regions — the §4.2 check that "content outside the sentinel body regions is unchanged" reduces
    to a byte comparison of this normalized form.
    """
    out: list[str] = []
    inside = False
    for line in body.split("\n"):
        if _SENTINEL_START_RE.match(line):
            out.append(line)
            out.append(_REGION_BLANK)
            inside = True
            continue
        if _SENTINEL_END_RE.match(line):
            inside = False
            out.append(line)
            continue
        if not inside:
            out.append(line)
    return "\n".join(out)
