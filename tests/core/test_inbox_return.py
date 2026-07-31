"""Tests for the inbox back-edge: returning an EXISTING event to its address (issue #99 WU-1).

``return_event_to_inbox`` is the one mover shared by the curator's retry/recovery paths and by
``agora requeue``. It is a *location-only* transition of an immutable event (DATA-MODEL §1), so
what is locked here is exactly what must never change: byte identity, non-destruction of an
occupied destination, refusal (never a raise) on an unaddressable writer/id, and — the property
``agora requeue --dry-run`` rests on — that the RESOLVER touches nothing at all.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from agora_kb.core import frontmatter
from agora_kb.core.inbox import (
    Inbox,
    failed_event_count,
    iter_failed_events,
    resolve_inbox_return,
    return_event_to_inbox,
)
from agora_kb.core.layout import RepoLayout

RUN_ID = "2026-06-13T03-00-00.000Z--04e370"
RUN_DATE = RUN_ID[:10]


def _terminal_event(
    layout: RepoLayout, *, second: int, writer: str = "dochan", run_id: str = RUN_ID
) -> Path:
    """Produce a terminal-failure event at ``_kb/failed/<date>/<run-id>/<id>.md``.

    Built by ``Inbox.write`` and then MOVED, exactly as ``worker._fail`` produces one, so the bytes
    under test are a real event's bytes and not a hand-rolled approximation.
    """
    receipt = Inbox(layout).write(
        text=f"A terminal capture at second {second}.",
        writer=writer,
        source="claude-code",
        now=datetime(2026, 6, 13, 2, 40, second, tzinfo=UTC),
    )
    src = layout.inbox_item_path(writer, receipt.id)
    dest_dir = layout.failed_dir / run_id[:10] / run_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{receipt.id}.md"
    os.replace(src, dest)
    return dest


def _rewrite_frontmatter(path: Path, **fields: object) -> None:
    """Hand-edit an event's frontmatter — what ``_kb/failed/`` being operator-editable permits."""
    fm, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    fm.update(fields)
    path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def _tree_map(root: Path) -> dict[str, str | None]:
    """Every path under ``root``, mapped to its sha256 (None for directories)."""
    snapshot: dict[str, str | None] = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        snapshot[key] = None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def test_returned_event_is_byte_identical_and_source_is_gone(tmp_path: Path) -> None:
    """(#99 crit 2) A return MOVES the event: same bytes, same id, at the canonical inbox address.

    The proof that requeue did not mint a NEW event via ``Inbox.write`` (which would give a fresh
    id + timestamp and duplicate the knowledge) is byte identity plus the inode: one ``os.replace``,
    not a copy.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    event_id = event.stem
    original = event.read_bytes()
    original_inode = event.stat().st_ino

    verdict = return_event_to_inbox(layout, event)

    assert verdict.ok
    assert verdict.status == "ok"
    assert verdict.detail == ""
    assert verdict.dest == layout.inbox_item_path("dochan", event_id)
    assert not event.exists()  # the source is gone — moved, never copied
    assert verdict.dest is not None
    assert verdict.dest.read_bytes() == original
    assert verdict.dest.stat().st_ino == original_inode
    assert failed_event_count(layout) == 0


def test_occupied_destination_is_skipped_and_reported(tmp_path: Path) -> None:
    """(#99 crit 3) An occupied destination is a REPORTED refusal — never an overwrite.

    Overwriting would destroy an immutable inbox event (invariant 3). Both files must survive
    untouched, and the verdict must still carry ``dest`` so the caller can name the collision.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    source_bytes = event.read_bytes()
    dest = layout.inbox_item_path("dochan", event.stem)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("an idempotent duplicate already sitting in the slot\n", encoding="utf-8")
    dest_bytes = dest.read_bytes()

    verdict = return_event_to_inbox(layout, event)

    assert verdict.status == "occupied"
    assert verdict.ok is False
    assert verdict.dest == dest
    assert "not overwritten" in verdict.detail
    assert event.read_bytes() == source_bytes  # source untouched
    assert dest.read_bytes() == dest_bytes  # destination untouched
    assert failed_event_count(layout) == 1


def test_return_rejects_traversal_event_id(tmp_path: Path) -> None:
    """(#99 R6) A hostile frontmatter ``id`` never becomes a path component.

    ``layout.inbox_item_path`` validates the WRITER but interpolates the id verbatim, so
    ``id: ../../../wiki/PWNED`` resolves inside the git-tracked read model only the curator may
    write (invariant 2). ``_kb/failed/`` is operator-editable and is requeue's INPUT, so the id
    goes through the canonical :func:`is_valid_event_id` rather than a second, private rule.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    _rewrite_frontmatter(event, id="../../../wiki/PWNED")
    before = event.read_bytes()

    verdict = return_event_to_inbox(layout, event)

    assert verdict.status == "unaddressable"
    assert verdict.dest is None  # no address was derived, so none can be acted on
    assert "not a valid event id" in verdict.detail
    assert not (tmp_path / "wiki" / "PWNED.md").exists()
    assert not list(tmp_path.glob("wiki/**/PWNED*"))
    assert event.read_bytes() == before  # the refusal left the source in place


def test_return_rejects_unsafe_writer_without_raising(tmp_path: Path) -> None:
    """(#99 R6) An unsafe ``writer`` is a VERDICT, not an ``InvalidWriterError``.

    Every caller runs a disposal loop — the curator's failure/recovery paths, requeue's batch — so
    a raise would strand every event after this one and, in ``cli.main`` (no global try/except),
    surface as a traceback.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    _rewrite_frontmatter(event, writer="../../../../etc")
    before = event.read_bytes()

    verdict = return_event_to_inbox(layout, event)  # must not raise

    assert verdict.status == "unaddressable"
    assert verdict.dest is None
    assert "invalid writer" in verdict.detail
    assert event.read_bytes() == before
    assert not (tmp_path.parent / "etc").exists()


def test_return_reports_unreadable_frontmatter(tmp_path: Path) -> None:
    """An unparseable event, and one whose frontmatter lacks a usable address, both report cleanly.

    Two different operator mistakes, one class of outcome: the file is not a usable event, so it
    stays where it is and the detail says why on ONE line (the report prints it inline).
    """
    layout = RepoLayout(tmp_path)
    failed_dir = layout.failed_dir / RUN_DATE / RUN_ID
    failed_dir.mkdir(parents=True, exist_ok=True)
    garbage = failed_dir / "2026-06-13T02-40-10.000Z--aaaaaa.md"
    garbage.write_text("no frontmatter fence here at all\n", encoding="utf-8")
    headless = _terminal_event(layout, second=20)
    _rewrite_frontmatter(headless, writer=None)

    garbage_verdict = return_event_to_inbox(layout, garbage)
    headless_verdict = return_event_to_inbox(layout, headless)

    assert garbage_verdict.status == "unreadable"
    assert garbage_verdict.dest is None
    assert "frontmatter is unreadable" in garbage_verdict.detail
    assert "\n" not in garbage_verdict.detail  # ONE operator-facing line
    assert headless_verdict.status == "unreadable"
    assert headless_verdict.detail == "frontmatter is missing a string 'writer' or 'id'"
    assert garbage.exists() and headless.exists()
    assert failed_event_count(layout) == 2


def test_resolve_is_pure(tmp_path: Path) -> None:
    """(#99 crit 5, structural half) The resolver creates NOTHING — not even a writer directory.

    ``agora requeue --dry-run`` promises a byte-identical filesystem, and it can promise it because
    planning is a different function from moving: ``resolve_inbox_return`` never calls
    ``dest.parent.mkdir``. Running it over a whole ``_kb/failed/`` tree — movable, occupied,
    unaddressable and unreadable alike — must leave the repo's path SET and every byte identical.
    """
    layout = RepoLayout(tmp_path)
    movable = _terminal_event(layout, second=10)
    occupied = _terminal_event(layout, second=20, writer="web")
    hostile_id = _terminal_event(layout, second=30)
    _rewrite_frontmatter(hostile_id, id="../../../wiki/PWNED")
    hostile_writer = _terminal_event(layout, second=40)
    _rewrite_frontmatter(hostile_writer, writer="../nope")
    # The occupied case needs an inbox slot held; every OTHER writer dir is removed so that a
    # resolver that mkdir'd its destination would show up as a new path in the snapshot.
    held = layout.inbox_item_path("web", occupied.stem)
    held.parent.mkdir(parents=True, exist_ok=True)
    held.write_text("holding the slot\n", encoding="utf-8")
    dochan_dir = layout.inbox_dir / "dochan"
    if dochan_dir.exists():
        dochan_dir.rmdir()

    before = _tree_map(tmp_path)
    verdicts = [resolve_inbox_return(layout, path) for path in iter_failed_events(layout)]
    after = _tree_map(tmp_path)

    assert after == before
    assert not dochan_dir.exists()  # the movable event's writer dir was NOT created
    assert sorted(v.status for v in verdicts) == [
        "occupied",
        "ok",
        "unaddressable",
        "unaddressable",
    ]
    # And the pure verdict agrees with what the mover would do (one resolver, one answer).
    assert return_event_to_inbox(layout, movable).status == "ok"


def test_iter_failed_events_matches_failed_event_count(tmp_path: Path) -> None:
    """(#99 crit 7, structural half) The enumerator and the counter describe ONE set.

    ``agora requeue`` selects from :func:`iter_failed_events` while ``agora status`` /
    MCP ``kb_status`` / the Prometheus exporter report :func:`failed_event_count`, so "failed_events
    dropped by exactly the moved count" only holds if the two can never disagree. The counter keeps
    its own lazy body (it is on the scrape path); THIS test is what ties them together.
    """
    layout = RepoLayout(tmp_path)
    assert iter_failed_events(layout) == []  # absent dir: a fresh repo has never failed
    assert failed_event_count(layout) == 0

    one = _terminal_event(layout, second=10)
    two = _terminal_event(layout, second=20, writer="web")
    other_run = "2026-06-14T03-00-00.000Z--beefed"
    three = _terminal_event(layout, second=30, run_id=other_run)
    # Non-events that share the tree: the retry record and operator litter are NOT failed events.
    (one.parent / "error.json").write_text('{"event_ids": []}\n', encoding="utf-8")
    (one.parent / "notes.txt").write_text("scratch\n", encoding="utf-8")

    events = iter_failed_events(layout)

    assert len(events) == failed_event_count(layout) == 3
    assert set(events) == {one, two, three}
    # FIFO by event id (time-sortable ⇒ chronological), so preview and execution walk one order.
    assert [path.stem for path in events] == sorted(path.stem for path in events)
