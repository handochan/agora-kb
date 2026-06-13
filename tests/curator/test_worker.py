"""Tests for the capstone transactional curator run-loop + recovery (ADR-0008/0011, DESIGN §4).

ZERO real model: every run is driven by a :class:`FakeBackend` built with a canned ``plan.json``
string + a ``{candidate_id: prose}`` map. Success is graded as a pure function of
``(plan, git_diff, manifest, lint)`` (ADR-0011 §4), so a fake brain is sufficient to exercise the
entire integrity boundary. We cover the four contractually-distinct outcomes:

* HAPPY PATH — a CREATE_THEME plan + prose publishes: the curated ref advances via CAS, the theme
  + lint are valid, events move to ``processed/``, ``state`` records the run, the manifest is final;
* INVALID PLAN — a plan naming a non-taxonomy domain is rejected by §4.1: ``failed``, branch
  UNCHANGED, events in ``failed/``, nothing published;
* EMPTY inbox — ``noop`` (no run dir, no commit);
* RECOVERY — a simulated crash at ``phase=published`` (ref already advanced) is finalized by
  :func:`recover` WITHOUT any backend call.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from agora_kb.core import frontmatter
from agora_kb.core.ids import new_event_id
from agora_kb.core.inbox import Inbox
from agora_kb.core.layout import RepoLayout
from agora_kb.core.repo import Repo
from agora_kb.core.state import StateStore
from agora_kb.curator.apply import body_sentinels
from agora_kb.curator.claim import curator_lock
from agora_kb.curator.manifest import RunManifest, manifest_path, read_manifest, write_manifest
from agora_kb.curator.worker import Backend, FakeBackend, RunReport, recover, run
from agora_kb.schema.emit import Taxonomy, emit_schema
from agora_kb.schema.lint import lint

NOW = datetime(2026, 6, 13, 3, 0, 0, tzinfo=UTC)
RUN_DATE = "2026-06-13"

TAXONOMY = Taxonomy(
    schema_version=1,
    taxonomy_policy="open",
    allowed_tags=("curator", "concurrency"),
    domains=("ai-tech", "general"),
)


# --- fixtures -----------------------------------------------------------------------------------


def _init_repo(tmp_path: Path) -> Repo:
    """Init a git knowledge repo with the emitted schema + taxonomy, committed at the curated tip.

    The schema doc + ``_meta/`` + ``_templates/`` are emitted and committed so the worktree at
    ``base_commit`` carries the read-only INGEST inputs the bundle/lint rely on.
    """
    layout = RepoLayout(tmp_path)
    repo = Repo(layout)
    repo.init(when=datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC))
    emit_schema(layout, taxonomy=TAXONOMY)
    _commit_all(repo, "chore: emit schema")
    return repo


def _commit_all(repo: Repo, message: str) -> str:
    """Stage + commit everything in the main working copy (advancing the curated branch tip)."""
    return repo.commit_worktree(repo.root, message, when=datetime(2026, 6, 12, 1, 0, 0, tzinfo=UTC))


def _write_capture(inbox: Inbox, *, text: str, second: int, domain: str = "ai-tech") -> str:
    """Write one inbox capture at a pinned second (deterministic, time-sortable id)."""
    now = datetime(2026, 6, 13, 2, 40, second, tzinfo=UTC)
    return inbox.write(text=text, writer="dochan", source="claude-code", domain=domain, now=now).id


def _seed_raw(repo: Repo, *event_ids: str, domain: str = "ai-tech") -> None:
    """Commit ``raw/<domain>/<event_id>.md`` artifacts so the curated themes' ``sources:`` resolve.

    APPLY writes ``sources: [raw/<domain>/<event_id>.md]`` (the provenance union, ADR-0010 D3); lint
    L1-8 then asserts each cited path EXISTS in the worktree. In production ``core.ingest`` persists
    these at capture time; here we materialize + commit them into the base so the run lints clean.
    """
    for event_id in event_ids:
        raw = repo.root / "raw" / domain / f"{event_id}.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f"raw capture {event_id}\n", encoding="utf-8")
    _commit_all(repo, "chore: seed raw artifacts")


def _create_theme_plan(run_id: str, candidate_id: str, event_id: str) -> str:
    """A canned single-CREATE_THEME ``plan.json`` over one candidate (the happy-path plan)."""
    return json.dumps(
        {
            "schema_version": 1,
            "run_id": run_id,
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": candidate_id,
                    "event_ids": [event_id],
                    "op": "CREATE_THEME",
                    "domain": "ai-tech",
                    "basename": "curator-concurrency",
                    "title": "Curator concurrency model",
                    "summary": "One curator advances the curated branch under a per-repo lock.",
                    "status": "active",
                    "tags": ["curator", "concurrency"],
                    "aliases": [],
                    "links": [],
                    "needs_prose": True,
                    "reason": "New concept; no related note above threshold.",
                }
            ],
        }
    )


def _run(repo: Repo, backend: Backend, *, now: datetime = NOW) -> RunReport:
    return run(
        repo,
        backend=backend,
        state_store=StateStore(repo.layout),
        now=now,
        taxonomy=TAXONOMY,
    )


# --- (1) HAPPY PATH -----------------------------------------------------------------------------


def test_happy_path_publishes_theme_advances_ref_and_finalizes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    e2 = _write_capture(inbox, text="The inbox is append-only and per-writer.", second=11)
    _seed_raw(repo, e1, e2)
    base = repo.head_commit()

    # The FIFO claim assigns candidate ids c1/c2 in event order (distinct bodies => two candidates).
    # The plan creates ONE theme from c1 and DROPs c2 (so coverage stays exact over the manifest).
    plan = json.dumps(
        {
            "schema_version": 1,
            "run_id": "ignored",
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": "c1",
                    "event_ids": [e1],
                    "op": "CREATE_THEME",
                    "domain": "ai-tech",
                    "basename": "curator-concurrency",
                    "title": "Curator concurrency model",
                    "summary": "One curator advances the curated branch under a per-repo lock.",
                    "status": "active",
                    "tags": ["curator", "concurrency"],
                    "aliases": [],
                    "links": [],
                    "needs_prose": True,
                    "reason": "New concept.",
                },
                {
                    "candidate_id": "c2",
                    "event_ids": [e2],
                    "op": "DROP",
                    "needs_prose": False,
                    "reason": "Redundant for this run.",
                },
            ],
        }
    )
    backend = FakeBackend(plan, prose={"c1": "The single curator holds a per-repo flock."})

    report = _run(repo, backend)

    # status published + the curated ref advanced from base via CAS.
    assert report.status == "published"
    assert report.published_commit is not None
    new_tip = repo.branch_commit()
    assert new_tip == report.published_commit
    assert new_tip != base

    # Inspect the PUBLISHED tree (the CAS moved only the ref; the main checkout on disk is stale, so
    # we check out the published commit into a fresh worktree). The theme exists, the published tree
    # lints clean (the §4.4 gate the worker ran), and the authored prose landed in the sentinel.
    with repo.worktree(at=new_tip) as published:
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        assert theme.is_file()
        assert lint(RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE).ok
        # The cited canonical raw/ source is committed alongside wiki/ (ADR-0010 D3) — the published
        # commit holds raw/ + wiki/ consistently, which is what makes lint L1-8 pass.
        assert (published / "raw" / "ai-tech" / f"{e1}.md").is_file()
        theme_text = theme.read_text(encoding="utf-8")
        assert "The single curator holds a per-repo flock." in theme_text
        # Injected-determinism contract (ADR-0010 D1): the integrity path reads NO wall clock — the
        # published frontmatter dates are the injected run_date (run_id[:10]), not the test box's
        # clock. Pins "run_date injected, no wall-clock leak" explicitly rather than via lint alone.
        fm, _ = frontmatter.parse(theme_text)
        assert fm["created"] == RUN_DATE
        assert fm["updated"] == RUN_DATE

    # Events moved to processed/<date>/; the inbox is drained.
    processed = layout.processed_dir / RUN_DATE
    assert (processed / f"{e1}.md").is_file()
    assert (processed / f"{e2}.md").is_file()
    assert inbox.depth() == 0

    # state.json: published_runs + last_commit + counters recorded.
    state = StateStore(layout).load()
    assert state.published_runs[report.run_id] == new_tip
    assert state.last_commit == new_tip
    assert state.counters.ingested == 1  # one CREATE_THEME
    assert state.counters.dropped == 1  # one DROP

    # The manifest is finalized (recovery is a no-op afterward).
    manifest = read_manifest(manifest_path(layout, report.run_id))
    assert manifest.phase == "finalized"
    assert manifest.published_commit == new_tip


# --- (1b) read-after-publish: the owner working copy is synced to the published tip -------------


def test_happy_path_syncs_owner_working_copy_so_query_sees_published_theme(tmp_path: Path) -> None:
    """ADR-0008 read-after-publish: after a successful CAS publish the worker fast-forwards the
    repo-owner's MAIN working copy to the curated tip, so ``core.Wiki`` over
    ``RepoLayout(repo.root)`` resolves the published theme WITHOUT any manual ``git reset`` — the
    read path reads the on-disk tree, and the CAS alone leaves it stale.
    """
    from agora_kb.core.wiki import Wiki

    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    base = repo.head_commit()
    plan = _create_theme_plan("ignored", "c1", e1)

    report = _run(
        repo, FakeBackend(plan, prose={"c1": "The single curator holds a per-repo flock."})
    )

    assert report.status == "published"
    new_tip = report.published_commit
    assert new_tip is not None
    assert new_tip != base
    # The MAIN working copy is AT the new tip (no manual git reset) — read-after-publish.
    assert repo.head_commit() == new_tip
    # The published theme is materialized on disk, so a plain Wiki over the repo root sees it.
    theme = layout.wiki_dir / "ai-tech" / "themes" / "curator-concurrency.md"
    assert theme.is_file()
    result = Wiki(RepoLayout(repo.root)).query("curator concurrency")
    assert result.status == "ok"
    assert any(h.path == "wiki/ai-tech/themes/curator-concurrency.md" for h in result.hits)


def test_sync_failure_after_publish_does_not_unpublish_or_unfinalize(tmp_path: Path) -> None:
    """ADR-0008 guarded sync: a post-finalize ``sync_to_branch`` GitError must NOT flip a published
    run to failed or undo it. The CAS already landed and state is finalized, so a sync failure
    leaves the publish durable in git; the worker only logs it and surfaces an observable signal.
    """
    from agora_kb.core.repo import GitError

    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    base = repo.head_commit()
    plan = _create_theme_plan("ignored", "c1", e1)

    # Make the post-publish sync raise: the CAS lands + finalize completes, then sync_to_branch
    # fails (as if the owner working tree were dirtied just before the read-after-publish sync).
    def boom(branch: str | None = None) -> str:
        raise GitError("simulated dirty owner working tree on post-publish sync")

    repo.sync_to_branch = boom  # type: ignore[method-assign]

    report = _run(
        repo, FakeBackend(plan, prose={"c1": "The single curator holds a per-repo flock."})
    )

    # The run is STILL published and the commit is durable in git despite the sync failure.
    assert report.status == "published"
    new_tip = report.published_commit
    assert new_tip is not None
    assert new_tip != base
    assert repo.branch_commit() == new_tip  # durable: the curated ref advanced
    # The stuck working copy is surfaced as an observable signal, not silently swallowed.
    assert report.counts.get("owner_working_copy_unsynced") == 1
    # state + manifest reflect a finalized publish (the sync failure un-did neither).
    state = StateStore(layout).load()
    assert state.published_runs[report.run_id] == new_tip
    manifest = read_manifest(manifest_path(layout, report.run_id))
    assert manifest.phase == "finalized"
    assert manifest.published_commit == new_tip


def test_failed_run_leaves_owner_working_copy_unchanged(tmp_path: Path) -> None:
    """A failed run publishes nothing and never touches the owner working copy: HEAD stays at base
    and no theme appears on disk (the sync runs ONLY on a published/recovered run)."""
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _write_capture(inbox, text="A fact in a non-existent domain.", second=10)
    _seed_raw(repo, e1)
    base = repo.head_commit()

    bad_plan = json.dumps(
        {
            "schema_version": 1,
            "run_id": "ignored",
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": "c1",
                    "event_ids": [e1],
                    "op": "CREATE_THEME",
                    "domain": "not-a-real-domain",
                    "basename": "rogue-theme",
                    "title": "Rogue",
                    "summary": "Should never be created.",
                    "status": "active",
                    "tags": [],
                    "aliases": [],
                    "links": [],
                    "needs_prose": True,
                    "reason": "Invalid domain.",
                }
            ],
        }
    )
    report = _run(repo, FakeBackend(bad_plan, prose={"c1": "unreachable"}))

    assert report.status == "failed"
    assert repo.head_commit() == base  # the owner working copy never advanced
    assert not (layout.wiki_dir / "ai-tech" / "themes" / "rogue-theme.md").exists()


# --- (2) INVALID PLAN ---------------------------------------------------------------------------


def test_invalid_plan_fails_leaves_branch_unchanged_and_events_in_failed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _write_capture(inbox, text="A fact in a non-existent domain.", second=10)
    _seed_raw(repo, e1)
    base = repo.head_commit()

    # The plan names a domain NOT in the taxonomy (§4.1 check 4 TAXONOMY rejects it) — a
    # structural failure that publishes NOTHING.
    bad_plan = json.dumps(
        {
            "schema_version": 1,
            "run_id": "ignored",
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": "c1",
                    "event_ids": [e1],
                    "op": "CREATE_THEME",
                    "domain": "not-a-real-domain",
                    "basename": "rogue-theme",
                    "title": "Rogue",
                    "summary": "Should never be created.",
                    "status": "active",
                    "tags": [],
                    "aliases": [],
                    "links": [],
                    "needs_prose": True,
                    "reason": "Invalid domain.",
                }
            ],
        }
    )
    report = _run(repo, FakeBackend(bad_plan, prose={"c1": "unreachable"}))

    assert report.status == "failed"
    # The curated branch never moved (nothing published).
    assert repo.branch_commit() == base
    # No theme was created on the published tree.
    assert not (layout.wiki_dir / "ai-tech" / "themes" / "rogue-theme.md").exists()
    # §5.1 retry budget: on the FIRST attempt (well under curator.max_attempts=3) a non-CAS
    # validation failure returns the event UNCHANGED to inbox/ to be re-claimed next run — it does
    # NOT go terminal to failed/ on a one-off bad plan. The event is back at its writer namespace.
    assert (layout.inbox_item_path("dochan", e1)).is_file()
    failed_run = layout.failed_dir / RUN_DATE / report.run_id
    # The durable §5.1 retry counter (error.json) is written, but the EVENT itself is NOT terminal —
    # it lives in inbox/, not under failed/.
    assert (failed_run / "error.json").is_file()
    assert not (failed_run / f"{e1}.md").exists()
    error = json.loads((failed_run / "error.json").read_text(encoding="utf-8"))
    assert any("TAXONOMY" in c for c in error["failed_checks"])
    assert report.counts == {"retried": 1, "failed": 0}
    # The processing dir is cleared; no terminal failure recorded yet (retry isn't a failure).
    assert not (layout.processing_dir / report.run_id).exists()
    assert StateStore(layout).load().counters.failed == 0


def test_plan_failure_goes_terminal_to_failed_at_retry_budget(tmp_path: Path) -> None:
    """§5.1: once an event reaches curator.max_attempts, a PLAN failure moves it TERMINAL to failed.

    A persistently-bad plan (a non-taxonomy domain) is retried back to inbox/ until the derived
    retry count (distinct failed/ error records referencing the event_id) hits max_attempts=3, at
    which point the event lands in failed/<date>/<run-id>/ with an error.json naming the check and
    counters.failed is bumped — the run no longer re-loops forever.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _write_capture(inbox, text="A fact in a non-existent domain.", second=10)
    _seed_raw(repo, e1)
    base = repo.head_commit()

    def bad_plan_for(event_id: str) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "run_id": "ignored",
                "finished": True,
                "dispositions": [
                    {
                        "candidate_id": "c1",
                        "event_ids": [event_id],
                        "op": "CREATE_THEME",
                        "domain": "not-a-real-domain",
                        "basename": "rogue-theme",
                        "title": "Rogue",
                        "summary": "Should never be created.",
                        "status": "active",
                        "tags": [],
                        "aliases": [],
                        "links": [],
                        "needs_prose": True,
                        "reason": "Invalid domain.",
                    }
                ],
            }
        )

    # Run the curator three times; the same event keeps coming back to inbox/ and is re-claimed.
    last = None
    for _ in range(3):
        last = _run(repo, FakeBackend(bad_plan_for(e1), prose={"c1": "x"}))
        assert last.status == "failed"
        assert repo.branch_commit() == base  # nothing ever published

    # On the third attempt the budget (max_attempts=3) is reached → terminal failed/.
    assert last is not None
    assert not (layout.inbox_item_path("dochan", e1)).is_file()
    failed_run = layout.failed_dir / RUN_DATE / last.run_id
    assert (failed_run / f"{e1}.md").is_file()
    error = json.loads((failed_run / "error.json").read_text(encoding="utf-8"))
    assert any("TAXONOMY" in c for c in error["failed_checks"])
    assert StateStore(layout).load().counters.failed == 1


