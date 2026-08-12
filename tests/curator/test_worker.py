"""Tests for the capstone transactional curator run-loop + recovery (ADR-0008/0011, DESIGN §4).

ZERO real model: every run is driven by a :class:`FakeBackend` built with a canned ``plan.json``
string + a prose map keyed by the PERSISTED, run-scoped region id
``region_sentinel_id(plan.run_id, candidate_id)`` — NOT the bare per-run ``candidate_id`` (#121;
the module-level :func:`_no_unintended_prose_pending` guard below enforces that). Success is graded
as a pure function of
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
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

import agora_kb.curator.worker as worker_mod
from agora_kb.core import frontmatter
from agora_kb.core.ids import new_event_id
from agora_kb.core.inbox import Inbox, InboxReturn, failed_event_count
from agora_kb.core.layout import RepoLayout
from agora_kb.core.repo import Repo
from agora_kb.core.state import LastFailure, StateStore
from agora_kb.curator.apply import body_sentinels, region_sentinel_id
from agora_kb.curator.claim import curator_lock
from agora_kb.curator.manifest import RunManifest, manifest_path, read_manifest, write_manifest
from agora_kb.curator.subprocess_backend import RoutedBackend
from agora_kb.curator.worker import (
    AuthorRegion,
    Backend,
    FakeBackend,
    RunFailure,
    RunReport,
    _unauthored_regions,
    recover,
    run,
)
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


# --- the prose-fake contract + the pending guard (#121) -----------------------------------------
#
# ``FakeBackend.author`` looks prose up by the id it is HANDED in ``needs_prose`` — the persisted,
# run-scoped ``region_sentinel_id(plan.run_id, candidate_id)`` APPLY stamped into the note — never
# the bare per-run ``candidate_id``. ~30 call sites here passed ``prose={"c1": …}``, which matches
# NOTHING: PASS 2 wrote no prose and the note published carrying APPLY's ``_summary pending_``
# placeholder, while the test asserted ``published`` and never looked at a body. That is the exact
# #115 failure shape — a published assertion with no body oracle — reproduced inside the tests
# meant to guard against it. The guard below is that oracle, applied to EVERY run in this file.

# The prose map for a run whose PLAN never survives the §4.1 gate (or that claims nothing at all):
# PASS 2 is never invoked, so the map is never read and its keys cannot matter. A NAMED constant
# rather than an inline ``{"c1": "unreachable"}`` so the bare id reads as the deliberate statement
# it is — "no prose can land on this path" — instead of looking like the #121 bug it resembles.
PLAN_REJECTED_PROSE = {"c1": "unreachable: the plan is rejected before PASS 2 is ever invoked"}


@pytest.fixture
def prose_pending_is_the_point() -> None:
    """Opt out of :func:`_no_unintended_prose_pending` — a pending region IS this test's subject.

    Requested BY NAME in the signature, so "this run publishes a placeholder body" is a claim the
    test makes out loud rather than a silence. Used by the #115 degrade shapes (a PASS 2 that wrote
    nothing / was rejected) and by the #119 cross-run shapes (a note carrying a legitimate
    ``body_status: pending`` owed by an EARLIER run).
    """


@pytest.fixture(autouse=True)
def _no_unintended_prose_pending(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Fail any test in this file that publishes a placeholder body without saying so (#121).

    The oracle is ``_unauthored_regions`` — the worker's OWN §4.2 per-region verdict — rather than
    ``report.counts["prose_pending"]``, because a FAILED run's counts carry no such key at all
    (``{"retried": 1}``), and the failed paths are precisely where the silent gaps hid: the
    final-diff, lint and CAS tests all reach PASS 2 before their gate rejects them, so a mismatched
    prose key there quietly made "the brain authored legit prose" false in fixtures whose docstrings
    assert it. Spying on the worker's module attribute leaves the two direct unit tests of
    ``_unauthored_regions`` (which import the function itself) untouched, as it should.

    Whole-suite inventory taken while writing this: the only OTHER runs that leave a region
    unauthored are ``tests/test_cli.py::test_curate_warns_loudly_when_pass2_authors_no_prose`` and
    ``tests/core/test_sentinel.py::test_grader_agrees_with_the_run_scoped_unauthored_regions_verdict``
    — both deliberate, and both named for it.
    """
    original = worker_mod._unauthored_regions
    pending: list[tuple[str, str]] = []

    def spy(
        needs_prose: dict[str, list[str]],
        per_file_old: dict[str, str],
        per_file_new: dict[str, str],
    ) -> list[tuple[str, str]]:
        unauthored = original(needs_prose, per_file_old, per_file_new)
        pending.extend(unauthored)
        return unauthored

    monkeypatch.setattr(worker_mod, "_unauthored_regions", spy)
    yield
    if "prose_pending_is_the_point" in request.fixturenames:
        return
    assert not pending, (
        f"PASS 2 left {len(pending)} body region(s) unauthored, so this test published placeholder "
        f"bodies while asserting nothing about them: {pending}. Key the FakeBackend prose map with "
        "region_sentinel_id(<the plan's run_id>, 'c1') — or, if a pending region IS the point, "
        "request the `prose_pending_is_the_point` fixture."
    )


def test_run_forwards_related_k_and_max_orphans_to_bundle_and_lint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker.run threads related_k → build_bundle and max_orphans → lint (the shared seam all three
    faces feed through run→_run_locked). Locks the forwarding so a dropped kwarg cannot silently
    revert an operator's curator.limits.related_k / curator.lint.max_orphans to the default."""
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)

    backend = FakeBackend(
        _create_theme_plan("ignored", "c1", e1),
        prose={region_sentinel_id("ignored", "c1"): "The single curator holds a per-repo flock."},
    )

    seen_related: list[int] = []
    seen_orphans: list[int | None] = []
    orig_bundle = worker_mod.build_bundle
    orig_lint = worker_mod.lint

    def bundle_spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen_related.append(kwargs.get("related_k"))
        return orig_bundle(*args, **kwargs)

    def lint_spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen_orphans.append(kwargs.get("max_orphans"))
        return orig_lint(*args, **kwargs)

    monkeypatch.setattr(worker_mod, "build_bundle", bundle_spy)
    monkeypatch.setattr(worker_mod, "lint", lint_spy)

    report = run(
        repo,
        backend=backend,
        state_store=StateStore(layout),
        now=NOW,
        taxonomy=TAXONOMY,
        related_k=3,
        max_orphans=0,
    )
    assert (
        report.status == "published"
    )  # the run reached BOTH sinks (bundle before, lint after apply)
    assert seen_related == [3]  # build_bundle received the operator's related_k, not the default 8
    assert seen_orphans == [0]  # lint received the operator's max_orphans, not the default None


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
    # The PERSISTED sentinel id is run-scoped ({plan.run_id}--{candidate_id}); FakeBackend keys
    # prose by the id it receives in needs_prose, so seed it run-scoped (plan.run_id="ignored").
    backend = FakeBackend(
        plan,
        prose={region_sentinel_id("ignored", "c1"): "The single curator holds a per-repo flock."},
    )

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
        # #119 criterion (a): PASS 2 really filled the region, so the worker RETRACTED APPLY's
        # `body_status: pending` before the §4.4 lint. The key is the schema's "this note still
        # owes prose" signal (ADR-0010 §2.6) — leaving it set on a published, authored note made
        # that signal worthless to every reader.
        assert "body_status" not in fm

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


def test_finalize_rebuilds_index_cache_after_publish(tmp_path: Path) -> None:
    """ADR-0012 §2 / issue #26: a successful (synced) publish rebuilds the derived reader cache.

    The cache is built best-effort in finalize AFTER the owner working copy is synced to the new
    curated tip, so it is present, fresh (stamped with the published commit), and no
    ``index_cache_unbuilt`` signal is raised on the happy path.
    """
    from agora_kb.core import index_cache
    from agora_kb.core.wiki import Wiki

    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    backend = FakeBackend(
        _create_theme_plan("ignored", "c1", e1),
        prose={region_sentinel_id("ignored", "c1"): "The single curator holds a per-repo flock."},
    )

    report = _run(repo, backend)

    assert report.status == "published"
    assert "index_cache_unbuilt" not in report.counts
    assert "owner_working_copy_unsynced" not in report.counts
    cache_path = layout.index_notes_path()
    assert cache_path.is_file()
    payload = index_cache.read_payload(cache_path)
    assert payload is not None
    assert payload.curated_commit == report.published_commit
    # the freshly-published theme is in the cache and query returns it via the cache path
    assert any(p.endswith("curator-concurrency.md") for p in payload.notes)
    assert Wiki(layout).query("curator concurrency").status == "ok"


def test_finalize_rebuilds_gold_pack_after_publish(tmp_path: Path) -> None:
    """ADR-0027 §2a / issue #37: a successful (synced) publish rebuilds the derived gold pack.

    The pack is built best-effort in finalize AFTER the owner working copy is synced to the new
    curated tip, so it is present, fresh (its sidecar stamped with the published commit), and no
    ``gold_unbuilt`` signal is raised on the happy path. The published theme appears in the pack.
    """
    from agora_kb.core.gold import read_meta

    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    backend = FakeBackend(
        _create_theme_plan("ignored", "c1", e1),
        prose={region_sentinel_id("ignored", "c1"): "The single curator holds a per-repo flock."},
    )

    report = _run(repo, backend)

    assert report.status == "published"
    assert "gold_unbuilt" not in report.counts
    pack_path = layout.gold_pack_path("default")
    assert pack_path.is_file()
    meta = read_meta(layout)
    assert meta is not None
    assert meta.curated_sha == report.published_commit
    # The freshly-curated theme is in the pack (it is an active, non-harvest theme).
    assert "Curator concurrency model" in pack_path.read_text(encoding="utf-8")


