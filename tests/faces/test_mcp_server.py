"""Tests for the MCP face (faces.mcp_server, DESIGN §5.1).

The cognitive logic lives in transport-free :class:`AgoraHandlers`, so these tests drive it directly
over a real on-disk repo (``tmp_path``) — no live MCP transport, no server I/O. A single smoke test
exercises :func:`build_server` to confirm the 4 tools are registered.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agora_kb.core import Repo
from agora_kb.faces.mcp_server import AgoraHandlers, build_server


# --- fixture helpers ----------------------------------------------------------------------------
def _init_repo(tmp_path: Path) -> Repo:
    """Create + initialize a knowledge repo and return it."""
    repo = Repo.resolve(tmp_path)
    repo.init()
    return repo


def _write_wiki_notes(tmp_path: Path) -> None:
    """Place a small navigable corpus on disk (index + MOC + two themes) under ``wiki/``."""
    (tmp_path / "index.md").write_text("# personal\n\n- [[ai-tech-moc]]\n", encoding="utf-8")
    domain = tmp_path / "wiki" / "ai-tech"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "ai-tech-moc.md").write_text(
        "---\nstatus: active\n---\n# AI Tech\n\n"
        "- [[curator-concurrency]] — single-writer curator\n"
        "- [[inbox-design]] — append-only inbox\n",
        encoding="utf-8",
    )
    (domain / "curator-concurrency.md").write_text(
        "---\nstatus: active\ntags: [single-writer, concurrency]\n---\n"
        "# Curator Concurrency\n\n"
        "The curator acquires a per-repo flock so exactly one writer advances the branch.\n",
        encoding="utf-8",
    )
    (domain / "inbox-design.md").write_text(
        "---\nstatus: active\ntags: [inbox, append-only]\n---\n"
        "# Inbox Design\n\nThe inbox is append-only and per-writer namespaced.\n",
        encoding="utf-8",
    )


# --- remember (write path) ----------------------------------------------------------------------
def test_remember_writes_inbox_event_and_returns_receipt(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.remember("compare-and-swap publishes the curated branch")

    assert isinstance(result, dict)
    assert result["queued"] is True
    assert isinstance(result["id"], str) and result["id"]
    assert result["inbox_depth"] == 1

    # The event landed in this writer's inbox namespace on disk.
    writer_dir = repo.layout.inbox_writer_dir("local")
    events = list(writer_dir.glob("*.md"))
    assert len(events) == 1
    assert events[0].stem == result["id"]


def test_remember_uses_server_writer_and_passes_metadata(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="agent-7")

    handlers.remember(
        "kebab tags are validated by the core model",
        target="personal",
        domain="ai-tech",
        tags=["single-writer", "concurrency"],
        source="claude-code",
    )

    # The capture lands in the *server-configured* writer namespace, not a caller-chosen one.
    assert list(repo.layout.inbox_writer_dir("agent-7").glob("*.md"))
    assert not repo.layout.inbox_writer_dir("local").exists()


# --- query (read path) --------------------------------------------------------------------------
def test_query_returns_hits_for_known_topic(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.query("curator concurrency compare and swap")

    assert result["status"] == "ok"
    hits = result["hits"]
    assert isinstance(hits, list) and hits
    first = hits[0]
    # Every documented citation field is present and JSON-serializable.
    assert set(first) == {
        "repo",
        "path",
        "anchor",
        "line",
        "excerpt",
        "match_reason",
        "score",
    }
    assert any("curator-concurrency" in h["path"] for h in hits)
    assert first["match_reason"] in {"linked-theme", "heading", "lexical"}


def test_query_not_found_for_nonsense(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.query("zzzqqq nonexistent gibberish token")

    assert result["status"] == "not_found"
    assert result["hits"] == []
    assert result["query"] == "zzzqqq nonexistent gibberish token"


# --- status (meta) ------------------------------------------------------------------------------
def test_status_fresh_repo_reports_depth_and_nulls(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.status()

    assert result["inbox_depth"] == 0
    assert result["last_consolidation"] is None
    assert result["processed_today"] == 0
    assert result["last_commit"] is None
    assert result["counters"] == {"ingested": 0, "merged": 0, "dropped": 0, "failed": 0}
    assert result["failed"] == 0


def test_status_reflects_pending_and_failed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")
    handlers.remember("one pending capture")
    handlers.remember("another pending capture")

    # Simulate a terminal failure record under _kb/failed/.
    failed_dir = repo.layout.failed_dir
    failed_dir.mkdir(parents=True, exist_ok=True)
    (failed_dir / "20260613T000000Z-deadbeef.md").write_text("oops\n", encoding="utf-8")

    result = handlers.status()
    assert result["inbox_depth"] == 2
    assert result["failed"] == 1


def test_status_processed_today_counts_today_partition(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    # Two items finalized today + one in a stale (yesterday-shaped) partition that must be ignored.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    today_dir = repo.layout.processed_dir / today
    today_dir.mkdir(parents=True, exist_ok=True)
    (today_dir / "a.md").write_text("done\n", encoding="utf-8")
    (today_dir / "b.md").write_text("done\n", encoding="utf-8")
    stale_dir = repo.layout.processed_dir / "2000-01-01"
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / "old.md").write_text("done\n", encoding="utf-8")

    result = handlers.status()
    assert result["processed_today"] == 2


# --- curate (admin trigger probe) ---------------------------------------------------------------
def test_curate_none_when_idle(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.curate()

    assert result["should_run"] is False
    assert result["reason"] == "none"
    assert result["inbox_depth"] == 0
    assert "curator.worker" in result["note"]


def test_curate_force_reports_force_reason_below_threshold(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")
    handlers.remember("a single capture")

    result = handlers.curate(force=True)

    # A forced run is an operator override, not a backlog-threshold trigger — even at depth 1
    # (below the default threshold of 10) the reason must read "force", matching agora_kb.cli.
    assert result["should_run"] is True
    assert result["reason"] == "force"
    assert result["inbox_depth"] == 1


def test_curate_threshold_when_depth_meets_default(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")
    # The default TriggerConfig threshold is 10; reach it so the probe fires without force.
    for i in range(10):
        handlers.remember(f"capture number {i}")

    result = handlers.curate()

    assert result["should_run"] is True
    assert result["reason"] == "threshold"
    assert result["inbox_depth"] == 10


# --- build_server wiring smoke test -------------------------------------------------------------
def test_build_server_registers_four_tools(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    server = build_server(repo_path=tmp_path, writer="local")

    # The FastMCP instance constructs and carries the 4 agent tools.
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {"kb_remember", "kb_query", "kb_status", "kb_curate"}
