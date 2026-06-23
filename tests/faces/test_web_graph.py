"""Tests for the Phase-3 web face knowledge-graph viz (the /api/graph JSON + /graph page + the
per-note embed; ADR-0019 §7).

Mirrors ``tests/faces/test_web.py``'s fixtures (``_init_repo`` / ``_write_wiki_notes`` /
``_client``) over a :class:`fastapi.testclient.TestClient`. Covers the first-class JSON contract
(shape + every
corpus note present + an authored body link is an edge + id == rel_path), the local ego-graph
(center echo + a 1-hop neighbour), the domain filter, the HTML graph page (loads the vendored
force-graph + graph.js, carries the data-graph-src + domain chips), the vendored asset's MIT header,
graph.js's XSS/click wiring, the per-note Connections embed, the nav link, and XSS faithfulness (the
JSON returns the raw title; escaping is graph.js's runtime job, asserted via its presence).

The TestClient cannot execute JS, so the viz correctness is asserted structurally (the exact
force-graph wiring strings are present in graph.js) per the unit brief.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agora_kb.core import Repo  # noqa: E402


# --- fixtures (mirror test_web.py) --------------------------------------------------------------
def _init_repo(tmp_path: Path) -> Repo:
    repo = Repo.resolve(tmp_path)
    repo.init()
    return repo


def _write_wiki_notes(tmp_path: Path) -> None:
    """A small navigable corpus (index + MOC + two themes) under ``wiki/`` (mirrors test_web.py)."""
    (tmp_path / "index.md").write_text(
        "---\ntype: index\nstatus: active\n---\n"
        "# personal\n\n- [AI Tech MOC](wiki/ai-tech/ai-tech-moc.md)\n",
        encoding="utf-8",
    )
    domain = tmp_path / "wiki" / "ai-tech"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "ai-tech-moc.md").write_text(
        "---\nstatus: active\ntype: moc\n---\n# AI Tech\n\n"
        "- [Curator concurrency](themes/curator-concurrency.md) — single-writer curator\n",
        encoding="utf-8",
    )
    themes = domain / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "curator-concurrency.md").write_text(
        "---\nstatus: active\ntype: theme\ntags: [single-writer, concurrency]\n"
        "title: Curator Concurrency Model\n---\n"
        "# Curator Concurrency\n\n"
        "The curator acquires a per-repo flock. See [Inbox design](inbox-design.md).\n",
        encoding="utf-8",
    )
    (themes / "inbox-design.md").write_text(
        "---\nstatus: active\ntype: theme\ntags: [inbox]\n---\n"
        "# Inbox Design\n\nThe inbox is append-only and per-writer namespaced.\n",
        encoding="utf-8",
    )


def _client(tmp_path: Path, *, user: str = "alice") -> TestClient:
    from agora_kb.faces.web import build_app

    app = build_app(repo_path=tmp_path, writer="web", user=user)
    return TestClient(app)


# --- JSON /api/graph ----------------------------------------------------------------------------
def test_api_graph_global_shape_and_edges(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/api/graph")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) >= {
        "nodes",
        "edges",
        "node_total",
        "edge_total",
        "truncated",
        "center",
        "depth",
    }

    # Every corpus note is a node, identified by rel_path.
    ids = {n["id"] for n in data["nodes"]}
    assert "index.md" in ids
    assert "wiki/ai-tech/ai-tech-moc.md" in ids
    assert "wiki/ai-tech/themes/curator-concurrency.md" in ids
    assert "wiki/ai-tech/themes/inbox-design.md" in ids

    # An authored body link (curator-concurrency -> inbox-design) is a directed edge between the two
    # real nodes (both endpoints resolve to rel_path ids).
    edges = {(e["source"], e["target"]) for e in data["edges"]}
    assert (
        "wiki/ai-tech/themes/curator-concurrency.md",
        "wiki/ai-tech/themes/inbox-design.md",
    ) in edges
    # node id == rel_path (no separate identity scheme).
    for n in data["nodes"]:
        assert n["id"] == n["id"].strip()
        assert n["id"].endswith(".md")
    assert data["center"] is None


def test_api_graph_local_center_and_neighbour(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    client = _client(tmp_path)

    center = "wiki/ai-tech/themes/curator-concurrency.md"
    resp = client.get("/api/graph", params={"center": center, "depth": 1})
    assert resp.status_code == 200
    data = resp.json()
    # The resolved center is echoed back verbatim.
    assert data["center"] == center
    assert data["depth"] == 1
    # A 1-hop neighbour (the linked inbox-design note) is reached.
    ids = {n["id"] for n in data["nodes"]}
    assert center in ids
    assert "wiki/ai-tech/themes/inbox-design.md" in ids


def test_api_graph_domain_filter(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/api/graph", params={"domain": "ai-tech"})
    assert resp.status_code == 200
    ids = {n["id"] for n in resp.json()["nodes"]}
    # Only ai-tech notes survive the domain filter; the top-level index.md is excluded.
    assert ids
    assert all(i.startswith("wiki/ai-tech/") for i in ids)
    assert "index.md" not in ids


# --- HTML /graph page ---------------------------------------------------------------------------
def test_graph_page_renders_with_assets_and_chips(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/graph")
    assert resp.status_code == 200
    html = resp.text
    assert "/static/force-graph.min.js" in html
    assert "/static/graph.js" in html
    assert 'data-graph-src="/api/graph"' in html
    # Domain-filter chips: the "All" link plus the ai-tech domain.
    assert "/graph?domain=ai-tech" in html
    assert ">All<" in html
    assert ">ai-tech<" in html


def test_graph_page_domain_active(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/graph", params={"domain": "ai-tech"})
    assert resp.status_code == 200
    html = resp.text
    # The server-built api_src carries the domain filter for the canvas to fetch.
    assert 'data-graph-src="/api/graph?domain=ai-tech"' in html


# --- vendored asset + graph.js wiring -----------------------------------------------------------
def test_force_graph_vendor_asset_served(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/static/force-graph.min.js")
    assert resp.status_code == 200
    body = resp.text
    assert "force-graph" in body
    assert "Vasco Asturiano" in body  # the MIT vendor header attribution


def test_graph_js_served_with_xss_and_click_wiring(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/static/graph.js")
    assert resp.status_code == 200
    body = resp.text
    assert "escapeHtml" in body  # the XSS-escaped label wiring
    assert "onNodeClick" in body  # the click handler is wired
    assert "/note/" in body  # the click navigates to the note route


# --- per-note Connections embed -----------------------------------------------------------------
def test_note_page_embeds_local_graph(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/note/wiki/ai-tech/themes/curator-concurrency.md")
    assert resp.status_code == 200
    html = resp.text
    # The local-graph container is present; the query separator is the HTML entity &amp; (which the
    # browser decodes back to & for dataset), so assert on the center= part and depth, not the &.
    assert 'class="local-graph"' in html
    assert "center=wiki/ai-tech/themes/curator-concurrency.md" in html
    assert "depth=1" in html
    # The note page loads the viz scripts.
    assert "/static/graph.js" in html
    assert "/static/force-graph.min.js" in html


def test_note_not_found_loads_no_viz(tmp_path: Path) -> None:
    """The 404 note branch has no graph container, so it must NOT pull the 177KB force-graph lib."""
    _init_repo(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/note/wiki/general/nope.md")
    assert resp.status_code == 404
    assert "/static/force-graph.min.js" not in resp.text
    assert "/static/graph.js" not in resp.text


# --- nav link -----------------------------------------------------------------------------------
def test_home_has_graph_nav_link(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/graph"' in resp.text


# --- XSS faithfulness: the JSON layer is faithful (raw title); escaping is graph.js runtime -------
def test_api_graph_returns_raw_title_xss_faithful(tmp_path: Path) -> None:
    """A note titled with a `<script>` must come back RAW in the JSON (the data layer is faithful);
    the escaping is graph.js's runtime job (asserted via the escapeHtml presence test above)."""
    _init_repo(tmp_path)
    domain = tmp_path / "wiki" / "general"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "evil.md").write_text(
        "---\ntype: theme\nstatus: active\n"
        'title: "<script>alert(1)</script>"\n---\n'
        "# Evil\n\nbody.\n",
        encoding="utf-8",
    )
    client = _client(tmp_path)

    resp = client.get("/api/graph")
    assert resp.status_code == 200
    titles = [n["title"] for n in resp.json()["nodes"]]
    # The JSON contract is faithful — the raw, UNESCAPED title comes back (escaping is client-side).
    assert "<script>alert(1)</script>" in titles