def test_routed_backend_runs_plan_and_author_on_distinct_brains(tmp_path: Path) -> None:
    """ADR-0015: a :class:`RoutedBackend` runs PASS-1 on the ``plan`` brain and PASS-2 on the
    ``author`` brain, and the worker publishes EXACTLY as for a single backend — proving routing is
    integrity-neutral (``worker.run`` never learns it is routing). The ``author`` brain carries a
    POISON plan with no dispositions; that the theme is still published, with the author brain's
    prose in its sentinel, proves ``plan()`` ran on the plan brain and ``author()`` on the author
    brain (had the acts been swapped, the empty plan would create no theme / the prose would be
    missing).
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    e2 = _write_capture(inbox, text="The inbox is append-only and per-writer.", second=11)
    _seed_raw(repo, e1, e2)

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
    poison = json.dumps({"schema_version": 1, "run_id": "x", "finished": True, "dispositions": []})
    sid = region_sentinel_id("ignored", "c1")
    plan_brain = FakeBackend(plan, prose={})  # plans the theme; writes no prose
    author_brain = FakeBackend(poison, prose={sid: "Prose authored by the dedicated author brain."})
    backend = RoutedBackend(plan_backend=plan_brain, author_backend=author_brain)

    report = _run(repo, backend)

    assert report.status == "published"
    with repo.worktree(at=report.published_commit) as published:
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        assert theme.is_file()  # the PLAN brain's plan created it
        # the AUTHOR brain filled the sentinel (not the plan brain, whose prose dict is empty)
        assert "Prose authored by the dedicated author brain." in theme.read_text(encoding="utf-8")


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
        repo,
        FakeBackend(
            plan,
            prose={
                region_sentinel_id("ignored", "c1"): "The single curator holds a per-repo flock."
            },
        ),
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
    # PASS-2 prose actually landed in the published body (run-scoped region id matched needs_prose).
    assert "per-repo flock" in theme.read_text(encoding="utf-8")
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
        repo,
        FakeBackend(
            plan,
            prose={
                region_sentinel_id("ignored", "c1"): "The single curator holds a per-repo flock."
            },
        ),
    )

    # The run is STILL published and the commit is durable in git despite the sync failure.
    assert report.status == "published"
    new_tip = report.published_commit
    assert new_tip is not None
    assert new_tip != base
    assert repo.branch_commit() == new_tip  # durable: the curated ref advanced
    # ...and what it published is a REAL note, not a placeholder (#121). The owner working copy is
    # deliberately stuck at the old tip here, so the published COMMIT is the only honest source.
    with repo.worktree(at=new_tip) as published:
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        assert "The single curator holds a per-repo flock." in theme.read_text(encoding="utf-8")
    # The stuck working copy is surfaced as an observable signal, not silently swallowed.
    assert report.counts.get("owner_working_copy_unsynced") == 1
    # ADR-0012 §2 / #26: because the working copy is UNSYNCED, the index rebuild is skipped (the
    # `not synced` short-circuit never calls rebuild_index_cache; building from the stale tree
    # would stamp new_commit onto base_commit content). Surfaced as index_cache_unbuilt, no
    # cache is written for this run (the read path full-scans until a later synced run rebuilds it).
    assert report.counts.get("index_cache_unbuilt") == 1
    assert not layout.index_notes_path().is_file()
    # ADR-0027 / #37: gold rebuild is skipped for the same reason (the `not synced` short-circuit),
    # surfaced as gold_unbuilt with no pack written (a later synced run rebuilds it).
    assert report.counts.get("gold_unbuilt") == 1
    assert not layout.gold_pack_path("default").is_file()
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
    report = _run(repo, FakeBackend(bad_plan, prose=PLAN_REJECTED_PROSE))

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
    report = _run(repo, FakeBackend(bad_plan, prose=PLAN_REJECTED_PROSE))

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


def test_merge_targeting_a_moc_is_rejected_by_validate_plan_not_crash_apply(tmp_path: Path) -> None:
    """A MERGE_INTO_THEME whose target is a MOC (not a theme) FAILS at the §4.1 PLAN gate.

    Regression for the integrity-core bug: live_basenames includes MOC/index/daily stems, so a
    MERGE/CONTEST naming the domain MOC was validate_plan-ACCEPTED but apply._resolve_target_path
    (theme_only=True) then raised ApplyError — uncaught around apply_plan — crashing the run with
    the events stuck in processing/. With the THEME-only target check the plan is rejected cleanly
    here: the run FAILS (nothing published, branch unchanged), never an uncaught APPLY traceback.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    # Run 1 (happy path): publish a theme so the domain MOC ``ai-tech-moc`` is materialized live.
    e0 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=5)
    _seed_raw(repo, e0)
    report0 = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e0),
            prose={
                region_sentinel_id("ignored", "c1"): "The single curator holds a per-repo flock."
            },
        ),
    )
    assert report0.status == "published"
    # Sanity: the MOC exists in the live tree but is NOT a theme.
    with repo.worktree(at=repo.branch_commit()) as wt:
        assert (wt / "wiki" / "ai-tech" / "ai-tech-moc.md").is_file()
        assert not (wt / "wiki" / "ai-tech" / "themes" / "ai-tech-moc.md").exists()
        # The setup publish is a real authored theme, not a placeholder one (#121) — run 2's
        # rejection has to be attributable to the MOC target, not to a half-built run 1.
        theme = wt / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        assert "The single curator holds a per-repo flock." in theme.read_text(encoding="utf-8")

    # Run 2: a MERGE_INTO_THEME whose target is the MOC basename — in live_basenames, NOT a theme.
    e1 = _write_capture(inbox, text="A claim that overlaps an existing topic.", second=10)
    _seed_raw(repo, e1)
    # Capture the curated tip AFTER seeding raw/ (which advances the branch) so the failed run's
    # "ref unchanged" assertion compares against the true pre-run base.
    pre_run_tip = repo.branch_commit()
    bad_plan = json.dumps(
        {
            "schema_version": 1,
            "run_id": "ignored",
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": "c1",
                    "event_ids": [e1],
                    "op": "MERGE_INTO_THEME",
                    "domain": "ai-tech",
                    "target_basename": "ai-tech-moc",  # a MOC, not a theme
                    "title": None,
                    "summary": "Overlaps the topic.",
                    "status": "active",
                    "tags": [],
                    "aliases": [],
                    "links": [],
                    "needs_prose": True,
                    "reason": "Merge into existing (but it's the MOC).",
                }
            ],
        }
    )
    report = _run(repo, FakeBackend(bad_plan, prose=PLAN_REJECTED_PROSE))

    # The run FAILS at the PLAN gate (BASENAME), publishing NOTHING — never an uncaught ApplyError.
    assert report.status == "failed"
    assert repo.branch_commit() == pre_run_tip  # ref unchanged by the failed run
    failed_run = layout.failed_dir / RUN_DATE / report.run_id
    error = json.loads((failed_run / "error.json").read_text(encoding="utf-8"))
    assert any("BASENAME" in c for c in error["failed_checks"])
    # Nothing stuck in processing/ (the crash symptom): the dir was cleared.
    assert not (layout.processing_dir / report.run_id).exists()


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
        last = _run(repo, FakeBackend(bad_plan_for(e1), prose=PLAN_REJECTED_PROSE))
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
    report = _run(
        repo,
        FakeBackend(plan, prose={region_sentinel_id("ignored", "c1"): "Single-writer detail."}),
    )
    assert report.status == "published"
    published_commit = report.published_commit
    assert published_commit is not None
    # The commit the crashed run is then rewound onto is a REAL publish, prose and all (#121):
    # recovery must be shown finalizing a genuine tip, not one holding a placeholder body. The
    # owner working copy is at that tip (read-after-publish), so it is the same bytes.
    theme = layout.wiki_dir / "ai-tech" / "themes" / "curator-concurrency.md"
    assert "Single-writer detail." in theme.read_text(encoding="utf-8")

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


# --- (5) HARVEST CURSOR COUNTERS (ADR-0017 §7 — curator-owned accepted/rejected) ----------------


def _write_harvested(
    inbox: Inbox, *, text: str, second: int, agent: str = "demo-agent", event_key: str | None = None
) -> str:
    """Write one GATED harvested candidate (kind=candidate, source=harvest:<agent>) to the inbox.

    Mirrors :meth:`agora_kb.harvester.harvester.Harvester._run_one`'s inbox write: a candidate-kind,
    low-confidence event in the per-connector ``harvest-<agent>`` writer namespace with
    ``source=harvest:<agent>``. The bundle flags it ``is_gated`` (kind=candidate), so the §4.1 gate
    restricts its plan op to MERGE_INTO_THEME / MARK_CONTESTED / DROP.
    """
    from agora_kb.core.models import Confidence, Kind

    now = datetime(2026, 6, 13, 2, 50, second, tzinfo=UTC)
    return inbox.write(
        text=text,
        writer=f"harvest-{agent}",
        source=f"harvest:{agent}",
        domain="ai-tech",
        kind=Kind.candidate,
        confidence=Confidence.low,
        event_key=event_key,
        now=now,
    ).id


def _write_adapters_yaml(layout: RepoLayout, *agents: str) -> None:
    """Write a minimal ``adapters.yaml`` declaring one ``file:<agent>`` connector per agent."""
    lines = ["connectors:"]
    for agent in agents:
        lines += [f"  file:{agent}:", "    path: x/*.md", "    scope: personal"]
    (layout.root / "adapters.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _merge_and_drop_plan(run_id: str, merge_event: str, drop_event: str, target: str) -> str:
    """A plan that MERGES one gated candidate into ``target`` (accepted) and DROPS the other."""
    return json.dumps(
        {
            "schema_version": 1,
            "run_id": run_id,
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": "c1",
                    "event_ids": [merge_event],
                    "op": "MERGE_INTO_THEME",
                    "domain": "ai-tech",
                    "target_basename": target,
                    "summary": "Corroborates the existing theme.",
                    "needs_prose": True,
                    "reason": "Harvested corroboration of an existing theme.",
                },
                {
                    "candidate_id": "c2",
                    "event_ids": [drop_event],
                    "op": "DROP",
                    "reason": "Harvested noise — drop.",
                },
            ],
        }
    )


def _seed_theme_and_harvested(tmp_path: Path) -> tuple[Repo, str, str]:
    """Init a repo, publish a theme (the merge target), and queue two gated harvested candidates.

    Returns ``(repo, merge_event_id, drop_event_id)`` ready for a MERGE+DROP run over the harvested
    candidates with a ``file:demo-agent`` connector configured in ``adapters.yaml``.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    # Run 1: publish a theme 'curator-concurrency' so there is a live MERGE target.
    e0 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=5)
    _seed_raw(repo, e0)
    report0 = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e0),
            prose={
                region_sentinel_id("ignored", "c1"): "The single curator holds a per-repo flock."
            },
        ),
    )
    assert report0.status == "published"
    # The MERGE target is a fully-authored theme (#121). Every caller below merges a harvested
    # candidate INTO this note, so a target published with a placeholder body would leave each of
    # them asserting cursor arithmetic over a note that never held any knowledge.
    target = repo.layout.wiki_dir / "ai-tech" / "themes" / "curator-concurrency.md"
    assert "The single curator holds a per-repo flock." in target.read_text(encoding="utf-8")

    # Configure the connector + queue two harvested gated candidates (distinct content → c1, c2).
    _write_adapters_yaml(layout, "demo-agent")
    e_merge = _write_harvested(inbox, text="Harvested: curators serialize writes.", second=10)
    e_drop = _write_harvested(inbox, text="Harvested: unrelated low-value chatter.", second=20)
    _seed_raw(repo, e_merge, e_drop)
    return repo, e_merge, e_drop


def test_harvest_cursor_accepted_rejected_bumped_after_finalize(tmp_path: Path) -> None:
    """A MERGE (accepted) + DROP (rejected) over harvested candidates bumps the §6 cursor (§7).

    End-to-end through the real curator run (FakeBackend, ZERO model): the two harvested candidates
    are gated (kind=candidate); the plan MERGES one into the live theme and DROPS the other. After
    finalize the per-connector cursor records accepted=1 / rejected=1, reconciling with proposed.
    """
    from agora_kb.harvester.harvester import CursorStore

    repo, e_merge, e_drop = _seed_theme_and_harvested(tmp_path)
    layout = repo.layout

    plan = _merge_and_drop_plan("merge-run", e_merge, e_drop, "curator-concurrency")
    report = _run(
        repo,
        FakeBackend(
            plan, prose={region_sentinel_id("merge-run", "c1"): "Harvested corroboration prose."}
        ),
    )
    assert report.status == "published"

    cursor = CursorStore(layout).load("file:demo-agent")
    assert cursor.accepted == 1
    assert cursor.rejected == 1


def test_phase3_exit_web_upload_becomes_a_queryable_curated_note(tmp_path: Path) -> None:
    """Phase-3 EXIT, end-to-end across all three web-face seams (FakeBackend, ZERO model).

    The headline exit criterion ("upload a PDF/URL in the browser → it becomes a linked wiki note;
    dashboard shows the queue") as ONE chain: a browser upload (web face, extract→Inbox.write per
    ADR-0020) → the deterministic curator publishes it as a theme → the read face (kb_query) finds
    it. Locks the criterion the per-seam tests cover only compositionally.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from agora_kb.faces.mcp_server import AgoraHandlers
    from agora_kb.faces.web import build_app

    repo = _init_repo(tmp_path)

    # 1) UPLOAD through the browser face → one web:-sourced inbox capture (queued, not searchable).
    # base_url pins the Host to loopback: the default web.security.allowed_hosts (issue #94) has
    # no `testserver` entry, and this exit-criterion chain really does POST /api/upload.
    client = TestClient(
        build_app(repo_path=tmp_path, writer="web", user="alice"), base_url="http://127.0.0.1"
    )
    resp = client.post(
        "/api/upload",
        data={
            "text": "One curator advances the curated branch under a per-repo lock.",
            "domain": "ai-tech",
        },
    )
    assert resp.status_code == 200
    receipt = resp.json()
    assert receipt["queued"] is True
    event_id = receipt["id"]
    # Eventual consistency: before curation the read face does NOT yet find it.
    assert AgoraHandlers(repo).query("curator advances the branch")["status"] == "not_found"

    # 2) CURATE the capture into a theme (deterministic worker; canned plan, zero model). _seed_raw
    # mirrors production core.ingest persisting raw/<domain>/<event_id>.md at capture time so the
    # theme sources: resolve (lint L1-8); the curator never overwrites a pre-existing raw/ artifact.
    _seed_raw(repo, event_id)
    report = _run(
        repo,
        FakeBackend(
            _create_theme_plan("exit-run", "c1", event_id),
            prose={
                region_sentinel_id("exit-run", "c1"): (
                    "The single curator serializes every wiki write behind one flock."
                )
            },
        ),
    )
    assert report.status == "published"

    # 3) the uploaded knowledge is now QUERYABLE through the read face, as a real linked wiki note.
    result = AgoraHandlers(repo).query("curator advances the branch")
    assert result["status"] == "ok"
    assert result["hits"]
    assert any(hit["path"].endswith("curator-concurrency.md") for hit in result["hits"])
    # The exit criterion says "becomes a linked wiki NOTE", so the note has to hold the prose PASS 2
    # authored. Keyed bare, this whole chain ended on APPLY's `_summary pending_` placeholder and
    # the query hit above was the only thing anyone checked (#121).
    theme = repo.layout.wiki_dir / "ai-tech" / "themes" / "curator-concurrency.md"
    assert "serializes every wiki write behind one flock" in theme.read_text(encoding="utf-8")


def test_harvest_cursor_not_bumped_for_unconfigured_connector(tmp_path: Path) -> None:
    """With no ``adapters.yaml`` connector for the agent, the run writes NO cursor (no stray)."""
    from agora_kb.harvester.harvester import CursorStore

    repo, e_merge, e_drop = _seed_theme_and_harvested(tmp_path)
    layout = repo.layout
    # Remove the connector config so the harvested agent maps to no configured connector.
    (layout.root / "adapters.yaml").unlink()

    plan = _merge_and_drop_plan("merge-run", e_merge, e_drop, "curator-concurrency")
    report = _run(
        repo,
        FakeBackend(
            plan, prose={region_sentinel_id("merge-run", "c1"): "Harvested corroboration prose."}
        ),
    )
    assert report.status == "published"

    # No stray cursor file was created for the unconfigured connector.
    assert not layout.harvest_cursor_path("file:demo-agent").exists()
    # A fresh load is the zero cursor (nothing bumped).
    cursor = CursorStore(layout).load("file:demo-agent")
    assert cursor.accepted == 0 and cursor.rejected == 0


