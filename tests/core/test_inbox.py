"""Tests for the inbox write path (DESIGN §2.2, ADR-0002; DATA-MODEL §1)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agora_kb.config import ReadOnlySchemaVersionError
from agora_kb.core.frontmatter import parse
from agora_kb.core.hashing import content_sha256
from agora_kb.core.ids import is_valid_event_id
from agora_kb.core.inbox import Inbox, failed_event_count
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


def test_last_write_none_on_empty(tmp_path: Path) -> None:
    assert Inbox(RepoLayout(tmp_path)).last_write() is None


def test_last_write_returns_newest_event_timestamp(inbox: Inbox) -> None:
    early = datetime(2026, 6, 13, 2, 0, 0, tzinfo=UTC)
    late = datetime(2026, 6, 13, 5, 30, 0, tzinfo=UTC)
    inbox.write(text="a", writer="dochan", source="manual", now=early)
    inbox.write(text="b", writer="alice", source="manual", now=late)  # across writer namespaces
    # The newest event id (time-sortable) wins, regardless of which writer wrote it.
    assert inbox.last_write() == late


def test_last_write_ignores_foreign_filenames(inbox: Inbox) -> None:
    ts = datetime(2026, 6, 13, 2, 0, 0, tzinfo=UTC)
    inbox.write(text="a", writer="dochan", source="manual", now=ts)
    inbox.layout.inbox_dir.joinpath("dochan", "not-an-event.md").write_text("x", encoding="utf-8")
    assert inbox.last_write() == ts


def test_failed_event_count_counts_the_nested_layout(tmp_path: Path) -> None:
    """Terminal failures are NESTED at ``failed/<date>/<run-id>/<event>.md`` — so count RECURSIVELY.

    #96 crit 8, half 1: ``agora status``'s ``failed_events`` and MCP ``kb_status.failed`` now share
    THIS one function, so the on-disk shape it has to understand is locked here. A direct-children
    ``glob("*.md")`` reports 0 on exactly this (real) layout — that was the bug the promotion must
    not re-introduce — and the sibling ``error.json`` retry record is not an event.
    """
    layout = RepoLayout(tmp_path)
    assert failed_event_count(layout) == 0  # absent dir: a fresh repo has never failed

    run_dir = layout.failed_dir / "2026-06-13" / "20260613T000000Z-deadbeef"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "20260613T024010Z-a1b2c3.md").write_text("oops\n", encoding="utf-8")
    (run_dir / "20260613T024011Z-d4e5f6.md").write_text("oops\n", encoding="utf-8")
    (run_dir / "error.json").write_text('{"failed_checks": []}\n', encoding="utf-8")

    assert failed_event_count(layout) == 2
    assert list(layout.failed_dir.glob("*.md")) == []  # the non-recursive glob would say 0


# --- ADR-0041 D6: the KB-schema write refusal, at the one write primitive ------------------------


def _declare_schema(layout: RepoLayout, version: int | None, *, text: str | None = None) -> None:
    """Write ``_meta/taxonomy.yaml`` declaring ``version`` (or ``text`` verbatim)."""
    layout.meta_dir.mkdir(parents=True, exist_ok=True)
    body = text if text is not None else f"schema_version: {version}\ndomains: [general]\n"
    (layout.meta_dir / "taxonomy.yaml").write_text(body, encoding="utf-8")


def test_a_capture_into_a_schema_1_repo_refuses_rather_than_queueing(tmp_path: Path) -> None:
    """ADR-0041 D6: an inbox that can never drain is silent data loss dressed as success.

    The curator of this build writes schema 2 — schema-2 paths and schema-2 frontmatter — so an
    event accepted into a schema-1 repo could never be consolidated, and a re-import into a NEW
    repo (the one crossing that exists) would orphan it. So the write REFUSES, loudly, naming
    ``agora import --from-kb``, and leaves the inbox untouched.

    The gate lives on ``Inbox.write`` and not on a face because with SUPPORTED_KB_SCHEMA_VERSIONS
    widened to {1, 2} every entry-point guard PASSES for a schema-1 repo (reads must keep working),
    so a construction-time guard structurally cannot let ``kb_query`` through while refusing
    ``kb_remember`` on the same server object.
    """
    layout = RepoLayout(tmp_path)
    _declare_schema(layout, 1)

    with pytest.raises(ReadOnlySchemaVersionError) as exc:
        Inbox(layout).write(text="remember this", writer="dochan", source="claude-code")

    assert "agora import --from-kb" in str(exc.value)
    assert Inbox(layout).depth() == 0
    assert not layout.inbox_dir.exists()


def test_a_capture_into_a_schema_2_repo_is_accepted(tmp_path: Path) -> None:
    """The other side of the same predicate — the current schema is writable, unremarkably."""
    layout = RepoLayout(tmp_path)
    _declare_schema(layout, 2)

    receipt = Inbox(layout).write(text="remember this", writer="dochan", source="claude-code")

    assert receipt.queued is True
    assert Inbox(layout).depth() == 1


def test_a_directory_that_declares_no_schema_is_not_refused(tmp_path: Path) -> None:
    """ "Declares nothing" is UNKNOWN, never "schema 1" — the gate must not fire on a bare dir.

    ``read_canonical_kb_schema_version`` returns ``None`` when ``_meta/taxonomy.yaml`` is absent.
    Treating that as 1 would turn "wrong cwd" into a confusing schema complaint, and would refuse
    every capture into a repo that has not been initialized yet — a directory with no schema-1 tree
    to corrupt and no curator that could drain it either way.
    """
    layout = RepoLayout(tmp_path)
    assert not (layout.meta_dir / "taxonomy.yaml").exists()

    assert Inbox(layout).write(text="a fact", writer="dochan", source="claude-code").queued is True


def test_a_pre_98_taxonomy_with_no_schema_key_reads_as_schema_1_and_refuses(tmp_path: Path) -> None:
    """A READABLE taxonomy with no ``schema_version`` key is a pre-#98 repo — i.e. schema 1.

    The distinction that matters: the FILE's absence is "unknown", the KEY's absence is the
    documented default of 1. Conflating them would let the oldest repos in existence — the ones
    with the most to lose — write into a layout their tree is not in.
    """
    layout = RepoLayout(tmp_path)
    _declare_schema(layout, None, text="domains: [general]\nallowed_tags: {}\n")

    with pytest.raises(ReadOnlySchemaVersionError):
        Inbox(layout).write(text="a fact", writer="dochan", source="claude-code")


def test_the_git_ignored_repo_yaml_mirror_cannot_gate_the_write_path(tmp_path: Path) -> None:
    """The CANONICAL declaration decides, never the operator-local mirror (ADR-0010 §5.1).

    ``_kb/repo.yaml`` is git-IGNORED and rewritten by ``agora repo init`` itself; letting it gate
    the write path would let one machine's untracked edit decide whether everybody's captures are
    accepted. So a schema-2 canonical declaration is writable even when a stale local mirror still
    says 1 — that DISAGREEMENT is lint L1-17's finding, not this gate's.
    """
    layout = RepoLayout(tmp_path)
    _declare_schema(layout, 2)
    layout.kb_dir.mkdir(parents=True, exist_ok=True)
    (layout.kb_dir / "repo.yaml").write_text("schema_version: 1\n", encoding="utf-8")

    assert Inbox(layout).write(text="a fact", writer="dochan", source="claude-code").queued is True
