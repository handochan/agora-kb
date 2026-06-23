"""Tests for the knowledge-graph read method AgoraHandlers.graph (ADR-0003/0019 §7, graph-plan).

This shapes the core ``Wiki.list_notes`` link graph into JSON-serializable node/edge data for the
web ``/graph`` viz — read-only, deterministic, thin-face (no canonical change). It reuses the SAME
core.wiki seam and the SAME orphan derivation as :meth:`AgoraHandlers.health` (parity asserted
below). The corpus mirrors the browse tests (index + MOC + two themes) on a real on-disk repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.core import Repo
from agora_kb.faces import mcp_server
from agora_kb.faces.mcp_server import AgoraHandlers


def _init_repo(tmp_path: Path) -> Repo:
    repo = Repo.resolve(tmp_path)
    repo.init()
    return repo


def _write_wiki_notes(tmp_path: Path) -> None:
    """Place a small navigable corpus on disk (index + MOC + two themes) under ``wiki/``.

    Edges: index → ai-tech-moc; moc → curator-concurrency + inbox-design; curator-concurrency →
    inbox-design (a body link AND a frontmatter ``related:`` wikilink, so the related-array path is
    exercised). Both themes are linked-to, so neither is an orphan.
    """
    (tmp_path / "index.md").write_text(
        "---\ntype: index\nstatus: active\n---\n"
        "# personal\n\n- [AI Tech MOC](wiki/ai-tech/ai-tech-moc.md)\n",
        encoding="utf-8",
    )
    domain = tmp_path / "wiki" / "ai-tech"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "ai-tech-moc.md").write_text(
        "---\nstatus: active\ntype: moc\n---\n# AI Tech\n\n"
        "- [Curator concurrency](themes/curator-concurrency.md) — single-writer curator\n"
        "- [Inbox design](themes/inbox-design.md) — append-only inbox\n",
        encoding="utf-8",
    )
    themes = domain / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "curator-concurrency.md").write_text(
        "---\nstatus: active\ntype: theme\ntags: [single-writer, concurrency]\n"
        'title: Curator Concurrency Model\nrelated: ["[[inbox-design]]"]\n---\n'
        "# Curator Concurrency\n\n"
        "The curator acquires a per-repo flock. See [Inbox design](inbox-design.md).\n",
        encoding="utf-8",
    )
    (themes / "inbox-design.md").write_text(
        "---\nstatus: active\ntype: theme\ntags: [inbox, append-only]\n---\n"
        "# Inbox Design\n\nThe inbox is append-only and per-writer namespaced.\n",
        encoding="utf-8",
    )


_CC = "wiki/ai-tech/themes/curator-concurrency.md"
_INBOX = "wiki/ai-tech/themes/inbox-design.md"
_MOC = "wiki/ai-tech/ai-tech-moc.md"


# --- global -------------------------------------------------------------------------------------
def test_graph_global_nodes_and_edges(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.graph()
    assert set(result) == {
        "nodes",
        "edges",
        "node_total",
        "edge_total",
        "truncated",
        "center",
        "depth",
    }
    assert result["center"] is None
    assert result["truncated"] is False

    # Every note is a node with id == rel_path and the documented summary fields.
    node_ids = {n["id"] for n in result["nodes"]}
    assert node_ids == {"index.md", _MOC, _CC, _INBOX}
    for node in result["nodes"]:
        assert set(node) == {"id", "title", "domain", "status", "type", "orphan"}
    cc = next(n for n in result["nodes"] if n["id"] == _CC)
    assert cc["title"] == "Curator Concurrency Model"  # frontmatter title wins
    assert cc["domain"] == "ai-tech"
    assert cc["status"] == "active"
    assert cc["type"] == "theme"

    edges = {(e["source"], e["target"]) for e in result["edges"]}
    # An authored body link [Inbox design](inbox-design.md) => edge curator-concurrency -> inbox.
    assert (_CC, _INBOX) in edges
    # A frontmatter related: ["[[inbox-design]]"] is also that same edge (dedup keeps one).
    # The index/MOC child bullets resolve to their targets too.
    assert ("index.md", _MOC) in edges
    assert (_MOC, _CC) in edges
    assert (_MOC, _INBOX) in edges
    assert result["node_total"] == 4
    assert result["edge_total"] == len(result["edges"])

    # Dedup is by COUNT, not just membership: curator-concurrency -> inbox-design is authored BOTH
    # as a body markdown link AND a frontmatter related: [[inbox-design]], yet must appear exactly
    # once — and no parallel edges exist anywhere (a list-based regression would emit duplicates).
    assert sum(1 for e in result["edges"] if (e["source"], e["target"]) == (_CC, _INBOX)) == 1
    edge_pairs = [(e["source"], e["target"]) for e in result["edges"]]
    assert len(edge_pairs) == len(set(edge_pairs))


def test_graph_related_frontmatter_yields_edge(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    # A theme whose ONLY link to another note is a frontmatter related: [[ ]] entry.
    domain = tmp_path / "wiki" / "ai-tech"
    themes = domain / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "alpha.md").write_text(
        "---\nstatus: active\ntype: theme\nrelated: ["
        '"[[beta]]"]\n---\n# Alpha\n\nProse with no body link.\n',
        encoding="utf-8",
    )
    (themes / "beta.md").write_text(
        "---\nstatus: active\ntype: theme\n---\n# Beta\n\nLeaf.\n",
        encoding="utf-8",
    )
    handlers = AgoraHandlers(repo, writer="local")

    edges = {(e["source"], e["target"]) for e in handlers.graph()["edges"]}
    assert ("wiki/ai-tech/themes/alpha.md", "wiki/ai-tech/themes/beta.md") in edges


# --- orphan -------------------------------------------------------------------------------------
def test_graph_orphan_flag_and_health_parity(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    # Add an UNLINKED theme — nothing references it, so it is an orphan.
    themes = tmp_path / "wiki" / "ai-tech" / "themes"
    (themes / "lonely.md").write_text(
        "---\nstatus: active\ntype: theme\n---\n# Lonely\n\nNo one links here.\n",
        encoding="utf-8",
    )
    handlers = AgoraHandlers(repo, writer="local")

    nodes = {n["id"]: n for n in handlers.graph()["nodes"]}
    # Unlinked theme => orphan True; a linked theme => orphan False.
    assert nodes["wiki/ai-tech/themes/lonely.md"]["orphan"] is True
    assert nodes[_INBOX]["orphan"] is False
    assert nodes[_CC]["orphan"] is False
    # A non-theme (index/moc) is never an orphan even if nothing links to it.
    assert nodes["index.md"]["orphan"] is False
    assert nodes[_MOC]["orphan"] is False

    # The orphan COUNT equals health()'s orphans (the derivation is reused verbatim).
    orphan_count = sum(1 for n in nodes.values() if n["orphan"])
    assert orphan_count == handlers.health()["orphans"]


def test_graph_orphan_is_global_under_domain_filter(tmp_path: Path) -> None:
    # Orphan is a GLOBAL property computed over EVERY note BEFORE any domain filter: a theme
    # referenced only from OUTSIDE the filtered domain is still orphan=False. (A naive refactor that
    # built the referenced-set over the filtered node subset would flip it to True — and every other
    # test would still pass, so this is the guard for that regression.)
    repo = _init_repo(tmp_path)
    ai = tmp_path / "wiki" / "ai-tech" / "themes"
    ai.mkdir(parents=True, exist_ok=True)
    ops = tmp_path / "wiki" / "ops" / "themes"
    ops.mkdir(parents=True, exist_ok=True)
    # The ONLY inbound link to the ops theme comes from an ai-tech note (a different domain).
    (ai / "src.md").write_text(
        "---\nstatus: active\ntype: theme\n---\n# Src\n\nSee [Ops](../../ops/themes/opsnote.md).\n",
        encoding="utf-8",
    )
    (ops / "opsnote.md").write_text(
        "---\nstatus: active\ntype: theme\n---\n# Ops Note\n\nA leaf referenced from ai-tech.\n",
        encoding="utf-8",
    )
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.graph(domain="ops")
    nodes = {n["id"]: n for n in result["nodes"]}
    # Only the ops note is kept (the referencing ai-tech note is filtered out)…
    assert set(nodes) == {"wiki/ops/themes/opsnote.md"}
    # …yet it is NOT an orphan, because the referenced set is computed globally over all notes.
    assert nodes["wiki/ops/themes/opsnote.md"]["orphan"] is False


# --- dangling / self-loop edges -----------------------------------------------------------------
def test_graph_dangling_link_yields_no_edge(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    domain = tmp_path / "wiki" / "ai-tech" / "themes"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "solo.md").write_text(
        "---\nstatus: active\ntype: theme\n---\n# Solo\n\nA [missing](nope.md) link.\n",
        encoding="utf-8",
    )
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.graph()
    # The dangling target is not a node and yields no edge — and nothing crashes.
    assert "nope" not in {n["id"] for n in result["nodes"]}
    assert result["edges"] == []


def test_graph_self_loop_dropped(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    domain = tmp_path / "wiki" / "ai-tech" / "themes"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "selfref.md").write_text(
        "---\nstatus: active\ntype: theme\n---\n# Selfref\n\nLink to [self](selfref.md).\n",
        encoding="utf-8",
    )
    handlers = AgoraHandlers(repo, writer="local")

    edges = handlers.graph()["edges"]
    assert all(e["source"] != e["target"] for e in edges)
    assert edges == []  # the only link is a self-loop, which is dropped


# --- domain filter ------------------------------------------------------------------------------
def test_graph_domain_filter(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    # Add a second domain so the filter has something to exclude.
    other = tmp_path / "wiki" / "ops"
    other.mkdir(parents=True, exist_ok=True)
    (other / "ops-moc.md").write_text(
        "---\nstatus: active\ntype: moc\n---\n# Ops\n\nNo children yet.\n",
        encoding="utf-8",
    )
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.graph(domain="ai-tech")
    node_ids = {n["id"] for n in result["nodes"]}
    # Only ai-tech notes; index.md (domain None) and ops are excluded.
    assert node_ids == {_MOC, _CC, _INBOX}
    assert "index.md" not in node_ids
    assert "wiki/ops/ops-moc.md" not in node_ids
    # Edges are induced on the kept set: moc->theme + body/related edges remain, index->moc gone.
    edges = {(e["source"], e["target"]) for e in result["edges"]}
    assert (_MOC, _CC) in edges
    assert (_CC, _INBOX) in edges
    assert all(s != "index.md" for (s, _t) in edges)


# --- truncation cap -----------------------------------------------------------------------------
def test_graph_truncation_caps_nodes_and_reports_honest_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Drive the cap branch by shrinking MAX_GRAPH_NODES below the corpus size. The contract: kept
    # nodes are the FIRST N by sorted rel_path, edges are recomputed on the survivors only,
    # truncated is True, and node_total/edge_total report the HONEST pre-cap counts (which can
    # differ from the post-cap len(nodes)/len(edges) — no silent truncation).
    repo = _init_repo(tmp_path)
    themes = tmp_path / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    # A linear chain a -> b -> c -> d -> e (5 themes) + the root index.md = 6 notes, 4 edges.
    chain = ["a", "b", "c", "d", "e"]
    for i, name in enumerate(chain):
        tail = f"\n\nSee [next]({chain[i + 1]}.md).\n" if i + 1 < len(chain) else "\n\nLeaf.\n"
        (themes / f"{name}.md").write_text(
            f"---\nstatus: active\ntype: theme\n---\n# {name}{tail}", encoding="utf-8"
        )
    monkeypatch.setattr(mcp_server, "MAX_GRAPH_NODES", 3)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.graph()
    assert result["truncated"] is True
    assert len(result["nodes"]) == 3
    # node_total is the TRUE pre-cap count (> the cap), never the truncated length.
    assert result["node_total"] == 6
    # The survivors are the FIRST 3 ids by sorted rel_path (index.md sorts before wiki/...).
    kept_ids = [n["id"] for n in result["nodes"]]
    assert kept_ids == sorted(kept_ids)
    assert kept_ids == [
        "index.md",
        "wiki/ai-tech/themes/a.md",
        "wiki/ai-tech/themes/b.md",
    ]
    # Edges are induced on survivors ONLY: a->b survives; b->c/c->d/d->e are dropped (c,d,e capped).
    survivor_edges = {(e["source"], e["target"]) for e in result["edges"]}
    assert survivor_edges == {("wiki/ai-tech/themes/a.md", "wiki/ai-tech/themes/b.md")}
    # edge_total is the HONEST pre-cap count (all 4 chain edges) and explicitly differs from the
    # post-cap survivor count — the documented divergence under truncation.
    assert result["edge_total"] == 4
    assert result["edge_total"] != len(result["edges"])


# --- local ego-graph ----------------------------------------------------------------------------
def test_graph_local_one_hop_both_directions(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    # curator-concurrency links TO inbox-design and is linked FROM the moc — both 1-hop neighbors.
    result = handlers.graph(center=_CC, depth=1)
    assert result["center"] == _CC
    assert result["depth"] == 1
    assert result["truncated"] is False
    node_ids = {n["id"] for n in result["nodes"]}
    assert _CC in node_ids  # the center
    assert _INBOX in node_ids  # a note the center links TO
    assert _MOC in node_ids  # a note that links TO the center
    # index.md is 2 hops away (index -> moc -> cc), excluded at depth 1.
    assert "index.md" not in node_ids

    # Edges are INDUCED on the reached set, not just the edges incident to the center: the
    # moc -> inbox edge (between two reached NON-center neighbors) must still appear.
    edges = {(e["source"], e["target"]) for e in result["edges"]}
    assert (_MOC, _CC) in edges  # neighbor -> center
    assert (_CC, _INBOX) in edges  # center -> neighbor
    assert (_MOC, _INBOX) in edges  # neighbor -> neighbor (does NOT touch the center)
    # No edge references a node outside the reached set.
    assert all(s in node_ids and t in node_ids for (s, t) in edges)


def test_graph_local_multi_hop_reaches_further_then_saturates(tmp_path: Path) -> None:
    # Multi-hop BFS over the chain index -> moc -> cc -> inbox (undirected adjacency). depth=1 from
    # inbox reaches only {inbox, cc, moc}; depth=2 additionally reaches index.md; depth=3 saturates
    # (early-break) to the same set as depth=2. Pins hop-count semantics + the frontier early exit.
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    one = {n["id"] for n in handlers.graph(center=_INBOX, depth=1)["nodes"]}
    assert one == {_INBOX, _CC, _MOC}
    assert "index.md" not in one  # index is 3 undirected hops from inbox

    two = {n["id"] for n in handlers.graph(center=_INBOX, depth=2)["nodes"]}
    assert two == {_INBOX, _CC, _MOC, "index.md"}  # the next hop reaches index

    three = {n["id"] for n in handlers.graph(center=_INBOX, depth=3)["nodes"]}
    assert three == two  # graph saturates — no further hops, the early break holds the set steady


def test_graph_local_unknown_center_returns_empty(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.graph(center="wiki/ai-tech/themes/nope.md", depth=2)
    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["node_total"] == 0
    assert result["edge_total"] == 0
    assert result["truncated"] is False
    assert result["center"] is None  # unknown center is echoed as None
    assert result["depth"] == 2  # clamped (within range) and echoed


def test_graph_local_depth_clamped(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    # depth 0 clamps up to 1; depth 99 clamps down to MAX_GRAPH_DEPTH (3).
    assert handlers.graph(center=_CC, depth=0)["depth"] == 1
    assert handlers.graph(center=_CC, depth=99)["depth"] == 3


# --- determinism / empty repo -------------------------------------------------------------------
def test_graph_is_deterministic(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    first = handlers.graph()
    second = handlers.graph()
    assert first["nodes"] == second["nodes"]
    assert first["edges"] == second["edges"]

    # Determinism is not merely call-to-call stability (same PYTHONHASHSEED) — the OUTPUT is
    # explicitly sorted, which is what survives a hash-seed-dependent set-iteration leak. Assert the
    # contract directly: node ids and (source, target) edge pairs come out in sorted order.
    node_ids = [n["id"] for n in first["nodes"]]
    assert node_ids == sorted(node_ids)
    edge_pairs = [(e["source"], e["target"]) for e in first["edges"]]
    assert edge_pairs == sorted(edge_pairs)


def test_graph_duplicate_basename_resolves_to_first_sorted_path(tmp_path: Path) -> None:
    # Two notes share the basename dup.md across domains a/ and b/. A link to dup.md must resolve
    # via the setdefault-over-sorted_notes tie-break to the FIRST rel_path (wiki/a/.../dup.md),
    # pinning the deterministic by_basename resolver.
    repo = _init_repo(tmp_path)
    for dom in ("a", "b"):
        themes = tmp_path / "wiki" / dom / "themes"
        themes.mkdir(parents=True, exist_ok=True)
        (themes / "dup.md").write_text(
            f"---\nstatus: active\ntype: theme\n---\n# Dup {dom}\n\nLeaf.\n", encoding="utf-8"
        )
    (tmp_path / "wiki" / "a" / "themes" / "src.md").write_text(
        "---\nstatus: active\ntype: theme\n---\n# Src\n\nSee [Dup](dup.md).\n", encoding="utf-8"
    )
    handlers = AgoraHandlers(repo, writer="local")

    edges = {(e["source"], e["target"]) for e in handlers.graph()["edges"]}
    # wiki/a/themes/dup.md sorts before wiki/b/themes/dup.md, so the link resolves to the a/ copy.
    assert ("wiki/a/themes/src.md", "wiki/a/themes/dup.md") in edges
    assert ("wiki/a/themes/src.md", "wiki/b/themes/dup.md") not in edges


def test_graph_empty_repo(tmp_path: Path) -> None:
    # A freshly-initialized repo has only the schema-compliant root index.md.
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.graph()
    assert {n["id"] for n in result["nodes"]} == {"index.md"}
    assert result["edges"] == []
    assert result["node_total"] == 1
    assert result["edge_total"] == 0
    assert result["truncated"] is False