def test_harvest_cursor_increment_is_not_double_counted_on_recovery(tmp_path: Path) -> None:
    """RECOVERY does NOT re-bump the cursor — the increment is happy-path-only (mirrors counters).

    After the happy-path run bumps accepted=1/rejected=1, we simulate a crash at phase=published
    (ref advanced, manifest still 'published') over the SAME run id and drive recover(). Because
    _finalize_recovered NEVER touches the cursor (exactly like state.counters), the cursor stays at
    the single happy-path increment — no double count.
    """
    from agora_kb.harvester.harvester import CursorStore

    repo, e_merge, e_drop = _seed_theme_and_harvested(tmp_path)
    layout = repo.layout

    plan = _merge_and_drop_plan("merge-run", e_merge, e_drop, "curator-concurrency")
    report = _run(
        repo,
        FakeBackend(
            plan, prose={region_sentinel_id("merge-run", "c1"): "Harvested corroboration prose."}
        ),
    )
    assert report.status == "published"
    assert report.run_id is not None

    cursor_after_run = CursorStore(layout).load("file:demo-agent")
    assert cursor_after_run.accepted == 1 and cursor_after_run.rejected == 1

    # Simulate a published-but-unfinalized crash for a SEPARATE run id whose events are harvested,
    # then drive recovery. _finalize_recovered must NOT bump the cursor (no plan in scope; counters
    # and cursor are both rebuildable + happy-path-only). We hand-place a phase=published manifest
    # whose published_commit is the curated tip, with a claimed harvested event.
    published_commit = repo.branch_commit()
    inbox = Inbox(layout)
    e_extra = _write_harvested(inbox, text="Harvested: a crashed-run fact.", second=40)
    crashed_run_id = new_event_id(now=datetime(2026, 6, 13, 5, 0, 0, tzinfo=UTC))
    events_dir = layout.processing_dir / crashed_run_id / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    os.replace(layout.inbox_item_path("harvest-demo-agent", e_extra), events_dir / f"{e_extra}.md")
    write_manifest(
        layout,
        RunManifest(
            run_id=crashed_run_id,
            base_commit=published_commit,
            event_ids=(e_extra,),
            phase="published",
            prose_complete=True,
            published_commit=published_commit,
            started="2026-06-13T05:00:00Z",
        ),
    )

    recover(repo, state_store=StateStore(layout))

    # The cursor is UNCHANGED by recovery: still the single happy-path increment, never re-bumped.
    cursor_after_recover = CursorStore(layout).load("file:demo-agent")
    assert cursor_after_recover.accepted == 1
    assert cursor_after_recover.rejected == 1


def test_harvest_cursor_io_error_does_not_abort_an_already_published_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cursor IO error MUST NOT abort an already-published run (ADR-0017 §7, best-effort).

    The cursor bump runs AFTER the CAS (the run is durable in git) but BEFORE state is saved, events
    move to processed/, and the manifest finalizes. Unlike _bump_counters (pure in-memory), the
    cursor write does disk IO and CAN raise OSError (ENOSPC/EACCES/read-only FS). The worker must
    degrade to under-count (rebuildable) — NOT propagate the OSError and crash finalize. We force
    _apply_harvest_cursor_deltas to raise and assert the publish still completes end-to-end.
    """
    from agora_kb import curator
    from agora_kb.harvester.harvester import CursorStore

    repo, e_merge, e_drop = _seed_theme_and_harvested(tmp_path)
    layout = repo.layout

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(curator.worker, "_apply_harvest_cursor_deltas", _boom)

    plan = _merge_and_drop_plan("merge-run", e_merge, e_drop, "curator-concurrency")
    report = _run(
        repo,
        FakeBackend(
            plan, prose={region_sentinel_id("merge-run", "c1"): "Harvested corroboration prose."}
        ),
    )

    # The publish is unperturbed: status published, the diff is durable in git.
    assert report.status == "published"
    assert report.published_commit is not None
    new_tip = repo.branch_commit()
    assert new_tip == report.published_commit

    # state_store.save ran (the cursor failure happened BEFORE it but did not abort it).
    state = StateStore(layout).load()
    assert state.published_runs[report.run_id] == new_tip

    # Events moved to processed/ and the manifest reached finalized — finalize was NOT aborted.
    processed = layout.processed_dir / RUN_DATE
    assert (processed / f"{e_merge}.md").is_file()
    assert (processed / f"{e_drop}.md").is_file()
    assert read_manifest(manifest_path(layout, report.run_id)).phase == "finalized"

    # The cursor is left under-counted (rebuildable) — the IO error degraded, it did not corrupt.
    cursor = CursorStore(layout).load("file:demo-agent")
    assert cursor.accepted == 0
    assert cursor.rejected == 0


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

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],  # noqa: ARG002 — out-of-region fake ignores grounding
    ) -> None:
        for rel_path in needs_prose:
            path = worktree / rel_path
            text = path.read_text(encoding="utf-8")
            # Tamper with frontmatter (owned by APPLY) — an out-of-region edit §4.2 must reject.
            path.write_text(text.replace("status: active", "status: active\nrogue: 1"), "utf-8")


class _StrayLinkBackend(FakeBackend):
    """A :class:`FakeBackend` whose prose embeds a stray ``[[X]]`` not in the plan links (§4.6)."""


class _OffAllowlistBackend(FakeBackend):
    """A :class:`FakeBackend` that ALSO writes a file outside the allowlist + one under scratch."""

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],
    ) -> None:
        super().author(worktree, needs_prose, context)
        # A file OUTSIDE the canonical allowlist (the final-diff gate must reject the run).
        (worktree / "_templates").mkdir(parents=True, exist_ok=True)
        (worktree / "_templates" / "evil.md").write_text("planted\n", encoding="utf-8")
        # A file under the git-ignored scratch dir (must produce ZERO tracked changes).
        (worktree / "_agora_scratch").mkdir(parents=True, exist_ok=True)
        (worktree / "_agora_scratch" / "plan.json").write_text("{}\n", encoding="utf-8")


class _ScratchOnlyBackend(FakeBackend):
    """A :class:`FakeBackend` that also writes under ``_agora_scratch/`` (publish must survive)."""

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],
    ) -> None:
        super().author(worktree, needs_prose, context)
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

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],
    ) -> None:
        super().author(worktree, needs_prose, context)
        forged = worktree / self._forge_ref
        forged.parent.mkdir(parents=True, exist_ok=True)
        forged.write_text(
            "FORGED by the brain — verification baseline corrupted\n", encoding="utf-8"
        )
        planted = worktree / self._plant_ref
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text("planted by brain\n", encoding="utf-8")


# --- (5) §4.2 AUTHOR degrade-or-publish ---------------------------------------------------------


def test_author_failure_degrades_prose_but_run_still_publishes(
    tmp_path: Path, prose_pending_is_the_point: None
) -> None:
    """The §4.2 degrade path DELIBERATELY publishes a placeholder body — asserted below."""
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
        start, end = body_sentinels(region_sentinel_id("ignored", "c1"))
        region = body[body.find(start) + len(start) : body.find(end)]
        assert region.strip() == "> _summary pending_"
        assert lint(RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE).ok

    # prose_complete is False on the finalized manifest (the degrade path).
    manifest = read_manifest(manifest_path(layout, report.run_id))
    assert manifest.phase == "finalized"
    assert manifest.prose_complete is False


class _SilentBackend(FakeBackend):
    """A brain whose PASS 2 does NOTHING — the shape of a backend that could not start.

    Exactly what bwrap produced on Linux when ``execvp`` failed for every region (#115): zero bytes
    written, zero changed files, and — before the fix — zero complaints from the engine.
    """

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],
    ) -> list[str]:
        return ["AUTHOR wiki/x.md [c1]: backend 'stub' exited 1: bwrap: execvp …: No such file"]


def test_author_that_writes_nothing_publishes_but_reports_prose_pending(
    tmp_path: Path, prose_pending_is_the_point: None
) -> None:
    """A PASS 2 that authors NOTHING must not be reported as an unqualified success (#115).

    The §4.2 validator only ever saw CHANGED files, so a backend that wrote nothing produced an
    empty diff, no validation errors, ``prose_complete=True`` and ``status: published`` — while
    every body on disk was still APPLY's ``_summary pending_`` placeholder. On Linux that ran for a
    whole KB without a single error line. The run still publishes (the structure is valid and
    APPLY-owned), but it now says so: ``prose_pending`` on the report, the backend's own stderr in
    ``warnings``, and ``prose_complete=False`` on the manifest.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    plan = _create_theme_plan("ignored", "c1", e1)

    report = _run(repo, _SilentBackend(plan))

    assert report.status == "published"  # structure is valid — the publish is still correct
    assert report.counts["prose_regions"] == 1
    assert report.counts["prose_pending"] == 1
    assert any("PROSE PENDING" in w for w in report.warnings)
    # The backend's own diagnostic rides along, so the operator sees WHY (not just THAT).
    assert any("execvp" in w for w in report.warnings)
    assert read_manifest(manifest_path(layout, report.run_id)).prose_complete is False
    with repo.worktree(at=report.published_commit) as published:  # type: ignore[arg-type]
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        assert "_summary pending_" in theme.read_text(encoding="utf-8")
        assert lint(RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE).ok


def test_unauthored_regions_grades_each_region_not_each_file() -> None:
    """``_unauthored_regions`` is REGION-granular: a partly-authored note is partly pending.

    The per-FILE ``changed`` list cannot express "2 of 3 regions came back empty" — the file
    changed, so the whole note counted as authored. Placeholder bodies (both APPLY's fill and the
    §4.2 reset form) count as unauthored even when the file's bytes moved.
    """
    start_a, end_a = body_sentinels("a")
    start_b, end_b = body_sentinels("b")
    head = "---\ntitle: T\n---\n"
    old = (
        f"{head}\n{start_a}\n_summary pending_\n{end_a}\n\n{start_b}\n_summary pending_\n{end_b}\n"
    )
    new = (
        f"{head}\n{start_a}\nReal prose landed here.\n{end_a}\n"
        f"\n{start_b}\n> _summary pending_\n{end_b}\n"
    )
    needs = {"wiki/d/themes/n.md": ["a", "b"]}

    pending = _unauthored_regions(needs, {"wiki/d/themes/n.md": old}, {"wiki/d/themes/n.md": new})

    assert pending == [("wiki/d/themes/n.md", "b")]


def test_unauthored_regions_flags_an_untouched_note() -> None:
    """Byte-identical old/new (the backend never ran) is the #115 case: every region is pending."""
    start, end = body_sentinels("c1")
    text = f"---\ntitle: T\n---\n\n{start}\n_summary pending_\n{end}\n"
    needs = {"wiki/d/themes/n.md": ["c1"]}

    pending = _unauthored_regions(needs, {"wiki/d/themes/n.md": text}, {"wiki/d/themes/n.md": text})

    assert pending == [("wiki/d/themes/n.md", "c1")]


def test_stray_wikilink_is_stripped_and_run_publishes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)

    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    plan = _create_theme_plan("ignored", "c1", e1)
    # PASS 2 emits prose containing a stray [[ghost]] not in the plan links — §4.6 strips it
    # (delimiters removed, inner text kept) so the otherwise-good pass publishes, NOT degrades.
    backend = _StrayLinkBackend(
        plan, prose={region_sentinel_id("ignored", "c1"): "See [[ghost]] for the flock detail."}
    )

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


class _VandalBackend(FakeBackend):
    """Authors its region, then ALSO rewrites an unrelated ``wiki/`` note (body + frontmatter)."""

    def __init__(self, *args: object, victim: str, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._victim = victim

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],
    ) -> None:
        super().author(worktree, needs_prose, context)
        note = worktree / self._victim
        text = note.read_text(encoding="utf-8")
        text = text.replace(_VICTIM_SENTENCE, "TAMPERED BY PASS 2 — never named by the plan.")
        note.write_text(text.replace("status: active", "status: deprecated"), encoding="utf-8")


class _CoveringDeleteBackend(FakeBackend):
    """Authors its region, then DELETES an unrelated note AND scrubs every MOC reference to it."""

    def __init__(self, *args: object, victim: str, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._victim = victim

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],
    ) -> None:
        super().author(worktree, needs_prose, context)
        (worktree / self._victim).unlink()
        stem = Path(self._victim).stem
        for moc in (worktree / "wiki").rglob("*-moc.md"):
            kept = [
                ln
                for ln in moc.read_text(encoding="utf-8").splitlines(keepends=True)
                if stem not in ln
            ]
            moc.write_text("".join(kept), encoding="utf-8")


_VICTIM_SENTENCE = "The original, human-trusted sentence that must not change."
_VICTIM_REL = "wiki/ai-tech/themes/victim-note.md"
_VICTIM_TEXT = f"""---
title: Victim note
type: theme
created: 2026-06-12
updated: 2026-06-12
status: active
summary: A pre-existing curated note the plan never mentions.
description: A pre-existing curated note the plan never mentions.
timestamp: 2026-06-12T00:00:00Z
tags: [curator]
aliases: []
related: []
sources: [raw/ai-tech/victim-src.md]
---

# Victim note

{_VICTIM_SENTENCE}
"""


def _repo_with_a_bystander_note(tmp_path: Path) -> tuple[Repo, str, str, dict[str, str]]:
    """A repo carrying one lint-clean curated note that this run's plan never mentions."""
    repo = _init_repo(tmp_path)
    src = repo.root / "raw" / "ai-tech" / "victim-src.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("raw source for the victim note\n", encoding="utf-8")
    victim = repo.root / _VICTIM_REL
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text(_VICTIM_TEXT, encoding="utf-8")
    _commit_all(repo, "chore: plant a pre-existing curated note")

    e1 = _write_capture(Inbox(repo.layout), text="One curator advances the branch.", second=10)
    _seed_raw(repo, e1)
    base = repo.head_commit()
    plan = _create_theme_plan("bystander-run", "c1", e1)
    prose = {region_sentinel_id("bystander-run", "c1"): "A legitimately authored region."}
    return repo, base, plan, prose


