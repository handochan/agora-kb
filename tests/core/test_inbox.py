"""Tests for the inbox write path (DESIGN §2.2, ADR-0002; DATA-MODEL §1)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agora_kb.core.frontmatter import parse
from agora_kb.core.hashing import content_sha256
from agora_kb.core.ids import is_valid_event_id
from agora_kb.core.inbox import Inbox
from agora_kb.core.layout import InvalidWriterError, RepoLayout
from agora_kb.core.models import Confidence, InboxItem, Kind

FIXED = datetime(2026, 6, 13, 10, 22, 33, tzinfo=UTC)


@pytest.fixture()
def inbox(tmp_path: Path) -> Inbox:
    return Inbox(RepoLayout(tmp_path))


def _read(path: Path) -> tuple[dict, str]:
    return parse(path.read_text(encoding="utf-8"))


def test_write_creates_event_at_namespaced_path(inbox: Inbox) -> None:
    receipt = inbox.write(text="remember this", writer="dochan", source="claude-code")
    assert receipt.queued is True
    assert is_valid_event_id(receipt.id)
    assert receipt.inbox_depth == 1
    path = inbox.layout.inbox_item_path("dochan", receipt.id)
    assert path.is_file()
    assert path.parent == inbox.layout.inbox_dir / "dochan"


def test_frontmatter_contract(inbox: Inbox) -> None:
    receipt = inbox.write(
        text="body text here",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        tags=["curator", "concurrency"],
        cwd="/Users/handochan/dev/x",
        now=FIXED,
    )
    fm, body = _read(inbox.layout.inbox_item_path("dochan", receipt.id))
    assert fm["id"] == receipt.id
    assert fm["source"] == "claude-code"
    assert fm["writer"] == "dochan"
    assert fm["target"] == "personal"  # default
    assert fm["domain"] == "ai-tech"
    assert fm["tags"] == ["curator", "concurrency"]
    assert fm["cwd"] == "/Users/handochan/dev/x"
    assert fm["created"] == "2026-06-13T10:22:33Z"
    assert fm["kind"] == "capture"
    assert fm["content_sha256"] == content_sha256("body text here")
    assert body == "body text here"
    # optional absent fields are omitted, not null
    assert "confidence" not in fm
    assert "event_key" not in fm
    assert "raw_ref" not in fm


def test_field_order_matches_spec(inbox: Inbox) -> None:
    receipt = inbox.write(
        text="x",
        writer="dochan",
        source="harvest:claude",
        kind=Kind.candidate,
        confidence=Confidence.low,
        event_key="k1",
        raw_ref="raw/ai-tech/foo.pdf",
        domain="ai-tech",
        tags=["t"],
        cwd="/tmp",
        now=FIXED,
    )
    fm, _ = _read(inbox.layout.inbox_item_path("dochan", receipt.id))
    # Parse the YAML block and assert key ORDER matches DATA-MODEL §1 (don't substring-scan, which a
    # value containing a "key:" token could fool).
    assert list(fm.keys()) == [
        "id",
        "source",
        "writer",
        "cwd",
        "target",
        "domain",
        "tags",
        "created",
        "kind",
        "confidence",
        "event_key",
        "content_sha256",
        "raw_ref",
    ]
    # ...and the optional values serialize correctly.
    assert fm["kind"] == "candidate"
    assert fm["confidence"] == "low"
    assert fm["event_key"] == "k1"
    assert fm["raw_ref"] == "raw/ai-tech/foo.pdf"


def test_empty_text_rejected(inbox: Inbox) -> None:
    for bad in ["", "   ", "\n\t "]:
        with pytest.raises(ValueError):
            inbox.write(text=bad, writer="dochan", source="manual")


def test_invalid_writer_rejected(inbox: Inbox) -> None:
    with pytest.raises(InvalidWriterError):
        inbox.write(text="x", writer="../evil", source="manual")


def test_invalid_source_rejected(inbox: Inbox) -> None:
    with pytest.raises(ValueError):
        inbox.write(text="x", writer="dochan", source="not-a-source")


def test_team_target_accepted(inbox: Inbox) -> None:
    receipt = inbox.write(text="x", writer="dochan", source="manual", target="team:engineering")
    fm, _ = _read(inbox.layout.inbox_item_path("dochan", receipt.id))
    assert fm["target"] == "team:engineering"


def test_event_key_idempotency_same_writer(inbox: Inbox) -> None:
    r1 = inbox.write(text="first", writer="dochan", source="manual", event_key="k1")
    r2 = inbox.write(text="second (ignored)", writer="dochan", source="manual", event_key="k1")
    assert r1.queued is True
    assert r2.queued is False
    assert r2.id == r1.id
    assert inbox.depth() == 1  # no duplicate event created
    # the original content is untouched (best-effort idempotency returns the first)
    _, body = _read(inbox.layout.inbox_item_path("dochan", r1.id))
    assert body == "first"


def test_event_key_namespaced_by_writer(inbox: Inbox) -> None:
    r1 = inbox.write(text="a", writer="alice", source="manual", event_key="k1")
    r2 = inbox.write(text="b", writer="bob", source="manual", event_key="k1")
    assert r2.id != r1.id  # same key, different writer => distinct events
    assert inbox.depth() == 2


def test_no_event_key_never_dedups(inbox: Inbox) -> None:
    inbox.write(text="dup", writer="dochan", source="manual")
    inbox.write(text="dup", writer="dochan", source="manual")
    assert inbox.depth() == 2  # identical content but no event_key => two events


def test_append_only_does_not_touch_existing(inbox: Inbox) -> None:
    r1 = inbox.write(text="one", writer="dochan", source="manual")
    p1 = inbox.layout.inbox_item_path("dochan", r1.id)
    before = p1.read_text(encoding="utf-8")
    mtime = p1.stat().st_mtime_ns
    inbox.write(text="two", writer="dochan", source="manual")
    assert p1.read_text(encoding="utf-8") == before
    assert p1.stat().st_mtime_ns == mtime


def test_depth_counts_across_writers(inbox: Inbox) -> None:
    assert inbox.depth() == 0
    inbox.write(text="a", writer="alice", source="manual")
    inbox.write(text="b", writer="bob", source="manual")
    inbox.write(text="c", writer="alice", source="manual")
    assert inbox.depth() == 3


def test_no_temp_files_left_behind(inbox: Inbox) -> None:
    inbox.write(text="x", writer="dochan", source="manual")
    leftover = list(inbox.layout.inbox_dir.glob("**/.*tmp*"))
    assert leftover == []


def test_receipt_as_dict(inbox: Inbox) -> None:
    receipt = inbox.write(text="x", writer="dochan", source="manual")
    d = receipt.as_dict()
    assert set(d) == {"id", "queued", "inbox_depth"}
    assert d["queued"] is True


def test_event_roundtrips_to_equal_item(inbox: Inbox) -> None:
    r = inbox.write(
        text="round trip body",
        writer="dochan",
        source="harvest:claude",
        target="team:eng",
        domain="ai-tech",
        tags=["a", "b"],
        cwd="/tmp/x",
        kind=Kind.candidate,
        confidence=Confidence.low,
        event_key="k1",
        raw_ref="raw/ai-tech/foo.pdf",
        now=FIXED,
    )
    fm, body = _read(inbox.layout.inbox_item_path("dochan", r.id))
    reloaded = InboxItem(**fm, body=body)  # parse back into a model
    assert reloaded.to_frontmatter() == fm  # stable round-trip (values + key order)
    assert reloaded.body == "round trip body"
    assert reloaded.created.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-06-13T10:22:33Z"


def test_created_is_second_precision_z(inbox: Inbox) -> None:
    micros = datetime(2026, 6, 13, 10, 22, 33, 481_000, tzinfo=UTC)
    r = inbox.write(text="x", writer="dochan", source="manual", now=micros)
    fm, _ = _read(inbox.layout.inbox_item_path("dochan", r.id))
    assert fm["created"] == "2026-06-13T10:22:33Z"  # no fractional seconds, explicit Z
    assert ".481Z--" in r.id  # the id, however, retains millisecond precision


def test_unicode_body_persisted_intact(inbox: Inbox) -> None:
    body = "café 한글 🚀\nsecond line"
    r = inbox.write(text=body, writer="dochan", source="manual")
    fm, got = _read(inbox.layout.inbox_item_path("dochan", r.id))
    assert got == body
    assert fm["content_sha256"] == content_sha256(body)


def test_invalid_target_rejected(inbox: Inbox) -> None:
    for bad in ["team:", "other", "team:bad/name"]:
        with pytest.raises(ValueError):
            inbox.write(text="x", writer="dochan", source="manual", target=bad)


def test_id_collision_refused(inbox: Inbox, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin a constant id so two writes collide; the append-only invariant must refuse the second.
    import agora_kb.core.inbox as inbox_mod

    monkeypatch.setattr(inbox_mod, "new_event_id", lambda **_: "2026-06-13T10-22-33.481Z--a1b2c3")
    inbox.write(text="first", writer="dochan", source="manual")
    with pytest.raises(FileExistsError):
        inbox.write(text="second", writer="dochan", source="manual")
    assert inbox.depth() == 1


def test_atomic_write_is_exclusive(inbox: Inbox) -> None:
    # Directly exercise the exclusive create (the TOCTOU backstop): a second write to the same path
    # must raise instead of clobbering, and must leave no temp behind.
    wdir = inbox.layout.inbox_writer_dir("dochan")
    wdir.mkdir(parents=True, exist_ok=True)
    target = wdir / "e.md"
    inbox._atomic_write(target, "first")
    with pytest.raises(FileExistsError):
        inbox._atomic_write(target, "second")
    assert target.read_text(encoding="utf-8") == "first"
    assert list(wdir.glob(".*tmp*")) == []


def test_event_key_collision_keeps_earliest(inbox: Inbox) -> None:
    # Plant two pending events (written out of FIFO order) sharing event_key=k1; the scan must
    # return the EARLIEST id (sorted glob, first match wins).
    from agora_kb.core import frontmatter as fm_mod

    wdir = inbox.layout.inbox_writer_dir("dochan")
    wdir.mkdir(parents=True, exist_ok=True)
    early = "2026-06-13T10-00-00.000Z--aaaaaa"
    late = "2026-06-13T11-00-00.000Z--bbbbbb"
    for eid in (late, early):
        (wdir / f"{eid}.md").write_text(
            fm_mod.render({"id": eid, "writer": "dochan", "event_key": "k1"}, "body"),
            encoding="utf-8",
        )
    r = inbox.write(text="new", writer="dochan", source="manual", event_key="k1")
    assert r.queued is False
    assert r.id == early


def test_no_temp_left_on_write_failure(inbox: Inbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.link", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        inbox.write(text="x", writer="dochan", source="manual")
    assert list(inbox.layout.inbox_dir.glob("**/.*tmp*")) == []
    assert inbox.depth() == 0


def test_depth_empty_no_inbox_dir(tmp_path: Path) -> None:
    ib = Inbox(RepoLayout(tmp_path))
    assert ib.depth() == 0
    assert not ib.layout.inbox_dir.exists()


def test_depth_glob_is_two_levels(inbox: Inbox) -> None:
    inbox.write(text="a", writer="dochan", source="manual")  # _kb/inbox/dochan/<id>.md
    # A stray .md directly under inbox/ and a deeper-nested .md must NOT be counted.
    inbox.layout.inbox_dir.joinpath("stray.md").write_text("x", encoding="utf-8")
    deep = inbox.layout.inbox_dir / "dochan" / "sub"
    deep.mkdir(parents=True, exist_ok=True)
    deep.joinpath("e.md").write_text("x", encoding="utf-8")
    assert inbox.depth() == 1
