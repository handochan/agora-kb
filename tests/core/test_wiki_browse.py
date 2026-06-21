"""Tests for the core browse read helpers Wiki.list_notes / Wiki.get_note (ADR-0003/0019).

These are READ-ONLY navigation helpers for the web/browse face — they delegate to the single
deterministic note scanner (``schema.notes.parse_all_notes``) and never touch ``query`` scoring.
The fixture mirrors the ``test_wiki`` corpus (index + an ai-tech MOC + two themes + a deprecated
roadmap note) so the produced-repo shape is exercised.
"""

from __future__ import annotations

from pathlib import Path

from agora_kb.core.layout import RepoLayout
from agora_kb.core.wiki import Wiki
from agora_kb.schema.notes import Note

INDEX_MD = """\
---
type: index
status: active
---
# personal

- [AI Tech MOC](wiki/ai-tech/ai-tech-moc.md)
"""

AI_TECH_MOC = """\
---
status: active
---
# AI Tech

- [Curator concurrency](themes/curator-concurrency.md) — single-writer curator serializes writes
"""

CURATOR_CONCURRENCY = """\
---
status: active
tags: [single-writer, concurrency]
---
# Curator Concurrency

The curator acquires a per-repo flock so exactly one writer advances the curated branch.
"""

ROADMAP = """\
---
status: deprecated
tags: [roadmap]
---
# Roadmap

Phase 1 is the personal MVP milestone.
"""


def _build_repo(root: Path) -> RepoLayout:
    """Materialize a small produced-repo corpus under ``root`` and return its layout."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(INDEX_MD, encoding="utf-8")
    themes = root / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True)
    (root / "wiki" / "ai-tech" / "ai-tech-moc.md").write_text(AI_TECH_MOC, encoding="utf-8")
    (themes / "curator-concurrency.md").write_text(CURATOR_CONCURRENCY, encoding="utf-8")
    personal = root / "wiki" / "personal"
    personal.mkdir(parents=True)
    (personal / "roadmap.md").write_text(ROADMAP, encoding="utf-8")
    return RepoLayout(root)


def test_list_notes_returns_all_tracked_notes_in_path_order(tmp_path: Path) -> None:
    wiki = Wiki(_build_repo(tmp_path / "personal"))
    notes = wiki.list_notes()

    assert all(isinstance(n, Note) for n in notes)
    rel_paths = [n.rel_path for n in notes]
    # index.md + MOC + theme + roadmap, deterministic POSIX-path order (schema docs excluded).
    assert rel_paths == [
        "index.md",
        "wiki/ai-tech/ai-tech-moc.md",
        "wiki/ai-tech/themes/curator-concurrency.md",
        "wiki/personal/roadmap.md",
    ]


def test_list_notes_empty_repo(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert Wiki(RepoLayout(empty)).list_notes() == []


def test_get_note_returns_matching_note(tmp_path: Path) -> None:
    wiki = Wiki(_build_repo(tmp_path / "personal"))

    note = wiki.get_note("wiki/ai-tech/themes/curator-concurrency.md")
    assert note is not None
    assert isinstance(note, Note)
    assert note.basename == "curator-concurrency"
    assert note.frontmatter.get("status") == "active"

    index = wiki.get_note("index.md")
    assert index is not None
    assert index.basename == "index"


def test_get_note_miss_returns_none(tmp_path: Path) -> None:
    wiki = Wiki(_build_repo(tmp_path / "personal"))
    assert wiki.get_note("wiki/ai-tech/themes/does-not-exist.md") is None


def test_get_note_is_traversal_safe(tmp_path: Path) -> None:
    # A traversal / absolute / untracked path matches no parse_all_notes result → None. The seam
    # never reads an arbitrary file off disk, so the face cannot escape the repo through it.
    wiki = Wiki(_build_repo(tmp_path / "personal"))
    for bad in (
        "../secret.md",
        "../../etc/passwd",
        "/etc/passwd",
        "wiki/../../escape.md",
        "AGENTS.md",  # parse-exempt schema doc is not a browsable note
    ):
        assert wiki.get_note(bad) is None