def test_pass2_cannot_tamper_with_a_note_the_plan_never_named(
    tmp_path: Path, prose_pending_is_the_point: None
) -> None:
    """PASS 2 may not edit a ``wiki/`` note outside ``needs_prose`` — body OR frontmatter.

    Regression lock for the §4.2 scope hole: the changed set was derived from ``needs_prose``, so it
    was a SUBSET of ``sentinels`` and ``validate_author_diff``'s "path not in sentinels" rejection
    could never fire in production, while §4.0 admits the whole ``wiki/`` prefix. A backend could
    therefore rewrite any bystander note's prose and flip its frontmatter ``status``, and the run
    PUBLISHED with ``failure=None``. The capture is not at fault, so the run still publishes (§4.2
    degrades) — but the bystander must come back byte-identical.
    """
    repo, _base, plan, prose = _repo_with_a_bystander_note(tmp_path)

    report = _run(repo, _VandalBackend(plan, prose=prose, victim=_VICTIM_REL))

    assert report.status == "published"
    published = (repo.root / _VICTIM_REL).read_text(encoding="utf-8")
    assert published == _VICTIM_TEXT  # byte-identical: body AND frontmatter restored
    assert "TAMPERED BY PASS 2" not in published
    assert "status: deprecated" not in published


def test_pass2_cannot_delete_a_note_while_scrubbing_its_moc_references(
    tmp_path: Path, prose_pending_is_the_point: None
) -> None:
    """A PASS-2 delete that covers its tracks must not lose the note.

    The naive delete was caught only by ACCIDENT — ``LINT L1-2 broken link`` on the MOC — so a
    backend that also scrubbed the MOC bullet published a tree with the note permanently gone. The
    §4.2 scope check is the real net; lint never had to be the one holding it.
    """
    repo, _base, plan, prose = _repo_with_a_bystander_note(tmp_path)

    report = _run(repo, _CoveringDeleteBackend(plan, prose=prose, victim=_VICTIM_REL))

    assert report.status == "published"
    assert (repo.root / _VICTIM_REL).is_file()
    assert (repo.root / _VICTIM_REL).read_text(encoding="utf-8") == _VICTIM_TEXT


# --- (6) final-diff allowlist gate + _agora_scratch/ gitignore ----------------------------------


def test_off_allowlist_file_is_rejected_by_final_diff_gate(
    tmp_path: Path, prose_pending_is_the_point: None
) -> None:
    # The planted `_templates/evil.md` is now VISIBLE to the §4.2 AUTHOR-diff too (the changed set
    # is the real git diff against the post-APPLY baseline, not just `needs_prose`), so PASS 2 is
    # rejected as a whole and every region degrades to the placeholder before §4.0 fails the run.
    # The prose pending here is that rejection, declared out loud — the assertion under test is
    # still that FINAL-DIFF, not §4.2, is what fails the run and keeps the branch unmoved.
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
    # The prose is keyed run-scoped so PASS 2 genuinely authors the region (#121): the rejection
    # under test must be attributable to the planted path ALONE, not to a pass that wrote nothing.
    report = _run(
        repo,
        _OffAllowlistBackend(
            plan, prose={region_sentinel_id("ignored", "c1"): "A legitimately authored region."}
        ),
    )

    assert report.status == "failed"
    assert repo.branch_commit() == base  # nothing published
    # The error record names the FINAL-DIFF check and points at the off-allowlist path.
    error_files = list((layout.failed_dir).rglob("error.json"))
    assert error_files
    checks = json.loads(error_files[0].read_text(encoding="utf-8"))["failed_checks"]
    assert any("FINAL-DIFF" in c and "_templates" in c for c in checks)
    # The scratch file did NOT cause a tracked change (it is git-ignored), so it is not a violation.
    assert not any("_agora_scratch" in c for c in checks)


def test_brain_cannot_forge_or_plant_raw_during_pass2(
    tmp_path: Path, prose_pending_is_the_point: None
) -> None:
    """A PASS-2 backend that overwrites/plants ``raw/`` is rejected by the final-diff gate (D3).

    The blocker the loosened ``raw/``-prefix allowlist re-introduced: the brain can never write
    ``raw/`` (ADR-0010 D3 / the Karpathy immutable-verification-baseline guarantee). The §4.2
    AUTHOR-diff now DOES see such a write — its changed set is the real git diff against the
    post-APPLY baseline, so a ``raw/`` path reaches check 1 and degrades the prose pass — but §4.2
    only ever degrades, and ``_restore_out_of_scope`` deliberately declines to sanitize a path
    outside the §4.0 allowlist. So the final-diff gate remains the enforcer that FAILS the run: it
    admits ONLY the EXACT engine-written paths-with-content (``apply_plan``'s ``raw_writes``), and a
    PASS-2 OVERWRITE of the materialized source (same path, forged bytes) AND a PLANTED new ``raw/``
    file both fall through to the off-allowlist rejection. The run must FAIL, the branch must NOT
    move, and the error must name the FINAL-DIFF check on a ``raw/`` path.
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
    # Run-scoped key so "beyond authoring legit prose" in the fixture's docstring is TRUE (#121):
    # the forge/plant must be rejected even on a pass whose in-region work was otherwise valid.
    backend = _RawForgingBackend(
        plan,
        forge_ref=forge_ref,
        plant_ref=plant_ref,
        prose={region_sentinel_id("ignored", "c1"): "legit prose"},
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
    report = _run(
        repo,
        _ScratchOnlyBackend(
            plan, prose={region_sentinel_id("ignored", "c1"): "A legitimately authored region."}
        ),
    )

    assert report.status == "published"
    with repo.worktree(at=report.published_commit) as published:  # type: ignore[arg-type]
        # The scratch dir is NOT part of the published tree.
        assert not (published / "_agora_scratch").exists()
        # ...and the "legit prose PLUS scratch" this test names really is legit: the region the
        # backend authored is in the published body. Keyed bare, it never was (#121).
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        assert "A legitimately authored region." in theme.read_text(encoding="utf-8")


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

    # PASS 2 runs and succeeds here (run-scoped key, #121) — the discard under test is the LINT
    # gate's alone, so the prose pass must not be silently failing underneath it.
    report = _run(
        repo, FakeBackend(plan, prose={region_sentinel_id("ignored", "c1"): "Authored detail."})
    )

    assert report.status == "failed"
    assert repo.branch_commit() == base  # the load-bearing guarantee: discard-after-commit
    error_files = list(layout.failed_dir.rglob("error.json"))
    assert error_files
    checks = json.loads(error_files[0].read_text(encoding="utf-8"))["failed_checks"]
    assert any(c.startswith("LINT") for c in checks)


def test_lint_failure_reasons_carry_errors_only_not_l2_warnings(tmp_path: Path) -> None:
    """A WARNING is not a failed check — and since #119 there can be unboundedly many of them.

    L2-6 fires once per note still carrying a stale ``body_status``, which is EVERY note on a repo
    published by a pre-#119 build. ``lint`` sorts findings by ``(path, code)``, so passing warnings
    into ``_fail(reasons=...)`` would push the real error behind them on both truncating operator
    surfaces: ``RunFailure.summary(limit=3)`` (the ``failed_checks:`` line ``agora curate`` and the
    ``agora watch`` tick print) and ``LastFailure``, which keeps only ``MAX_FAILURE_REASONS``.

    The legacy notes here are planted directly on the branch — exactly the shape an upgrade leaves
    behind — and are alphabetically BEFORE the note that actually fails, so a regression that lets
    warnings through is caught by the ordering rather than by luck.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    themes = layout.root / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        sid = f"2026-06-13T03-00-00.000Z--legacy--c{i}"
        (themes / f"aa-legacy-{i}.md").write_text(
            "---\n"
            f"title: Legacy {i}\ntype: theme\ndomain: ai-tech\ntags: []\naliases: []\n"
            "created: 2026-06-12\nupdated: 2026-06-12\nstatus: stub\n"
            f"summary: A legacy note {i}.\ndescription: A legacy note {i}.\n"
            "sources: []\nrelated: []\nconfidence: high\n"
            # `status: stub` keeps the note L1-7-exempt (a non-stub theme needs non-empty
            # `sources:`), so L2-6 is the ONLY finding these notes contribute — which is what makes
            # the ordering assertion below meaningful rather than accidental.
            "body_status: pending\n"  # the stale flag a pre-#119 build left behind
            "---\n\n"
            f"# Legacy {i}\n\n"
            f"<!-- agora:body:start id={sid} -->\n"
            "Authored prose, so L2-6 fires: the flag survives over a body with no pending region.\n"
            f"<!-- agora:body:end id={sid} -->\n",
            encoding="utf-8",
        )
    repo.commit_all("plant legacy prose-pending notes", when=datetime(2026, 6, 12, tzinfo=UTC))

    lint_before = lint(layout, taxonomy=TAXONOMY, run_date=RUN_DATE)
    stale = [f for f in lint_before.findings if f.code == "L2-6"]
    assert len(stale) == 4, "precondition: every planted note must emit its own L2-6 warning"
    assert lint_before.ok, "L2-6 is a warning — it must never flip the gate"

    # Now make the run fail for a REAL reason: a cited source that does not exist (L1-8, error).
    e1 = inbox.write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        raw_ref="raw/ai-tech/missing-upload.md",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    ).id
    report = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e1),
            prose={region_sentinel_id("ignored", "c1"): "Authored detail."},
        ),
    )

    assert report.status == "failed"
    checks = json.loads(next(layout.failed_dir.rglob("error.json")).read_text(encoding="utf-8"))[
        "failed_checks"
    ]
    assert checks, "the failure must still say why it failed"
    assert not any("L2-6" in c for c in checks), f"warnings leaked into failed_checks: {checks}"
    assert all(c.startswith("LINT L1") for c in checks)
    # The operator-facing one-liner is the surface that actually truncates.
    assert "L2-6" not in report.failure.summary()


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

    report = _run(
        repo, FakeBackend(plan, prose={region_sentinel_id("ignored", "c1"): "Authored detail."})
    )

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

    def author(
        self,
        worktree: Path,  # noqa: ARG002
        needs_prose: dict[str, list[str]],  # noqa: ARG002
        context: dict[str, AuthorRegion],  # noqa: ARG002
    ) -> None:
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

    report = _run(
        repo,
        FakeBackend(plan, prose={region_sentinel_id("ignored", "c1"): "One flock, one writer."}),
    )
    assert report.status == "published"
    # A real publish with a real body — the dedup below is only meaningful if the FIRST delivery
    # actually became knowledge rather than a placeholder note (#121).
    theme = layout.wiki_dir / "ai-tech" / "themes" / "curator-concurrency.md"
    assert "One flock, one writer." in theme.read_text(encoding="utf-8")

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
        needs, sentinels, context = _needs_prose_map(
            plan_default, wt, RUN_DATE, {"c1": "Daily capture source facts."}
        )
        key = f"wiki/ai-tech/daily/ai-tech-{RUN_DATE}.md"
        # The PERSISTED region id is run-scoped ({plan.run_id}--{candidate_id}); plan.run_id == "r".
        region_id = region_sentinel_id("r", "c1")
        assert needs == {key: [region_id]}
        assert sentinels == {key: {region_id}}
        # The §8.2 context carries the candidate's verbatim source + op keyed by the run-scoped id.
        assert context == {
            region_id: AuthorRegion(
                op="APPEND_DAILY",
                title=None,
                summary="s",
                source_text="Daily capture source facts.",
            )
        }


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
    report = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e1),
            prose={region_sentinel_id("ignored", "c1"): "One curator holds a per-repo flock."},
        ),
    )
    tip = report.published_commit
    assert tip is not None
    # A genuine authored publish (#121) — the git-ref recovery below is finalizing a real tip.
    theme = layout.wiki_dir / "ai-tech" / "themes" / "curator-concurrency.md"
    assert "One curator holds a per-repo flock." in theme.read_text(encoding="utf-8")

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
            _run(
                repo,
                FakeBackend(
                    plan,
                    prose={region_sentinel_id("ignored", "c1"): "One curator holds a flock."},
                ),
            )
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
    # The commit that landed before the crash holds the authored note, and recovery re-published
    # nothing on top of it: the prose appears EXACTLY once. Keyed bare, the body was a placeholder
    # and "no double publish" was asserted only over the ref (#121).
    with repo.worktree(at=tip) as published:
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        assert theme.read_text(encoding="utf-8").count("One curator holds a flock.") == 1


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
        repo,
        FakeBackend(
            plan,
            prose={
                region_sentinel_id("ignored", "c1"): "The single curator holds a per-repo flock."
            },
        ),
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
        theme_text = theme.read_text(encoding="utf-8")
        fm, _ = frontmatter.parse(theme_text)
        assert fm["sources"] == [f"raw/ai-tech/{e1}.md"]
        # PASS-2 prose actually landed (run-scoped region id matched the needs_prose instruction).
        assert "per-repo flock" in theme_text
        result = lint(
            RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=report.run_id
        )
        assert result.ok, [f for f in result.findings]


# --- max_candidates_per_run cap e2e (INGEST-CONTRACT §1.3, ADR-0024 OD-3a / #60) ----------------


