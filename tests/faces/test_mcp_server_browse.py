"""Tests for the browse read methods AgoraHandlers.browse / .note (ADR-0003/0019).

These shape the core ``Wiki.list_notes`` / ``Wiki.get_note`` helpers into JSON-serializable dicts
for the web face — the same transport-free handler the MCP face uses. They are read-only and never
render HTML (body stays raw markdown). The corpus is a KB WIKI SCHEMA 2 repo (ADR-0041 D1: the
first segment under ``wiki/`` IS the kind, the subject lives in ``subjects:``) on a real on-disk
repo — index + map + two concepts + one human-owned people note.
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
    """Place a small navigable schema-2 corpus on disk: index + map + two concepts + a person.

    ``_meta/taxonomy.yaml`` carries the ``schema_version: 2`` that
    :func:`agora_kb.schema.notes.resolve_schema_version` reads — the read side derives ``kind`` and
    ``subjects`` under the REPO's schema, so without that file the same bytes would be parsed under
    the v1 derivation and every ``kind`` would come back ``None``.
    """
    (tmp_path / "_meta").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_meta" / "taxonomy.yaml").write_text(
        "schema_version: 2\ndomains: [ai-tech]\nallowed_tags: []\n", encoding="utf-8"
    )
    (tmp_path / "index.md").write_text(
        "---\nkind: index\nstatus: active\n---\n# personal\n\n- [AI Tech](wiki/maps/ai-tech.md)\n",
        encoding="utf-8",
    )
    maps = tmp_path / "wiki" / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    (maps / "ai-tech.md").write_text(
        "---\nstatus: active\nkind: map\nsubjects: [ai-tech]\n---\n# AI Tech\n\n"
        "- [Curator concurrency](../concepts/curator-concurrency.md) — single-writer curator\n"
        "- [Inbox design](../concepts/inbox-design.md) — append-only inbox\n",
        encoding="utf-8",
    )
    concepts = tmp_path / "wiki" / "concepts"
    concepts.mkdir(parents=True, exist_ok=True)
    (concepts / "curator-concurrency.md").write_text(
        "---\nstatus: active\nkind: concept\nsubjects: [ai-tech]\n"
        "tags: [single-writer, concurrency]\n"
        "title: Curator Concurrency Model\n---\n"
        "# Curator Concurrency\n\n"
        "The curator acquires a per-repo flock. See [Inbox design](inbox-design.md).\n",
        encoding="utf-8",
    )
    (concepts / "inbox-design.md").write_text(
        "---\nstatus: active\nkind: concept\nsubjects: [ai-tech]\ntags: [inbox, append-only]\n"
        "---\n# Inbox Design\n\nThe inbox is append-only and per-writer namespaced.\n",
        encoding="utf-8",
    )
    people = tmp_path / "wiki" / "people" / "hando"
    people.mkdir(parents=True, exist_ok=True)
    (people / "reading-notes.md").write_text(
        "---\ntitle: Reading notes\nstatus: active\n---\n"
        "# Reading notes\n\nA human's own file, never written by the curator.\n",
        encoding="utf-8",
    )


# --- browse -------------------------------------------------------------------------------------
def test_browse_lists_notes_and_subjects(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.browse()

    assert set(result) == {"notes", "subjects"}
    notes = result["notes"]
    assert isinstance(notes, list) and notes

    # Sorted deterministically by rel_path, index.md first.
    rel_paths = [n["rel_path"] for n in notes]
    assert rel_paths == sorted(rel_paths)
    assert rel_paths[0] == "index.md"

    # Every row carries exactly the documented summary fields — `type`/`domain` are GONE, not
    # mirrored alongside the new ones (ADR-0041 D2/D3.2).
    for row in notes:
        assert set(row) == {"rel_path", "basename", "kind", "title", "status", "tags", "subjects"}

    by_path = {n["rel_path"]: n for n in notes}
    cc = by_path["wiki/concepts/curator-concurrency.md"]
    assert cc["subjects"] == ["ai-tech"]
    assert cc["kind"] == "concept"
    assert cc["status"] == "active"
    assert cc["title"] == "Curator Concurrency Model"  # frontmatter title wins
    assert cc["tags"] == ["single-writer", "concurrency"]

    index = by_path["index.md"]
    assert index["subjects"] == []  # the root map declares no subject
    assert index["title"] == "index"  # no frontmatter title → basename fallback
    assert index["kind"] == "index"
    assert index["tags"] == []

    # Read is FIRST CLASS over `wiki/people/**` (ADR-0041 D3.3): the human-owned note is listed,
    # with the DERIVED `person` kind it never declares in its own frontmatter.
    person = by_path["wiki/people/hando/reading-notes.md"]
    assert person["kind"] == "person"
    assert person["subjects"] == []

    # subjects = the sorted union of every note's declared subjects.
    assert result["subjects"] == ["ai-tech"]


def test_browse_freshly_initialized_repo(tmp_path: Path) -> None:
    # A freshly-initialized repo has only the schema-compliant root index.md and no subjects yet.
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.browse()
    assert [n["rel_path"] for n in result["notes"]] == ["index.md"]
    assert result["subjects"] == []


# --- note ---------------------------------------------------------------------------------------
def test_note_returns_full_payload(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    payload = handlers.note("wiki/concepts/curator-concurrency.md")
    assert payload is not None
    # `kind` is KEPT in the per-note payload (v1's `type` was dropped): it is DERIVED from the
    # directory, so it is not a duplicate of any frontmatter key the caller can read itself.
    assert set(payload) == {
        "rel_path",
        "basename",
        "kind",
        "title",
        "status",
        "tags",
        "subjects",
        "frontmatter",
        "body",
        "links",
    }
    assert payload["basename"] == "curator-concurrency"
    assert payload["kind"] == "concept"
    assert payload["subjects"] == ["ai-tech"]
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

    assert handlers.note("wiki/concepts/nope.md") is None
    # Traversal-safe: an escape path resolves to no tracked note.
    assert handlers.note("../../etc/passwd") is None
