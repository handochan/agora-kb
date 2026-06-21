"""Tests for the browse read methods AgoraHandlers.browse / .note (ADR-0003/0019).

These shape the core ``Wiki.list_notes`` / ``Wiki.get_note`` helpers into JSON-serializable dicts
for the web face — the same transport-free handler the MCP face uses. They are read-only and never
render HTML (body stays raw markdown). The corpus mirrors the other faces tests (index + MOC + two
themes) on a real on-disk repo.
"""

from __future__ import annotations

from pathlib import Path

from agora_kb.core import Repo
from agora_kb.faces.mcp_server import AgoraHandlers


def _init_repo(tmp_path: Path) -> Repo:
    repo = Repo.resolve(tmp_path)
    repo.init()
    return repo


def _write_wiki_notes(tmp_path: Path) -> None:
    """Place a small navigable corpus on disk (index + MOC + two themes) under ``wiki/``."""
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
        "title: Curator Concurrency Model\n---\n"
        "# Curator Concurrency\n\n"
        "The curator acquires a per-repo flock. See [Inbox design](inbox-design.md).\n",
        encoding="utf-8",
    )
    (themes / "inbox-design.md").write_text(
        "---\nstatus: active\ntype: theme\ntags: [inbox, append-only]\n---\n"
        "# Inbox Design\n\nThe inbox is append-only and per-writer namespaced.\n",
        encoding="utf-8",
    )


# --- browse -------------------------------------------------------------------------------------
def test_browse_lists_notes_and_domains(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.browse()

    assert set(result) == {"notes", "domains"}
    notes = result["notes"]
    assert isinstance(notes, list) and notes

    # Sorted deterministically by rel_path, index.md first.
    rel_paths = [n["rel_path"] for n in notes]
    assert rel_paths == sorted(rel_paths)
    assert rel_paths[0] == "index.md"

    # Every row carries exactly the documented summary fields.
    for row in notes:
        assert set(row) == {"rel_path", "basename", "type", "title", "status", "tags", "domain"}

    by_path = {n["rel_path"]: n for n in notes}
    cc = by_path["wiki/ai-tech/themes/curator-concurrency.md"]
    assert cc["domain"] == "ai-tech"
    assert cc["type"] == "theme"
    assert cc["status"] == "active"
    assert cc["title"] == "Curator Concurrency Model"  # frontmatter title wins
    assert cc["tags"] == ["single-writer", "concurrency"]

    index = by_path["index.md"]
    assert index["domain"] is None  # index.md is not in a domain
    assert index["title"] == "index"  # no frontmatter title → basename fallback
    assert index["type"] == "index"
    assert index["tags"] == []

    # domains = sorted unique non-None domains.
    assert result["domains"] == ["ai-tech"]


def test_browse_freshly_initialized_repo(tmp_path: Path) -> None:
    # A freshly-initialized repo has only the schema-compliant root index.md and no domains yet.
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.browse()
    assert [n["rel_path"] for n in result["notes"]] == ["index.md"]
    assert result["domains"] == []


# --- note ---------------------------------------------------------------------------------------
def test_note_returns_full_payload(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    payload = handlers.note("wiki/ai-tech/themes/curator-concurrency.md")
    assert payload is not None
    assert set(payload) == {
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
    assert payload["basename"] == "curator-concurrency"
    assert payload["domain"] == "ai-tech"
    assert payload["status"] == "active"
    # Body stays RAW markdown (rendering is the web layer's job), with its H1 intact.
    assert payload["body"].startswith("# Curator Concurrency")
    assert isinstance(payload["frontmatter"], dict)
    # Links are the body graph-edge basenames.
    assert payload["links"] == ["inbox-design"]


def test_note_missing_returns_none(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    assert handlers.note("wiki/ai-tech/themes/nope.md") is None
    # Traversal-safe: an escape path resolves to no tracked note.
    assert handlers.note("../../etc/passwd") is None