class _DropAllBackend:
    """A :class:`Backend` whose PASS-1 reads the REAL ``bundle/candidates.json`` and DROPs all.

    Unlike :class:`FakeBackend` (a canned plan), this fake derives its plan from the bundle the
    worker actually materialized — the SAME read surface ``run_plan`` (ollama_brain PASS-1)
    consumes — and records each run's candidate count, so the claim cap's effect on the REAL
    PASS-1 input size is assertable end-to-end. DROP needs no prose and no raw/ seed.
    """

    def __init__(self) -> None:
        self.bundle_sizes: list[int] = []

    def plan(self, bundle_dir: Path) -> str:
        doc = json.loads((bundle_dir / "candidates.json").read_text(encoding="utf-8"))
        cands = doc["candidates"]
        self.bundle_sizes.append(len(cands))
        dispositions = [
            {
                "candidate_id": c["candidate_id"],
                "event_ids": [p["event_id"] for p in c["provenance"]],
                "op": "DROP",
                "needs_prose": False,
                "reason": "cap e2e",
            }
            for c in cands
        ]
        return json.dumps(
            {
                "schema_version": 1,
                "run_id": doc["run_id"],
                "finished": True,
                "dispositions": dispositions,
            }
        )

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],
    ) -> None:  # pragma: no cover — a DROP-only plan never reaches PASS 2
        raise AssertionError("a DROP-only plan never needs prose")


def test_cap_bounds_bundle_and_drains_backlog_fifo(tmp_path: Path) -> None:
    """(#60 e2e) An over-cap backlog is consumed in capped FIFO slices across successive runs:
    each run's PLAN pass sees at most ``max_candidates`` candidates in the REAL bundle, the
    remainder stays in the inbox for the next trigger, and the batch shape is observable on the
    RunReport counts + ``state.json`` ``last_batch``."""
    from agora_kb.core.state import LastBatch

    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    for i in range(5):
        _write_capture(inbox, text=f"distinct fact {i}", second=10 + i)

    backend = _DropAllBackend()
    store = StateStore(layout)

    def _capped_run(minute: int) -> RunReport:
        return run(
            repo,
            backend=backend,
            state_store=store,
            now=datetime(2026, 6, 13, 3, minute, 0, tzinfo=UTC),
            taxonomy=TAXONOMY,
            max_candidates=2,
        )

    # Run 1: exactly the 2-candidate FIFO head is claimed/bundled; 3 events stay queued.
    report1 = _capped_run(0)
    assert report1.status == "published"
    assert backend.bundle_sizes == [2]
    assert report1.counts["claimed"] == 2
    assert report1.counts["candidates"] == 2
    assert report1.counts["inbox_remaining"] == 3
    assert report1.counts["DROP"] == 2
    assert inbox.depth() == 3
    # The batch shape is persisted for the dashboard/metrics (#60 observability).
    assert store.load().last_batch == LastBatch(claimed=2, candidates=2, cap=2, inbox_remaining=3)

    # Runs 2-3: the next triggers naturally continue FIFO over the remainder (2 then 1).
    report2 = _capped_run(10)
    assert report2.status == "published"
    assert backend.bundle_sizes == [2, 2]
    assert report2.counts["inbox_remaining"] == 1
    report3 = _capped_run(20)
    assert report3.status == "published"
    assert backend.bundle_sizes == [2, 2, 1]
    assert report3.counts["claimed"] == 1
    assert report3.counts["inbox_remaining"] == 0
    assert inbox.depth() == 0
    assert store.load().last_batch == LastBatch(claimed=1, candidates=1, cap=2, inbox_remaining=0)

    # Run 4: the backlog is drained — a plain noop, no phantom fourth bundle.
    report4 = _capped_run(30)
    assert report4.status == "noop"
    assert report4.counts == {"claimed": 0}
    assert backend.bundle_sizes == [2, 2, 1]


def test_default_cap_leaves_small_run_report_shape_intact(tmp_path: Path) -> None:
    """(#60) With no explicit cap (default 32) a small inbox claims whole — and the new
    observability keys report the uncapped shape on the happy path."""
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    backend = FakeBackend(
        _create_theme_plan("ignored", "c1", e1),
        prose={region_sentinel_id("ignored", "c1"): "The single curator holds a per-repo flock."},
    )

    report = _run(repo, backend)

    assert report.status == "published"
    assert report.counts["claimed"] == 1
    assert report.counts["candidates"] == 1
    assert report.counts["inbox_remaining"] == 0
    from agora_kb.core.state import LastBatch

    assert StateStore(repo.layout).load().last_batch == LastBatch(
        claimed=1, candidates=1, cap=32, inbox_remaining=0
    )


def test_recovery_clears_stale_last_batch_when_state_save_never_landed(tmp_path: Path) -> None:
    """(#60) ``last_batch`` is a point-in-time label for the LAST published run. A crash in the
    CAS-success window (ref advanced, state never saved) leaves the PREVIOUS run's shape in
    ``state.json``; recovery cannot recompute the crashed run's shape (candidates/cap went down
    with the process), so ``_finalize_recovered`` must CLEAR ``last_batch`` — the gauges then omit,
    exactly like never-run — instead of labeling run N-1's shape as run N's. A re-walked
    already-finalized manifest (``is_published``) keeps the recorded value."""
    from agora_kb.core.state import LastBatch

    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    # Run 1 publishes normally -> last_batch records run 1's shape.
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    r1 = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e1),
            prose={region_sentinel_id("ignored", "c1"): "One curator holds a per-repo flock."},
        ),
    )
    assert r1.status == "published"
    shape1 = LastBatch(claimed=1, candidates=1, cap=32, inbox_remaining=0)
    assert StateStore(layout).load().last_batch == shape1

    # recover() re-walks run 1's lingering FINALIZED manifest: is_published -> the recorded
    # last_batch SURVIVES (a normal restart never wipes the observability signal).
    recover(repo, state_store=StateStore(layout))
    assert StateStore(layout).load().last_batch == shape1

    # Run 2 crashes RIGHT AFTER the CAS lands (same interposition as the double-publish test):
    # manifest 'applied' + published_commit recorded, state (incl. last_batch) never saved.
    _write_capture(inbox, text="A second distinct fact.", second=20)
    real_advance = worker_mod._advance

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

    worker_mod._advance = faulting_advance  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            _run(repo, _DropAllBackend(), now=datetime(2026, 6, 13, 4, 0, 0, tzinfo=UTC))
    finally:
        worker_mod._advance = real_advance  # type: ignore[assignment]

    # Recovery finalizes run 2: last_commit points at the recovered run, but its batch shape is
    # unknowable -> last_batch is CLEARED, not left as run 1's shape mislabeled as run 2's.
    reports = recover(repo, state_store=StateStore(layout))
    assert any(r.status == "recovered" for r in reports)
    state = StateStore(layout).load()
    assert state.last_commit == repo.branch_commit()
    assert state.last_batch is None


# --- (17) #96: the failure vehicle on RunReport + the state.json failure fields ------------------

NOW2 = datetime(2026, 6, 13, 3, 1, 0, tzinfo=UTC)


def _bad_domain_plan(event_id: str) -> str:
    """A plan naming a domain OUTSIDE the taxonomy — the §4.1 TAXONOMY rejection (a PLAN failure).

    Fails BEFORE apply, so the run's phase stays ``claimed`` and (at the default max_attempts=3)
    the event returns to ``inbox/`` — the NON-TERMINAL failure #96 exists to make visible.
    """
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


def _lint_failure_event(inbox: Inbox) -> str:
    """Write a capture whose cited raw/ source will NOT exist, so §4.4 LINT fails AFTER apply.

    The explicit ``raw_ref`` points at an uploaded file the engine does not materialize (only
    free-text captures are written from the body, ADR-0010 D3), so APPLY cites a source that lint
    L1-8 cannot resolve — a real APPLY + commit happen first, then the whole diff is discarded.
    """
    return inbox.write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        raw_ref="raw/ai-tech/missing-upload.md",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    ).id


class _HugeStderrPlanBackend:
    """A backend whose PASS 1 dies with a multi-KILOBYTE multi-line message.

    The shape of a real dead brain: ``SubprocessBackend.plan`` embeds the child's stderr VERBATIM
    with no cap, so this is exactly what reaches ``_fail``'s ``reasons`` in production.
    """

    def __init__(self, blob: str) -> None:
        self._blob = blob

    def plan(self, bundle_dir: Path) -> str:  # noqa: ARG002
        from agora_kb.curator.subprocess_backend import BackendUnavailableError

        raise BackendUnavailableError(self._blob)

    def author(
        self,
        worktree: Path,  # noqa: ARG002
        needs_prose: dict[str, list[str]],  # noqa: ARG002
        context: dict[str, AuthorRegion],  # noqa: ARG002
    ) -> None:
        raise AssertionError("author must never be reached when PASS 1 fails to run")


def test_run_failure_summary_shape() -> None:
    """(#96) The ONE elision renderer: first 3 checks, then ``… +N more``, each clipped at 140.

    Pure unit — it lives on the report (not in a face) precisely so `agora curate`, the `agora
    watch` tick and MCP emit the SAME bytes for the same failure.
    """
    five = RunFailure(run_id="r", phase="claimed", reasons=("a", "b", "c", "d", "e"))
    assert five.summary() == "a | b | c | … +2 more"

    assert RunFailure(run_id="r", phase="claimed").summary() == "-"  # no reasons ⇒ the absent idiom

    three = RunFailure(run_id="r", phase="claimed", reasons=("a", "b", "c"))
    assert three.summary() == "a | b | c"  # exactly `limit` ⇒ no "+N more" suffix

    long_reason = "y" * 200
    rendered = RunFailure(run_id="r", phase="claimed", reasons=(long_reason,)).summary()
    assert rendered == long_reason[:140].rstrip() + "…"
    assert len(rendered) <= 141


def test_failed_run_report_carries_the_error_record_path(tmp_path: Path) -> None:
    """(#96 crit 6) A failed run points at its own durable error record, repo-RELATIVE.

    Repo-relative + POSIX so the same string can be persisted in ``state.json`` without leaking the
    host layout into a repo-scoped file (invariant 5) and without breaking when the repo moves.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="A fact in a non-existent domain.", second=10)
    _seed_raw(repo, e1)

    report = _run(repo, FakeBackend(_bad_domain_plan(e1), prose=PLAN_REJECTED_PROSE))

    assert report.status == "failed"
    failure = report.failure
    assert failure is not None
    assert failure.run_id == report.run_id
    assert failure.record_path == f"_kb/failed/{RUN_DATE}/{report.run_id}/error.json"
    # The pointer RESOLVES against the repo root — that is the whole point of printing it.
    assert (repo.root / failure.record_path).is_file()
    assert failure.cas_conflict is False
    assert failure.phase == "claimed"  # a PLAN failure never reached APPLY
    assert failure.reasons and failure.reasons[0].startswith("TAXONOMY")
    # The report's counts shape is UNCHANGED: the failure rides a new FIELD, never a new count key.
    assert report.counts == {"retried": 1, "failed": 0}


def test_cas_conflict_report_has_no_error_record(tmp_path: Path) -> None:
    """(#96) A CAS conflict is the ONE failure that writes no durable record — ``record_path`` None.

    The events are valid and simply stale: they return to ``inbox/``, no retry budget is burned and
    nothing needs auditing later, so the reason line itself IS the full explanation.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    repo.compare_and_swap_branch = lambda *, expected, new, branch=None: False  # type: ignore[method-assign]

    report = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e1),
            prose={region_sentinel_id("ignored", "c1"): "One curator holds a flock."},
        ),
    )

    assert report.status == "failed"
    failure = report.failure
    assert failure is not None
    assert failure.record_path is None
    assert failure.cas_conflict is True
    assert failure.reasons[0].startswith("CAS:")
    assert not list(layout.failed_dir.rglob("error.json"))
    assert report.counts == {"retried": 1}


