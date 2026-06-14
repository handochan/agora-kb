"""The capstone transactional curator run-loop + crash recovery (ADR-0008, ADR-0011, DESIGN §4).

This is the deterministic orchestrator that ties Stages A/B/the plan/apply gates into ONE
transaction. It owns everything around the two delegated cognitive acts (ADR-0011 §7); the backend
brain only decides (PASS 1 ``plan.json``) and authors prose (PASS 2). Success is a pure function of
``(plan.json, git_diff, manifest, bundle, lint)`` — the model is OUTSIDE the integrity boundary,
so this module is unit-testable with a :class:`FakeBackend` and ZERO real model in the loop.

The run transaction (:func:`run`) implements ADR-0008 steps 1-6 / DESIGN §4 / ADR-0011 §0:

1. acquire the per-repo :func:`~agora_kb.curator.claim.curator_lock` (held ⇒ ``noop``);
2. snapshot the inbox into ``processing/<run-id>/`` via the atomic FIFO
   :func:`~agora_kb.curator.claim.claim` (empty/all-deduped ⇒ ``noop``) and write the manifest
   (``phase=claimed``);
3. build the read-only :func:`~agora_kb.curator.bundle.build_bundle` (tier-2 dedup + ``related/``);
4. open a detached worktree at ``base_commit``; PASS 1 — :meth:`Backend.plan` → ``plan.json``
   → :func:`~agora_kb.curator.plan.validate_plan` (the §4.1 gate). On any PLAN/LINT/CAS failure
   the whole diff is discarded and events are returned to ``inbox/`` (retry the §5.1 budget) or
   moved to ``failed/`` with an error record — NOTHING partial is ever published;
5. APPLY the plan structurally (:func:`~agora_kb.curator.apply.apply_plan`), set ``phase=applied``;
   PASS 2 — :meth:`Backend.author` fills the candidate-id body sentinels, the §4.6 stray-wikilink
   strip repairs any stray ``[[X]]``, then :func:`~agora_kb.curator.apply.validate_author_diff`
   (degrade-or-fail per §4.2) and the deterministic :func:`~agora_kb.schema.lint.lint` (§4.4, must
   be ``ok``);
6. append ONE structured ``log.md`` entry (worker-only, AFTER validation, §4.3); assert the final
   ``git diff base_commit..HEAD`` touches ONLY canonical-ALLOWLIST paths with no introduced symlink/
   escape and ZERO ``_agora_scratch/`` tracked changes (§4.0/§4.3/§4.5 — the deterministic integrity
   boundary, ADR-0008 step 4); commit once; persist the candidate commit sha at ``phase=applied``
   BEFORE the CAS (so a crash in the CAS-success window recovers via the §9 git-ref row, never
   double-publishes); compare-and-swap the curated ref ``base_commit → new_commit`` (the durable
   publish point). Move events to ``processed/<date>/`` then record ``published_runs`` + counters +
   ``event_keys`` into ``state.json`` (processed-first so the keys are read from their final home),
   advance the manifest ``claimed → applied → published → finalized``.

Recovery (:func:`recover`) is the ADR-0011 §9 truth-table over
:func:`~agora_kb.curator.manifest.list_processing`: a ``published`` run (or one whose git ref
already points at its commit) is FINALIZED without any backend call (ADR-0008 step 6); a
``claimed`` or un-``prose_complete`` ``applied`` run returns its unchanged events to the inbox to
re-run from PASS 1 (the conservative default; the dropped worktree took ``plan.json`` with it); a
``finalized`` run is cleaned up.

Sandbox note (ADR-0013): the OS sandbox is NOT wired here. ``run`` invokes an INJECTED
:class:`Backend` abstraction; a sandbox wrapper around the *real* backend is a later layer. The
DETERMINISTIC validation chain (``validate_plan`` + ``validate_author_diff`` + ``lint`` + the
allowlisted final-diff assertion) is the integrity gate REGARDLESS of any sandbox (ADR-0008/0011
§7), which is exactly why the contract is gradable with a fake backend.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from ..core import frontmatter
from ..core.ids import new_event_id
from ..core.layout import RepoLayout
from ..core.repo import GitError, Repo
from ..core.state import CuratorState, StateStore
from ..schema.emit import Taxonomy
from ..schema.lint import lint
from ..schema.notes import parse_all_notes
from .apply import (
    apply_plan,
    body_sentinels,
    region_sentinel_id,
    strip_stray_wikilinks,
    validate_author_diff,
)
from .bundle import build_bundle
from .claim import LockHeld, claim, curator_lock
from .constants import (
    DEFAULT_MAX_ATTEMPTS,
    SCHEMA_SYMLINKS,
    SCRATCH_DIRNAME,
    is_allowlisted_path,
)
from .manifest import RunManifest, list_processing, write_manifest
from .plan import Disposition, Plan, PlanParseError, validate_plan
from .subprocess_backend import BackendUnavailableError

__all__ = [
    "Backend",
    "FakeBackend",
    "RunReport",
    "run",
    "recover",
]

# §4.2 AUTHOR-failure RESET placeholder (ADR-0011 §4.2): the blockquote derived from the plan
# summary. DISTINCT from APPLY's initial ``_summary pending_`` fill — this is the degrade-on-
# failure body the worker substitutes when a note's PASS-2 diff is rejected, so the run still
# publishes a structurally-valid (but prose-pending) note.
_RESET_PLACEHOLDER = "> _summary pending_"

_logger = logging.getLogger(__name__)


class Backend(Protocol):
    """The swappable INGEST brain — exactly the two delegated cognitive acts (ADR-0011 §7).

    The worker passes the backend ONLY read-only bundle + writable-worktree paths (argv/stdin in
    the real adapter, ADR-0008 §3); the backend is OUTSIDE the integrity boundary, so every output
    is graded deterministically by the worker (§4). A test :class:`FakeBackend` satisfies this
    Protocol with canned outputs and no model.
    """

    def plan(self, bundle_dir: Path) -> str:
        """PASS 1 — read the read-only ``bundle/``; return the ``plan.json`` text (§4.1 input).

        The backend reads ``bundle/candidates.json`` + ``bundle/related/*`` + ``bundle/schema.md`` +
        ``bundle/taxonomy.yaml`` and returns the closed-vocabulary plan as a JSON string. It writes
        no wiki files; the worker validates the returned text with :func:`Plan.from_json` +
        :func:`validate_plan`.
        """
        ...

    def author(self, worktree: Path, needs_prose: dict[str, list[str]]) -> None:
        """PASS 2 — fill the candidate-id body sentinels in ``worktree`` (§3 / §4.2).

        ``needs_prose`` maps each note's repo-relative path to the candidate ids whose
        ``agora:body:start id=<cid> … end`` sentinel regions the backend must author. The backend
        writes ONLY between those markers; the worker validates the diff with
        :func:`validate_author_diff` and strips/degrades anything out of bounds.
        """
        ...


class FakeBackend:
    """A model-free :class:`Backend` for tests: a canned ``plan.json`` + a ``{cid: prose}`` map.

    :meth:`plan` returns the canned plan text verbatim. :meth:`author` writes each candidate's prose
    BETWEEN that candidate's body sentinels in every note that declares it (so it honors the §4.2
    "only sentinel regions change" contract), leaving frontmatter and out-of-region text untouched.
    A candidate id missing from ``prose`` is left at its APPLY placeholder.
    """

    def __init__(self, plan_text: str, prose: dict[str, str] | None = None) -> None:
        self._plan_text = plan_text
        self._prose = dict(prose or {})

    def plan(self, bundle_dir: Path) -> str:  # noqa: ARG002 — bundle is unused by the fake
        return self._plan_text

    def author(self, worktree: Path, needs_prose: dict[str, list[str]]) -> None:
        for rel_path, cids in needs_prose.items():
            path = worktree / rel_path
            text = path.read_text(encoding="utf-8")
            for cid in cids:
                prose = self._prose.get(cid)
                if prose is None:
                    continue
                text = _replace_sentinel_region(text, cid, prose)
            path.write_text(text, encoding="utf-8")


def _replace_sentinel_region(text: str, candidate_id: str, prose: str) -> str:
    """Return ``text`` with the ``candidate_id`` body-sentinel region body replaced by ``prose``.

    Pure string surgery between the exact ``agora:body:start/end id=<cid>`` marker lines (so markers
    + frontmatter + out-of-region text are byte-preserved). Used only by :class:`FakeBackend`; the
    real backend writes the region itself inside the sandbox.
    """
    start, end = body_sentinels(candidate_id)
    si = text.find(start)
    ei = text.find(end)
    if si == -1 or ei == -1 or ei < si:
        return text
    region_start = si + len(start)
    return f"{text[:region_start]}\n{prose}\n{text[ei:]}"


@dataclass(frozen=True)
class RunReport:
    """The outcome of one :func:`run` (or a single :func:`recover` step).

    ``status`` is the terminal verdict: ``published`` (the CAS landed), ``failed`` (a PLAN/LINT/CAS
    rejection — nothing published, events moved to ``failed/`` or back to ``inbox/``), ``noop``
    (lock held or nothing to claim), or ``recovered`` (a crashed run finalized/returned on start).
    ``published_commit`` is the new curated sha iff ``published``. ``counts`` carries per-op
    disposition tallies (and recovery actions) for the dashboard / ``log.md``.
    """

    run_id: str
    status: Literal["published", "failed", "noop", "recovered"]
    published_commit: str | None = None
    counts: dict[str, int] = field(default_factory=dict)


def run(
    repo: Repo,
    *,
    backend: Backend,
    state_store: StateStore,
    now: datetime,
    taxonomy: Taxonomy,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> RunReport:
    """Execute ONE transactional curator run (ADR-0008 steps 1-6 / DESIGN §4 / ADR-0011 §0).

    Deterministic orchestration around the two delegated acts. ``now`` is the injected commit
    timestamp (the run reads no wall clock for the commit/dates — ADR-0010 D1); ``taxonomy``
    supplies
    the FIXED ``allowed_tags``/``domains`` the §4.1 / §4.4 gates enforce. ``max_attempts`` is the
    §5.1 per-event retry budget (``repo.yaml curator.max_attempts``, default
    :data:`~agora_kb.curator.constants.DEFAULT_MAX_ATTEMPTS`); keyword-only + additive so the frozen
    signature is preserved while an operator-configured budget is honored end-to-end.

    Returns a :class:`RunReport`. A held lock or empty/all-deduped inbox is a ``noop``; a PLAN,
    LINT,
    or CAS failure is a ``failed`` run that publishes NOTHING and discards the entire worktree diff
    (events return to ``inbox/`` within the §5.1 retry budget, else move to ``failed/``); a clean
    run
    is ``published`` with the new curated commit.
    """
    layout = repo.layout

    try:
        with curator_lock(layout):
            return _run_locked(
                repo,
                backend=backend,
                state_store=state_store,
                now=now,
                taxonomy=taxonomy,
                max_attempts=max_attempts,
            )
    except LockHeld:
        # A run is already in progress for this repo (ADR-0008 step 1 / DESIGN §4 step 1): exit
        # rather than block, preserving the single-writer invariant (ADR-0002).
        return RunReport(run_id="", status="noop", counts={"reason_lock_held": 1})


def _run_locked(
    repo: Repo,
    *,
    backend: Backend,
    state_store: StateStore,
    now: datetime,
    taxonomy: Taxonomy,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> RunReport:
    """The body of :func:`run`, executed UNDER the held curator lock (ADR-0011 §0)."""
    layout = repo.layout
    state = state_store.load()

    # ADR-0008 (read-after-publish, best-effort): a PRIOR run's post-publish sync may have failed
    # (owner working tree dirty/diverged), leaving HEAD behind the curated ref. Try a best-effort
    # fast-forward here so a transient dirty tree that has since been cleaned is reconciled before
    # we read the base. GUARDED by the same GitError swallow as the post-publish sync — a
    # still-dirty tree never aborts the run; we read the AUTHORITATIVE base from the ref below.
    _sync_owner_working_copy(repo, run_id="<pre-run>")

    # The CAS base is the curated branch ref (the authoritative published tip), NOT the owner's
    # working-copy HEAD: a stale HEAD left behind by a failed post-publish sync would otherwise
    # poison this run (plan against missing content + a CAS expected=stale that can never land —
    # a durable livelock). Reading the ref means a prior failed sync degrades the read path only,
    # never the next run's ability to make progress (ADR-0008 note: the read-after-publish sync is
    # best-effort and the CAS base is the curated ref, not the owner HEAD).
    base_commit = repo.branch_commit()
    run_id = _new_run_id(now)
    run_date = run_id[:10]

    # ADR-0008 step 1 / §0: atomic FIFO claim + tier-1 dedup (authoritative under the lock). A None
    # result ⇒ the inbox was empty or every event collapsed away — nothing to consolidate.
    manifest = claim(layout, base_commit=base_commit, run_id=run_id, started=_iso(now), state=state)
    if manifest is None:
        return RunReport(run_id=run_id, status="noop", counts={"claimed": 0})

    # ADR-0011 §1 / §5 tier-2: build the read-only bundle (candidates + provenance + related/).
    bundle = build_bundle(layout, repo, manifest)

    # ADR-0008 step 2: a detached worktree at base_commit is the ONLY writable mount; the model
    # never
    # touches the live tree or _kb/. live_basenames (the AUTHORITATIVE §1.2 registry) is read from
    # the committed worktree, so the §4.1 BASENAME/LINK checks grade against the real tree.
    with repo.worktree(at=base_commit) as wt:
        wt_layout = RepoLayout(wt)
        live_basenames = {n.basename for n in parse_all_notes(wt_layout)}

        # ADR-0011 §0 / §4.3: append `_agora_scratch/` to the WORKTREE's own .gitignore BEFORE
        # invoking the backend, so the backend's PASS-1 plan.json + any model scratch are writable
        # (the worktree is the only writable mount, ADR-0008) yet can NEVER appear in the curated
        # diff (`git add -A` skips it). The final-diff gate (below) additionally asserts it produced
        # zero tracked changes.
        _ignore_scratch(wt)

        # PASS 1 — DELEGATED: the backend reads the bundle and returns plan.json text. A bad-plan
        # TEXT is a PlanParseError; a backend that cannot run at all (missing/non-zero executable →
        # BackendUnavailableError, or a hung backend → subprocess.TimeoutExpired) is the OTHER
        # failure channel of the real SubprocessBackend seam. BOTH map to a deterministic FAILED run
        # here (lock released, worktree torn down, NOTHING published) so the "model outside the
        # integrity boundary → clean FAILED run" contract holds end-to-end — never an uncaught
        # traceback out of run() (ADR-0011 §4 / FOCUS/INGEST contract).
        try:
            plan = Plan.from_json(backend.plan(bundle.bundle_dir))
        except PlanParseError as exc:
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=[f"PLAN-PARSE: {exc}"],
                max_attempts=max_attempts,
            )
        except (BackendUnavailableError, subprocess.TimeoutExpired) as exc:
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=[f"PLAN-BACKEND: {exc}"],
                max_attempts=max_attempts,
            )

        # §4.1 PLAN validation (pure deterministic, model-independent).
        plan_errors = validate_plan(
            plan,
            manifest_event_ids=set(manifest.event_ids),
            allowed_tags=set(taxonomy.allowed_tags),
            domains=set(taxonomy.domains),
            live_basenames=live_basenames,
            gated_candidate_ids=bundle.gated_candidate_ids,
        )
        if plan_errors:
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=[f"{e.check}: {e.message}" for e in plan_errors],
                max_attempts=max_attempts,
            )

        # APPLY (deterministic, §3): the worker materializes ALL
        # structure/frontmatter/sources/links/
        # MOC/index/sentinels. confidence is MIRRORED from the candidate worst-case (never a plan
        # field) so the backend can never inflate it.
        confidence = {c.candidate_id: c.confidence for c in bundle.candidates if c.confidence}
        # raw_writes is the EXACT {raw_ref: content} set the engine materialized this run (ADR-0010
        # D3). The final-diff gate admits ONLY these exact paths-with-content under raw/; any other
        # raw/ change (a brain-planted file, or a PASS-2 overwrite of an engine-written source) is
        # then rejected off-allowlist, so the brain still never writes raw/.
        raw_writes = apply_plan(
            plan,
            worktree=wt,
            run_date=run_date,
            provenance=bundle.provenance,
            confidence=confidence,
        )

        # The post-APPLY tree is structurally complete with empty body regions (prose_complete still
        # false). Persist phase=applied BEFORE PASS 2 so a crash recovers at the right §9 entry.
        # BY DESIGN this `applied` manifest carries NO candidate commit sha (none exists yet) and
        # prose_complete=False, so recover() takes the conservative re-run-from-PASS-1 path (§9
        # rows applied|*|no): the dropped worktree took plan.json with it, so re-PLAN is the safe
        # default. Do NOT "optimize" this into a partial PASS-2 resume — that would risk a partial
        # publish. The candidate sha is recorded in a SECOND advance just before the CAS (below).
        _advance(layout, manifest, phase="applied", prose_complete=False)

        # PASS 2 — DELEGATED: author prose between the candidate-id sentinels, then validate the
        # diff.
        needs_prose, sentinels = _needs_prose_map(plan, wt, run_date)
        prose_complete = True
        if needs_prose:
            base_state = {rel: (wt / rel).read_text(encoding="utf-8") for rel in needs_prose}
            # A fatal backend-invocation failure on PASS 2 (missing executable / hung process) is
            # the same "model cannot run" channel as PASS 1: map it to a clean FAILED run rather
            # than let it escape run() as a traceback. A per-note NON-ZERO exit is NOT fatal — the
            # SubprocessBackend leaves it for the §4.2 AUTHOR-diff degrade path below.
            try:
                backend.author(wt, needs_prose)
            except (BackendUnavailableError, subprocess.TimeoutExpired) as exc:
                return _fail(
                    layout,
                    manifest,
                    state_store,
                    now=now,
                    reasons=[f"AUTHOR-BACKEND: {exc}"],
                    max_attempts=max_attempts,
                )
            # §4.6 deterministic stray-wikilink strip BEFORE validation: a `[[X]]` PASS 2 emitted
            # that is not a plan link for that note has its delimiters removed (inner text kept), so
            # an otherwise-good prose pass is repaired byte-deterministically instead of degrading
            # the WHOLE note to the placeholder over one stray link. Links are structure (APPLY-
            # owned); the strip is iterated to a fixed point in strip_stray_wikilinks.
            _strip_stray_links(wt, needs_prose, plan, live_basenames, run_date)
            new_state = {rel: (wt / rel).read_text(encoding="utf-8") for rel in needs_prose}
            changed = [rel for rel in needs_prose if base_state[rel] != new_state[rel]]
            author_errors = validate_author_diff(
                changed_paths=changed,
                per_file_old=base_state,
                per_file_new=new_state,
                sentinels=sentinels,
            )
            if author_errors:
                # §4.2 degrade-or-fail: RESTORE every needs_prose note to its post-APPLY base state
                # (structurally valid, body already at the placeholder + body_status: pending),
                # discarding ALL of PASS 2's edits — including any out-of-region/frontmatter tamper.
                # Structure is APPLY-owned and intact, so the run still PUBLISHES a prose-pending
                # note rather than failing (only the prose pass is lost).
                _degrade_prose(wt, needs_prose, base_state)
                prose_complete = False

        # §4.4 deterministic LINT of the full worktree — the SAME gate the dashboard runs. A lint
        # failure is a structural failure: discard the whole diff, publish nothing (§4.4).
        lint_result = lint(wt_layout, taxonomy=taxonomy, run_date=run_date, run_id=run_id)
        if not lint_result.ok:
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=[f"LINT {f.code} {f.path}: {f.message}" for f in lint_result.findings],
                max_attempts=max_attempts,
            )

        # §4.3 LOG (worker-only, AFTER validation): append ONE structured log.md entry. The model
        # never wrote log.md (asserted by the §4.2 diff scope above + the final-diff gate below).
        counts = _disposition_counts(plan)
        _append_log(wt, run_id=run_id, base_commit=base_commit, counts=counts, plan=plan)

        # §4.0/§4.3/§4.5 FINAL-DIFF ALLOWLIST GATE (ADR-0008 step 4): the deterministic integrity
        # boundary. Stage everything and assert the working tree touches ONLY canonical-ALLOWLIST
        # paths — no off-allowlist file the backend physically wrote (a new file the §4.2 scope
        # never surfaced, a planted symlink, an edit to _meta/_templates/raw/git internals), no
        # introduced/modified symlink or `..` escape, and ZERO tracked changes under
        # `_agora_scratch/`. A violation discards the whole worktree (no CAS) and fails the run.
        allowlist_errors = _assert_final_diff_allowlisted(
            wt, base_commit=base_commit, raw_writes=raw_writes
        )
        if allowlist_errors:
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=allowlist_errors,
                max_attempts=max_attempts,
            )

        # Commit once (advances only the detached worktree HEAD; the branch ref is unchanged until
        # the CAS below). `git add -A` is now safe — the final-diff gate already rejected anything
        # out of the allowlist.
        new_commit = repo.commit_worktree(wt, _commit_message(run_id, counts), when=now)

        # Persist the candidate commit sha DURABLY (phase=applied) BEFORE the CAS, so a crash in the
        # CAS-success window (ref advanced, state not yet saved) is recoverable: recover() sees the
        # recorded sha, checks repo.is_published, and FINALIZES instead of re-running PASS 1 and
        # double-publishing (ADR-0011 §9 "git ref already advanced" row). commit_worktree only moved
        # the detached HEAD, so recording the sha pre-CAS is safe even if the CAS then loses.
        _advance(
            layout,
            manifest,
            phase="applied",
            prose_complete=prose_complete,
            published_commit=new_commit,
        )

        # compare-and-swap the curated ref base→new — the SINGLE durable publish point (§4.3).
        # A lost CAS means a concurrent writer advanced the branch: discard + retry NEXT run (a CAS
        # conflict never burns the §5.1 retry budget — the events are valid, just stale).
        if not repo.compare_and_swap_branch(expected=base_commit, new=new_commit):
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=["CAS: curated ref moved since base_commit; conflict, discard + retry"],
                cas_conflict=True,
                max_attempts=max_attempts,
            )

    # --- published: the diff is durable in git. Record state + finalize (ADR-0008 step 5, §4.3).
    # ---
    _advance(
        layout,
        manifest,
        phase="published",
        prose_complete=prose_complete,
        published_commit=new_commit,
    )

    state = state_store.load()
    state.record_published_run(run_id, new_commit)
    state.mark_run(now, new_commit)
    _bump_counters(state, counts)
    # Move events to processed/ BEFORE recording tier-1 event_keys: _record_event_keys reads each
    # event's frontmatter from processed/<date>/, so the move must happen first or every key is
    # silently skipped (cross-run delivery idempotency would be lost, ADR-0011 §5 tier-1).
    _move_events_to_processed(layout, manifest, run_date)
    _record_event_keys(state, layout, manifest)
    state_store.save(state)

    _advance(
        layout,
        manifest,
        phase="finalized",
        prose_complete=prose_complete,
        published_commit=new_commit,
    )

    # ADR-0008 (readers resolve a PUBLISHED commit): the CAS moved only the curated ref; the
    # repo-owner's MAIN working copy is still parked at base_commit, so core.Wiki/kb_query (which
    # read the on-disk tree) would not yet see the published theme. Fast-forward the working copy to
    # the new tip AFTER state is saved + the manifest is finalized, so the read-after-publish
    # contract holds. GUARDED: a sync failure (e.g. an owner left the working tree dirty) must NOT
    # undo a durable publish — the diff is already in git and state is finalized; we keep
    # status=published and only log the sync failure.
    synced = _sync_owner_working_copy(repo, run_id)
    if not synced:
        # Surface the stuck state as a LOUD, observable RunReport signal (not just a log line): the
        # publish is durable in git, but HEAD now diverges from the curated tip so the read path is
        # stale until the owner reconciles the working tree. The next run still reads the
        # authoritative base from the curated ref and keeps making progress, so this is purely an
        # operator/dashboard signal that the working copy needs manual attention.
        counts = {**counts, "owner_working_copy_unsynced": 1}

    return RunReport(run_id=run_id, status="published", published_commit=new_commit, counts=counts)


# --- recovery (ADR-0011 §9 truth-table) --------------------------------------------------------


def recover(repo: Repo, *, state_store: StateStore) -> list[RunReport]:
    """Resolve every in-flight ``processing/<run-id>/`` run on start (ADR-0011 §9 / ADR-0008 §6).

    Deterministic, no backend call. For each manifest (chronological order) the action is a pure
    function of ``(phase, prose_complete, published_runs[run_id]?, git ref)``:

    * ``published`` (or ``state.published_runs`` has it, or the curated ref already points at the
      run's commit) ⇒ FINALIZE: ensure ``published_runs``/``state`` record the commit, move events
      to
      ``processed/``, set ``phase=finalized``. NO backend, NO re-commit (the CAS already landed).
    * ``claimed`` / ``applied`` with ``prose_complete=false`` ⇒ the run never published: return
      unchanged events to ``inbox/`` to re-run from PASS 1 next time, and clear the processing dir
      (the dropped worktree took ``plan.json`` with it — re-PLAN is the safe default).
    * ``finalized`` ⇒ nothing to do; clean up the (already empty) processing dir.

    Returns one :class:`RunReport` per processing run, each ``recovered``.
    """
    reports: list[RunReport] = []
    state = state_store.load()
    for manifest in list_processing(repo.layout):
        run_id = manifest.run_id
        # A run is "published" iff its commit is durable in git. The §9 "git ref already advanced"
        # row is the load-bearing one: the worker records `published_commit` at phase=applied
        # BEFORE the CAS, so a crash in the CAS-success window (ref advanced, manifest still
        # 'applied', state not yet saved) is detected here via repo.is_published — NOT re-run from
        # PASS 1 (which would double-publish). repo.is_published needs a recorded sha, so we keep
        # the None-guard; what makes this fire is the pre-CAS sha persistence, not a missing guard.
        published = (
            manifest.phase in ("published", "finalized")
            or state.is_published(run_id)
            or (
                manifest.published_commit is not None
                and repo.is_published(manifest.published_commit)
            )
        )
        if published:
            commit = (
                manifest.published_commit or state.published_commit(run_id) or repo.branch_commit()
            )
            reports.append(_finalize_recovered(repo, state_store, manifest, commit))
        else:
            reports.append(_return_to_inbox(repo, manifest))
        state = state_store.load()
    return reports


def _finalize_recovered(
    repo: Repo, state_store: StateStore, manifest: RunManifest, commit: str
) -> RunReport:
    """Finalize a published-but-unfinalized run WITHOUT any backend call (ADR-0008 step 6)."""
    run_id = manifest.run_id
    run_date = run_id[:10]
    layout = repo.layout

    state = state_store.load()
    if not state.is_published(run_id):
        state.record_published_run(run_id, commit)
    if state.last_commit != commit:
        # last_run timestamp is unknown post-crash; the published commit is the durable fact.
        state.last_commit = commit

    # Move events to processed/ FIRST, then record tier-1 event_keys (reads from processed/<date>/),
    # mirroring the happy-path order. A crash-at-published must NOT lose delivery idempotency: a
    # later same-key retry has to be dropped at claim time, so the keys for this run's events are
    # persisted here too (ADR-0011 §5 tier-1 / §9). Counters are NOT replayed — the plan went with
    # the dropped worktree, and counters are a best-effort dashboard signal rebuildable from git
    # (DATA-MODEL §4); event_keys are the correctness-bearing fact we must not lose.
    _move_events_to_processed(layout, manifest, run_date)
    _record_event_keys(state, layout, manifest)
    state_store.save(state)

    _advance(
        layout,
        manifest,
        phase="finalized",
        prose_complete=manifest.prose_complete,
        published_commit=commit,
    )

    # Same read-after-publish sync as the happy path (ADR-0008): a run finalized on restart already
    # advanced the curated ref, but the owner's working copy may still be behind, so fast-forward it
    # to the published commit. GUARDED: a sync failure never un-finalizes the recovered run.
    _sync_owner_working_copy(repo, run_id)

    return RunReport(
        run_id=run_id, status="recovered", published_commit=commit, counts={"finalized": 1}
    )


def _return_to_inbox(repo: Repo, manifest: RunManifest) -> RunReport:
    """Return an unpublished run's unchanged events to ``inbox/`` + clear its processing dir (§9).

    Events are byte-for-byte the original inbox files (claim moved them by rename, DATA-MODEL §1),
    so
    a recovered run re-claims them next pass and re-runs from PASS 1. The destination writer
    namespace
    is recovered from each event's frontmatter; the run directory (manifest + bundle + worktree
    scratch) is removed entirely.
    """
    layout = repo.layout
    run_dir = layout.processing_dir / manifest.run_id
    returned = _return_events_to_inbox(layout, run_dir / "events")
    shutil.rmtree(run_dir, ignore_errors=True)
    return RunReport(run_id=manifest.run_id, status="recovered", counts={"returned": returned})


# --- failure handling (ADR-0011 §4.3 / §5.1) ---------------------------------------------------


def _fail(
    layout: RepoLayout,
    manifest: RunManifest,
    state_store: StateStore,
    *,
    now: datetime,
    reasons: list[str],
    cas_conflict: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> RunReport:
    """Discard the run (publish NOTHING) and dispose of its events per the §5.1 retry budget.

    The entire worktree diff is dropped by the caller (the ``with repo.worktree`` block exits
    without a successful CAS, so the detached commit — if any — is unreachable). NOTHING is ever
    published on this path; only the events' LOCATION changes (DATA-MODEL §1, immutable events).

    Per-event disposition (ADR-0011 §4.3 / §5.1):

    * ``cas_conflict`` — a concurrent writer advanced the curated ref since ``base_commit``. The
      events are valid; only the base is stale. Return them ALL to ``inbox/`` to re-claim next run;
      a CAS conflict NEVER burns the retry budget.
    * otherwise (a PLAN/LINT/APPLY/diff validation failure) — for EACH event derive its retry count
      (the number of distinct ``processing/<run-id>/`` manifests + ``failed/**/error.json`` records
      that already reference its event_id, §5.1, this run inclusive). If it has NOT reached
      ``max_attempts`` (``repo.yaml curator.max_attempts``, default 3) return it to ``inbox/`` for a
      retry; only at the budget limit does it go terminal to ``failed/`` with an error record. The
      counter is DERIVED, never stored in the immutable event, so it is rebuildable from retained
      manifests/error records.
    """
    run_id = manifest.run_id
    run_date = run_id[:10]
    events_dir = layout.processing_dir / run_id / "events"

    if cas_conflict:
        returned = _return_events_to_inbox(layout, events_dir)
        shutil.rmtree(layout.processing_dir / run_id, ignore_errors=True)
        return RunReport(run_id=run_id, status="failed", counts={"retried": returned})

    # Prior attempt count per event = number of retained failed/ error records already referencing
    # it (§5.1). THIS run is one more attempt, so attempt N = prior + 1. We ALWAYS write this run's
    # error record (the durable retry counter + audit trail); the EVENT then either returns to
    # inbox/ (attempt < max_attempts) or stays terminal in failed/ (attempt >= max_attempts).
    prior = _event_attempt_counts(layout)

    failed_dir = layout.failed_dir / run_date / run_id
    failed_dir.mkdir(parents=True, exist_ok=True)
    error_record = {
        "run_id": run_id,
        "base_commit": manifest.base_commit,
        "event_ids": list(manifest.event_ids),
        "phase": manifest.phase,
        "failed_checks": reasons,
    }
    (failed_dir / "error.json").write_text(
        json.dumps(error_record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    retried = 0
    failed = 0
    if events_dir.is_dir():
        for event_path in sorted(events_dir.glob("*.md")):
            event_id = event_path.stem
            attempt = prior.get(event_id, 0) + 1  # this run is attempt N
            if attempt < max_attempts:
                # Within budget: return the unchanged event to inbox/ (re-claimed next run, §5.1).
                # The error.json above stays as the durable retry counter even though the event
                # leaves failed/.
                if _return_one_to_inbox(layout, event_path):
                    retried += 1
            else:
                # Budget exhausted: the event stays TERMINAL in failed/ alongside the error record.
                os.replace(event_path, failed_dir / event_path.name)
                failed += 1

    # Record terminal failures in the cumulative counters (retries are not failures yet, §5.1).
    if failed:
        state = state_store.load()
        state.counters.failed += failed
        state_store.save(state)

    # Remove the run's processing dir (manifest + bundle); events now live under inbox/ or failed/.
    shutil.rmtree(layout.processing_dir / run_id, ignore_errors=True)
    return RunReport(run_id=run_id, status="failed", counts={"retried": retried, "failed": failed})


def _event_attempt_counts(layout: RepoLayout) -> dict[str, int]:
    """Derive each event_id's PRIOR attempt count (ADR-0011 §5.1): retained ``failed/`` records.

    The retry count is NOT stored in the immutable event; it equals the number of distinct
    ``failed/**/error.json`` records that reference the event_id (one is written by :func:`_fail`
    per non-CAS attempt, so the count is durable even when the event itself is returned to inbox/
    for a retry). Rebuildable by scanning retained records, so a lost counter is recoverable
    (DATA-MODEL §1 / §4). Returns ``{event_id: prior_attempts}`` (THIS run not yet counted).
    """
    counts: dict[str, int] = {}
    failed_root = layout.failed_dir
    if not failed_root.exists():
        return counts
    for error_path in sorted(failed_root.rglob("error.json")):
        try:
            record = json.loads(error_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # One attempt per distinct error record, regardless of how many event_ids it lists.
        for event_id in record.get("event_ids", []):
            if isinstance(event_id, str):
                counts[event_id] = counts.get(event_id, 0) + 1
    return counts


def _return_events_to_inbox(layout: RepoLayout, events_dir: Path) -> int:
    """Return every event under ``events_dir`` to its frontmatter writer-namespace in ``inbox/``."""
    returned = 0
    if events_dir.is_dir():
        for event_path in sorted(events_dir.glob("*.md")):
            if _return_one_to_inbox(layout, event_path):
                returned += 1
    return returned


def _return_one_to_inbox(layout: RepoLayout, event_path: Path) -> bool:
    """Rename one claimed event back to ``inbox/<writer>/<id>.md`` (writer from frontmatter).

    The destination writer namespace is recovered from the event's immutable frontmatter
    (DATA-MODEL §1), so a returned event is re-claimed FIFO next run. An already-present inbox event
    (an idempotent duplicate) is never clobbered. Returns True iff the event moved.
    """
    try:
        fm, _ = frontmatter.parse(event_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    writer = fm.get("writer")
    event_id = fm.get("id")
    if not isinstance(writer, str) or not isinstance(event_id, str):
        return False
    dest = layout.inbox_item_path(writer, event_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return False
    os.replace(event_path, dest)
    return True


# --- helpers -----------------------------------------------------------------------------------


def _sync_owner_working_copy(repo: Repo, run_id: str) -> bool:
    """Fast-forward the repo-owner's MAIN working copy to the published curated tip (ADR-0008).

    Called after a run is durably published + finalized (happy path or recovery), and best-effort at
    the START of a run to reconcile a working tree left behind by a prior failed sync. The CAS moves
    the curated ref but leaves the owner's on-disk working copy at base_commit, so the read path
    (``core.Wiki`` / ``kb_query``, which read the on-disk tree) would not yet resolve the published
    content. :meth:`Repo.sync_to_branch` does a safe ``--ff-only`` advance. GUARDED: the publish is
    already durable in git and state is finalized, so a sync failure (e.g. a dirty/diverged owner
    working tree) must NOT undo it — log a warning and return ``False``, keeping status=published.
    Returns ``True`` iff the working copy is now at the curated tip.
    """
    try:
        repo.sync_to_branch()
    except GitError as exc:
        _logger.warning(
            "curator run %s published but the owner working copy could not be fast-forwarded "
            "to the curated tip (%s); the publish is durable in git — resolve the working tree "
            "manually so the read path reflects it",
            run_id,
            exc,
        )
        return False
    return True


def _new_run_id(now: datetime) -> str:
    return new_event_id(now=now)


def _iso(now: datetime) -> str:
    """The DATA-MODEL §5 ``started`` form: ``2026-06-13T03:00:00Z`` (second precision)."""
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- §3 / §4.3 worktree gitignore + final-diff allowlist gate ----------------------------------


def _ignore_scratch(worktree: Path) -> None:
    """Ignore ``_agora_scratch/`` in the worktree via its PER-WORKTREE git exclude (ADR-0011 §4.3).

    The detached worktree at ``base_commit`` carries the repo's committed ``.gitignore`` (``_kb/`` +
    ``.DS_Store`` only, core/repo.py), which does NOT ignore the backend scratch dir — so without
    this the backend's ``plan.json`` + model scratch would be staged by ``git add -A`` and leak into
    the curated tree. We write the ignore rule to the worktree's UNTRACKED ``$GIT_DIR/info/exclude``
    (``git rev-parse --git-path info/exclude``) rather than the tracked ``.gitignore``, so the
    scratch is writable inside the only writable mount (ADR-0008) AND ignored, while producing ZERO
    tracked change — the §4.3 ``.gitignore`` intent without a spurious curated diff entry that the
    §4.0 final-diff gate would (correctly) reject. Idempotent: the line is added once.
    """
    exclude_rel = _git(worktree, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    exclude = Path(exclude_rel)
    if not exclude.is_absolute():
        exclude = worktree / exclude
    line = f"{SCRATCH_DIRNAME}/"
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if line in existing.split("\n"):
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = existing if existing == "" or existing.endswith("\n") else existing + "\n"
    exclude.write_text(f"{prefix}{line}\n", encoding="utf-8")


def _strip_stray_links(
    worktree: Path,
    needs_prose: dict[str, list[str]],
    plan: Plan,
    live_basenames: set[str],
    run_date: str,
) -> None:
    """Strip §4.6 stray wikilinks from each authored sentinel region IN PLACE before validation.

    For each needs_prose note, the allowed link set is the union of every disposition's plan
    ``links`` targeting that note plus the live-tree basenames (resolvable links APPLY may have
    placed). Any ``[[X]]`` PASS 2 introduced whose key is NOT allowed has its delimiters removed
    (inner text kept) by :func:`strip_stray_wikilinks` (byte-deterministic, iterated to a fixed
    point), so an otherwise-good prose pass is REPAIRED rather than degraded over a single stray
    link (§4.6). Only the region BODY is rewritten; frontmatter, markers, and out-of-region text are
    byte-preserved, so a clean pass produces no spurious diff.
    """
    plan_links_by_note: dict[str, set[str]] = {}
    for disp in plan.dispositions:
        if not disp.needs_prose:
            continue
        rel = _disposition_note_rel_path(disp, worktree, run_date)
        if rel is not None:
            plan_links_by_note.setdefault(rel, set()).update(disp.links)

    for rel, cids in needs_prose.items():
        path = worktree / rel
        if not path.is_file():
            continue
        allowed = live_basenames | plan_links_by_note.get(rel, set())
        text = path.read_text(encoding="utf-8")
        new_text = text
        for cid in cids:
            new_text = _strip_region_stray_links(new_text, cid, allowed)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")


def _strip_region_stray_links(text: str, candidate_id: str, allowed: set[str]) -> str:
    """Return ``text`` with stray wikilinks stripped only inside ``candidate_id``'s body region.

    Pure string surgery between the exact ``agora:body:start/end id=<cid>`` markers (markers,
    frontmatter, and out-of-region text are byte-preserved); the region body is passed through
    :func:`strip_stray_wikilinks` so the §4.6 strip is scoped to the authored region exactly.
    """
    start, end = body_sentinels(candidate_id)
    si = text.find(start)
    ei = text.find(end)
    if si == -1 or ei == -1 or ei < si:
        return text
    region_start = si + len(start)
    region = text[region_start:ei]
    return f"{text[:region_start]}{strip_stray_wikilinks(region, allowed)}{text[ei:]}"


def _is_engine_written_raw(
    path: str, status: str, worktree: Path, raw_writes: dict[str, str]
) -> bool:
    """True iff ``path`` is EXACTLY an engine-written canonical ``raw/`` source (ADR-0010 D3).

    Admits an added/modified ``raw/`` entry into the final diff ONLY when BOTH hold:

    * ``path`` is in ``raw_writes`` — the exact set of refs the deterministic APPLY pass
      materialized this run (a brain-PLANTED ``raw/`` file is absent from the set → rejected); and
    * the file's current bytes equal what the engine wrote (``raw_writes[path]``) — a PASS-2
      OVERWRITE of an engine source (same path, forged content; appears as ``A``/``M`` in the cached
      diff vs ``base_commit``) has mismatched bytes → rejected.

    This replaces the prior blanket ``raw/``-prefix admit, which waved through ANY ``raw/`` add or
    modify and so let a brain forge the immutable verification baseline during PASS 2 (the §4.2
    AUTHOR-diff check never sees ``raw/`` — it grades only needs_prose notes' sentinel regions). A
    ``D`` (delete) of an engine source is NEVER admitted here: deleting a cited ``raw/`` source must
    fall through to the off-allowlist rejection. ``_kb/``, ``_meta/``, ``_templates/``, git
    internals, and hooks stay rejected by :func:`agora_kb.curator.constants.is_allowlisted_path`.
    """
    if status[:1] not in ("A", "M"):
        return False
    if path not in raw_writes:
        return False
    full = worktree / path
    if not full.is_file() or full.is_symlink():
        return False
    return full.read_text(encoding="utf-8") == raw_writes[path]


def _assert_final_diff_allowlisted(
    worktree: Path, *, base_commit: str, raw_writes: dict[str, str]
) -> list[str]:
    """Assert the worktree's pending diff touches ONLY canonical-ALLOWLIST paths (ADR-0011 §4.0).

    The deterministic integrity boundary (ADR-0008 step 4): stage everything (``git add -A``) and
    read ``git diff --cached --name-status``. The run FAILS (returns non-empty reasons) unless every
    added/modified/renamed/deleted path is allowlisted (§4.0) OR is an EXACT engine-written
    canonical source under ``raw/`` (ADR-0010 D3 — membership in ``raw_writes`` with matching
    content, the precise set :func:`agora_kb.curator.apply.apply_plan` materialized this run). The
    brain-only restriction on ``raw/`` is enforced HERE, by that exact-set check: a brain-planted
    ``raw/`` file (a path not in ``raw_writes``) or a PASS-2 overwrite of an engine source (same
    path, different bytes) both fall through to the off-allowlist rejection — the §4.2 AUTHOR-diff
    check cannot see ``raw/`` (it grades only needs_prose notes' sentinel body regions), so this is
    the only gate that protects the immutable verification baseline against a brain write. No entry
    introduces or modifies a symlink or a ``..`` escape (§4.5, diff-scoped — pre-existing schema
    symlinks are whitelisted as unchanged), and ``_agora_scratch/`` produced ZERO tracked changes.
    Reasons are
    sorted for deterministic error records. This catches any out-of-allowlist file the backend
    physically wrote that the §4.1 PLAN gate (which only constrains plan-IMPLIED paths) cannot see.
    ``_kb/``, ``_meta/``, ``_templates/``, git internals, and hooks stay REJECTED.
    """
    _git(worktree, "add", "-A")
    out = _git(worktree, "diff", "--cached", "--name-status", "-z").stdout

    reasons: list[str] = []
    for status, path in _parse_name_status_z(out):
        # §4.5: a NEW/MODIFIED schema symlink (or any change touching one) fails the run; an
        # unchanged pre-existing symlink never appears in the diff, so any appearance here is an
        # illegal mutation.
        if path in SCHEMA_SYMLINKS:
            reasons.append(
                f"FINAL-DIFF: schema symlink {path!r} was modified ({status}) — immutable"
            )
            continue
        if SCRATCH_DIRNAME in Path(path).parts:
            reasons.append(
                f"FINAL-DIFF: {path!r} under {SCRATCH_DIRNAME}/ produced a tracked change "
                f"(scratch must be git-ignored)"
            )
            continue
        # ADR-0010 D3: the engine (deterministic APPLY) legitimately commits canonical ``raw/``
        # free-text sources alongside ``wiki/``; admit ONLY the EXACT paths-with-content it wrote
        # (``raw_writes``), in addition to the §4.0 curated allowlist. A brain-planted ``raw/`` file
        # or a PASS-2 overwrite of an engine source is NOT in that exact set → falls through to the
        # rejection below, so the brain still never writes ``raw/``. The symlink/``..``-escape check
        # below still applies to admitted raw/ adds.
        if not (
            is_allowlisted_path(path) or _is_engine_written_raw(path, status, worktree, raw_writes)
        ):
            reasons.append(f"FINAL-DIFF: {path!r} ({status}) is outside the canonical ALLOWLIST")
            continue
        # §4.5: reject any added/modified entry that is a symlink or a path-escape, scoped to the
        # diff. Deleted (D) entries cannot introduce a symlink, so only A/M/R are checked.
        if status[:1] in ("A", "M", "R"):
            full = worktree / path
            if full.is_symlink():
                reasons.append(f"FINAL-DIFF: {path!r} ({status}) introduced/modified a symlink")
            if ".." in Path(path).parts:
                reasons.append(f"FINAL-DIFF: {path!r} ({status}) contains a '..' path escape")

    return sorted(reasons)


def _parse_name_status_z(out: str) -> list[tuple[str, str]]:
    """Parse ``git diff --name-status -z`` output into ``[(status, path), ...]`` (NUL-separated).

    ``-z`` separates every field by NUL: ``A\\0path\\0`` for simple statuses, and for
    renames/copies ``R<score>\\0old\\0new\\0`` (three NUL fields). We report the NEW path for a
    rename/copy (the one that lands in the curated tree). Empty trailing tokens are ignored.
    """
    tokens = [t for t in out.split("\0") if t != ""]
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if status[:1] in ("R", "C") and i + 2 < len(tokens):
            pairs.append((status, tokens[i + 2]))  # new path
            i += 3
        elif i + 1 < len(tokens):
            pairs.append((status, tokens[i + 1]))
            i += 2
        else:  # pragma: no cover — malformed/truncated git output
            break
    return pairs


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a hermetic, no-shell ``git`` command in ``worktree`` (mirrors core.repo._git flags).

    ``core.hooksPath=<devnull>`` neutralizes any host/repo hook so the deterministic gate can never
    execute planted code (ADR-0008 step 3). argv list, never a shell string. Raises on failure so a
    git error in the integrity gate surfaces rather than being silently treated as a clean diff.
    """
    cmd = ["git", "-c", f"core.hooksPath={os.devnull}", *args]
    cp = subprocess.run(  # noqa: S603 (argv list, no shell)
        cmd, cwd=str(worktree), capture_output=True, text=True
    )
    if cp.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed (rc={cp.returncode}): {cp.stderr.strip()}")
    return cp


def _advance(
    layout: RepoLayout,
    manifest: RunManifest,
    *,
    phase: Literal["claimed", "applied", "published", "finalized"],
    prose_complete: bool,
    published_commit: str | None = None,
) -> RunManifest:
    """Atomically rewrite the manifest at a new phase (DATA-MODEL §5; write_manifest is
    in-place)."""
    advanced = manifest.model_copy(
        update={
            "phase": phase,
            "prose_complete": prose_complete,
            "published_commit": published_commit
            if published_commit is not None
            else manifest.published_commit,
        }
    )
    write_manifest(layout, advanced)
    return advanced


def _needs_prose_map(
    plan: Plan, worktree: Path, run_date: str
) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    """Map ``needs_prose`` notes to their RUN-SCOPED region ids + build the §4.2 ``sentinels`` set.

    Returns ``(needs_prose, sentinels)`` where ``needs_prose`` is ``{rel_path: [region_id, ...]}``
    (the PASS-2 instruction the backend receives) and ``sentinels`` is ``{rel_path: {region_id}}`` —
    the COMPLETE set of body-sentinel regions :func:`validate_author_diff` expects in each note at
    post-APPLY state. Each ``region_id`` is the run-scoped ``region_sentinel_id`` value
    (``{run_id}--{candidate_id}``), computed via the SAME helper APPLY uses to PLACE the region so
    the two can never drift (the bare candidate_id is per-run and collides across runs).
    CREATE_THEME → ``wiki/<domain>/themes/<basename>.md``; APPEND_DAILY →
    ``wiki/<domain>/daily/<domain>-<run_date>.md`` (multiple dispositions can share one daily file,
    so ids accumulate); MERGE_INTO_THEME → the target theme path resolved in the live tree.
    """
    needs: dict[str, list[str]] = {}
    sentinels: dict[str, set[str]] = {}
    for disp in plan.dispositions:
        if not disp.needs_prose:
            continue
        rel = _disposition_note_rel_path(disp, worktree, run_date)
        if rel is None:
            continue
        # The PERSISTED region id is RUN-SCOPED (region_sentinel_id), computed via the SAME shared
        # helper APPLY uses to PLACE the region, so the §4.2 sentinels set can never drift from the
        # ids on disk. The bare candidate_id is per-run and would collide across runs (a MERGE /
        # cross-run daily append into a note holding a prior-run region).
        sentinel_id = region_sentinel_id(plan.run_id, disp.candidate_id)
        needs.setdefault(rel, []).append(sentinel_id)
        sentinels.setdefault(rel, set()).add(sentinel_id)
    # A note that already carried prior-run sentinel regions must list ALL live regions in
    # `sentinels` (validate_author_diff enforces exact set equality). Re-scan each touched note for
    # every present start-sentinel id so a prior CREATE_THEME body under a now-MERGED theme is kept.
    for rel in list(sentinels):
        present = _present_sentinel_ids(worktree / rel)
        sentinels[rel] |= present
    return needs, sentinels


def _disposition_note_rel_path(disp: Disposition, worktree: Path, run_date: str) -> str | None:
    """Return the repo-relative path the disposition authors prose into, or ``None`` if none.

    For APPEND_DAILY the daily basename defaults to ``<domain>-<run_date>`` when the plan omits it —
    MIRRORING :func:`agora_kb.curator.apply._apply_append_daily`, which derives daily basenames
    worker-side (ADR-0011 §2/§3.1, daily basenames are not a model field). Without this default a
    §4.1-valid APPEND_DAILY that omits ``basename`` (the normal case) would build no sentinel entry,
    leaving APPLY's authored region unfilled and failing the §4.4 lint.
    """
    if disp.op == "CREATE_THEME" and disp.domain and disp.basename:
        return f"wiki/{disp.domain}/themes/{disp.basename}.md"
    if disp.op == "APPEND_DAILY" and disp.domain:
        basename = disp.basename or f"{disp.domain}-{run_date}"
        return f"wiki/{disp.domain}/daily/{basename}.md"
    if disp.op == "MERGE_INTO_THEME" and disp.target_basename:
        matches = sorted(
            p
            for p in (worktree / "wiki").rglob(f"{disp.target_basename}.md")
            if p.is_file() and p.parent.name == "themes"
        )
        if matches:
            return matches[0].relative_to(worktree).as_posix()
    return None


_START_SENTINEL_RE = re.compile(r"\A<!-- agora:body:start id=(?P<cid>.+) -->\Z")


def _present_sentinel_ids(path: Path) -> set[str]:
    """Return every ``agora:body:start id=<cid>`` candidate id present in ``path`` (or ``set()``).

    Uses the SAME start-marker grammar as :func:`agora_kb.curator.apply.body_sentinels` /
    ``validate_author_diff`` so the §4.2 ``sentinels`` set the worker builds matches exactly what
    the
    AUTHOR-diff validator extracts.
    """
    if not path.is_file():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").split("\n"):
        m = _START_SENTINEL_RE.match(line)
        if m is not None:
            ids.add(m.group("cid"))
    return ids


def _degrade_prose(
    worktree: Path, needs_prose: dict[str, list[str]], base_state: dict[str, str]
) -> None:
    """Degrade every needs_prose note to a prose-pending state on AUTHOR-diff rejection (§4.2).

    The note is RESTORED to its post-APPLY ``base_state`` text (structurally valid, with the
    candidate-id body regions still at APPLY's initial fill and ``body_status: pending`` already
    set), which discards EVERY PASS-2 edit — including any out-of-region or frontmatter tamper the
    model wrote, since frontmatter/structure are APPLY-owned and the model is outside the integrity
    boundary (§4.2 / §7). Each candidate-id region body is then normalized to the §4.2 RESET
    placeholder ``> _summary pending_`` (derived from the plan summary). The structure is intact, so
    the run still PUBLISHES; only the prose pass is lost (``prose_complete=False``). Falls back to
    the on-disk note if (defensively) the base state is missing a path.
    """
    for rel, cids in needs_prose.items():
        path = worktree / rel
        base_text = base_state.get(rel)
        if base_text is None:
            if not path.is_file():
                continue
            base_text = path.read_text(encoding="utf-8")
        fm, body = frontmatter.parse(base_text)
        for cid in cids:
            body = _replace_sentinel_region(body, cid, _RESET_PLACEHOLDER)
        fm["body_status"] = "pending"
        path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def _disposition_counts(plan: Plan) -> dict[str, int]:
    """Per-op disposition tallies for the run (log.md + state.counters / dashboard signal)."""
    counts: dict[str, int] = {}
    for disp in plan.dispositions:
        counts[disp.op] = counts.get(disp.op, 0) + 1
    return counts


def _bump_counters(state: CuratorState, counts: dict[str, int]) -> None:
    """Update ``state.counters`` from the run dispositions (ADR-0011 §4.3 step 4 / DATA-MODEL §4).

    ``ingested`` += every originated/augmented content op
    (CREATE_THEME/APPEND_DAILY/MERGE/CONTESTED);
    ``merged`` += MERGE_INTO_THEME; ``dropped`` += DROP + NOOP. ``failed`` is bumped on the failure
    path (:func:`_fail`), never here.
    """
    ingested = sum(
        counts.get(op, 0)
        for op in ("CREATE_THEME", "APPEND_DAILY", "MERGE_INTO_THEME", "MARK_CONTESTED")
    )
    state.counters.ingested += ingested
    state.counters.merged += counts.get("MERGE_INTO_THEME", 0)
    state.counters.dropped += counts.get("DROP", 0) + counts.get("NOOP", 0)


def _record_event_keys(state: CuratorState, layout: RepoLayout, manifest: RunManifest) -> None:
    """Record each claimed event's ``writer:event_key`` into ``state.event_keys`` (ADR-0011 §4.3).

    Tier-1 delivery idempotency persists at finalization (NOT at claim time, §5 tier-1): a future
    run's claim drops a same-key retry even after this run's events have left the inbox. Each
    event's
    writer + event_key is read from its (now ``processed/``-bound) immutable frontmatter; an event
    with no event_key is skipped (un-keyed events are never deduped).
    """
    run_date = manifest.run_id[:10]
    processed_dir = layout.processed_dir / run_date
    for event_id in manifest.event_ids:
        path = processed_dir / f"{event_id}.md"
        if not path.is_file():
            continue
        try:
            fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        writer = fm.get("writer")
        event_key = fm.get("event_key")
        if isinstance(writer, str) and isinstance(event_key, str) and event_key:
            state.record_event_key(writer, event_key, event_id)


def _move_events_to_processed(layout: RepoLayout, manifest: RunManifest, run_date: str) -> None:
    """Move the claimed events ``processing/<run-id>/events/`` → ``processed/<date>/`` (§4.3 step
    4).

    A same-filesystem rename (``_kb/`` is one tree), so each event stays byte-for-byte immutable
    (DATA-MODEL §1). Idempotent: an already-moved event (a re-finalize after a crash) is skipped.
    """
    events_dir = layout.processing_dir / manifest.run_id / "events"
    if not events_dir.is_dir():
        return
    dest_dir = layout.processed_dir / run_date
    dest_dir.mkdir(parents=True, exist_ok=True)
    for event_path in sorted(events_dir.glob("*.md")):
        dest = dest_dir / event_path.name
        if dest.exists():
            continue
        os.replace(event_path, dest)


def _commit_message(run_id: str, counts: dict[str, int]) -> str:
    """One-line conventional commit subject for a curator run (one commit per run, §4.3)."""
    summary = ", ".join(f"{op}={n}" for op, n in sorted(counts.items()))
    return f"curate: run {run_id} ({summary})" if summary else f"curate: run {run_id}"


def _append_log(
    worktree: Path,
    *,
    run_id: str,
    base_commit: str,
    counts: dict[str, int],
    plan: Plan,
) -> None:
    """Append ONE structured entry to ``log.md`` (worker-only, AFTER validation; ADR-0011 §4.3).

    ``log.md`` MUST have been byte-identical to ``base_commit`` throughout PASS 1/PASS 2 (the model
    never touches it — enforced by the §4.2 diff scope), so this is the SINGLE in-run mutation of
    it,
    written by deterministic code only (append-only + single-writer, ADR-0001/0002). The entry
    records the run id, the per-op disposition counts, and the contested/dropped/pending lists.
    """
    log_path = worktree / "log.md"
    existing = log_path.read_text(encoding="utf-8") if log_path.is_file() else "# Curator log\n"

    contested = [d.target_basename for d in plan.dispositions if d.op == "MARK_CONTESTED"]
    dropped = [d.candidate_id for d in plan.dispositions if d.op in ("DROP", "NOOP")]
    pending = [
        d.candidate_id
        for d in plan.dispositions
        if d.needs_prose and d.op in ("CREATE_THEME", "APPEND_DAILY", "MERGE_INTO_THEME")
    ]
    counts_line = ", ".join(f"{op}={n}" for op, n in sorted(counts.items())) or "no-op"

    entry_lines = [
        f"## {run_id}",
        f"- base: `{base_commit}`",
        f"- dispositions: {counts_line}",
    ]
    if contested:
        entry_lines.append(f"- contested: {', '.join(str(c) for c in contested)}")
    if dropped:
        entry_lines.append(f"- dropped: {', '.join(dropped)}")
    if pending:
        entry_lines.append(f"- pending-body: {', '.join(pending)}")
    entry = "\n".join(entry_lines) + "\n"

    body = existing if existing.endswith("\n") else existing + "\n"
    log_path.write_text(f"{body}\n{entry}", encoding="utf-8")
