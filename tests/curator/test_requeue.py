"""Tests for the ``agora requeue`` engine — the ``_kb/failed/`` → ``_kb/inbox/`` back-edge (#99).

Requeue is the FIRST non-curator process to mutate the curator's private spool, so what is locked
here is mostly the ADR-0002 spool-custodian rule rather than the happy path: the whole batch runs
under ``curator_lock`` and refuses cleanly when it is held; every mutation is a rename of an
immutable event (same bytes, same INODE); an occupied destination is reported, never clobbered;
nothing is ever deleted; and ``_kb/state.json`` is never written — not even under ``--force``.

The two engine-level structural claims get their own locks too: ``--dry-run`` produces the SAME
per-item list the real run produces (one resolver, two callers), and ``--reset-attempts`` archives
by the DRAIN RULE, which is what makes the preview, the real run and a crash-interrupted re-run all
agree. The CLI's rendering, exit codes and stderr warnings belong to the face and are tested there.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

import agora_kb.curator.requeue as requeue_mod
from agora_kb.core.inbox import Inbox, InboxReturn, failed_event_count
from agora_kb.core.layout import RepoLayout
from agora_kb.core.state import CuratorState, StateStore
from agora_kb.curator.apply import region_sentinel_id
from agora_kb.curator.claim import LockHeld, claim, curator_lock
from agora_kb.curator.requeue import (
    RequeueItem,
    RequeueOutcome,
    Selector,
    StateUnreadable,
    archive_attempt_records,
    plan_requeue,
    run_requeue,
    select_failed_events,
)
from agora_kb.curator.subprocess_backend import BackendUnavailableError
from agora_kb.curator.worker import FakeBackend, _event_attempt_counts
from tests.curator.test_worker import (
    _bad_domain_plan,
    _create_theme_plan,
    _init_repo,
    _run,
    _seed_raw,
    _write_capture,
)

# `agora_kb.curator.__init__` re-exports the FUNCTION `claim`, which shadows the submodule of the
# same name on the package, so `import agora_kb.curator.claim as claim_mod` would bind the function.
claim_mod = importlib.import_module("agora_kb.curator.claim")

RUN_ID = "2026-06-13T03-00-00.000Z--04e370"
RUN_ID_2 = "2026-06-13T04-00-00.000Z--b17c91"
ALL = Selector(kind="all")


# --- fixtures -----------------------------------------------------------------------------------
def _terminal_event(
    layout: RepoLayout,
    *,
    second: int,
    writer: str = "dochan",
    run_id: str = RUN_ID,
    event_key: str | None = None,
) -> Path:
    """Produce ONE terminal-failure event at ``_kb/failed/<date>/<run-id>/<id>.md``.

    Built by ``Inbox.write`` and then MOVED, exactly as ``worker._fail`` produces one, so the bytes
    under test are a real event's bytes and not a hand-rolled approximation.
    """
    receipt = Inbox(layout).write(
        text=f"A terminal capture at second {second}.",
        writer=writer,
        source="claude-code",
        event_key=event_key,
        now=datetime(2026, 6, 13, 2, 40, second, tzinfo=UTC),
    )
    src = layout.inbox_item_path(writer, receipt.id)
    dest_dir = layout.failed_dir / run_id[:10] / run_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{receipt.id}.md"
    os.replace(src, dest)
    # A repo that has EVER had a terminal failure has necessarily had a curator run, and
    # `curator_lock` creates the 0-byte `_kb/curator.lock` on its first acquisition. The fixture
    # reproduces that, so the byte-identity assertions below are about requeue's own behaviour and
    # not about the documented R4 residual (a repo restored from backup WITHOUT the lock file).
    layout.lock_file.touch()
    return dest


def _error_record(layout: RepoLayout, *, event_ids: list[str], run_id: str = RUN_ID) -> Path:
    """Write the ``error.json`` retry record ``worker._fail`` writes beside a terminal event."""
    dest_dir = layout.failed_dir / run_id[:10] / run_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "error.json"
    record = {
        "run_id": run_id,
        "base_commit": "0" * 40,
        "event_ids": event_ids,
        "phase": "claimed",
        "failed_checks": ["TAXONOMY: unknown domain 'not-a-real-domain'"],
    }
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def _save_state(layout: RepoLayout, **event_keys: str) -> CuratorState:
    """Persist a state whose ``event_keys`` names ``<writer>:<key> -> <event_id>`` pairs."""
    state = CuratorState()
    for composite, event_id in event_keys.items():
        writer, _, key = composite.partition("__")
        state.record_event_key(writer, key, event_id)
    StateStore(layout).save(state)
    return state


def _tree_map(root: Path) -> dict[str, str | None]:
    """Every path under ``root``, mapped to its sha256 (None for directories)."""
    snapshot: dict[str, str | None] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        snapshot[key] = None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _normalized(items: tuple[RequeueItem, ...]) -> list[tuple[str, str, str | None, str]]:
    """Per-item lines with the plan-only ``movable`` folded into its executed form ``requeued``.

    That fold is the ONLY difference a correct dry-run may have from the real run, so normalizing it
    away turns "the preview equals the result" into a plain list equality.
    """
    return [
        (
            item.label,
            "requeued" if item.outcome is RequeueOutcome.movable else str(item.outcome),
            None if item.dest is None else item.dest.as_posix(),
            item.detail,
        )
        for item in items
    ]


def _terminalized_repo(tmp_path: Path) -> tuple[object, str]:
    """Drive a real curator to TERMINAL failure: 3 bad runs on one capture (``max_attempts=3``).

    The full machine, not a stand-in: the event in ``_kb/failed/`` and the three ``error.json``
    budget records are the ones ``worker._fail`` actually wrote, so the budget assertions below are
    about production data.
    """
    repo = _init_repo(tmp_path)
    event_id = _write_capture(
        Inbox(repo.layout), text="A fact in a non-existent domain.", second=10
    )
    _seed_raw(repo, event_id)
    for _ in range(3):
        report = _run(repo, FakeBackend(_bad_domain_plan(event_id), prose={"c1": "x"}))
        assert report.status == "failed"
    assert failed_event_count(repo.layout) == 1
    return repo, event_id


# --- crit 4 + the ADR-0002 spool-custodian rule --------------------------------------------------
def test_requeue_under_held_lock_is_clean_and_changes_nothing(tmp_path: Path) -> None:
    """(#99 crit 4) A run in progress ⇒ a refusal, not a traceback, and ``_kb/`` is untouched.

    ``curator_lock`` is non-blocking by design (ADR-0008 step 1), so contention surfaces as
    ``LockHeld``. Requeue must let it out CLEANLY — the caller renders one line and exits non-zero —
    and, critically, must not have moved anything first.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    _terminal_event(layout, second=11)

    with curator_lock(layout):
        before = _tree_map(layout.kb_dir)
        with pytest.raises(LockHeld):
            run_requeue(layout, selector=ALL)
        assert _tree_map(layout.kb_dir) == before  # not one byte, not one path


def test_requeue_takes_the_lock_before_touching_the_filesystem(tmp_path: Path) -> None:
    """R1: the lock is acquired BEFORE any mutation — single-writer discipline, not politeness.

    Patches the lock in the IMPORTING module (``agora_kb.curator.requeue``): every module here uses
    ``from … import curator_lock``, so patching the defining module would rebind a name requeue
    never reads and the test would pass vacuously (the ``tests/test_cli.py`` precedent).
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    before = _tree_map(layout.kb_dir)
    at_acquisition: list[dict[str, str | None]] = []

    @contextlib.contextmanager
    def spy_lock(lock_layout: RepoLayout) -> Iterator[None]:
        at_acquisition.append(_tree_map(lock_layout.kb_dir))
        yield

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(requeue_mod, "curator_lock", spy_lock)
        report = run_requeue(layout, selector=ALL)

    assert len(report.moved) == 1  # the batch really ran, so the assertion below is not vacuous
    assert at_acquisition == [before]  # nothing had changed by the time the lock was taken


def test_requeue_holds_the_lock_for_the_whole_batch(tmp_path: Path) -> None:
    """R1: ONE lock span covers scan → plan → every move, not one span per event.

    Per-event locking would let a curate run interleave, staling the tier-1 pre-check and the budget
    report mid-batch and making crit 5's dry-run promise unprovable. ``flock`` conflicts between two
    file descriptors even inside one process, so a second acquisition attempt is a faithful probe.
    """
    layout = RepoLayout(tmp_path)
    for second in (10, 11, 12):
        _terminal_event(layout, second=second)
    real_mover = requeue_mod.return_event_to_inbox
    probes = 0

    def probing_mover(mover_layout: RepoLayout, event_path: Path) -> InboxReturn:
        nonlocal probes
        probes += 1
        with pytest.raises(LockHeld):  # still held, at EVERY point in the batch
            with curator_lock(mover_layout):
                pass
        return real_mover(mover_layout, event_path)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(requeue_mod, "return_event_to_inbox", probing_mover)
        report = run_requeue(layout, selector=ALL)

    assert probes == 3
    assert len(report.moved) == 3


def test_requeue_never_writes_state_json(tmp_path: Path) -> None:
    """C4: ``_kb/state.json`` is never written — not ``event_keys``, not ``counters``, not
    ``last_failure``, and not under ``--force`` either.

    Each would be actively wrong: clearing a delivered key would risk re-publishing, decrementing
    ``counters.failed`` would rewrite history (the run really did fail), and clearing
    ``last_failure`` would make ``agora status`` print ``none`` while the brain is still dead — the
    exact blind spot #96 closed. Requeue does not fix a cause; it only relocates events.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10, event_key="k1")
    _save_state(layout, dochan__k1="2026-06-13T01-00-00.000Z--older1")
    before = layout.state_file.read_bytes()

    run_requeue(layout, selector=ALL)
    assert layout.state_file.read_bytes() == before

    # ...and the forced path, which is the one that knowingly creates a zombie. (The default run
    # above declined, so the event is still exactly where it was.)
    assert event.is_file()
    run_requeue(layout, selector=ALL, force=True)
    assert layout.inbox_item_path("dochan", event.stem).is_file()
    assert layout.state_file.read_bytes() == before


def test_requeue_never_touches_curated_artifacts(tmp_path: Path) -> None:
    """C4: ``wiki/``, ``raw/``, ``index.md``, ``log.md`` and git are all out of bounds.

    Requeue publishes nothing, so a real ``--all`` must leave the working tree clean and HEAD where
    it was. It deliberately uses ``RepoLayout`` rather than ``Repo.resolve`` for the same reason:
    the outage that produced the failures may well have broken git too.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    _terminal_event(layout, second=10)
    _terminal_event(layout, second=11)
    head = repo.head_commit()
    tracked = {
        name: _tree_map(layout.root / name) for name in ("wiki", "raw", "_meta", "_templates")
    }
    index_before = layout.index_file.read_bytes()
    assert (
        not layout.log_file.exists()
    )  # `repo init` seeds index.md only; log.md arrives on publish

    report = run_requeue(layout, selector=ALL)

    assert len(report.moved) == 2
    assert repo.head_commit() == head
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=layout.root, capture_output=True, text=True
    )
    assert status.stdout == ""  # nothing staged, nothing modified, nothing untracked
    assert {name: _tree_map(layout.root / name) for name in tracked} == tracked
    assert layout.index_file.read_bytes() == index_before
    assert not layout.log_file.exists()


def test_requeue_deletes_nothing(tmp_path: Path) -> None:
    """C5: the post path-set is a PERMUTATION of the pre path-set, never a shrink.

    Emptied ``_kb/failed/<date>/<run>/`` directories are left in place on purpose: tidying them is
    the one operation that could turn a bug in the mover into data loss, and litter is the cheaper
    price. This asserts the property directly, so an ``os.rmdir`` "cleanup" fails here.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    _terminal_event(layout, second=11, run_id=RUN_ID_2)
    _error_record(layout, event_ids=["whatever"])
    before = {path for path in layout.kb_dir.rglob("*") if path.is_file()}
    before_shas = sorted(hashlib.sha256(path.read_bytes()).hexdigest() for path in before)

    run_requeue(layout, selector=ALL)

    after = {path for path in layout.kb_dir.rglob("*") if path.is_file()}
    assert len(after) == len(before)
    assert sorted(hashlib.sha256(path.read_bytes()).hexdigest() for path in after) == before_shas
    assert (layout.failed_dir / RUN_ID[:10] / RUN_ID).is_dir()  # emptied, but still there


def test_requeue_contains_an_oserror_per_event_and_still_reports(tmp_path: Path) -> None:
    """R9: an OSError at item 3 of 5 must not hide the 2 events that already moved.

    ``cli.main`` has no global ``try/except``, so an exception escaping the batch would leave the
    operator with a traceback and no idea which events are now in the inbox. Containment is per
    item, and the batch continues.
    """
    layout = RepoLayout(tmp_path)
    events = [_terminal_event(layout, second=second) for second in range(10, 15)]
    real_mover = requeue_mod.return_event_to_inbox
    calls = 0

    def flaky_mover(mover_layout: RepoLayout, event_path: Path) -> InboxReturn:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("Read-only file system")
        return real_mover(mover_layout, event_path)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(requeue_mod, "return_event_to_inbox", flaky_mover)
        report = run_requeue(layout, selector=ALL)

    outcomes = [item.outcome for item in report.items]
    assert outcomes == [
        RequeueOutcome.requeued,
        RequeueOutcome.requeued,
        RequeueOutcome.error,
        RequeueOutcome.requeued,
        RequeueOutcome.requeued,
    ]
    assert "Read-only file system" in report.items[2].detail
    assert events[2].is_file()  # the failed one stayed put — contained, not lost
    assert failed_event_count(layout) == 1
    assert report.failed_events_after == 1


def test_the_requeue_precheck_and_the_claim_ask_one_function(tmp_path: Path) -> None:
    """R13: both the pre-check and the claim route through ``claim.is_already_delivered``.

    The whole value of the pre-check is that it answers the question the claim will actually ask.
    Two copies of "is this key delivered?" would drift, and the drift would be invisible until an
    operator's requeued event became a permanent inbox zombie.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10, event_key="k1")
    _save_state(layout, dochan__k1="2026-06-13T01-00-00.000Z--older1")
    asked: list[tuple[str, str | None]] = []

    def recording(state: CuratorState, *, writer: str, event_key: str | None) -> bool:
        asked.append((writer, event_key))
        return False

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(requeue_mod, "is_already_delivered", recording)
        run_requeue(layout, selector=ALL)
    assert asked == [("dochan", "k1")]

    # ...and the claim's tier-1 asks the same function (patched at ITS importing module, which is
    # the defining one here — `_dedup_tier1` resolves the name through module globals).
    asked.clear()
    snapshot = [("2026-06-13T02-40-10.000Z--aaaaaa", "dochan", "k1", "sha", Path("x.md"))]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(claim_mod, "is_already_delivered", recording)
        claim_mod._dedup_tier1(snapshot, CuratorState())
    assert asked == [("dochan", "k1")]


# --- crit 2 / crit 3: rename-only, non-destructive ------------------------------------------------
def test_requeue_is_rename_only_bytes_and_inode_preserved(tmp_path: Path) -> None:
    """(#99 crit 2) The event arrives at ``inbox/<writer>/<id>.md`` byte- AND inode-identical.

    Byte identity alone would still permit a copy; the inode is the proof that this was ONE
    ``os.replace`` of the original file, i.e. that no new event was minted with a fresh id and
    timestamp (which is how requeue would silently duplicate knowledge).
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    original = event.read_bytes()
    inode = event.stat().st_ino

    report = run_requeue(layout, selector=ALL)

    dest = layout.inbox_item_path("dochan", event.stem)
    assert [item.outcome for item in report.items] == [RequeueOutcome.requeued]
    assert report.items[0].dest == dest
    assert dest.read_bytes() == original
    assert dest.stat().st_ino == inode
    assert not event.exists()


def test_requeue_force_still_does_not_overwrite(tmp_path: Path) -> None:
    """(#99 crit 3) ``--force`` overrides the tier-1 pre-check ONLY — never an occupied destination.

    Overwriting an inbox event would destroy an immutable event (invariant 3), which no flag may
    authorize. The forced item therefore falls through to the next rung of the decline ladder.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10, event_key="k1")
    _save_state(layout, dochan__k1="2026-06-13T01-00-00.000Z--older1")
    squatter = layout.inbox_item_path("dochan", event.stem)
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("a DIFFERENT event already holds this address\n", encoding="utf-8")
    squatter_bytes = squatter.read_bytes()
    event_bytes = event.read_bytes()

    report = run_requeue(layout, selector=ALL, force=True)

    assert [item.outcome for item in report.items] == [RequeueOutcome.destination_exists]
    assert squatter.read_bytes() == squatter_bytes  # not overwritten
    assert event.read_bytes() == event_bytes  # and the source is untouched too
    assert "not overwritten" in report.items[0].detail


def test_requeue_decline_precedence_already_delivered_beats_destination_exists(
    tmp_path: Path,
) -> None:
    """The decline ladder is deterministic because the report's bytes are locked.

    An event that is BOTH already delivered and blocked by an occupied slot is reported as
    ``already-delivered``: that is the cause an operator can act on (it explains why ``--force``
    exists), whereas the occupied slot is a symptom of the same duplicate.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10, event_key="k1")
    _save_state(layout, dochan__k1="2026-06-13T01-00-00.000Z--older1")
    squatter = layout.inbox_item_path("dochan", event.stem)
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("occupied\n", encoding="utf-8")

    report = run_requeue(layout, selector=ALL)

    assert [item.outcome for item in report.items] == [RequeueOutcome.already_delivered]
    assert "state.event_keys" in report.items[0].detail


# --- crit 5: the dry run changes nothing and predicts exactly ------------------------------------
def test_requeue_dry_run_changes_not_one_byte(tmp_path: Path) -> None:
    """(#99 crit 5) A dry run over a whole ``failed/`` tree leaves the ``_kb/`` sha256 map intact.

    The mechanism is structural, not disciplinary: the plan is built by ``resolve_inbox_return``,
    which never even ``mkdir``s the writer directory.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    _terminal_event(layout, second=11, writer="web")
    _terminal_event(layout, second=12, run_id=RUN_ID_2)
    _error_record(layout, event_ids=["x"])
    before = _tree_map(layout.kb_dir)

    report = run_requeue(layout, selector=ALL, dry_run=True, reset_attempts=True)

    assert _tree_map(layout.kb_dir) == before
    assert len(report.moved) == 3
    assert report.failed_events_after == 0  # PREDICTED: the count the preview promises to produce


def test_dry_run_plan_equals_real_run(tmp_path: Path) -> None:
    """(#99 crit 5) The previewed per-item list EQUALS the executed one, modulo movable→requeued.

    One resolver produces both, so this is a property of the code shape rather than of care taken.
    The mixed tree matters: a decline must predict as the same decline, not just the movers.
    """
    layout = RepoLayout(tmp_path)
    movable = _terminal_event(layout, second=10)
    occupied = _terminal_event(layout, second=11)
    delivered = _terminal_event(layout, second=12, event_key="k1")
    _save_state(layout, dochan__k1="2026-06-13T01-00-00.000Z--older1")
    squatter = layout.inbox_item_path("dochan", occupied.stem)
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("occupied\n", encoding="utf-8")

    predicted = run_requeue(layout, selector=ALL, dry_run=True)
    actual = run_requeue(layout, selector=ALL)

    assert _normalized(predicted.items) == _normalized(actual.items)
    assert predicted.failed_events_after == actual.failed_events_after
    assert layout.inbox_item_path("dochan", movable.stem).is_file()
    assert delivered.is_file() and occupied.is_file()  # both declines really declined


def test_requeue_dry_run_on_a_pristine_repo_creates_no_kb_dir(tmp_path: Path) -> None:
    """(#99 crit 5 / R4) ``curator_lock`` CREATES ``_kb/`` + a 0-byte lock file on a fresh repo.

    So the empty spool is short-circuited by a pure ``failed_event_count`` read BEFORE the lock is
    opened. Without that, a dry run on a repo that has never failed would change the filesystem and
    criterion 5 would be literally false. The real run must be just as inert.
    """
    layout = RepoLayout(tmp_path)

    for dry_run in (True, False):
        report = run_requeue(layout, selector=ALL, dry_run=dry_run, reset_attempts=True)
        assert report.items == ()
        assert report.failed_events_after == 0
        assert not layout.kb_dir.exists()
        assert list(tmp_path.iterdir()) == []


# --- crit 6: the tier-1 pre-check ----------------------------------------------------------------
def test_requeue_skips_an_already_delivered_event_key(tmp_path: Path) -> None:
    """(#99 crit 6) Default = skip, and SAY so. The failure it prevents is not a second deletion.

    A returned already-delivered event becomes a permanent inbox ZOMBIE: ``claim()`` drops it on
    every run while the file sits there forever, and ``Inbox.write`` then answers every later
    capture of that key with the zombie's id. Quieter and worse than a visible failure.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10, event_key="k1")
    _save_state(layout, dochan__k1="2026-06-13T01-00-00.000Z--older1")

    report = run_requeue(layout, selector=ALL)

    assert [item.outcome for item in report.items] == [RequeueOutcome.already_delivered]
    assert report.items[0].detail.startswith("dochan:k1 is already in state.event_keys")
    assert "--force" in report.items[0].detail
    assert event.is_file()
    assert failed_event_count(layout) == 1


def test_requeue_force_moves_an_already_delivered_event_and_says_so(tmp_path: Path) -> None:
    """(#99 crit 6) ``--force`` moves it; the outcome stays ``forced`` so the report can say why.

    Collapsing ``forced`` into ``requeued`` would hide the one fact the operator most needs: this
    event is expected to be dropped again at claim time.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10, event_key="k1")
    _save_state(layout, dochan__k1="2026-06-13T01-00-00.000Z--older1")

    report = run_requeue(layout, selector=ALL, force=True)

    assert [item.outcome for item in report.items] == [RequeueOutcome.forced]
    assert report.items[0].dest == layout.inbox_item_path("dochan", event.stem)
    assert layout.inbox_item_path("dochan", event.stem).is_file()
    assert failed_event_count(layout) == 0


def test_forced_requeue_is_still_dropped_by_the_claim(tmp_path: Path) -> None:
    """(R7) The flag's consequence is PINNED, not hidden: tier-1 drops the forced event anyway.

    ``--force`` overrides requeue's pre-check, never the claim's authoritative tier-1 dedup
    (ADR-0011 §5). The event is back in the inbox and will stay there.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10, event_key="k1")
    state = _save_state(layout, dochan__k1="2026-06-13T01-00-00.000Z--older1")

    run_requeue(layout, selector=ALL, force=True)
    manifest = claim(
        layout,
        base_commit="0" * 40,
        run_id=RUN_ID_2,
        started="2026-06-13T04:00:00Z",
        state=state,
    )

    assert manifest is None  # every candidate dropped ⇒ no run at all
    assert layout.inbox_item_path("dochan", event.stem).is_file()  # the zombie, on disk forever


def test_forced_requeue_swallows_a_later_capture_of_the_same_key(tmp_path: Path) -> None:
    """(R7) The zombie also SWALLOWS future captures of its key — the quiet half of the damage.

    ``Inbox.write``'s best-effort idempotency scans the writer's pending inbox, finds the zombie
    and returns ``queued=False`` with its id. A fresh capture that should have been stored is
    silently answered with a stale one. This is why skip-by-default is non-negotiable.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10, event_key="k1")
    _save_state(layout, dochan__k1="2026-06-13T01-00-00.000Z--older1")

    run_requeue(layout, selector=ALL, force=True)
    receipt = Inbox(layout).write(
        text="A genuinely NEW capture that reuses the key.",
        writer="dochan",
        source="claude-code",
        event_key="k1",
    )

    assert receipt.queued is False
    assert receipt.id == event.stem


def test_requeue_moves_an_unkeyed_event_the_precheck_does_not_apply(tmp_path: Path) -> None:
    """(#99 crit 6) No ``event_key`` ⇒ never "already delivered" — the MAJORITY case.

    ``event_key`` is omitted from frontmatter when absent (``kb_remember`` without a key, web
    uploads, vault_import), and an event with no key is never de-duplicated by tier-1. Treating a
    missing key as a match would refuse to requeue almost everything.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    _save_state(layout, dochan__k1="2026-06-13T01-00-00.000Z--older1")

    report = run_requeue(layout, selector=ALL)

    assert report.items[0].event_key is None
    assert [item.outcome for item in report.items] == [RequeueOutcome.requeued]
    assert layout.inbox_item_path("dochan", event.stem).is_file()


def test_requeue_refuses_on_an_unreadable_state_json_without_force(tmp_path: Path) -> None:
    """(#99 crit 6) A corrupt ``state.json`` REFUSES the run — a check that evaporates is no check.

    ``StateStore.load`` deliberately raises rather than discarding ``event_keys`` (silently
    dropping them risks double-publish), so requeue inherits that posture and moves NOTHING.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    layout.state_file.parent.mkdir(parents=True, exist_ok=True)
    layout.state_file.write_text("{ this is not state", encoding="utf-8")

    with pytest.raises(StateUnreadable) as excinfo:
        run_requeue(layout, selector=ALL)

    assert "\n" not in str(excinfo.value)  # ONE operator-facing line
    assert event.is_file()
    assert failed_event_count(layout) == 1


def test_requeue_force_proceeds_on_an_unreadable_state_json(tmp_path: Path) -> None:
    """(#99 crit 6) ``--force`` relaxes the LOAD as well as the check, and flags that it did.

    An operator who has explicitly accepted the zombie risk should not additionally be blocked by a
    corrupt file they may be trying to escape — but the report must record that the pre-check never
    ran, so the face can say so.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10, event_key="k1")
    layout.state_file.parent.mkdir(parents=True, exist_ok=True)
    layout.state_file.write_text("{ this is not state", encoding="utf-8")

    report = run_requeue(layout, selector=ALL, force=True)

    assert report.precheck_skipped is True
    assert [item.outcome for item in report.items] == [RequeueOutcome.requeued]
    assert layout.inbox_item_path("dochan", event.stem).is_file()


# --- crit 7: the count drops by exactly the moved count -------------------------------------------
def test_failed_event_count_drops_by_exactly_the_moved_count(tmp_path: Path) -> None:
    """(#99 crit 7) ``failed_events`` falls by the number that moved — not by the number selected.

    The same helper backs ``agora status``, MCP ``kb_status`` and the Prometheus gauge, so an
    off-by-one here is an off-by-one in every observability surface at once.
    """
    layout = RepoLayout(tmp_path)
    for second in (10, 11, 12):
        _terminal_event(layout, second=second)
    blocked = _terminal_event(layout, second=13)
    squatter = layout.inbox_item_path("dochan", blocked.stem)
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("occupied\n", encoding="utf-8")
    before = failed_event_count(layout)
    assert before == 4

    report = run_requeue(layout, selector=ALL)

    assert len(report.moved) == 3
    assert before - failed_event_count(layout) == 3
    assert report.failed_events_after == failed_event_count(layout) == 1


# --- crit 9: the selectors move exactly their subset ----------------------------------------------
def test_requeue_run_selector_moves_only_that_run(tmp_path: Path) -> None:
    """(#99 crit 9) ``--run`` is the narrow, advertised selector; the other run is untouched."""
    layout = RepoLayout(tmp_path)
    mine = [_terminal_event(layout, second=second) for second in (10, 11)]
    theirs = _terminal_event(layout, second=12, run_id=RUN_ID_2)

    report = run_requeue(layout, selector=Selector(kind="run", value=RUN_ID))

    assert {item.source for item in report.moved} == set(mine)
    assert theirs.is_file()
    assert failed_event_count(layout) == 1


def test_requeue_event_selector_moves_exactly_one(tmp_path: Path) -> None:
    """(#99 crit 9) ``--event`` moves one event out of a run that holds several."""
    layout = RepoLayout(tmp_path)
    wanted = _terminal_event(layout, second=10)
    other = _terminal_event(layout, second=11)

    report = run_requeue(layout, selector=Selector(kind="event", value=wanted.stem))

    assert [item.source for item in report.moved] == [wanted]
    assert other.is_file()


def test_requeue_event_selector_accepts_the_frontmatter_id(tmp_path: Path) -> None:
    """(#99 crit 9) A hand-placed file whose STEM and frontmatter ``id`` disagree still matches.

    The report labels items by id, so an operator who pastes back a printed id must get a hit —
    otherwise the command contradicts its own output. The stem pass wins first; this is the
    fallback.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    renamed = event.with_name("hand-placed.md")
    os.replace(event, renamed)

    report = run_requeue(layout, selector=Selector(kind="event", value=event.stem))

    assert [item.source for item in report.moved] == [renamed]
    assert layout.inbox_item_path("dochan", event.stem).is_file()  # addressed by its OWN id


def test_requeue_all_moves_every_terminal_event(tmp_path: Path) -> None:
    """(#99 crit 9) ``--all`` spans run directories, dates and writer namespaces."""
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    _terminal_event(layout, second=11, writer="web", run_id=RUN_ID_2)
    _terminal_event(layout, second=12, writer="harvest-claude")

    report = run_requeue(layout, selector=ALL)

    assert len(report.moved) == 3
    assert failed_event_count(layout) == 0
    assert len(list(layout.inbox_dir.glob("*/*.md"))) == 3  # back in three writer namespaces


def test_requeue_without_reset_attempts_leaves_error_json_in_place(tmp_path: Path) -> None:
    """(#99 crit 9) The DEFAULT touches only ``*.md``: the retry records stay where they are.

    That retention IS the default budget policy — a requeued event keeps its spent attempts and so
    gets exactly one more run. Moving records without the flag would silently reset it.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    record = _error_record(layout, event_ids=[event.stem])
    record_bytes = record.read_bytes()

    report = run_requeue(layout, selector=ALL)

    assert len(report.moved) == 1
    assert report.archived == () and report.kept == ()
    assert record.read_bytes() == record_bytes
    assert not layout.requeued_dir.exists()


def test_requeue_run_selector_cannot_escape_the_failed_tree(tmp_path: Path) -> None:
    """(R6) The selector is a FILTER, so traversal is not representable — it just matches nothing.

    ``--run ../../..`` never reaches a filesystem API; it is compared against ``path.parent.name``
    over an enumerated set. A clean "no such run" beats a guard that has to be got right.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    outside = tmp_path.parent / "outside-the-repo"
    outside.mkdir(exist_ok=True)
    before = _tree_map(layout.kb_dir)

    for hostile in ("../../..", "/etc", "..", "../2026-06-13"):
        report = run_requeue(layout, selector=Selector(kind="run", value=hostile))
        assert report.items == ()
    assert select_failed_events(layout, Selector(kind="event", value="../../../etc/passwd")) == []

    assert _tree_map(layout.kb_dir) == before
    assert event.is_file()
    assert list(outside.iterdir()) == []


# --- crit 1 + crit 8: the retry budget -----------------------------------------------------------
def test_terminal_event_republishes_after_requeue(tmp_path: Path) -> None:
    """(#99 crit 1) The whole cycle: dead brain → terminal → fix → requeue → curate → PUBLISHED.

    Driven end-to-end on the real worker with a ``FakeBackend``, because the value of this test is
    that the SAME event — same id, same bytes — reaches the wiki. A live Ollama dogfood proves the
    brain, not the back-edge, and belongs to the release checklist rather than the suite.
    """
    repo, event_id = _terminalized_repo(tmp_path)
    layout = repo.layout
    terminal = layout.failed_dir / RUN_ID[:10]
    assert list(terminal.rglob(f"{event_id}.md"))
    original = next(iter(terminal.rglob(f"{event_id}.md"))).read_bytes()

    report = run_requeue(layout, selector=ALL)
    assert len(report.moved) == 1
    assert layout.inbox_item_path("dochan", event_id).read_bytes() == original

    published = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", event_id),
            prose={region_sentinel_id("ignored", "c1"): "One curator holds a per-repo flock."},
        ),
    )

    assert published.status == "published"
    assert failed_event_count(layout) == 0
    assert (layout.processed_dir / published.run_id[:10] / f"{event_id}.md").is_file()
    with repo.worktree(at=published.published_commit) as tree:
        assert (tree / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md").is_file()


def test_default_requeue_spends_its_one_remaining_attempt_then_returns_terminal(
    tmp_path: Path,
) -> None:
    """(#99 crit 8 / R10) The default gives exactly ONE more run — and the count is strictly
    monotone, which is the mechanical proof that requeue↔fail cannot loop forever.

    The budget is DERIVED from retained ``error.json`` records, so leaving them in place means the
    requeued event re-enters at ``prior == max_attempts``: the very next failing run is terminal
    again, one tick rather than three.
    """
    repo, event_id = _terminalized_repo(tmp_path)
    layout = repo.layout

    run_requeue(layout, selector=ALL)

    assert _event_attempt_counts(layout) == {event_id: 3}  # the spent budget survived the move
    assert not layout.requeued_dir.exists()

    again = _run(repo, FakeBackend(_bad_domain_plan(event_id), prose={"c1": "x"}))

    assert again.counts == {"retried": 0, "failed": 1}  # terminal on the FIRST re-failure
    assert _event_attempt_counts(layout) == {event_id: 4}  # 3 → 4: strictly monotone


def test_default_requeue_still_gets_one_real_attempt_when_the_cause_is_fixed(
    tmp_path: Path,
) -> None:
    """(#99 crit 8) The other half of the pinned sentence: if that one run publishes, it is saved.

    A budget that gave the requeued event ZERO attempts would make the command useless; one is
    exactly enough for the "fix the cause, then requeue" workflow the docs prescribe.
    """
    repo, event_id = _terminalized_repo(tmp_path)
    layout = repo.layout

    run_requeue(layout, selector=ALL)
    published = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", event_id),
            prose={region_sentinel_id("ignored", "c1"): "One curator holds a per-repo flock."},
        ),
    )

    assert published.status == "published"
    assert failed_event_count(layout) == 0
    assert StateStore(layout).load().last_commit == published.published_commit


def test_reset_attempts_archives_outside_failed_dir_and_restores_the_budget(
    tmp_path: Path,
) -> None:
    """(#99 crit 8) ``--reset-attempts`` really resets; the archive lands OUTSIDE ``failed_dir``.

    Both directions of "outside" are asserted, because only one of them is the bug: a record under
    ``failed_dir`` is still counted by ``rglob`` (so the reset would be a no-op), and a record that
    did not reach ``_kb/requeued/`` went somewhere nobody can find. The emptied source directory
    stays: requeue deletes nothing.
    """
    repo, event_id = _terminalized_repo(tmp_path)
    layout = repo.layout
    records = {path: path.read_bytes() for path in sorted(layout.failed_dir.rglob("error.json"))}
    assert len(records) == 3  # one per attempt (ADR-0011 §5.1)

    report = run_requeue(layout, selector=ALL, reset_attempts=True)

    assert _event_attempt_counts(layout) == {}
    assert len(report.archived) == 3 and report.kept == ()
    for archived in report.archived:
        dest = layout.root / archived.dest
        assert dest.read_bytes() == records[layout.root / archived.source]
        assert layout.failed_dir not in dest.parents  # not merely hidden inside failed/
        assert dest.relative_to(layout.requeued_dir)  # ...and genuinely under _kb/requeued/
        assert not (layout.root / archived.source).exists()
        assert (layout.root / archived.source).parent.is_dir()  # emptied, never removed

    again = _run(repo, FakeBackend(_bad_domain_plan(event_id), prose={"c1": "x"}))
    assert again.counts == {"retried": 1, "failed": 0}  # a FULL budget again


def test_a_dotted_archive_inside_failed_dir_would_not_reset_the_budget(tmp_path: Path) -> None:
    """(R8) The regression lock behind ``_kb/requeued/`` living outside ``_kb/failed/``.

    This test exists to stop a future tidy-up from moving the archive into
    ``_kb/failed/.archive/``: ``Path.rglob`` DESCENDS into dotted directories, so a record parked
    there is still counted by ``_event_attempt_counts`` and ``--reset-attempts`` would silently
    reset nothing at all — the worst kind of failure, since the command would still report success.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    hidden = layout.failed_dir / ".archive" / RUN_ID
    hidden.mkdir(parents=True)
    (hidden / "error.json").write_text(
        json.dumps({"run_id": RUN_ID, "event_ids": [event.stem]}), encoding="utf-8"
    )

    assert _event_attempt_counts(layout) == {event.stem: 1}  # rglob walked straight into ".archive"
    assert layout.failed_dir not in layout.requeued_dir.parents
    assert layout.requeued_dir.parent == layout.kb_dir


def test_reset_attempts_keeps_a_record_shared_with_an_event_that_stays_terminal(
    tmp_path: Path,
) -> None:
    """(#99 crit 8/9 / R8) The drain rule refuses to drop a budget an UNSELECTED event still needs.

    One ``error.json`` can govern many events. Archiving it because one of them was requeued would
    silently hand the others a fresh budget the operator never asked for — a selector violation
    expressed as a budget bug. Archiving nothing at all while events DID move is the loud-null
    outcome the face warns about, so the report must make that state visible.
    """
    layout = RepoLayout(tmp_path)
    moved = _terminal_event(layout, second=10)
    stays = _terminal_event(layout, second=11)
    record = _error_record(layout, event_ids=[moved.stem, stays.stem])
    record_bytes = record.read_bytes()

    report = run_requeue(
        layout, selector=Selector(kind="event", value=moved.stem), reset_attempts=True
    )

    assert len(report.moved) == 1
    assert report.archived == ()  # ...while ≥1 event moved: the loud-null-outcome state
    assert [(kept.source, kept.reason) for kept in report.kept] == [
        (record.relative_to(layout.root).as_posix(), "records an event that is still terminal")
    ]
    assert record.read_bytes() == record_bytes
    assert _event_attempt_counts(layout) == {moved.stem: 1, stays.stem: 1}


def test_reset_attempts_is_crash_convergent(tmp_path: Path) -> None:
    """(R9) A kill mid-archive converges on a re-run — it does not strand the leftover records.

    This is why the drain rule is keyed on the PREDICTED post-state rather than on "the ids I moved
    in this invocation": after a crash the events are already in the inbox, so a second invocation
    moves nothing, and an invocation-scoped rule would leave those records un-archivable FOREVER —
    a silent budget corruption nobody would ever be told about.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    first = _error_record(layout, event_ids=[event.stem])
    second = _error_record(layout, event_ids=[event.stem], run_id=RUN_ID_2)

    # The crash state: the event moved, record 1 archived, record 2 not yet.
    os.replace(event, layout.inbox_item_path("dochan", event.stem))
    layout.inbox_item_path("dochan", event.stem).parent.mkdir(parents=True, exist_ok=True)
    archived_first = layout.requeued_record_path(date=RUN_ID[:10], run_id=RUN_ID)
    archived_first.parent.mkdir(parents=True, exist_ok=True)
    os.replace(first, archived_first)
    assert failed_event_count(layout) == 0  # ZERO movable events on the re-run
    assert _event_attempt_counts(layout) == {event.stem: 1}  # ...and a leaked budget

    report = run_requeue(layout, selector=ALL, reset_attempts=True)

    assert report.moved == ()
    assert [record.source for record in report.archived] == [
        second.relative_to(layout.root).as_posix()
    ]
    assert _event_attempt_counts(layout) == {}
    assert not second.exists()


def test_dry_run_reset_attempts_moves_nothing_and_predicts_exactly(tmp_path: Path) -> None:
    """(#99 crit 5 ∩ crit 8) The previewed ``archived:``/``kept:`` lists EQUAL the real run's.

    The drain rule computes ``remaining`` identically in both modes — planned moves in dry-run,
    actual ones in a real run — which is what makes this a literal equality rather than a
    hand-checked resemblance.
    """
    layout = RepoLayout(tmp_path)
    moved = _terminal_event(layout, second=10)
    stays = _terminal_event(layout, second=11, run_id=RUN_ID_2)
    _error_record(layout, event_ids=[moved.stem])
    _error_record(layout, event_ids=[stays.stem], run_id=RUN_ID_2)
    before = _tree_map(layout.kb_dir)

    predicted = run_requeue(
        layout, selector=Selector(kind="run", value=RUN_ID), dry_run=True, reset_attempts=True
    )
    assert _tree_map(layout.kb_dir) == before  # not one byte

    actual = run_requeue(layout, selector=Selector(kind="run", value=RUN_ID), reset_attempts=True)

    assert predicted.archived == actual.archived
    assert predicted.kept == actual.kept
    assert len(actual.archived) == 1 and len(actual.kept) == 1
    assert _event_attempt_counts(layout) == {stays.stem: 1}


# --- the archive's own edge cases -----------------------------------------------------------------
def test_archive_reports_every_reason_it_declines(tmp_path: Path) -> None:
    """The ``kept:`` reason set is CLOSED and every member is reachable — silence is the failure.

    An unreadable or oddly-shaped record still holds a real budget, so dropping it from the report
    would leave an operator staring at a reset that did not reset. Each reason names a different
    thing to do about it.
    """
    layout = RepoLayout(tmp_path)
    unreadable = layout.failed_dir / RUN_ID[:10] / RUN_ID / "error.json"
    unreadable.parent.mkdir(parents=True)
    unreadable.write_text("{ not json", encoding="utf-8")
    no_ids = _error_record(layout, event_ids=[], run_id=RUN_ID_2)
    unsafe_dir = layout.failed_dir / "2026-06-13" / "..hostile"
    unsafe_dir.mkdir(parents=True)
    unsafe = unsafe_dir / "error.json"
    unsafe.write_text(json.dumps({"event_ids": ["e"]}), encoding="utf-8")

    archived, kept = archive_attempt_records(layout, remaining_failed_ids=set())

    assert archived == []
    assert {record.reason for record in kept} == {
        "unreadable record",
        "no event ids",
        "run directory name is not a safe path component",
    }
    assert unreadable.exists() and no_ids.exists() and unsafe.exists()  # nothing was moved


def test_archive_never_clobbers_an_existing_twin(tmp_path: Path) -> None:
    """C5 again, on the record path: an occupied archive destination is KEPT, never overwritten.

    Reachable after a partially-completed archive plus a hand-restored record, and the safe answer
    is always the same one requeue gives everywhere else — report it and leave both files alone.
    """
    layout = RepoLayout(tmp_path)
    record = _error_record(layout, event_ids=["gone"])
    twin = layout.requeued_record_path(date=RUN_ID[:10], run_id=RUN_ID)
    twin.parent.mkdir(parents=True)
    twin.write_text("an older archive of the same run\n", encoding="utf-8")
    twin_bytes = twin.read_bytes()

    archived, kept = archive_attempt_records(layout, remaining_failed_ids=set())

    assert archived == []
    assert [record.reason for record in kept] == ["destination already exists"]
    assert twin.read_bytes() == twin_bytes
    assert record.exists()


def test_plan_reports_an_unaddressable_event_as_unreadable_without_moving_it(
    tmp_path: Path,
) -> None:
    """(R6) A hand-edited frontmatter ``id``/``writer`` is a REPORTED decline, never a raise.

    ``_kb/failed/`` is operator-editable, so a hostile ``id: ../../../wiki/PWNED`` is reachable —
    and an exception out of the batch would strand every event after it. The two primitive statuses
    collapse into one operator-facing class here: "this file is not a usable event".
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    event.write_text(
        event.read_text(encoding="utf-8").replace(
            f"id: {event.stem}", "id: ../../../wiki/PWNED", 1
        ),
        encoding="utf-8",
    )

    report = run_requeue(layout, selector=ALL)

    assert [item.outcome for item in report.items] == [RequeueOutcome.unreadable]
    assert report.items[0].dest is None
    assert not (layout.root / "wiki" / "PWNED.md").exists()
    assert event.is_file()
    assert failed_event_count(layout) == 1


def test_plan_is_pure_over_a_whole_failed_tree(tmp_path: Path) -> None:
    """(#99 crit 5, structural half) Planning creates NOTHING — not even a writer directory.

    ``plan_requeue`` is what ``--dry-run`` runs, so its purity is the dry-run guarantee. Note the
    writer namespaces below do not exist yet: a plan that ``mkdir``s the destination's parent would
    fail this instantly.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    _terminal_event(layout, second=11, writer="web")
    shutil.rmtree(layout.inbox_dir)  # the writer namespaces must not exist when the plan runs
    before = _tree_map(layout.kb_dir)

    items = plan_requeue(layout, state=CuratorState(), selector=ALL)

    assert [item.outcome for item in items] == [RequeueOutcome.movable] * 2
    assert _tree_map(layout.kb_dir) == before
    assert not layout.inbox_dir.exists()  # a plan that mkdir'd the destination parent fails here


def test_selection_is_fifo_and_shared_with_the_failed_event_count(tmp_path: Path) -> None:
    """The preview and the execution walk ONE list in ONE order (event id ⇒ chronological).

    ``iter_failed_events`` is the same set ``failed_event_count`` counts; a divergence between the
    two would make ``failed_events:`` disagree with the list printed right above it.
    """
    layout = RepoLayout(tmp_path)
    late = _terminal_event(layout, second=30)
    early = _terminal_event(layout, second=10, run_id=RUN_ID_2)
    middle = _terminal_event(layout, second=20)
    _error_record(layout, event_ids=[late.stem])

    selected = select_failed_events(layout, ALL)

    assert selected == [early, middle, late]  # FIFO by event id, across run directories
    assert len(selected) == failed_event_count(layout)


def test_a_named_selector_that_matched_nothing_archives_nothing(tmp_path: Path) -> None:
    """A mistyped ``--run`` must not drain the repo's retry budget on the way to "not found".

    ``--reset-attempts`` scopes RECORDS while the selector scopes EVENTS, so the drain rule alone
    would happily release every record whose events are no longer terminal — on the strength of a
    run id that names nothing, under a report saying ``matched=0``. The operator pastes a stale id
    from scrollback and every pending event silently gets a fresh ``max_attempts``. ``--all`` is the
    opposite case and still drains: it asserts nothing, and its zero-match state IS the crash
    residue the rule exists to reclaim (``test_reset_attempts_is_crash_convergent``).
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    drained = _error_record(layout, event_ids=["an-event-that-is-long-gone"])
    os.replace(event, layout.inbox_item_path("dochan", event.stem))
    budget_before = _event_attempt_counts(layout)

    report = run_requeue(
        layout,
        selector=Selector(kind="run", value="2026-06-13T03-00-00.000Z--typoed"),
        reset_attempts=True,
    )

    assert report.items == () and report.archived == () and report.kept == ()
    assert drained.exists()
    assert _event_attempt_counts(layout) == budget_before

    # ...and the same repo under `--all` DOES reclaim it, so this is a scope rule, not a lockout.
    assert len(run_requeue(layout, selector=ALL, reset_attempts=True).archived) == 1


def test_reset_attempts_keeps_a_record_whose_terminal_event_was_renamed(tmp_path: Path) -> None:
    """(R8) The drain rule matches ``error.json``'s ids, so a renamed terminal file still counts.

    ``_kb/failed/`` is operator-editable and ``select_failed_events`` has a whole fallback pass for
    the stem/frontmatter-``id`` disagreement that produces (``test_requeue_event_selector_accepts_
    the_frontmatter_id``). A stem-only ``remaining`` set is blind to exactly those files, so the
    budget of an event still sitting in ``_kb/failed/`` would be reset — the one thing criterion 9
    forbids.
    """
    layout = RepoLayout(tmp_path)
    moved = _terminal_event(layout, second=10)
    stays = _terminal_event(layout, second=11)
    renamed = stays.with_name("operator-renamed.md")
    os.replace(stays, renamed)
    record = _error_record(layout, event_ids=[moved.stem, stays.stem])

    report = run_requeue(
        layout, selector=Selector(kind="event", value=moved.stem), reset_attempts=True
    )

    assert len(report.moved) == 1
    assert report.archived == ()
    assert [kept.reason for kept in report.kept] == ["records an event that is still terminal"]
    assert record.exists() and renamed.is_file()
    assert _event_attempt_counts(layout) == {moved.stem: 1, stays.stem: 1}


def test_plan_declines_a_second_claimant_of_one_inbox_address(tmp_path: Path) -> None:
    """(#99 crit 5) Two selected events resolving to ONE address: the preview says so too.

    Reachable because ``_kb/failed/`` is operator-editable input — a restore that merged two failed
    trees, or the ``--event`` id fallback matching several files. The resolver is stateless, so
    without intra-batch bookkeeping both plan ``movable``, the dry run promises two moves and a
    ``failed_events:`` of 0, and the real run delivers one. The second claimant is declined with the
    SAME detail execution would give it, so the two reports stay byte-identical.
    """
    layout = RepoLayout(tmp_path)
    first = _terminal_event(layout, second=10)
    duplicate = layout.failed_dir / RUN_ID_2[:10] / RUN_ID_2 / first.name
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_bytes(first.read_bytes())  # one event id under two run dirs

    predicted = run_requeue(layout, selector=ALL, dry_run=True)
    actual = run_requeue(layout, selector=ALL)

    assert _normalized(predicted.items) == _normalized(actual.items)
    assert predicted.failed_events_after == actual.failed_events_after == 1
    assert [item.outcome for item in actual.items] == [
        RequeueOutcome.requeued,
        RequeueOutcome.destination_exists,
    ]
    assert duplicate.is_file()  # the loser stayed exactly where it was


def test_archive_keeps_a_record_that_is_not_at_the_twin_depth(tmp_path: Path) -> None:
    """``rglob`` matches ``error.json`` at ANY depth; the archive is defined as the exact twin.

    A record parked directly under ``_kb/failed/`` would derive ``<date>/<run-id>`` from ``_kb`` and
    ``failed`` and land at an address ``cli._record_pointer`` can never follow — a real budget
    released to nowhere. Keeping it is the conservative answer: the budget stays counted and the
    reason names the shape that is wrong.
    """
    layout = RepoLayout(tmp_path)
    shallow = layout.failed_dir / "error.json"
    shallow.parent.mkdir(parents=True)
    shallow.write_text(json.dumps({"event_ids": ["gone"]}), encoding="utf-8")

    archived, kept = archive_attempt_records(layout, remaining_failed_ids=set())

    assert archived == []
    assert [record.reason for record in kept] == ["record is not at <date>/<run-id>/error.json"]
    assert shallow.exists() and not layout.requeued_dir.exists()


class _DeadBrain:
    """A configured brain that cannot answer — ``BackendUnavailableError`` at PASS 1.

    The shape of a dead Ollama or a missing CLI binary, and the ONE brain failure that still
    terminalises events (an UNCONFIGURED brain skips the tick entirely since #97, burning no
    attempt). ``author`` is unreachable: PLAN never returns.
    """

    def plan(self, bundle_dir: Path) -> str:  # noqa: ARG002 — the brain never gets that far
        raise BackendUnavailableError("ollama: connection refused")

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, object],
    ) -> None:  # pragma: no cover — PASS 1 always raises first
        raise AssertionError("a dead brain never reaches PASS 2")


def test_a_dead_brain_terminal_event_republishes_after_requeue(tmp_path: Path) -> None:
    """(#99 crit 1) The issue's literal sentence: brain DIES → terminal → fix → requeue → published.

    The other end-to-end lock terminalises with a brain that answers WRONGLY; this one terminalises
    with a brain that cannot answer at all (``BackendUnavailableError`` — a dead Ollama, the outage
    the whole command exists for). Both funnel through ``worker._fail``, but only this one is the
    acceptance criterion as written, so it is worth owning a test rather than a composition.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    event_id = _write_capture(Inbox(layout), text="A capture nobody could curate.", second=10)
    _seed_raw(repo, event_id)
    for _ in range(3):
        assert _run(repo, _DeadBrain()).status == "failed"
    assert failed_event_count(layout) == 1

    report = run_requeue(layout, selector=ALL, reset_attempts=True)

    assert len(report.moved) == 1 and failed_event_count(layout) == 0
    assert _event_attempt_counts(layout) == {}  # the cause is fixed: give it a full budget back
    published = _run(
        repo,
        FakeBackend(
            _create_theme_plan("ignored", "c1", event_id),
            prose={region_sentinel_id("ignored", "c1"): "One curator holds a per-repo flock."},
        ),
    )

    assert published.status == "published"
    assert failed_event_count(layout) == 0


def test_dry_run_creates_the_lock_file_when_the_repo_has_none(tmp_path: Path) -> None:
    """(R4, the DOCUMENTED residual) Pin it in the one state the other crit-5 tests cannot see.

    Every dry-run fixture ``touch``es ``_kb/curator.lock`` first, because a repo that has EVER had a
    terminal failure has necessarily taken the lock — so those tests are about requeue rather than
    about this. The uncovered state is a repo restored from backup (or a hand-deleted lock file)
    with ``_kb/failed/`` but no lock: ``curator_lock`` creates the 0-byte file and criterion 5 is
    literally false. This test exists so that residual is a decision on record and not a surprise:
    the lock file appears, and NOTHING else does.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    layout.lock_file.unlink()
    before = _tree_map(layout.kb_dir)

    report = run_requeue(layout, selector=ALL, dry_run=True, reset_attempts=True)

    assert len(report.moved) == 1
    assert layout.lock_file.read_bytes() == b""
    assert _tree_map(layout.kb_dir) == {
        **before,
        layout.lock_file.relative_to(layout.kb_dir).as_posix(): hashlib.sha256(b"").hexdigest(),
    }


def test_the_preflight_hook_runs_before_anything_moves(tmp_path: Path) -> None:
    """(#99 §4.3) The ``--all`` UNRESOLVED warning must reach the operator BEFORE the batch moves.

    That is why it is a hook the face passes IN rather than a second ``StateStore.load()`` the face
    does afterwards: the engine holds the lock, and only inside that span can a warning be both
    ordered before the moves and computed from the state the batch actually used. The hook also
    sees the same object ``plan_requeue`` does, so the face can never report a value the batch never
    saw. Asserted mechanically — the spool is still whole when the hook runs.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    _terminal_event(layout, second=11)
    seen: list[tuple[int, CuratorState]] = []

    report = run_requeue(
        layout,
        selector=ALL,
        preflight=lambda state: seen.append((failed_event_count(layout), state)),
    )

    assert len(report.moved) == 2
    assert [count for count, _ in seen] == [2]  # nothing had moved yet
    assert isinstance(seen[0][1], CuratorState)
    assert failed_event_count(layout) == 0
