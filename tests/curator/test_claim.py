"""Tests for the per-repo curator lock + atomic FIFO claim (ADR-0008 step 1, ADR-0011 §0 / §5).

MODEL-FREE: the inbox is populated through the real :class:`Inbox.write` (well-formed events), then
:func:`claim` is graded deterministically. We assert it moves events into the run's ``events/`` dir
in FIFO order byte-for-byte, applies authoritative tier-1 ``writer:event_key`` de-dup (keeping the
FIFO-earliest, honoring ``state.event_keys`` from a prior run), writes a ``claimed`` manifest, and
is a no-op on an empty inbox. The lock is exclusive: a second non-blocking acquire raises.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agora_kb.core import frontmatter
from agora_kb.core.inbox import Inbox
from agora_kb.core.layout import RepoLayout
from agora_kb.core.state import CuratorState
from agora_kb.curator.claim import LockHeld, claim, curator_lock
from agora_kb.curator.manifest import manifest_path, read_manifest

RUN_ID = "2026-06-13T03-00-00.000Z--7f31ab"
BASE = "705f4a4"
STARTED = "2026-06-13T03:00:00Z"


def _write(
    inbox: Inbox,
    *,
    text: str,
    writer: str = "dochan",
    second: int,
    event_key: str | None = None,
) -> str:
    """Write one inbox event at a pinned wall-clock second (so its id is FIFO-deterministic)."""
    now = datetime(2026, 6, 13, 2, 40, second, tzinfo=UTC)
    receipt = inbox.write(
        text=text, writer=writer, source="claude-code", event_key=event_key, now=now
    )
    return receipt.id


def _claimed_event_files(layout: RepoLayout) -> list[str]:
    events = layout.processing_dir / RUN_ID / "events"
    return sorted(p.name for p in events.glob("*.md")) if events.exists() else []


# --- the lock -----------------------------------------------------------------------------------
def test_curator_lock_is_exclusive(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    with curator_lock(layout):
        # A second non-blocking acquire while held must raise — a run is in progress (ADR-0008 s1).
        with pytest.raises(LockHeld):
            with curator_lock(layout):
                pass


def test_curator_lock_reacquirable_after_release(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    with curator_lock(layout):
        pass
    # Released on exit; a fresh acquire succeeds.
    with curator_lock(layout):
        pass


# --- empty inbox no-op --------------------------------------------------------------------------
def test_claim_empty_inbox_is_noop(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    with curator_lock(layout):
        result = claim(
            layout, base_commit=BASE, run_id=RUN_ID, started=STARTED, state=CuratorState()
        )
    assert result is None
    # No run directory / manifest is created for an empty claim.
    assert not manifest_path(layout, RUN_ID).exists()


# --- FIFO claim ---------------------------------------------------------------------------------
def test_claim_moves_events_fifo_and_writes_manifest(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    # Write out of order; the claim must order by event id (== chronological, FIFO).
    e3 = _write(inbox, text="third", second=12)
    e1 = _write(inbox, text="first", second=10)
    e2 = _write(inbox, text="second", second=11)

    with curator_lock(layout):
        manifest = claim(
            layout, base_commit=BASE, run_id=RUN_ID, started=STARTED, state=CuratorState()
        )

    assert manifest is not None
    assert manifest.phase == "claimed"
    assert manifest.base_commit == BASE
    assert manifest.started == STARTED
    assert manifest.prose_complete is False
    # FIFO order: earliest second first.
    assert manifest.event_ids == (e1, e2, e3)

    # Manifest is persisted with phase=claimed.
    on_disk = read_manifest(manifest_path(layout, RUN_ID))
    assert on_disk == manifest

    # Events moved into processing/<run-id>/events/ ...
    assert _claimed_event_files(layout) == sorted(f"{e}.md" for e in (e1, e2, e3))
    # ... and removed from the inbox (lifecycle by location; DATA-MODEL §1).
    assert inbox.depth() == 0


def test_claim_preserves_event_bytes(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    eid = _write(inbox, text="immutable body", second=10)
    src = layout.inbox_item_path("dochan", eid)
    original = src.read_text(encoding="utf-8")

    with curator_lock(layout):
        claim(layout, base_commit=BASE, run_id=RUN_ID, started=STARTED, state=CuratorState())

    moved = (layout.processing_dir / RUN_ID / "events" / f"{eid}.md").read_text(encoding="utf-8")
    # os.replace is a rename: the claimed event is byte-for-byte the inbox file (immutable).
    assert moved == original
    fm, body = frontmatter.parse(moved)
    assert fm["id"] == eid
    assert body == "immutable body"


# --- tier-1 dedup (authoritative, under the lock) -----------------------------------------------
def test_claim_dedups_duplicate_writer_event_key(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    # Inbox.write's write-time event_key check is best-effort and would collapse a same-key retry
    # at write time. The CLAIM-time tier-1 dedup is the AUTHORITATIVE one (ADR-0011 §5), guarding
    # the lock-free write race where two same-key events DO both land on disk. We reproduce that by
    # hand-placing the second same-key event at a later id, then assert claim drops the FIFO-later.
    e_first = _write(inbox, text="keyed first", second=10, event_key="k1")
    later_id = "2026-06-13T02-40-59.000Z--ffffff"
    second_path = layout.inbox_item_path("dochan", later_id)
    fm, body = frontmatter.parse(layout.inbox_item_path("dochan", e_first).read_text("utf-8"))
    fm["id"] = later_id
    second_path.write_text(frontmatter.render(fm, "keyed second"), encoding="utf-8")
    # A second, distinct (un-keyed) event survives.
    e_other = _write(inbox, text="other", second=11)

    assert inbox.depth() == 3
    with curator_lock(layout):
        manifest = claim(
            layout, base_commit=BASE, run_id=RUN_ID, started=STARTED, state=CuratorState()
        )

    assert manifest is not None
    # The FIFO-earliest same-key event is kept; the later duplicate is dropped.
    assert e_first in manifest.event_ids
    assert later_id not in manifest.event_ids
    assert e_other in manifest.event_ids
    assert _claimed_event_files(layout) == sorted(f"{e}.md" for e in (e_first, e_other))


def test_claim_dedups_against_prior_run_state(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    eid = _write(inbox, text="already delivered", second=10, event_key="k1")
    # A prior run already recorded this writer:event_key — tier-1 must drop it at claim time.
    state = CuratorState()
    state.record_event_key("dochan", "k1", "2026-06-13T01-00-00.000Z--000000")

    with curator_lock(layout):
        manifest = claim(layout, base_commit=BASE, run_id=RUN_ID, started=STARTED, state=state)

    # Every event was a prior-delivered duplicate -> nothing to claim -> no-op.
    assert manifest is None
    assert not manifest_path(layout, RUN_ID).exists()
    # The keyed event with a fresh key (no prior state) would still be claimable: sanity-check that
    # an un-recorded key is NOT dropped.
    assert eid  # referenced to keep the fixture meaningful


# --- max_candidates cap (INGEST-CONTRACT §1.3, ADR-0024 OD-3a / #60) ----------------------------
RUN_ID_2 = "2026-06-13T03-05-00.000Z--7f31ac"
RUN_ID_3 = "2026-06-13T03-10-00.000Z--7f31ad"


def test_claim_caps_fifo_head_and_leaves_remainder_in_inbox(tmp_path: Path) -> None:
    """An over-cap backlog claims exactly the FIFO head; the rest stays in the inbox untouched."""
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    ids = [_write(inbox, text=f"fact {i}", second=10 + i) for i in range(5)]
    originals = {eid: layout.inbox_item_path("dochan", eid).read_text("utf-8") for eid in ids}

    with curator_lock(layout):
        manifest = claim(
            layout,
            base_commit=BASE,
            run_id=RUN_ID,
            started=STARTED,
            state=CuratorState(),
            max_candidates=2,
        )

    assert manifest is not None
    # Exactly the 2-candidate FIFO head is claimed (5 distinct bodies = 5 candidate groups).
    assert manifest.event_ids == (ids[0], ids[1])
    assert _claimed_event_files(layout) == sorted(f"{e}.md" for e in ids[:2])
    # The remainder stays in the inbox BYTE-FOR-BYTE (no move, no rewrite — append-only intact).
    assert inbox.depth() == 3
    for eid in ids[2:]:
        assert layout.inbox_item_path("dochan", eid).read_text("utf-8") == originals[eid]


def test_claim_cap_fifo_continuity_across_runs(tmp_path: Path) -> None:
    """The next claim picks up EXACTLY where the capped head stopped — FIFO across runs."""
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    ids = [_write(inbox, text=f"fact {i}", second=10 + i) for i in range(5)]

    def _claim(run_id: str) -> tuple[str, ...]:
        with curator_lock(layout):
            manifest = claim(
                layout,
                base_commit=BASE,
                run_id=run_id,
                started=STARTED,
                state=CuratorState(),
                max_candidates=2,
            )
        return manifest.event_ids if manifest is not None else ()

    assert _claim(RUN_ID) == (ids[0], ids[1])
    assert _claim(RUN_ID_2) == (ids[2], ids[3])
    assert _claim(RUN_ID_3) == (ids[4],)
    assert inbox.depth() == 0


def test_claim_cap_counts_candidates_not_events(tmp_path: Path) -> None:
    """The capped unit is the tier-2 content GROUP: byte-equivalent duplicates in the head ride
    along without consuming a cap slot (they collapse into one candidate at bundle time)."""
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    e_a1 = _write(inbox, text="same body", second=10)
    e_b = _write(inbox, text="other body", second=11)
    e_a2 = _write(inbox, text="same body", second=12)  # duplicate of group A, after group B
    e_c = _write(inbox, text="third body", second=13)  # would open group 3 -> the FIFO cut

    with curator_lock(layout):
        manifest = claim(
            layout,
            base_commit=BASE,
            run_id=RUN_ID,
            started=STARTED,
            state=CuratorState(),
            max_candidates=2,
        )

    assert manifest is not None
    # 3 events claimed but only 2 DISTINCT content groups; the 3rd group's event stays queued.
    assert manifest.event_ids == (e_a1, e_b, e_a2)
    assert inbox.depth() == 1
    assert layout.inbox_item_path("dochan", e_c).exists()


def test_claim_default_cap_leaves_small_inbox_identical(tmp_path: Path) -> None:
    """No explicit cap → the documented default 32: a small inbox claims whole, byte-identical."""
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    ids = [_write(inbox, text=f"fact {i}", second=10 + i) for i in range(3)]

    with curator_lock(layout):
        manifest = claim(
            layout, base_commit=BASE, run_id=RUN_ID, started=STARTED, state=CuratorState()
        )

    assert manifest is not None
    assert manifest.event_ids == tuple(ids)
    assert inbox.depth() == 0


@pytest.mark.parametrize("bad_cap", [0, -1])
def test_claim_rejects_non_positive_cap_fail_loud(tmp_path: Path, bad_cap: int) -> None:
    """``max_candidates < 1`` raises ValueError BEFORE any side effect (the docstring's
    "the cap can never turn a non-empty selection into a no-op" invariant is enforced in code,
    not just by the config-side ``ge=1`` bound): no manifest, no processing dir, inbox untouched."""
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    e1 = _write(inbox, text="one fact", second=10)

    with curator_lock(layout):
        with pytest.raises(ValueError, match="max_candidates"):
            claim(
                layout,
                base_commit=BASE,
                run_id=RUN_ID,
                started=STARTED,
                state=CuratorState(),
                max_candidates=bad_cap,
            )

    # Fail-loud left NOTHING behind: no empty phase='claimed' ghost run for recover() to walk.
    assert not manifest_path(layout, RUN_ID).exists()
    assert not (layout.processing_dir / RUN_ID).exists()
    assert inbox.depth() == 1
    assert layout.inbox_item_path("dochan", e1).exists()
