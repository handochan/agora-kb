"""Tests for the #58 read-side MCP companion tools ``kb_read`` / ``kb_neighbors``.

Both tools are pure WIRING in :func:`build_server` — ``kb_read`` delegates to the already-tested
:meth:`AgoraHandlers.note` and ``kb_neighbors`` to :meth:`AgoraHandlers.graph` (the ADR-0021
ego-graph) — so these tests drive them through a REAL ``fastmcp.Client`` over the protocol, the
only place the wrapper logic (not-found shaping, description text) lives. The corpus is the graph
test fixture reused verbatim (index + MOC + two themes; see ``test_mcp_server_graph``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastmcp import Client

from agora_kb.core import Repo
from agora_kb.faces import mcp_server
from agora_kb.faces.mcp_server import build_server
from tests.faces.test_mcp_server_graph import _write_wiki_notes

_CENTER = "wiki/ai-tech/themes/curator-concurrency.md"


def _server(tmp_path: Path):
    repo = Repo.resolve(tmp_path)
    repo.init()
    _write_wiki_notes(tmp_path)
    return build_server(repo_path=tmp_path, writer="local")


def _call(server: object, tool: str, args: dict[str, object]) -> dict[str, object]:
    async def _run() -> dict[str, object]:
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            return result.data

    return asyncio.run(_run())


# --- kb_read ------------------------------------------------------------------------------------
def test_kb_read_returns_note_payload(tmp_path: Path) -> None:
    """Happy path: kb_read renders the EXACT note() payload — same data the web face serves."""
    data = _call(_server(tmp_path), "kb_read", {"path": _CENTER})

    assert set(data) == {
        "rel_path",
        "basename",
        "title",
        "status",
        "tags",
        "domain",
        "frontmatter",
        "body",
        "links",
    }
    assert data["rel_path"] == _CENTER
    assert data["basename"] == "curator-concurrency"
    assert data["title"] == "Curator Concurrency Model"  # frontmatter title wins over basename
    assert data["status"] == "active"
    assert data["domain"] == "ai-tech"
    # Body stays RAW markdown (rendering is the consumer's job), with its H1 intact.
    assert data["body"].startswith("# Curator Concurrency")
    assert isinstance(data["frontmatter"], dict)
    # Links are the body graph-edge BASENAMES, not rel_paths: follow them via kb_neighbors on
    # this note's rel_path — its node ids (rel_paths) are what feed kb_read.
    assert data["links"] == ["inbox-design"]


def test_kb_read_not_found_and_traversal_safe(tmp_path: Path) -> None:
    """An unknown path AND an escape path both yield the clear not-found shape, never contents."""
    server = _server(tmp_path)

    for path in ("wiki/ai-tech/themes/nope.md", "../../etc/passwd"):
        data = _call(server, "kb_read", {"path": path})
        assert data["error"] == "not_found"
        assert data["path"] == path
        assert "kb_query" in data["note"]  # actionable: points back to the hit source
        assert "body" not in data


# --- kb_neighbors -------------------------------------------------------------------------------
def test_kb_neighbors_depth1_returns_ego_graph(tmp_path: Path) -> None:
    """depth=1 around a theme: undirected 1-hop reach + induced edges, center/depth echoed."""
    data = _call(_server(tmp_path), "kb_neighbors", {"path": _CENTER})

    assert data["center"] == _CENTER
    assert data["depth"] == 1
    assert data["truncated"] is False
    ids = {n["id"] for n in data["nodes"]}
    # 1 hop from curator-concurrency: the MOC links TO it, and it links to inbox-design.
    assert ids == {
        _CENTER,
        "wiki/ai-tech/ai-tech-moc.md",
        "wiki/ai-tech/themes/inbox-design.md",
    }
    assert data["node_total"] == 3
    # Induced directed edges among the reached set (moc→cc, moc→inbox, cc→inbox).
    edges = {(e["source"], e["target"]) for e in data["edges"]}
    assert edges == {
        ("wiki/ai-tech/ai-tech-moc.md", _CENTER),
        ("wiki/ai-tech/ai-tech-moc.md", "wiki/ai-tech/themes/inbox-design.md"),
        (_CENTER, "wiki/ai-tech/themes/inbox-design.md"),
    }
    # Every node carries the id (rel_path — feeds kb_read) + title label.
    for node in data["nodes"]:
        assert isinstance(node["id"], str) and node["id"]
        assert isinstance(node["title"], str) and node["title"]


def test_kb_neighbors_depth_is_clamped_by_existing_cap(tmp_path: Path) -> None:
    """An oversized depth is clamped to graph()'s existing MAX_GRAPH_DEPTH cap and echoed."""
    data = _call(_server(tmp_path), "kb_neighbors", {"path": _CENTER, "depth": 99})

    assert data["depth"] == mcp_server.MAX_GRAPH_DEPTH
    # At the clamped depth the 2-hop index.md is reachable too — the whole 4-note corpus.
    ids = {n["id"] for n in data["nodes"]}
    assert "index.md" in ids
    assert data["node_total"] == 4


def test_kb_neighbors_unknown_path_returns_empty_graph_with_note(tmp_path: Path) -> None:
    """An unknown center is SAFE: empty graph, center=null, plus the actionable not-found note."""
    data = _call(_server(tmp_path), "kb_neighbors", {"path": "wiki/ai-tech/themes/ghost.md"})

    assert data["center"] is None
    assert data["nodes"] == []
    assert data["edges"] == []
    assert data["node_total"] == 0
    assert "kb_query" in data["note"]


# --- descriptions carry the navigation protocol (#58) -------------------------------------------
def test_read_tool_descriptions_carry_navigation_protocol(tmp_path: Path) -> None:
    """Both new tools teach the query → read → neighbors → re-query loop in their descriptions."""
    server = _server(tmp_path)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}

    read_desc = tools["kb_read"].description or ""
    neighbors_desc = tools["kb_neighbors"].description or ""
    for desc in (read_desc, neighbors_desc):
        assert "Navigation protocol" in desc
        assert "kb_query" in desc  # the loop's entry and re-query step
    assert "kb_neighbors" in read_desc  # kb_read points at the next step…
    assert "kb_read" in neighbors_desc  # …and kb_neighbors points back into reading