def test_non_failed_reports_carry_no_failure(tmp_path: Path) -> None:
    """(#96) ``failure`` is set ONLY by ``_fail`` — published / noop / recovered reports carry None.

    Covers every non-failed construction site: the happy publish, BOTH noop shapes (lock held,
    nothing to claim) and BOTH recovery shapes (finalize-published, return-to-inbox).
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    # noop — nothing to claim (empty inbox).
    assert _run(repo, FakeBackend("{}")).failure is None

    # noop — the per-repo lock is already held by another run.
    with curator_lock(layout):
        held = _run(repo, FakeBackend("{}"))
    assert held.status == "noop"
    assert held.failure is None

    # published — the happy path.
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    published = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e1),
            prose={region_sentinel_id("ignored", "c1"): "One curator holds a per-repo flock."},
        ),
    )
    assert published.status == "published"
    assert published.failure is None
    tip = published.published_commit
    assert tip is not None

    # recovered — hand-place BOTH §9 shapes: a published-but-unfinalized run (finalize) and a
    # claimed run that never published (return to inbox).
    crashed_published = new_event_id(now=datetime(2026, 6, 13, 4, 0, 0, tzinfo=UTC))
    crashed_claimed = new_event_id(now=datetime(2026, 6, 13, 5, 0, 0, tzinfo=UTC))
    for run_id, phase, commit, second in (
        (crashed_published, "published", tip, 20),
        (crashed_claimed, "claimed", None, 21),
    ):
        event_id = _write_capture(inbox, text=f"A fact for {run_id}.", second=second)
        events_dir = layout.processing_dir / run_id / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        os.replace(layout.inbox_item_path("dochan", event_id), events_dir / f"{event_id}.md")
        write_manifest(
            layout,
            RunManifest(
                run_id=run_id,
                base_commit=tip,
                event_ids=(event_id,),
                phase=phase,  # type: ignore[arg-type]
                prose_complete=phase == "published",
                published_commit=commit,
                started="2026-06-13T04:00:00Z",
            ),
        )

    reports = recover(repo, state_store=StateStore(layout))
    recovered_ids = {r.run_id for r in reports}
    assert {crashed_published, crashed_claimed} <= recovered_ids
    assert all(r.status == "recovered" for r in reports)
    assert all(r.failure is None for r in reports)


def test_report_reasons_are_flattened_and_bounded_but_error_json_is_lossless(
    tmp_path: Path,
) -> None:
    """(#96) The REPORT echo is one bounded line; ``error.json`` keeps the raw multi-line text.

    A dead brain's stderr is routinely a multi-kilobyte traceback, and every consumer renders the
    report's reasons (a terminal, an agent's context window, ``state.json``). The bound lives at
    construction, ONCE — and the untruncated text is always one ``record_path`` away.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)

    # Space-FREE segments so the clip lands on a non-whitespace char (rstrip cannot shorten it),
    # newline-SEPARATED so flattening is observable.
    blob = "X" * 2500 + "\n" + "Y" * 2500

    report = _run(repo, _HugeStderrPlanBackend(blob))

    assert report.status == "failed"
    assert report.failure is not None
    reason = report.failure.reasons[0]
    assert "\n" not in reason  # whitespace-COLLAPSED, not first-line-truncated
    assert len(reason) <= 401  # 400 chars + the U+2026 elision marker
    assert reason.endswith("…")
    assert reason.startswith("PLAN-BACKEND: XXX")

    # The durable record is LOSSLESS — the report echo only points at it.
    record = json.loads((repo.root / report.failure.record_path).read_text(encoding="utf-8"))
    assert blob in record["failed_checks"][0]
    assert "\n" in record["failed_checks"][0]


def test_failure_phase_distinguishes_pre_and_post_apply(tmp_path: Path) -> None:
    """(#96) ``phase`` is truthful: ``claimed`` = failed BEFORE apply, ``applied`` = failed after.

    Regression lock for the ``_advance`` return-value bug: both callers discarded the advanced
    manifest, so the in-memory phase was pinned at ``claimed`` and BOTH ``RunFailure.phase`` and
    ``error.json``'s ``"phase"`` reported ``claimed`` for LINT / AUTHOR / CAS failures.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    # PLAN failure — rejected at the §4.1 gate, long before APPLY.
    e1 = _write_capture(inbox, text="A fact in a non-existent domain.", second=10)
    _seed_raw(repo, e1)
    plan_report = _run(repo, FakeBackend(_bad_domain_plan(e1), prose=PLAN_REJECTED_PROSE))
    assert plan_report.failure is not None
    assert plan_report.failure.phase == "claimed"

    # LINT failure — a real APPLY + commit happened on the detached worktree first.
    repo2 = _init_repo(tmp_path / "kb2")
    e2 = _lint_failure_event(Inbox(repo2.layout))
    # PASS 2 authors its region successfully; the failure under test is the LINT gate's alone.
    lint_report = _run(
        repo2,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e2),
            prose={region_sentinel_id("ignored", "c1"): "One curator holds a flock."},
        ),
    )
    assert lint_report.status == "failed"
    assert lint_report.failure is not None
    assert lint_report.failure.phase == "applied"
    record = json.loads((repo2.root / lint_report.failure.record_path).read_text(encoding="utf-8"))
    assert record["phase"] == "applied"  # the on-disk record agrees with the report


def test_non_terminal_failure_is_visible_in_state(tmp_path: Path) -> None:
    """(#96 crit 7) A failure INSIDE the retry budget is visible in state.json — the blind spot.

    Nothing else moves: ``last_run``/``last_commit`` stay unset (mark_run fires only on publish),
    ``counters.failed`` stays 0 (retries are not failures yet, §5.1) and the event goes back to
    ``inbox/`` so the depth is unchanged. ``last_attempt`` + ``last_failure`` are the ONLY evidence.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="A fact in a non-existent domain.", second=10)
    _seed_raw(repo, e1)

    report = _run(repo, FakeBackend(_bad_domain_plan(e1), prose=PLAN_REJECTED_PROSE))
    assert report.status == "failed"

    st = StateStore(layout).load()
    # The pre-#96 surface is unmoved — this is what made the failure invisible.
    assert st.last_run is None
    assert st.last_commit is None
    assert st.counters.failed == 0
    assert layout.inbox_item_path("dochan", e1).is_file()

    # ...yet the attempt and its cause are both recorded.
    assert st.last_attempt == NOW
    lf = st.last_failure
    assert lf is not None
    assert lf.when == NOW
    assert lf.run_id == report.run_id
    assert lf.phase == "claimed"
    assert lf.reasons[0].startswith("TAXONOMY")
    assert lf.reasons_total == len(lf.reasons)
    assert lf.record_path == f"_kb/failed/{RUN_DATE}/{report.run_id}/error.json"
    assert (repo.root / lf.record_path).is_file()
    assert st.failure_is_current is True


def test_last_attempt_is_stamped_even_when_the_run_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(#96) The stamp sits right after the CLAIM, so a crash-shaped run is still honest.

    Stamping at each terminus instead would leave `last_attempt` silent for exactly the runs an
    operator most needs to see: the ones that died before reaching any terminus at all.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated crash before any terminus")

    monkeypatch.setattr(worker_mod, "build_bundle", boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        _run(repo, FakeBackend("{}"))

    st = StateStore(layout).load()
    assert st.last_attempt == NOW
    # No _fail ran, so there is no cause to record — the attempt alone is the signal.
    assert st.last_failure is None


def test_publish_leaves_state_semantics_unchanged(tmp_path: Path) -> None:
    """(#96 crit 9) A later publish moves last_run/last_commit and NOTHING about last_failure.

    ``last_failure`` is sticky like ``counters`` — a publish over DIFFERENT events resolves nothing,
    so clearing it would manufacture a false all-clear. ``failure_is_current`` is what answers
    "is it still broken?", and it flips on its own.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)

    failed = _run(repo, FakeBackend(_bad_domain_plan(e1), prose=PLAN_REJECTED_PROSE), now=NOW)
    assert failed.status == "failed"

    # The event came back to inbox/; run 2 re-claims it with a VALID plan.
    published = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e1),
            prose={region_sentinel_id("ignored", "c1"): "One curator holds a per-repo flock."},
        ),
        now=NOW2,
    )
    assert published.status == "published"

    st = StateStore(layout).load()
    assert st.last_run == NOW2
    assert st.last_attempt == NOW2  # one stamp per claimed run ⇒ equal after a publish
    assert st.last_commit == published.published_commit
    lf = st.last_failure
    assert lf is not None  # STICKY: the historical fact survives the publish
    assert lf.when == NOW
    assert lf.run_id == failed.run_id
    assert st.failure_is_current is False  # ...but it is no longer the current verdict
    assert st.counters.failed == 0


def test_cas_conflict_stamps_attempt_but_records_no_failure(tmp_path: Path) -> None:
    """(#96) A CAS conflict is not a failure: it burns no budget and writes no ``last_failure``.

    ``last_attempt`` still moves — the curator genuinely attempted a consolidation — which is the
    honest reading of a repo where a concurrent writer keeps winning the race.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    repo.compare_and_swap_branch = lambda *, expected, new, branch=None: False  # type: ignore[method-assign]

    report = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e1),
            prose={region_sentinel_id("ignored", "c1"): "One curator holds a flock."},
        ),
    )
    assert report.status == "failed"

    st = StateStore(layout).load()
    assert st.last_attempt == NOW
    assert st.last_failure is None
    assert st.failure_is_current is False
    assert st.counters.failed == 0
    assert not list(layout.failed_dir.rglob("error.json"))


def test_terminal_failure_records_counter_and_last_failure_together(tmp_path: Path) -> None:
    """(#96) At budget exhaustion the counter bump and the failure record land in ONE state save.

    Two facts about the SAME run written separately could disagree after a crash between them; one
    save makes that unrepresentable.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="A fact in a non-existent domain.", second=10)
    _seed_raw(repo, e1)

    last = None
    for _ in range(3):  # curator.max_attempts defaults to 3
        last = _run(repo, FakeBackend(_bad_domain_plan(e1), prose=PLAN_REJECTED_PROSE))
        assert last.status == "failed"
    assert last is not None

    st = StateStore(layout).load()
    assert st.counters.failed == 1  # the event went TERMINAL on the third attempt
    assert st.last_failure is not None
    assert st.last_failure.run_id == last.run_id
    assert st.last_run is None  # a failure is never a "last successful publish"
    assert st.failure_is_current is True


def test_recovery_leaves_the_failure_fields_untouched(tmp_path: Path) -> None:
    """(#96) Recovery neither writes nor clears ``last_attempt``/``last_failure``.

    ``recover()`` reads no clock (ADR-0010 D1) so it has no honest value to stamp, and it already
    declines the analogous ``last_run`` write. ``last_batch`` IS cleared — it labels the run being
    finalized, whereas ``last_failure`` is a standalone historical fact whose analogue is
    ``counters``, which recovery deliberately does not replay either.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    published = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e1),
            prose={region_sentinel_id("ignored", "c1"): "One curator holds a per-repo flock."},
        ),
    )
    tip = published.published_commit
    assert tip is not None

    # Seed both fields with a KNOWN older failure, then crash-and-recover over the top of it.
    store = StateStore(layout)
    seeded = store.load()
    seeded.last_attempt = NOW
    seeded.last_failure = LastFailure.from_run_failure(
        when=NOW,
        run_id="2026-06-13T02-00-00.000Z--seeded",
        phase="claimed",
        reasons=("TAXONOMY: unknown domain 'x'",),
        record_path="_kb/failed/2026-06-13/2026-06-13T02-00-00.000Z--seeded/error.json",
    )
    store.save(seeded)

    # The §9 "CAS succeeded but state.json wasn't recorded" row: manifest applied + the ref tip.
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
            published_commit=tip,
            started="2026-06-13T04:00:00Z",
        ),
    )

    reports = recover(repo, state_store=store)
    assert any(r.run_id == crashed and r.status == "recovered" for r in reports)

    st = store.load()
    assert st.last_attempt == seeded.last_attempt
    assert st.last_failure == seeded.last_failure
    assert st.last_batch is None  # cleared: the recovered run's batch shape is unknowable


def test_noop_run_does_not_write_state(tmp_path: Path) -> None:
    """(#96) An idle tick writes NOTHING — ``last_attempt`` marks a CLAIM, not a poll.

    ``agora watch`` runs ~1440 ticks/day on an empty inbox; stamping each one would rewrite
    state.json all day and would answer a question nobody asked ("when did the curator last look?").
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    assert not layout.state_file.exists()  # `repo init` writes no state.json

    report = _run(repo, FakeBackend("{}"))

    assert report.status == "noop"
    assert not layout.state_file.exists()


def test_failure_state_write_error_becomes_a_warning_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(#96) A failed state write degrades to a #115 warning — a FAILED run never becomes CRASHED.

    Before #96 ``_fail`` touched state only at budget exhaustion, so making the save unconditional
    would hand `agora curate` / MCP `kb_curate` an uncaught OSError on a full or read-only disk.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="A fact in a non-existent domain.", second=10)
    _seed_raw(repo, e1)

    real_save = StateStore.save

    def flaky_save(self: StateStore, state: object) -> None:
        # Fail ONLY the _fail-time save: the claim-time stamp is deliberately unguarded (the
        # filesystem was just proven writable by claim()), so breaking it would test nothing here.
        if getattr(state, "last_failure", None) is not None:
            raise OSError("No space left on device")
        real_save(self, state)  # type: ignore[arg-type]

    monkeypatch.setattr(StateStore, "save", flaky_save)

    report = _run(repo, FakeBackend(_bad_domain_plan(e1), prose=PLAN_REJECTED_PROSE))

    assert report.status == "failed"  # the terminal verdict is unchanged...
    assert report.failure is not None  # ...and the cause still reaches the operator
    assert "could not record this failure in _kb/state.json" in report.warnings[0]
    assert "No space left on device" in report.warnings[0]
    assert StateStore(layout).load().last_failure is None  # the write genuinely did not land


# --- issue #124: the no-loss floor ---------------------------------------------------------------
def test_fail_preserves_an_event_whose_inbox_return_is_refused(tmp_path: Path) -> None:
    """(#124 crit 1) A REFUSED within-budget return preserves the event; it is never destroyed.

    Before #124 this branch had no ``else``: a refused inbox return left the event in
    ``processing/``, which ``_fail`` then ``rmtree``d. The event vanished and NOTHING recorded it —
    ``counts`` said ``{"retried": 0, "failed": 0}``, ``failed_event_count`` said 0. The refusal is
    reached with no attacker involved: an inbox slot occupied by an idempotent duplicate is enough.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="A fact in a non-existent domain.", second=10)
    _seed_raw(repo, e1)
    original = layout.inbox_item_path("dochan", e1).read_bytes()

    # Occupy the destination so the return is refused: claim() moves the event out of the inbox, so
    # re-creating the path mid-run is what a duplicate id would look like from _fail's side.
    real_return = worker_mod.return_event_to_inbox

    def occupy_then_return(layout_arg: RepoLayout, event_path: Path) -> InboxReturn:
        dest = layout_arg.inbox_item_path("dochan", e1)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("an idempotent duplicate already sitting in the slot\n", encoding="utf-8")
        return real_return(layout_arg, event_path)

    worker_mod.return_event_to_inbox = occupy_then_return  # type: ignore[assignment]
    try:
        report = _run(repo, FakeBackend(_bad_domain_plan(e1), prose=PLAN_REJECTED_PROSE))
    finally:
        worker_mod.return_event_to_inbox = real_return  # type: ignore[assignment]

    assert report.status == "failed"
    # The refusal is now a TERMINAL disposition, so it is counted as one — no new counts KEY.
    assert report.counts == {"retried": 0, "failed": 1}
    preserved = list(layout.failed_dir.rglob(f"{e1}.md"))
    assert len(preserved) == 1
    # Rename-only: the preserved event is byte-for-byte the original (DATA-MODEL §1 immutability).
    assert preserved[0].read_bytes() == original
    assert preserved[0].parent == layout.failed_dir / RUN_DATE / report.run_id
    # It is visible to every operator surface that counts terminal failures.
    assert failed_event_count(layout) == 1
    assert not (layout.processing_dir / report.run_id).exists()


