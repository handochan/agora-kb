"""Tests for the core browse read helpers Wiki.list_notes / Wiki.get_note (ADR-0003/0019).

These are READ-ONLY navigation helpers for the web/browse face — they delegate to the single
deterministic note scanner (``schema.notes.parse_all_notes``) and never touch ``query`` scoring.
The fixture mirrors the ``test_wiki`` corpus (index + an ai-tech map + a concept + a deprecated
roadmap concept) in the KB wiki schema-2 kind-first layout (ADR-0041 D1), so the produced-repo shape
is exercised — including the two schema-2 facets a browse face filters on, ``Note.kind`` (derived
from the DIRECTORY, D2.1) and ``Note.subjects`` (D2.2/D3.2).
"""

from __future__ import annotations

from pathlib import Path

from agora_kb.core.layout import RepoLayout
from agora_kb.core.wiki import Wiki
from agora_kb.schema.notes import Note

INDEX_MD = """\
---
kind: index
status: active
---
# personal

- [AI Tech MOC](wiki/maps/ai-tech.md)
"""

AI_TECH_MOC = """\
---
status: active
kind: map
subjects: [ai-tech]
---
# AI Tech

- [Curator concurrency](../concepts/curator-concurrency.md) — single-writer curator serializes
"""

CURATOR_CONCURRENCY = """\
---
status: active
kind: concept
subjects: [ai-tech]
tags: [single-writer, concurrency]
---
# Curator Concurrency

The curator acquires a per-repo flock so exactly one writer advances the curated branch.
"""

ROADMAP = """\
---
status: deprecated
kind: concept
subjects: [personal]
tags: [roadmap]
---
# Roadmap

Phase 1 is the personal MVP milestone.
"""


def _build_repo(root: Path) -> RepoLayout:
    """Materialize a small schema-2 produced-repo corpus under ``root`` and return its layout."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(INDEX_MD, encoding="utf-8")
    concepts = root / "wiki" / "concepts"
    concepts.mkdir(parents=True)
    (root / "wiki" / "maps").mkdir(parents=True)
    # `parse_all_notes` derives `kind`/`subjects` under the REPO's own schema_version (ADR-0010
    # §5.1's canonical location), so a schema-2 tree must say so or its notes are read through the
    # v1 derivation — which reads the subject off the PATH and would call this map's subject "maps".
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    (root / "_meta" / "taxonomy.yaml").write_text(
        "schema_version: 2\ndomains: [ai-tech, personal]\n", encoding="utf-8"
    )
    (root / "wiki" / "maps" / "ai-tech.md").write_text(AI_TECH_MOC, encoding="utf-8")
    (concepts / "curator-concurrency.md").write_text(CURATOR_CONCURRENCY, encoding="utf-8")
    (concepts / "roadmap.md").write_text(ROADMAP, encoding="utf-8")
    return RepoLayout(root)


def test_list_notes_returns_all_tracked_notes_in_path_order(tmp_path: Path) -> None:
    wiki = Wiki(_build_repo(tmp_path / "personal"))
    notes = wiki.list_notes()

    assert all(isinstance(n, Note) for n in notes)
    rel_paths = [n.rel_path for n in notes]
    # index.md + two concepts + the map, deterministic POSIX-path order (schema docs excluded).
    assert rel_paths == [
        "index.md",
        "wiki/concepts/curator-concurrency.md",
        "wiki/concepts/roadmap.md",
        "wiki/maps/ai-tech.md",
    ]


def test_list_notes_empty_repo(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert Wiki(RepoLayout(empty)).list_notes() == []


def test_get_note_returns_matching_note(tmp_path: Path) -> None:
    wiki = Wiki(_build_repo(tmp_path / "personal"))

    note = wiki.get_note("wiki/concepts/curator-concurrency.md")
    assert note is not None
    assert isinstance(note, Note)
    assert note.basename == "curator-concurrency"
    assert note.frontmatter.get("status") == "active"

    index = wiki.get_note("index.md")
    assert index is not None
    assert index.basename == "index"


def test_get_note_miss_returns_none(tmp_path: Path) -> None:
    wiki = Wiki(_build_repo(tmp_path / "personal"))
    assert wiki.get_note("wiki/concepts/does-not-exist.md") is None


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


def test_list_notes_exposes_the_schema_2_kind_and_subject_facets(tmp_path: Path) -> None:
    """The two facets a browse face filters on come through ``list_notes`` (ADR-0041 D2.1/D3.2).

    ``kind`` is read from the DIRECTORY — ``wiki/maps/`` is a ``map`` however its file is named,
    which is the whole axis flip — and ``subjects`` is read from frontmatter and NOWHERE else: no
    code derives a subject from a path in schema 2, so the map's own basename never becomes one.
    """
    wiki = Wiki(_build_repo(tmp_path / "personal"))
    by_path = {n.rel_path: n for n in wiki.list_notes()}

    assert by_path["wiki/maps/ai-tech.md"].kind == "map"
    assert by_path["wiki/concepts/curator-concurrency.md"].kind == "concept"
    assert by_path["index.md"].kind == "index"

    assert by_path["wiki/maps/ai-tech.md"].subjects == ("ai-tech",)
    assert by_path["wiki/concepts/roadmap.md"].subjects == ("personal",)
    # The root map is filed under no subject; `[]` asserts nothing and loses nothing (D2.2).
    assert by_path["index.md"].subjects == ()