# --- (3) EMPTY inbox ----------------------------------------------------------------------------


def test_empty_inbox_is_noop(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base = repo.head_commit()

    # No captures written. The claim finds nothing => noop; no run dir, no commit.
    report = _run(repo, FakeBackend(_create_theme_plan("x", "c1", "y")))

    assert report.status == "noop"
    assert repo.branch_commit() == base
    assert not repo.layout.processing_dir.exists() or not any(repo.layout.processing_dir.iterdir())


# --- (4) RECOVERY -------------------------------------------------------------------------------


def test_recover_finalizes_a_published_crashed_run_without_backend(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    # First run a real publish so the curated ref genuinely advances to a published commit.
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    plan = _create_theme_plan("ignored", "c1", e1)
    report = _run(repo, FakeBackend(plan, prose={"c1": "Single-writer detail."}))
    assert report.status == "published"
    published_commit = report.published_commit
    assert published_commit is not None

    # Simulate a crash AFTER the CAS landed but BEFORE finalize: rewind the manifest to
    # phase=published (events still claimed in processing/, state lacks the run, processed/ empty).
    crashed_run_id = new_event_id(now=datetime(2026, 6, 13, 4, 0, 0, tzinfo=UTC))
    e2 = _write_capture(inbox, text="A second fact for the crashed run.", second=20)
    # Hand-place the crashed run's claimed event + a phase=published manifest whose published_commit
    # is the (already-advanced) curated ref tip — the §9 "published, ref advanced" row.
    events_dir = layout.processing_dir / crashed_run_id / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    src = layout.inbox_item_path("dochan", e2)
    import os

    os.replace(src, events_dir / f"{e2}.md")
    crashed_manifest = RunManifest(
        run_id=crashed_run_id,
        base_commit=published_commit,
        event_ids=(e2,),
        phase="published",
        prose_complete=True,
        published_commit=published_commit,
        started="2026-06-13T04:00:00Z",
    )
    write_manifest(layout, crashed_manifest)

    # recover() takes NO backend (ADR-0008 §6: a published run is finalized without any backend
    # call), so its signature alone proves no model is invoked on this path.
    reports = recover(repo, state_store=StateStore(layout))

    # The crashed run was recovered + finalized; no backend was called.
    crashed_reports = [r for r in reports if r.run_id == crashed_run_id]
    assert len(crashed_reports) == 1
    assert crashed_reports[0].status == "recovered"

    # Finalization moved the claimed event to processed/ and advanced the manifest to finalized.
    finalized = read_manifest(manifest_path(layout, crashed_run_id))
    assert finalized.phase == "finalized"
    assert (layout.processed_dir / crashed_run_id[:10] / f"{e2}.md").is_file()
    # state.json now records the crashed run's published commit.
    assert StateStore(layout).load().published_runs[crashed_run_id] == published_commit


# --- misbehaving backends (for the §4.2 degrade + §4.6 strip + final-diff gate) ----------------


class _OutOfRegionBackend:
    """A :class:`Backend` whose PASS-2 author writes OUTSIDE the sentinel region (frontmatter).

    Drives the §4.2 AUTHOR-degrade-or-publish path: validate_author_diff rejects the out-of-region
    edit, the worker resets the body to ``> _summary pending_`` + ``body_status: pending``, and the
    run STILL publishes (structure is valid). FakeBackend can never reach this, so this fake is the
    only way to exercise _degrade_prose / prose_complete=False.
    """

    def __init__(self, plan_text: str) -> None:
        self._plan_text = plan_text

    def plan(self, bundle_dir: Path) -> str:  # noqa: ARG002
        return self._plan_text

    def author(self, worktree: Path, needs_prose: dict[str, list[str]]) -> None:
        for rel_path in needs_prose:
            path = worktree / rel_path
            text = path.read_text(encoding="utf-8")
            # Tamper with frontmatter (owned by APPLY) — an out-of-region edit §4.2 must reject.
            path.write_text(text.replace("status: active", "status: active\nrogue: 1"), "utf-8")


class _StrayLinkBackend(FakeBackend):
    """A :class:`FakeBackend` whose prose embeds a stray ``[[X]]`` not in the plan links (§4.6)."""


class _OffAllowlistBackend(FakeBackend):
    """A :class:`FakeBackend` that ALSO writes a file outside the allowlist + one under scratch."""

    def author(self, worktree: Path, needs_prose: dict[str, list[str]]) -> None:
        super().author(worktree, needs_prose)
        # A file OUTSIDE the canonical allowlist (the final-diff gate must reject the run).
        (worktree / "_templates").mkdir(parents=True, exist_ok=True)
        (worktree / "_templates" / "evil.md").write_text("planted\n", encoding="utf-8")
        # A file under the git-ignored scratch dir (must produce ZERO tracked changes).
        (worktree / "_agora_scratch").mkdir(parents=True, exist_ok=True)
        (worktree / "_agora_scratch" / "plan.json").write_text("{}\n", encoding="utf-8")


class _ScratchOnlyBackend(FakeBackend):
    """A :class:`FakeBackend` that also writes under ``_agora_scratch/`` (publish must survive)."""

    def author(self, worktree: Path, needs_prose: dict[str, list[str]]) -> None:
        super().author(worktree, needs_prose)
        (worktree / "_agora_scratch").mkdir(parents=True, exist_ok=True)
        (worktree / "_agora_scratch" / "plan.json").write_text("{}\n", encoding="utf-8")


class _RawForgingBackend(FakeBackend):
    """A :class:`FakeBackend` that forges ``raw/`` during PASS 2 (the brain-exclusion attack).

    Beyond authoring legit prose, it OVERWRITES the APPLY-materialized canonical source at
    ``forge_ref`` with corrupted content AND PLANTS a brand-new ``raw/`` file at ``plant_ref`` — the
    exact attack the §4.0 final-diff gate must reject. The brain can never write ``raw/`` (ADR-0010
    D3): the gate admits ONLY the engine's EXACT paths-with-content, so a forged overwrite (same
    path, different bytes) and a planted path (absent from the engine's set) both fail the run.
    """

    def __init__(self, plan_text: str, *, forge_ref: str, plant_ref: str, **kw: object) -> None:
        super().__init__(plan_text, **kw)  # type: ignore[arg-type]
        self._forge_ref = forge_ref
        self._plant_ref = plant_ref

    def author(self, worktree: Path, needs_prose: dict[str, list[str]]) -> None:
        super().author(worktree, needs_prose)
        forged = worktree / self._forge_ref
        forged.parent.mkdir(parents=True, exist_ok=True)
        forged.write_text(
            "FORGED by the brain — verification baseline corrupted\n", encoding="utf-8"
        )
        planted = worktree / self._plant_ref
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text("planted by brain\n", encoding="utf-8")


# --- (5) §4.2 AUTHOR degrade-or-publish ---------------------------------------------------------


def test_author_failure_degrades_prose_but_run_still_publishes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    plan = _create_theme_plan("ignored", "c1", e1)

    report = _run(repo, _OutOfRegionBackend(plan))

    # The §4.2 outcome: AUTHOR failed for the note, so its body is reset to the placeholder and
    # body_status stays pending — but the STRUCTURE is valid, so the run publishes.
    assert report.status == "published"
    with repo.worktree(at=report.published_commit) as published:  # type: ignore[arg-type]
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
        assert fm["body_status"] == "pending"
        assert "rogue" not in fm  # the out-of-region frontmatter tamper was discarded
        start, end = body_sentinels("c1")
        region = body[body.find(start) + len(start) : body.find(end)]
        assert region.strip() == "> _summary pending_"
        assert lint(RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE).ok

    # prose_complete is False on the finalized manifest (the degrade path).
    manifest = read_manifest(manifest_path(layout, report.run_id))
    assert manifest.phase == "finalized"
    assert manifest.prose_complete is False


def test_stray_wikilink_is_stripped_and_run_publishes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    plan = _create_theme_plan("ignored", "c1", e1)
    # PASS 2 emits prose containing a stray [[ghost]] not in the plan links — §4.6 strips it
    # (delimiters removed, inner text kept) so the otherwise-good pass publishes, NOT degrades.
    backend = _StrayLinkBackend(plan, prose={"c1": "See [[ghost]] for the flock detail."})

    report = _run(repo, backend)

    assert report.status == "published"
    with repo.worktree(at=report.published_commit) as published:  # type: ignore[arg-type]
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        text = theme.read_text(encoding="utf-8")
        assert "[[ghost]]" not in text  # delimiters stripped
        assert "See ghost for the flock detail." in text  # inner text kept
        assert lint(RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE).ok
    # The prose pass succeeded after the strip — prose_complete stays True (not degraded).
    assert read_manifest(manifest_path(repo.layout, report.run_id)).prose_complete is True


# --- (6) final-diff allowlist gate + _agora_scratch/ gitignore ----------------------------------


def test_off_allowlist_file_is_rejected_by_final_diff_gate(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    base = repo.head_commit()
    plan = _create_theme_plan("ignored", "c1", e1)

    # The backend plants a file OUTSIDE the allowlist (_templates/) AND a scratch file. The §4.0
    # final-diff gate must FAIL the run and publish nothing; the scratch file is git-ignored so it
    # never even reaches the diff (it would otherwise be a second violation).
    report = _run(repo, _OffAllowlistBackend(plan, prose={"c1": "detail"}))

    assert report.status == "failed"
    assert repo.branch_commit() == base  # nothing published
    # The error record names the FINAL-DIFF check and points at the off-allowlist path.
    error_files = list((layout.failed_dir).rglob("error.json"))
    assert error_files
    checks = json.loads(error_files[0].read_text(encoding="utf-8"))["failed_checks"]
    assert any("FINAL-DIFF" in c and "_templates" in c for c in checks)
    # The scratch file did NOT cause a tracked change (it is git-ignored), so it is not a violation.
    assert not any("_agora_scratch" in c for c in checks)


def test_brain_cannot_forge_or_plant_raw_during_pass2(tmp_path: Path) -> None:
    """A PASS-2 backend that overwrites/plants ``raw/`` is rejected by the final-diff gate (D3).

    The blocker the loosened ``raw/``-prefix allowlist re-introduced: the brain can never write
    ``raw/`` (ADR-0010 D3 / the Karpathy immutable-verification-baseline guarantee), yet the
    §4.2 AUTHOR-diff check never sees ``raw/`` (it grades only needs_prose notes' sentinel regions),
    so the final-diff gate is the ONLY protection. It must admit ONLY the EXACT engine-written
    paths-with-content (``apply_plan``'s ``raw_writes``): a PASS-2 OVERWRITE of the materialized
    source (same path, forged bytes) AND a PLANTED new ``raw/`` file both fall through to the
    off-allowlist rejection. The run must FAIL, the branch must NOT move, and the error must name
    the FINAL-DIFF check on a ``raw/`` path.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    # A genuine free-text capture (no raw_ref) → APPLY materializes raw/ai-tech/<e1>.md from the
    # immutable body. raw/ is deliberately NOT pre-seeded.
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    base = repo.head_commit()
    plan = _create_theme_plan("ignored", "c1", e1)

    forge_ref = f"raw/ai-tech/{e1}.md"  # the engine-materialized source the brain overwrites
    plant_ref = "raw/ai-tech/planted-by-brain.md"  # a brand-new raw/ file the brain plants
    backend = _RawForgingBackend(
        plan, forge_ref=forge_ref, plant_ref=plant_ref, prose={"c1": "legit prose"}
    )

    report = _run(repo, backend)

    assert report.status == "failed"
    assert repo.branch_commit() == base  # nothing published; the baseline cannot be forged
    error_files = list(layout.failed_dir.rglob("error.json"))
    assert error_files
    checks = json.loads(error_files[0].read_text(encoding="utf-8"))["failed_checks"]
    # The forged overwrite (same path, different bytes) and the planted file are BOTH rejected as
    # off-allowlist FINAL-DIFF entries on a raw/ path.
    assert any("FINAL-DIFF" in c and "raw" in c for c in checks)
    assert any("FINAL-DIFF" in c and "planted-by-brain" in c for c in checks)


def test_scratch_only_writes_do_not_break_publish(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    plan = _create_theme_plan("ignored", "c1", e1)

    # A backend that writes legit prose PLUS scratch under _agora_scratch/ still publishes cleanly —
    # the worktree .gitignore exclude keeps the scratch out of the curated diff (§4.3).
    report = _run(repo, _ScratchOnlyBackend(plan, prose={"c1": "detail"}))

    assert report.status == "published"
    with repo.worktree(at=report.published_commit) as published:  # type: ignore[arg-type]
        # The scratch dir is NOT part of the published tree.
        assert not (published / "_agora_scratch").exists()


# --- (7) LINT failure after a real APPLY+commit -------------------------------------------------


def test_lint_failure_after_apply_discards_diff_leaves_branch_unchanged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    # The capture carries an explicit raw_ref pointing at an UPLOADED file that does NOT exist (and
    # which the engine does NOT materialize — only free-text captures without a raw_ref are written
    # from the body, ADR-0010 D3). APPLY cites sources: [raw/ai-tech/missing-upload.md], so lint
    # L1-8 (cited source must exist) FAILS on the post-APPLY tree — a real APPLY + commit happened
    # on the detached worktree, then the §4.4 gate discards the whole diff.
    e1 = inbox.write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        raw_ref="raw/ai-tech/missing-upload.md",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    ).id
    base = repo.head_commit()
    plan = _create_theme_plan("ignored", "c1", e1)

    report = _run(repo, FakeBackend(plan, prose={"c1": "detail"}))

    assert report.status == "failed"
    assert repo.branch_commit() == base  # the load-bearing guarantee: discard-after-commit
    error_files = list(layout.failed_dir.rglob("error.json"))
    assert error_files
    checks = json.loads(error_files[0].read_text(encoding="utf-8"))["failed_checks"]
    assert any(c.startswith("LINT") for c in checks)


# --- (8) CAS conflict ---------------------------------------------------------------------------


def test_cas_conflict_publishes_nothing_and_retries_without_burning_budget(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    base = repo.head_commit()
    plan = _create_theme_plan("ignored", "c1", e1)

    # Force a CAS conflict by monkeypatching compare_and_swap_branch to report the ref moved.
    repo.compare_and_swap_branch = lambda *, expected, new, branch=None: False  # type: ignore[method-assign]

    report = _run(repo, FakeBackend(plan, prose={"c1": "detail"}))

    assert report.status == "failed"
    assert repo.branch_commit() == base  # nothing partial published
    # A CAS conflict is a retry, never a terminal failure (the events are valid, only stale): the
    # event returns to inbox/ and NO error.json / counters.failed is recorded (§4.3).
    assert report.counts == {"retried": 1}
    assert (layout.inbox_item_path("dochan", e1)).is_file()
    assert not list(layout.failed_dir.rglob("error.json"))
    assert StateStore(layout).load().counters.failed == 0


# --- (9) PlanParseError -------------------------------------------------------------------------


def test_malformed_plan_text_fails_with_plan_parse(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    base = repo.head_commit()

    report = _run(repo, FakeBackend("{not json"))

    assert report.status == "failed"
    assert repo.branch_commit() == base
    error_files = list(repo.layout.failed_dir.rglob("error.json"))
    assert error_files
    checks = json.loads(error_files[0].read_text(encoding="utf-8"))["failed_checks"]
    assert any("PLAN-PARSE" in c for c in checks)


# --- (9b) backend cannot run: a fatal PASS-1 invocation failure is a clean FAILED run -----------


class _UnavailablePlanBackend:
    """A :class:`Backend` whose PASS-1 ``plan`` raises ``BackendUnavailableError`` (no executable).

    The real SubprocessBackend raises this when the configured brain is missing / exits non-zero.
    It is NOT a PlanParseError, so without the worker mapping it would escape ``run()`` uncaught —
    the contract this guards is "model outside the integrity boundary → deterministic FAILED run".
    """

    def plan(self, bundle_dir: Path) -> str:  # noqa: ARG002
        from agora_kb.curator.subprocess_backend import BackendUnavailableError

        raise BackendUnavailableError("backend 'ghost' could not be executed")

    def author(self, worktree: Path, needs_prose: dict[str, list[str]]) -> None:  # noqa: ARG002
        raise AssertionError("author must never be reached when PASS 1 fails to run")


def test_backend_unavailable_on_plan_fails_cleanly_without_escaping(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    base = repo.head_commit()

    # No traceback escapes run(): a missing/non-zero backend maps to a deterministic FAILED run.
    report = _run(repo, _UnavailablePlanBackend())

    assert report.status == "failed"
    assert repo.branch_commit() == base  # nothing published
    error_files = list(repo.layout.failed_dir.rglob("error.json"))
    assert error_files
    checks = json.loads(error_files[0].read_text(encoding="utf-8"))["failed_checks"]
    assert any("PLAN-BACKEND" in c for c in checks)


# --- (10) tier-1 event_keys: recorded at finalize + cross-run dedup -----------------------------


def test_keyed_capture_records_event_key_and_dedups_a_later_retry(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    # A capture WITH an event_key: tier-1 delivery idempotency must persist after publish so a later
    # same-key retry is dropped at claim time (ADR-0011 §5 tier-1).
    ev = inbox.write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        event_key="k1",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    e1 = ev.id
    _seed_raw(repo, e1)
    plan = _create_theme_plan("ignored", "c1", e1)

    report = _run(repo, FakeBackend(plan, prose={"c1": "detail"}))
    assert report.status == "published"

    # The composite writer:event_key is persisted into state.event_keys (the blocker: this was
    # silently empty when keys were recorded BEFORE the move to processed/).
    state = StateStore(layout).load()
    assert state.event_keys == {"dochan:k1": e1}

    # A LATER run delivering the SAME writer:event_key retry is dropped by tier-1 at claim time.
    inbox.write(
        text="A duplicate same-key retry.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        event_key="k1",
        now=datetime(2026, 6, 13, 2, 41, 0, tzinfo=UTC),
    )
    later = _run(
        repo,
        FakeBackend(_create_theme_plan("ignored", "c1", "x")),
        now=datetime(2026, 6, 13, 5, 0, 0, tzinfo=UTC),
    )
    assert later.status == "noop"  # the only event was tier-1-deduped at claim


# --- (11) all-deduped noop + held-lock noop -----------------------------------------------------


def test_all_deduped_claim_is_noop(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    base = repo.head_commit()

    # Pre-seed state.event_keys so the single keyed inbox event is dropped by tier-1 at claim time.
    store = StateStore(layout)
    state = store.load()
    state.record_event_key("dochan", "k1", "prior-event")
    store.save(state)
    inbox.write(
        text="A duplicate.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        event_key="k1",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )

    report = _run(repo, FakeBackend(_create_theme_plan("x", "c1", "y")))

    assert report.status == "noop"
    assert repo.branch_commit() == base
    assert not layout.processing_dir.exists() or not any(layout.processing_dir.iterdir())


def test_held_lock_is_noop(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    base = repo.head_commit()
    _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)

    # Hold the curator lock; run() must report noop (a run in progress) rather than block.
    with curator_lock(layout):
        report = _run(repo, FakeBackend(_create_theme_plan("x", "c1", "y")))

    assert report.status == "noop"
    assert report.counts == {"reason_lock_held": 1}
    assert repo.branch_commit() == base


# --- (12) APPEND_DAILY needs-prose mapping (the basename + run_date contract, fix #8) -----------


def test_append_daily_needs_prose_maps_to_the_daily_path(tmp_path: Path) -> None:
    """``_needs_prose_map`` builds the daily sentinel entry mirroring APPLY's daily basename (§3.1).

    The worker's needs_prose mapping for APPEND_DAILY must resolve to the SAME
    ``wiki/<domain>/daily/<basename>.md`` path APPLY writes — using the supplied basename, and
    DEFAULTING to ``<domain>-<run_date>`` when omitted (mirroring apply._apply_append_daily) — so
    PASS 2 fills the candidate-id region APPLY created (otherwise the §4.4 sentinel/lint check would
    fail the run). Unit-level so it does not couple to the plan.run_id↔lint daily-id equality (an
    APPLY/plan contract owned elsewhere).
    """
    from agora_kb.curator.plan import Plan
    from agora_kb.curator.worker import _disposition_note_rel_path, _needs_prose_map

    repo = _init_repo(tmp_path)

    def disp(basename: str | None) -> dict[str, object]:
        d: dict[str, object] = {
            "candidate_id": "c1",
            "event_ids": ["e1"],
            "op": "APPEND_DAILY",
            "domain": "ai-tech",
            "summary": "s",
            "status": "active",
            "tags": [],
            "aliases": [],
            "links": [],
            "needs_prose": True,
            "reason": "r",
        }
        if basename is not None:
            d["basename"] = basename
        return d

    with repo.worktree(at=repo.head_commit()) as wt:
        # Explicit basename → the exact daily path.
        plan_named = Plan.model_validate(
            {"schema_version": 1, "run_id": "r", "finished": True, "dispositions": [disp("d-x")]}
        )
        rel = _disposition_note_rel_path(plan_named.dispositions[0], wt, RUN_DATE)
        assert rel == "wiki/ai-tech/daily/d-x.md"

        # Omitted basename → defaulted to <domain>-<run_date> (mirrors apply._apply_append_daily).
        plan_default = Plan.model_validate(
            {"schema_version": 1, "run_id": "r", "finished": True, "dispositions": [disp(None)]}
        )
        d0 = plan_default.dispositions[0]
        assert (
            _disposition_note_rel_path(d0, wt, RUN_DATE)
            == f"wiki/ai-tech/daily/ai-tech-{RUN_DATE}.md"
        )
        needs, sentinels = _needs_prose_map(plan_default, wt, RUN_DATE)
        key = f"wiki/ai-tech/daily/ai-tech-{RUN_DATE}.md"
        assert needs == {key: ["c1"]}
        assert sentinels == {key: {"c1"}}


# --- (13) recovery: conservative return-to-inbox for an unpublished claimed/applied run ---------


def test_recover_returns_unpublished_run_events_to_inbox(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    base = repo.head_commit()

    # Hand-place a phase='claimed' run that NEVER published: a claimed event + a manifest, with the
    # curated ref NOT advanced to any run commit. recover() must return the event UNCHANGED to
    # inbox/ and clear the run dir, recording NOTHING in published_runs (the §9 safety half).
    run_id = new_event_id(now=datetime(2026, 6, 13, 4, 0, 0, tzinfo=UTC))
    e2 = _write_capture(inbox, text="An unpublished claimed event.", second=20)
    events_dir = layout.processing_dir / run_id / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    src = layout.inbox_item_path("dochan", e2)
    original_bytes = src.read_bytes()
    os.replace(src, events_dir / f"{e2}.md")
    write_manifest(
        layout,
        RunManifest(
            run_id=run_id,
            base_commit=base,
            event_ids=(e2,),
            phase="claimed",
            prose_complete=False,
            published_commit=None,
            started="2026-06-13T04:00:00Z",
        ),
    )

    reports = recover(repo, state_store=StateStore(layout))

    rec = [r for r in reports if r.run_id == run_id]
    assert len(rec) == 1
    assert rec[0].status == "recovered"
    assert rec[0].counts["returned"] == 1
    # The event is back at inbox/<writer>/<id>.md, byte-for-byte (claim moved it by rename).
    restored = layout.inbox_item_path("dochan", e2)
    assert restored.is_file()
    assert restored.read_bytes() == original_bytes
    # The processing dir is gone and the run was NEVER published.
    assert not (layout.processing_dir / run_id).exists()
    assert run_id not in StateStore(layout).load().published_runs
    assert repo.branch_commit() == base


def test_recover_never_clobbers_an_already_present_inbox_event(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    base = repo.head_commit()

    run_id = new_event_id(now=datetime(2026, 6, 13, 4, 0, 0, tzinfo=UTC))
    e2 = _write_capture(inbox, text="An event that also exists in inbox.", second=20)
    events_dir = layout.processing_dir / run_id / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    # Copy (not move) the event into processing/, leaving the original in inbox/ — the never-clobber
    # guard must NOT overwrite the present inbox event during recovery.
    inbox_path = layout.inbox_item_path("dochan", e2)
    (events_dir / f"{e2}.md").write_bytes(inbox_path.read_bytes())
    inbox_sentinel = inbox_path.read_bytes()
    write_manifest(
        layout,
        RunManifest(
            run_id=run_id,
            base_commit=base,
            event_ids=(e2,),
            phase="claimed",
            prose_complete=False,
            published_commit=None,
            started="2026-06-13T04:00:00Z",
        ),
    )

    recover(repo, state_store=StateStore(layout))

    # The pre-existing inbox event is untouched; the processing copy is dropped with the run dir.
    assert inbox_path.read_bytes() == inbox_sentinel
    assert not (layout.processing_dir / run_id).exists()


# --- (14) recovery: the §9 "git ref advanced but state not recorded" row ------------------------


def test_recover_finalizes_via_git_ref_when_state_missing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    # A real publish so the curated ref genuinely points at a run commit.
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    report = _run(repo, FakeBackend(_create_theme_plan("ignored", "c1", e1), prose={"c1": "x"}))
    tip = report.published_commit
    assert tip is not None

    # Simulate the §9 line "CAS succeeded but state.json wasn't recorded" row: a manifest left at
    # phase='applied' prose_complete=True with published_commit==the advanced ref tip, but
    # published_runs/state EMPTY. recover() must FINALIZE via the git-ref branch (is_published),
    # NOT re-run PASS 1 / double-publish.
    crashed = new_event_id(now=datetime(2026, 6, 13, 4, 0, 0, tzinfo=UTC))
    e2 = _write_capture(inbox, text="A second fact.", second=20)
    events_dir = layout.processing_dir / crashed / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    os.replace(layout.inbox_item_path("dochan", e2), events_dir / f"{e2}.md")
    write_manifest(
        layout,
        RunManifest(
            run_id=crashed,
            base_commit=tip,
            event_ids=(e2,),
            phase="applied",
            prose_complete=True,
            published_commit=tip,  # the already-advanced ref tip
            started="2026-06-13T04:00:00Z",
        ),
    )
    # Clear state.published_runs so ONLY the git-ref check can detect publication.
    store = StateStore(layout)
    state = store.load()
    assert crashed not in state.published_runs
    store.save(state)

    reports = recover(repo, state_store=store)

    rec = [r for r in reports if r.run_id == crashed]
    assert len(rec) == 1
    assert rec[0].status == "recovered"
    # Finalized via git-ref: published_runs now records the run and the event moved to processed/.
    assert StateStore(layout).load().published_runs[crashed] == tip
    assert (layout.processed_dir / crashed[:10] / f"{e2}.md").is_file()
    assert read_manifest(manifest_path(layout, crashed)).phase == "finalized"


# --- (15) the DOUBLE-PUBLISH window: crash at applied with ref already advanced -----------------


def test_recover_does_not_double_publish_after_crash_in_cas_success_window(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    plan = _create_theme_plan("ignored", "c1", e1)

    # Interpose a fault so the run stops RIGHT AFTER the CAS lands: monkeypatch _advance so the
    # transition to phase='published' raises, leaving the manifest at phase='applied' with the
    # candidate commit sha recorded (the pre-CAS advance) while the git ref HAS advanced. This is
    # the real crash window the recovery blocker is about (NOT a hand-built phase='published').
    import agora_kb.curator.worker as W

    real_advance = W._advance

    def faulting_advance(layout_, manifest_, *, phase, prose_complete, published_commit=None):  # type: ignore[no-untyped-def]
        if phase == "published":
            raise RuntimeError("simulated crash right after CAS, before finalize")
        return real_advance(
            layout_,
            manifest_,
            phase=phase,
            prose_complete=prose_complete,
            published_commit=published_commit,
        )

    W._advance = faulting_advance  # type: ignore[assignment]
    try:
        try:
            _run(repo, FakeBackend(plan, prose={"c1": "detail"}))
        except RuntimeError:
            pass  # the simulated crash
    finally:
        W._advance = real_advance  # type: ignore[assignment]

    # The git ref HAS advanced (CAS landed) but the manifest is still 'applied', published_commit
    # recorded, state.published_runs empty — the exact double-publish window.
    tip = repo.branch_commit()
    in_flight = [
        m
        for m in __import__(
            "agora_kb.curator.manifest", fromlist=["list_processing"]
        ).list_processing(layout)
    ]
    assert len(in_flight) == 1
    crashed = in_flight[0]
    assert crashed.phase == "applied"
    assert crashed.published_commit == tip
    assert crashed.run_id not in StateStore(layout).load().published_runs

    # recover() must FINALIZE (NOT re-run PASS 1) — the events are not returned to inbox and the
    # wiki theme is not duplicated.
    reports = recover(repo, state_store=StateStore(layout))
    rec = [r for r in reports if r.run_id == crashed.run_id]
    assert len(rec) == 1
    assert rec[0].status == "recovered"
    assert rec[0].published_commit == tip
    # The event is in processed/, NOT back in inbox/ (no re-run, no double-publish).
    assert (layout.processed_dir / crashed.run_id[:10] / f"{e1}.md").is_file()
    assert not (layout.inbox_item_path("dochan", e1)).is_file()
    assert repo.branch_commit() == tip  # the ref did not advance a second time
    assert StateStore(layout).load().published_runs[crashed.run_id] == tip


# --- (16) capture -> consolidate -> publish, end-to-end with NO pre-seeded raw/ -----------------


def test_free_text_capture_publishes_with_engine_materialized_raw_source(tmp_path: Path) -> None:
    """The Phase-1 integration path: a real kb_remember capture (no raw_ref) consolidates clean.

    Regression guard for the ADR-0010 D3 gap: a CREATE_THEME from a free-text capture cites
    raw/<domain>/<event_id>.md, and the deterministic engine (APPLY) — never the sandboxed brain —
    materializes that source in the worktree from the immutable claimed-event body, so the curator's
    commit holds raw/ + wiki/ consistently, schema.lint L1-8 ("sources path does not exist") passes,
    and the run PUBLISHES. raw/ is deliberately NOT pre-seeded here (the bug was that raw/ was never
    materialized, so the run could never publish).
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    # A genuine free-text capture written through the real core.Inbox.write — NO raw_ref, NO
    # pre-seeded raw/ artifact. This is exactly the kb_remember -> kb_curate path.
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    base = repo.head_commit()
    assert not (layout.root / "raw" / "ai-tech" / f"{e1}.md").exists()  # raw/ does NOT exist yet

    plan = _create_theme_plan("ignored", "c1", e1)
    report = _run(
        repo, FakeBackend(plan, prose={"c1": "The single curator holds a per-repo flock."})
    )

    # The run published — the integration gap is closed.
    assert report.status == "published"
    new_tip = repo.branch_commit()
    assert new_tip == report.published_commit
    assert new_tip != base

    # The published commit holds the engine-materialized raw/ source AND lints clean (the SAME §4.4
    # gate the worker ran). Lint over a worktree at the new commit is the authoritative check.
    with repo.worktree(at=new_tip) as published:
        raw = published / "raw" / "ai-tech" / f"{e1}.md"
        assert raw.is_file()
        # The raw/ artifact carries the immutable capture body (the engine wrote it from the event).
        assert raw.read_text(encoding="utf-8") == "One curator advances the branch under a lock."
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
        assert fm["sources"] == [f"raw/ai-tech/{e1}.md"]
        result = lint(
            RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=report.run_id
        )
        assert result.ok, [f for f in result.findings]