@pytest.mark.parametrize(
    "hostile_id",
    ["../../../../wiki/PWNED", "../escape", "..", "a/b", "with space", ""],
)
def test_a_traversing_frontmatter_id_is_refused_at_claim(tmp_path: Path, hostile_id: str) -> None:
    """(#124 crit 4) A hostile frontmatter ``id`` never becomes a path component.

    The id is interpolated into TWO destinations with no validation between them:
    ``claim`` builds ``processing/<run>/events/<id>.md`` (claim.py) and ``return_event_to_inbox``
    builds ``inbox/<writer>/<id>.md`` (core/inbox.py). ``inbox_item_path`` validates the WRITER but
    passes the id through verbatim, so ``id: ../../../../wiki/PWNED`` resolves inside the
    git-tracked read model only the curator may write (invariant 2).

    Claim is the FIRST gate and therefore the one that fires: a rejected event is skipped, so it
    stays in ``inbox/`` — still counted by ``depth()``, still on disk, nothing lost. That is the
    same fail-closed posture claim already used for unparseable frontmatter.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="A fact in a non-existent domain.", second=10)
    _seed_raw(repo, e1)

    src = layout.inbox_item_path("dochan", e1)
    fm, body = frontmatter.parse(src.read_text(encoding="utf-8"))
    fm["id"] = hostile_id
    src.write_text(frontmatter.render(fm, body), encoding="utf-8")
    before = src.read_bytes()

    report = _run(repo, FakeBackend(_bad_domain_plan(e1), prose=PLAN_REJECTED_PROSE))

    # Nothing was claimable, so the run is a clean noop — no crash, no traceback out of run().
    assert report.status == "noop"
    # NOTHING escaped: not into the curated read model, not anywhere outside _kb/.
    assert not list(repo.root.glob("wiki/**/PWNED*"))
    assert not (repo.root / "PWNED.md").exists()
    assert not list(layout.processing_dir.rglob("PWNED*"))
    # And the event is untouched, in place, still visible as backlog.
    assert src.read_bytes() == before
    assert inbox.depth() == 1
    assert failed_event_count(layout) == 0


def test_cas_conflict_preserves_an_event_whose_return_is_refused(tmp_path: Path) -> None:
    """(#124 crit 2) The CAS path shares the floor; a clean CAS conflict's counts stay unchanged.

    ``_return_events_to_inbox`` + ``rmtree`` is the same shape as the budget path, so the same
    refusal destroyed events here too. ``preserved`` is emitted ONLY when non-zero, which is why
    ``test_cas_conflict_report_has_no_error_record`` still asserts ``{"retried": 1}`` unmodified.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)
    original = layout.inbox_item_path("dochan", e1).read_bytes()
    repo.compare_and_swap_branch = lambda *, expected, new, branch=None: False  # type: ignore[method-assign]

    real_return = worker_mod.return_event_to_inbox
    worker_mod.return_event_to_inbox = lambda layout_arg, event_path: InboxReturn(  # type: ignore[assignment]
        status="unreadable", source=event_path, dest=None, detail="refused, for the test"
    )
    try:
        report = _run(
            repo,
            FakeBackend(
                _create_theme_plan("ignored", "c1", e1),
                prose={region_sentinel_id("ignored", "c1"): "One curator holds a flock."},
            ),
        )
    finally:
        worker_mod.return_event_to_inbox = real_return  # type: ignore[assignment]

    assert report.status == "failed"
    assert report.counts == {"retried": 0, "preserved": 1}
    preserved = list(layout.failed_dir.rglob(f"{e1}.md"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == original
    assert failed_event_count(layout) == 1


def test_recovery_preserves_an_event_whose_return_is_refused(tmp_path: Path) -> None:
    """(#124 crit 3) The ADR-0011 §9 recovery path shares the floor.

    ``_return_to_inbox`` runs on events that are byte-identical originals from ``claim``, so a
    refusal there loses a capture the operator never got back — the worst of the three shapes.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    base = repo.branch_commit()
    run_id = new_event_id(now=datetime(2026, 6, 13, 4, 0, 0, tzinfo=UTC))
    e2 = _write_capture(inbox, text="An unpublished claimed event.", second=20)
    events_dir = layout.processing_dir / run_id / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    src = layout.inbox_item_path("dochan", e2)
    original = src.read_bytes()
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
    # Occupy the inbox destination so the recovery return is refused.
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("a duplicate holding the slot\n", encoding="utf-8")

    reports = recover(repo, state_store=StateStore(layout))

    rec = [r for r in reports if r.run_id == run_id]
    assert len(rec) == 1
    assert rec[0].counts == {"returned": 0, "preserved": 1}
    preserved = layout.failed_dir / run_id[:10] / run_id / f"{e2}.md"
    assert preserved.is_file()
    assert preserved.read_bytes() == original
    assert failed_event_count(layout) == 1
    assert not (layout.processing_dir / run_id).exists()


# --- issue #99: ONE budget derivation ------------------------------------------------------------
def test_iter_attempt_records_is_the_single_budget_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(#99 R13) ``_event_attempt_counts`` DERIVES from ``iter_attempt_records`` — one enumeration.

    ``agora requeue --reset-attempts`` archives against the same generator, so "what counts as one
    spent attempt" (ADR-0011 §5.1) exists in exactly one place: a future change to the count is
    necessarily a change to the reset. Two copies would drift, and the drift would surface as a
    ``--reset-attempts`` that reports success while resetting nothing.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)
    e1 = _write_capture(inbox, text="A fact in a non-existent domain.", second=10)
    _seed_raw(repo, e1)
    for _ in range(2):
        assert (
            _run(repo, FakeBackend(_bad_domain_plan(e1), prose=PLAN_REJECTED_PROSE)).status
            == "failed"
        )

    records = list(worker_mod.iter_attempt_records(layout))

    assert [ids for _path, ids in records] == [[e1], [e1]]  # one record per attempt
    assert all(path.name == "error.json" for path, _ids in records)
    assert worker_mod._event_attempt_counts(layout) == {e1: 2}

    # A record whose BYTES are unreadable is skipped by the generator (pre-existing tolerance).
    stray = layout.failed_dir / RUN_DATE / "hand-made" / "error.json"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("[not, an, object]", encoding="utf-8")  # not valid JSON at all
    assert worker_mod._event_attempt_counts(layout) == {e1: 2}

    # ...and so is a record that is valid JSON of the WRONG SHAPE. This is the branch that matters:
    # `_kb/failed/` is operator-editable, and before #99 a top-level list reached
    # `record.get("event_ids")` and raised an UNCAUGHT AttributeError out of _fail — a hand-edited
    # audit record could crash a curator run. Each shape is asserted separately because they take
    # different branches, and a single fixture would let one of them rot untested.
    for shape in ('["evt-a", "evt-b"]', '"a string"', "42", "null", '{"event_ids": {"evt-a": 1}}'):
        stray.write_text(shape, encoding="utf-8")
        assert worker_mod._event_attempt_counts(layout) == {e1: 2}, shape
        assert stray not in {path for path, _ids in worker_mod.iter_attempt_records(layout)}, shape

    # A bare string is the one wrong-ish shape that IS tolerated — as one id, never as its letters
    # (which would invent a budget for three events that do not exist).
    stray.write_text('{"event_ids": "evt-solo"}', encoding="utf-8")
    assert worker_mod._event_attempt_counts(layout) == {e1: 2, "evt-solo": 1}
    stray.unlink()

    # ...and the counter routes THROUGH the generator rather than re-globbing beside it.
    monkeypatch.setattr(
        worker_mod, "iter_attempt_records", lambda _layout: iter([(Path("x"), ["spliced"])])
    )
    assert worker_mod._event_attempt_counts(layout) == {"spliced": 1}


# --- (17) #119: the worker retracts `body_status: pending` once the prose really lands -----------
#
# APPLY stamps `body_status: pending` on every needs_prose note and — until #119 — NOTHING ever
# removed it, so the schema's "this note still owes prose" signal (ADR-0010 §2.6) was set on every
# published note and useless to every reader. PASS 2 cannot retract it (validate_author_diff
# requires frontmatter byte-identity and the model is outside the integrity boundary, ADR-0011 §4),
# so the worker does it behind the §4.2 gate. The predicate is NOTE-LOCAL, never this run's region
# ids — the cross-run tests below are what distinguish the two rules.


def _merge_plan_two_regions(run_id: str, e1: str, e2: str, target: str) -> str:
    """Two MERGE_INTO_THEME dispositions into the SAME theme → one note, TWO needs_prose regions.

    APPEND_DAILY would be the other natural two-region shape, but a daily's `run_id:` frontmatter is
    taken from ``plan.run_id`` while lint L1-14 compares it to the run's INJECTED manifest run_id —
    which ``run()`` generates internally — so an end-to-end daily plan cannot be canned here. The
    MERGE shape carries no such coupling.
    """
    return json.dumps(
        {
            "schema_version": 1,
            "run_id": run_id,
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": cid,
                    "event_ids": [event],
                    "op": "MERGE_INTO_THEME",
                    "domain": "ai-tech",
                    "target_basename": target,
                    "summary": f"Augmentation {cid}.",
                    "needs_prose": True,
                    "reason": "Corroborates an existing theme.",
                }
                for cid, event in (("c1", e1), ("c2", e2))
            ],
        }
    )


def _merge_plan(run_id: str, event_id: str, target: str) -> str:
    """A single non-gated MERGE_INTO_THEME that appends a NEW prose region to an existing theme."""
    return json.dumps(
        {
            "schema_version": 1,
            "run_id": run_id,
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": "c1",
                    "event_ids": [event_id],
                    "op": "MERGE_INTO_THEME",
                    "domain": "ai-tech",
                    "target_basename": target,
                    "summary": "Augments the existing theme.",
                    "needs_prose": True,
                    "reason": "Corroborates an existing theme.",
                }
            ],
        }
    )


def _published_theme_fm(repo: Repo, report: RunReport) -> dict[str, object]:
    """Parse the published `curator-concurrency` theme's frontmatter out of the published commit."""
    with repo.worktree(at=report.published_commit) as published:  # type: ignore[arg-type]
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    return fm


def test_body_status_cleared_when_pass2_fills_every_region(tmp_path: Path) -> None:
    """(a) The flag is REMOVED when PASS 2 really authored the prose — and the prose is there.

    Both halves matter together: asserting only "the key is gone" would also pass if the clear ran
    over an empty region, and asserting only "the prose landed" is the pre-#119 status quo.
    """
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)

    report = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e1),
            prose={region_sentinel_id("ignored", "c1"): "The single curator holds a flock."},
        ),
    )

    assert report.status == "published"
    with repo.worktree(at=report.published_commit) as published:  # type: ignore[arg-type]
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
        assert "body_status" not in fm
        start, end = body_sentinels(region_sentinel_id("ignored", "c1"))
        region = body[body.find(start) + len(start) : body.find(end)]
        assert region.strip() == "The single curator holds a flock."
        # The §4.4 gate the worker ran saw the POST-clear tree — including the new L2-6 rule, which
        # is the exact inverse of the clear, so a note the worker just wrote can never trip it.
        result = lint(RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE)
        assert result.ok
        assert all(f.code != "L2-6" for f in result.findings)


def _publish_authored_theme(tmp_path: Path) -> Repo:
    """Run 1: publish `curator-concurrency` with its single region fully authored (flag cleared)."""
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)
    e0 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=5)
    _seed_raw(repo, e0)
    report = _run(
        repo,
        FakeBackend(
            _create_theme_plan("run-one", "c1", e0),
            prose={region_sentinel_id("run-one", "c1"): "The single curator holds a flock."},
        ),
    )
    assert report.status == "published", report.failure
    assert "body_status" not in _published_theme_fm(repo, report)
    return repo


