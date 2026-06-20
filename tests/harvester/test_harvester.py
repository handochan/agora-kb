"""Tests for the harvester orchestrator (agora_kb.harvester.harvester; ADR-0007).

Covers the scope gate (privacy, fail-closed), the §6 cursor + tolerant store, and the orchestrator:
the gated-candidate write contract (kind/confidence/source/writer), per-connector namespacing,
event_key dedup, dry-run, the ``only`` filter, the disabled no-op, and cursor advancement.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agora_kb.config import ConnectorSpec, HarvestPolicy
from agora_kb.core.frontmatter import parse
from agora_kb.core.hashing import content_sha256
from agora_kb.core.layout import RepoLayout
from agora_kb.harvester.connectors import (
    ConnectorError,
    ConnectorScan,
    FileConnector,
    HarvestedFact,
    Scope,
)
from agora_kb.harvester.harvester import (
    CursorStore,
    HarvestCursor,
    Harvester,
    ScopeViolation,
    build_connectors,
    check_scope,
)

FIXED = datetime(2026, 6, 20, 9, 0, 0, tzinfo=UTC)


class FakeConnector:
    """A minimal in-memory connector for orchestrator tests (implements the Connector Protocol)."""

    def __init__(self, name: str, agent: str, scope: Scope, facts: list[HarvestedFact]) -> None:
        self._name = name
        self._agent = agent
        self._scope = scope
        self._facts = facts

    @property
    def name(self) -> str:
        return self._name

    @property
    def agent(self) -> str:
        return self._agent

    @property
    def scope(self) -> Scope:
        return self._scope

    def scan(self, *, last_content_sha256: str | None) -> ConnectorScan:
        return ConnectorScan(
            unchanged=False,
            content_sha256="deadbeef",
            source_path=f"mem://{self._agent}",
            facts=tuple(self._facts),
        )


def _fact(text: str) -> HarvestedFact:
    return HarvestedFact(text=text, fact_key=content_sha256(text))


# --- scope gate ---------------------------------------------------------------------------------


def test_scope_personal_into_personal_ok() -> None:
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")
    check_scope(Scope.personal, policy)  # no raise


def test_scope_personal_into_team_refused() -> None:
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="team")
    with pytest.raises(ScopeViolation):
        check_scope(Scope.personal, policy)


def test_scope_fail_closed_on_missing_kind() -> None:
    # An absent/unknown repo kind is treated as 'team' → a personal source is REFUSED.
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind=None)
    with pytest.raises(ScopeViolation):
        check_scope(Scope.personal, policy)


def test_scope_config_mismatch_refused() -> None:
    # connector scope must match the repo's declared scope_lock.
    policy = HarvestPolicy(enabled=True, scope_lock="team", repo_kind="team")
    with pytest.raises(ScopeViolation):
        check_scope(Scope.personal, policy)


def test_scope_team_into_team_ok() -> None:
    policy = HarvestPolicy(enabled=True, scope_lock="team", repo_kind="team")
    check_scope(Scope.team, policy)  # no raise


# --- cursor + store -----------------------------------------------------------------------------


def test_cursor_roundtrip_and_counter_preserved(tmp_path: Path) -> None:
    store = CursorStore(RepoLayout(tmp_path))
    cur = HarvestCursor(
        connector="file:demo",
        source_path="/x/MEMORY.md",
        last_scan=FIXED,
        last_content_sha256="abc",
        proposed=3,
        accepted=2,  # curator-owned; must survive a harvester re-save
        rejected=1,
    )
    store.save(cur)
    loaded = store.load("file:demo")
    assert loaded.connector == "file:demo"
    assert loaded.last_content_sha256 == "abc"
    assert loaded.proposed == 3
    assert loaded.accepted == 2 and loaded.rejected == 1
    assert loaded.last_scan == FIXED


def test_cursor_missing_is_fresh(tmp_path: Path) -> None:
    cur = CursorStore(RepoLayout(tmp_path)).load("file:demo")
    assert cur.connector == "file:demo"
    assert cur.proposed == 0
    assert cur.last_content_sha256 is None


def test_cursor_corrupt_is_tolerated(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    path = layout.harvest_cursor_path("file:demo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    cur = CursorStore(layout).load("file:demo")
    assert cur.proposed == 0  # re-scan from scratch rather than wedge


# --- orchestrator -------------------------------------------------------------------------------


def _read_inbox(layout: RepoLayout) -> list[tuple[dict, str]]:
    return [parse(p.read_text(encoding="utf-8")) for p in sorted(layout.inbox_dir.glob("*/*.md"))]


def test_disabled_is_noop(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    policy = HarvestPolicy(enabled=False)
    conn = FakeConnector("file:a", "a", Scope.personal, [_fact("- f")])
    report = Harvester(layout).run([conn], policy=policy, now=FIXED)
    assert report.enabled is False
    assert report.connectors == ()
    assert not layout.inbox_dir.exists() or list(layout.inbox_dir.glob("*/*.md")) == []


def test_run_writes_gated_candidates(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")
    conn = FakeConnector(
        "file:claude-code", "claude-code", Scope.personal, [_fact("- one"), _fact("- two")]
    )
    report = Harvester(layout).run([conn], policy=policy, now=FIXED)

    cr = report.connectors[0]
    assert cr.status == "ok" and cr.written == 2 and cr.deduped == 0

    items = _read_inbox(layout)
    assert len(items) == 2
    for fm, _ in items:
        assert fm["source"] == "harvest:claude-code"
        assert fm["writer"] == "harvest-claude-code"
        assert fm["kind"] == "candidate"  # the candidate gate contract — must not regress
        assert fm["confidence"] == "low"
        assert fm["target"] == "personal"

    # The cursor advanced (harvester-owned fields).
    cur = CursorStore(layout).load("file:claude-code")
    assert cur.proposed == 2
    assert cur.last_content_sha256 == "deadbeef"
    assert cur.last_scan == FIXED


def test_run_scope_refused_writes_nothing(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="team")
    conn = FakeConnector("file:a", "a", Scope.personal, [_fact("- secret")])
    report = Harvester(layout).run([conn], policy=policy, now=FIXED)
    assert report.connectors[0].status == "scope-refused"
    assert report.total_written == 0
    assert not layout.inbox_dir.exists() or list(layout.inbox_dir.glob("*/*.md")) == []


def test_run_dedups_identical_facts_via_event_key(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")
    dup = _fact("- the same fact")
    conn = FakeConnector("file:a", "a", Scope.personal, [dup, dup])
    report = Harvester(layout).run([conn], policy=policy, now=FIXED)
    cr = report.connectors[0]
    assert cr.written == 1 and cr.deduped == 1
    assert len(_read_inbox(layout)) == 1


def test_distinct_agents_with_identical_text_both_land(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")
    text = "- shared knowledge"
    a = FakeConnector("file:a", "a", Scope.personal, [_fact(text)])
    b = FakeConnector("file:b", "b", Scope.personal, [_fact(text)])
    Harvester(layout).run([a, b], policy=policy, now=FIXED)
    items = _read_inbox(layout)
    writers = sorted(fm["writer"] for fm, _ in items)
    assert writers == ["harvest-a", "harvest-b"]  # distinct namespaces, both provenance preserved


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")
    conn = FakeConnector("file:a", "a", Scope.personal, [_fact("- f1"), _fact("- f2")])
    report = Harvester(layout).run([conn], policy=policy, now=FIXED, dry_run=True)
    cr = report.connectors[0]
    assert cr.dry_run is True and cr.facts_found == 2 and len(cr.preview) == 2
    assert not layout.inbox_dir.exists() or list(layout.inbox_dir.glob("*/*.md")) == []
    # No cursor written on a dry run.
    assert not layout.harvest_cursor_path("file:a").exists()


def test_only_filter(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")
    a = FakeConnector("file:a", "a", Scope.personal, [_fact("- a")])
    b = FakeConnector("file:b", "b", Scope.personal, [_fact("- b")])
    report = Harvester(layout).run([a, b], policy=policy, now=FIXED, only="file:a")
    assert [c.name for c in report.connectors] == ["file:a"]


def test_unchanged_connector_reports_unchanged(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")

    class Unchanged(FakeConnector):
        def scan(self, *, last_content_sha256: str | None) -> ConnectorScan:
            return ConnectorScan(unchanged=True, content_sha256="x", source_path="mem")

    conn = Unchanged("file:a", "a", Scope.personal, [])
    report = Harvester(layout).run([conn], policy=policy, now=FIXED)
    assert report.connectors[0].status == "unchanged"


# --- build_connectors ---------------------------------------------------------------------------


def test_build_connectors_file_type() -> None:
    specs = [ConnectorSpec(name="file:demo", scope="personal", path="/x/MEMORY.md")]
    conns = build_connectors(specs)
    assert conns[0].name == "file:demo" and conns[0].agent == "demo"


def test_build_connectors_unknown_type_fails_loud() -> None:
    specs = [ConnectorSpec(name="letta:cloud", scope="personal", path=None)]
    with pytest.raises(ConnectorError):
        build_connectors(specs)


# --- review-finding regressions -----------------------------------------------------------------


def test_no_match_preserves_prior_cursor_hash(tmp_path: Path) -> None:
    # A transient no-match (source temporarily gone) must NOT clobber the cursor's fast-no-op hash.
    layout = RepoLayout(tmp_path)
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")
    store = CursorStore(layout)
    store.save(HarvestCursor(connector="file:a", last_content_sha256="priorhash", proposed=1))

    class NoMatch(FakeConnector):
        def scan(self, *, last_content_sha256: str | None) -> ConnectorScan:
            # not 'unchanged' (a prior hash exists), but nothing matched → content_sha256 is None.
            return ConnectorScan(
                unchanged=last_content_sha256 is None,
                content_sha256=None,
                source_path="mem",
                facts=(),
            )

    conn = NoMatch("file:a", "a", Scope.personal, [])
    Harvester(layout).run([conn], policy=policy, now=FIXED)
    assert store.load("file:a").last_content_sha256 == "priorhash"


def test_rescan_after_inbox_drained_is_noop(tmp_path: Path) -> None:
    # After the curator consolidates items out of the inbox, a re-scan of the UNCHANGED source with
    # the intact cursor is a fast no-op (the cursor hash, not pending dedup, is the guard).
    layout = RepoLayout(tmp_path)
    mem = tmp_path / "MEMORY.md"
    mem.write_text("# m\n\n- fact one\n- fact two\n", encoding="utf-8")
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")
    conn = FileConnector(name="file:a", path=str(mem), scope=Scope.personal)
    h = Harvester(layout)

    r1 = h.run([conn], policy=policy, now=FIXED)
    assert r1.connectors[0].written == 2
    for p in layout.inbox_dir.glob("*/*.md"):  # simulate consolidation draining the pending inbox
        p.unlink()

    r2 = h.run([conn], policy=policy, now=FIXED)
    assert r2.connectors[0].status == "unchanged"
    assert list(layout.inbox_dir.glob("*/*.md")) == []


def test_cursor_loss_after_drain_refloods_bounded(tmp_path: Path) -> None:
    # If the cursor is lost after consolidation, the fact is re-proposed (re-flood is expected, and
    # bounded downstream by the curator gate + durable event_key dedup, ADR-0017 §3/§5).
    layout = RepoLayout(tmp_path)
    mem = tmp_path / "MEMORY.md"
    mem.write_text("# m\n\n- the one fact\n", encoding="utf-8")
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")
    conn = FileConnector(name="file:a", path=str(mem), scope=Scope.personal)
    h = Harvester(layout)

    h.run([conn], policy=policy, now=FIXED)
    for p in layout.inbox_dir.glob("*/*.md"):
        p.unlink()
    layout.harvest_cursor_path("file:a").unlink()  # cursor lost

    r2 = h.run([conn], policy=policy, now=FIXED)
    assert r2.connectors[0].written == 1  # re-proposed (no cursor, drained inbox → no dedup hit)


def test_invalid_team_name_yields_error_report(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    policy = HarvestPolicy(enabled=True, scope_lock="team", repo_kind="team")
    conn = FakeConnector("file:a", "a", Scope.team, [_fact("- t")])
    report = Harvester(layout).run([conn], policy=policy, repo_name="bad name", now=FIXED)
    cr = report.connectors[0]
    assert cr.status == "error"
    assert "team target" in (cr.message or "")
    assert not layout.inbox_dir.exists() or list(layout.inbox_dir.glob("*/*.md")) == []


def test_cursor_last_scan_serialized_as_z_form(tmp_path: Path) -> None:
    # The on-disk DATA-MODEL §6 form: explicit Z, second precision, UTC-normalized (microseconds and
    # a non-UTC offset are normalized away). Asserting the STRING guards against a regression to
    # plain .isoformat() that the parsed-datetime round-trip would not catch.
    store = CursorStore(RepoLayout(tmp_path))
    when = datetime(2026, 6, 20, 14, 30, 5, 123456, tzinfo=timezone(timedelta(hours=5)))
    store.save(HarvestCursor(connector="file:a", last_scan=when))
    raw = RepoLayout(tmp_path).harvest_cursor_path("file:a").read_text(encoding="utf-8")
    assert '"last_scan": "2026-06-20T09:30:05Z"' in raw


# --- link-following through the orchestrator (ADR-0018) ------------------------------------------


def _follow_repo(tmp_path: Path) -> Path:
    src = tmp_path / "mem"
    src.mkdir()
    return src


def test_followed_fact_is_a_gated_candidate(tmp_path: Path) -> None:
    # A SIBLING-derived (followed) fact must still hit the candidate gate (kind/confidence/source).
    src = _follow_repo(tmp_path)
    (src / "MEMORY.md").write_text("# I\n\n- [Curator](curator.md) — note\n", encoding="utf-8")
    (src / "curator.md").write_text("# Curator\n\nOne curator holds a lock.\n", encoding="utf-8")
    layout = RepoLayout(tmp_path)
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")
    conn = FileConnector(
        name="file:demo", path=str(src / "MEMORY.md"), scope=Scope.personal, follow_links=True
    )
    Harvester(layout).run([conn], policy=policy, now=FIXED)
    items = _read_inbox(layout)
    assert len(items) == 1
    fm, body = items[0]
    assert fm["kind"] == "candidate"
    assert fm["confidence"] == "low"
    assert fm["source"] == "harvest:demo"
    assert "One curator holds a lock." in body


def test_followed_same_sibling_dedupes_through_inbox(tmp_path: Path) -> None:
    # Two bullets to one sibling → body-only event_key collapses them at the inbox (ADR-0018 D5).
    src = _follow_repo(tmp_path)
    (src / "MEMORY.md").write_text(
        "# I\n\n- [One](s.md) — a\n- [Two](s.md) — b\n", encoding="utf-8"
    )
    (src / "s.md").write_text("# S\n\nshared body\n", encoding="utf-8")
    layout = RepoLayout(tmp_path)
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")
    conn = FileConnector(
        name="file:demo", path=str(src / "MEMORY.md"), scope=Scope.personal, follow_links=True
    )
    report = Harvester(layout).run([conn], policy=policy, now=FIXED)
    cr = report.connectors[0]
    assert cr.written == 1 and cr.deduped == 1
    assert len(_read_inbox(layout)) == 1