def test_body_status_stays_when_only_some_regions_are_authored(
    tmp_path: Path, prose_pending_is_the_point: None
) -> None:
    """A note whose PASS 2 filled 1 of 2 regions legitimately has prose AND a legitimate pending.

    This is the predicate most likely to be got wrong: the rule is "NO region is still a
    placeholder", not "prose exists anywhere in the note".
    """
    repo = _publish_authored_theme(tmp_path)
    inbox = Inbox(repo.layout)
    e1 = _write_capture(inbox, text="Curators serialize every wiki write.", second=20)
    e2 = _write_capture(inbox, text="The inbox is append-only and per-writer.", second=21)
    _seed_raw(repo, e1, e2)

    report = _run(
        repo,
        FakeBackend(
            _merge_plan_two_regions("run-two", e1, e2, "curator-concurrency"),
            # prose for c1's region ONLY — c2's stays at APPLY's `_summary pending_`.
            prose={region_sentinel_id("run-two", "c1"): "The curator serializes every write."},
        ),
    )

    assert report.status == "published", report.failure
    assert report.counts["prose_pending"] == 1  # exactly one region left unauthored (#115)
    with repo.worktree(at=report.published_commit) as published:  # type: ignore[arg-type]
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
        assert fm["body_status"] == "pending"  # the note still OWES prose — flag retained
        assert "The curator serializes every write." in body  # …while real prose is present
        assert lint(RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE).ok


def _publish_theme_with_pending_region(tmp_path: Path) -> tuple[Repo, str]:
    """Run 1: publish `curator-concurrency` whose ONLY region PASS 2 left at the placeholder.

    Returns ``(repo, run1_region_sentinel_id)``. This is the pre-existing-debt shape every later run
    has to reason about: a live note carrying a legitimate `body_status: pending` from an EARLIER
    run, whose region NO future run's needs_prose map will ever list again.
    """
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)
    e0 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=5)
    _seed_raw(repo, e0)

    # A backend that authors NOTHING (the #115 shape) leaves the region at APPLY's placeholder.
    report = _run(repo, FakeBackend(_create_theme_plan("run-one", "c1", e0), prose={}))
    assert report.status == "published"
    assert _published_theme_fm(repo, report)["body_status"] == "pending"
    return repo, region_sentinel_id("run-one", "c1")


class _AlsoHealsOlderRegionBackend(FakeBackend):
    """A PASS 2 that also fills a region left over from an EARLIER run.

    Contract-legal, not a cheat: ``_needs_prose_map`` unions every sentinel id PRESENT in the note
    into the §4.2 ``sentinels`` set, so ``validate_author_diff`` accepts an edit to a prior run's
    region. Nothing ASKS the brain to do it (``needs_prose`` lists only this run's ids), which is
    exactly why the healing in :func:`test_body_status_clears_once_the_older_region_is_filled` is
    opportunistic rather than something a run can be relied upon to perform.
    """

    def __init__(self, plan_text: str, *, extra: dict[str, str], **kw: object) -> None:
        super().__init__(plan_text, **kw)  # type: ignore[arg-type]
        self._extra = extra

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],
    ) -> None:
        super().author(worktree, needs_prose, context)
        for rel in needs_prose:
            path = worktree / rel
            text = path.read_text(encoding="utf-8")
            for sid, prose in self._extra.items():
                text = worker_mod._replace_sentinel_region(text, sid, prose)
            path.write_text(text, encoding="utf-8")


def test_body_status_survives_a_later_run_that_authors_only_its_own_region(
    tmp_path: Path, prose_pending_is_the_point: None
) -> None:
    """CROSS-RUN: run 2 authors 100% of ITS regions, yet the flag must SURVIVE (#119).

    This is the test that distinguishes the note-local rule from a run-scoped one. Run 1 left a
    region at the placeholder; run 2 MERGEs a new authored region into the same theme. A rule keyed
    on THIS run's `_unauthored_regions` would see zero pending regions and clear a flag that is
    still owed. The divergence it produces — `prose_pending: 0` next to a retained
    `body_status: pending` — is DELIBERATE: `prose_pending` grades this run's PASS 2 (#115),
    `body_status` describes THE NOTE (ADR-0010 §2.6). Do not "fix" it.
    """
    repo, _run1_sid = _publish_theme_with_pending_region(tmp_path)
    inbox = Inbox(repo.layout)
    e1 = _write_capture(inbox, text="Curators serialize every wiki write.", second=20)
    _seed_raw(repo, e1)

    report = _run(
        repo,
        FakeBackend(
            _merge_plan("run-two", e1, "curator-concurrency"),
            prose={region_sentinel_id("run-two", "c1"): "Corroborated by a second capture."},
        ),
    )

    assert report.status == "published"
    assert report.counts["prose_pending"] == 0  # run 2's OWN pass was a complete success…
    with repo.worktree(at=report.published_commit) as published:  # type: ignore[arg-type]
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
        assert fm["body_status"] == "pending"  # …yet the note still owes run 1's region
        assert "Corroborated by a second capture." in body
        assert "_summary pending_" in body  # run 1's region is the one still owed
        result = lint(RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE)
        assert result.ok
        # L2-6 agrees with the worker: the flag is legitimate here, so no stale-flag warning.
        assert all(f.code != "L2-6" for f in result.findings)


def test_body_status_clears_once_the_older_region_is_filled(
    tmp_path: Path, prose_pending_is_the_point: None
) -> None:
    """Free incremental healing: when the LAST unauthored region is filled, the flag drops.

    The corpus is not swept (a whole-worktree rewrite inside the curate hot path would touch notes
    the plan never named — that is `agora repo upgrade`'s job, #63), but any run whose PASS 2 leaves
    a note with zero placeholder regions repairs that note for free.
    """
    repo, run1_sid = _publish_theme_with_pending_region(tmp_path)
    inbox = Inbox(repo.layout)
    e1 = _write_capture(inbox, text="Curators serialize every wiki write.", second=20)
    _seed_raw(repo, e1)

    report = _run(
        repo,
        _AlsoHealsOlderRegionBackend(
            _merge_plan("run-two", e1, "curator-concurrency"),
            prose={region_sentinel_id("run-two", "c1"): "Corroborated by a second capture."},
            extra={run1_sid: "The single curator holds a per-repo flock."},
        ),
    )

    assert report.status == "published"
    with repo.worktree(at=report.published_commit) as published:  # type: ignore[arg-type]
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
        assert "body_status" not in fm  # every region is authored now → the flag is retracted
        assert "_summary pending_" not in body
        assert lint(RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE).ok


def test_clear_removes_exactly_the_body_status_line_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BYTE LOCK on the YAML round-trip: the clear is `parse -> pop -> render`, so a PyYAML quoting
    or key-order difference would silently churn every published note. Capture the note immediately
    before the clear and assert the published bytes differ by EXACTLY the one dropped line."""
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)

    before: dict[str, str] = {}
    original = worker_mod._clear_body_status

    def spy(worktree: Path, needs_prose: dict[str, list[str]]) -> list[str]:
        for rel in needs_prose:
            before[rel] = (worktree / rel).read_text(encoding="utf-8")
        return original(worktree, needs_prose)

    monkeypatch.setattr(worker_mod, "_clear_body_status", spy)

    report = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e1),
            prose={region_sentinel_id("ignored", "c1"): "The single curator holds a flock."},
        ),
    )

    assert report.status == "published"
    rel = "wiki/ai-tech/themes/curator-concurrency.md"
    assert "body_status: pending\n" in before[rel]
    with repo.worktree(at=report.published_commit) as published:  # type: ignore[arg-type]
        after = (published / rel).read_text(encoding="utf-8")
    assert after == before[rel].replace("body_status: pending\n", "", 1)


def test_clear_never_writes_a_note_that_has_no_body_status(tmp_path: Path) -> None:
    """(vii) A needs_prose note without the key is NOT rewritten — no churn, no diff line.

    The no-write oracle is deliberately structural rather than an mtime comparison: the note is
    written with NON-canonical YAML spacing that ``frontmatter.render`` would normalize away, so any
    write at all — even one that produced the same frontmatter mapping — is detectable.
    """
    from agora_kb.curator.worker import _clear_body_status

    rel = "wiki/ai-tech/themes/no-flag.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        "title:   No flag\n"  # non-canonical spacing: yaml.safe_dump would collapse it
        "type: theme\n"
        "---\n"
        "\n"
        "<!-- agora:body:start id=r--c1 -->\n"
        "Authored prose, and no body_status key was ever set.\n"
        "<!-- agora:body:end id=r--c1 -->\n"
    )
    path.write_text(text, encoding="utf-8")

    assert _clear_body_status(tmp_path, {rel: ["r--c1"]}) == []
    assert path.read_text(encoding="utf-8") == text


def test_clear_is_a_noop_for_a_note_pass2_deleted(tmp_path: Path) -> None:
    """Defensive: a note PASS 2 deleted (the §4.2 validator rejects it separately) must not raise —
    the clear runs unconditionally inside `if needs_prose:` and cannot be the thing that turns a
    clean FAILED verdict into an uncaught traceback out of run()."""
    from agora_kb.curator.worker import _clear_body_status

    assert _clear_body_status(tmp_path, {"wiki/ai-tech/themes/gone.md": ["r--c1"]}) == []


def test_clear_skips_a_malformed_note_instead_of_fabricating_a_fence(tmp_path: Path) -> None:
    """A note whose frontmatter will not parse is L1-4's finding at §4.4, not the clear's problem.

    Rendering one here would REPLACE the malformed document with a fabricated fence — the worker
    inventing structure the §4.2 gate never validated.
    """
    from agora_kb.curator.worker import _clear_body_status

    rel = "wiki/ai-tech/themes/broken.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "no frontmatter fence at all\n<!-- agora:body:start id=r--c1 -->\np\n"
    path.write_text(text, encoding="utf-8")

    assert _clear_body_status(tmp_path, {rel: ["r--c1"]}) == []
    assert path.read_text(encoding="utf-8") == text


class _GoodProseButTampersFrontmatter(FakeBackend):
    """PASS 2 authors REAL prose into the region *and* tampers frontmatter — §4.2 rejects the note.

    ``_OutOfRegionBackend`` writes no prose at all, so at the moment of the verdict its region is
    already the placeholder. This backend makes the region genuinely AUTHORED when §4.2 rejects,
    which is the only shape that can tell "the flag survives because the degrade rewound the prose"
    from "the flag survives because nothing was ever written".
    """

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],
    ) -> None:
        super().author(worktree, needs_prose, context)
        for rel in needs_prose:
            path = worktree / rel
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("status: active", "status: active\nrogue: 1"), "utf-8")


def test_a_rejected_pass_keeps_its_flag_even_though_prose_was_written(
    tmp_path: Path, prose_pending_is_the_point: None
) -> None:
    """(b) A §4.2-REJECTED pass keeps its flag even though PASS 2 had written genuine prose.

    ``has_unauthored_region`` would have said "authored" at the moment PASS 2 finished. The §4.2
    gate rejected the pass, `_degrade_prose` discarded ALL of it (including the good prose) and
    re-set `body_status: pending`; the clear then correctly leaves the note alone, because the
    predicate is evaluated on the POST-degrade bytes.

    NOTE on ordering: this does NOT lock "clear after degrade". Verified by mutation — moving the
    clear above `_degrade_prose` still passes, because `_degrade_prose` unconditionally re-stamps
    `body_status: pending`, so the reversed order is self-correcting (it would only churn a write).
    The ordering that IS load-bearing is "clear BEFORE the §4.4 lint", locked by
    :func:`test_the_clear_runs_before_the_lint_that_grades_it`.
    """
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)

    report = _run(
        repo,
        _GoodProseButTampersFrontmatter(
            _create_theme_plan("ignored", "c1", e1),
            prose={region_sentinel_id("ignored", "c1"): "The single curator holds a flock."},
        ),
    )

    assert report.status == "published", report.failure
    with repo.worktree(at=report.published_commit) as published:  # type: ignore[arg-type]
        theme = published / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
        assert fm["body_status"] == "pending"  # the flag SURVIVED the rejected pass
        assert "rogue" not in fm  # the frontmatter tamper was discarded
        start, end = body_sentinels(region_sentinel_id("ignored", "c1"))
        assert body[body.find(start) + len(start) : body.find(end)].strip() == "> _summary pending_"


def test_the_clear_runs_before_the_lint_that_grades_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) ORDERING LOCK — §4.4 lint must grade the POST-clear tree, read from DISK.

    `lint` reads the worktree, not `new_state`, so the clear has to have hit disk by the time it
    runs. If the clear moved below the lint call, the gate would grade pre-clear bytes and every
    freshly-published note would trip the very L2-6 rule this change adds — criterion (c) defeated
    while every other assertion in the suite still passed.
    """
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)
    e1 = _write_capture(inbox, text="One curator advances the branch under a lock.", second=10)
    _seed_raw(repo, e1)

    seen_at_lint_time: list[str] = []
    original = worker_mod.lint

    def lint_spy(layout, **kwargs):  # type: ignore[no-untyped-def]
        note = layout.root / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
        if note.is_file():
            seen_at_lint_time.append(note.read_text(encoding="utf-8"))
        return original(layout, **kwargs)

    monkeypatch.setattr(worker_mod, "lint", lint_spy)

    report = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", e1),
            prose={region_sentinel_id("ignored", "c1"): "The single curator holds a flock."},
        ),
    )

    assert report.status == "published", report.failure
    assert len(seen_at_lint_time) == 1
    # The bytes the §4.4 gate actually saw already had the flag retracted.
    assert "body_status" not in seen_at_lint_time[0]
    assert "The single curator holds a flock." in seen_at_lint_time[0]
