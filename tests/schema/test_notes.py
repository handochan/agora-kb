"""Tests for note parsing + the FROZEN content grammars (ADR-0010 §1, §3.1, §3.2, §4.1).

The grammar tests are graded against the ADR's own worked examples so two independent
implementations cannot disagree byte-for-byte (ADR-0010 D5).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agora_kb.core.frontmatter import FrontmatterError, render
from agora_kb.core.layout import RepoLayout
from agora_kb.schema.notes import (
    child_bullets,
    heading_slug,
    note_basename,
    parse_all_notes,
    wikilinks,
)

# --- fixtures -------------------------------------------------------------------------------------


def _write_note(layout: RepoLayout, rel: str, fm: dict[str, object], body: str) -> Path:
    path = layout.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(fm, body), encoding="utf-8")
    return path


def _seed_repo(layout: RepoLayout) -> None:
    _write_note(
        layout,
        "index.md",
        {"title": "KB index", "type": "index", "status": "active", "summary": "root"},
        "- [[ai-tech-moc]] — models",
    )
    _write_note(
        layout,
        "wiki/ai-tech/ai-tech-moc.md",
        {"title": "AI/Tech", "type": "moc", "status": "active", "summary": "domain hub"},
        "- [[curator-concurrency]] — the curator",
    )
    _write_note(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        {
            "title": "Curator concurrency model",
            "type": "theme",
            "status": "active",
            "summary": "one curator",
            "sources": ["raw/ai-tech/2026-06-09-cqrs-notes.md"],
        },
        "Exactly one curator advances the branch. See [[git-as-audit-log]].",
    )


# --- parse_all_notes ------------------------------------------------------------------------------


def test_parse_all_notes_finds_index_and_wiki(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    _seed_repo(layout)
    notes = parse_all_notes(layout)
    by_base = {n.basename: n for n in notes}
    assert set(by_base) == {"index", "ai-tech-moc", "curator-concurrency"}
    assert by_base["index"].type == "index"
    assert by_base["curator-concurrency"].type == "theme"
    assert by_base["curator-concurrency"].frontmatter["summary"] == "one curator"
    assert "Exactly one curator" in by_base["curator-concurrency"].body


def test_parse_all_notes_deterministic_path_order(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    _seed_repo(layout)
    paths = [n.rel_path for n in parse_all_notes(layout)]
    assert paths == sorted(paths)
    # POSIX-style relative paths, no leading slash, no backslashes.
    assert all(not p.startswith("/") and "\\" not in p for p in paths)


def test_parse_all_notes_skips_schema_doc_and_symlinks_by_basename(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    _seed_repo(layout)
    # The schema doc + its symlink aliases are parse-exempt (ADR-0010 §1) — here as plain files so
    # the basename-skip path is exercised regardless of OS symlink support.
    for name in ("AGENTS.md", "SCHEMA.md", "CLAUDE.md", "QWEN.md", "GEMINI.md"):
        (layout.root / name).write_text(
            render({"title": name, "type": "theme"}, "should never be parsed"),
            encoding="utf-8",
        )
    bases = {n.basename for n in parse_all_notes(layout)}
    assert bases.isdisjoint({"AGENTS", "SCHEMA", "CLAUDE", "QWEN", "GEMINI"})
    assert bases == {"index", "ai-tech-moc", "curator-concurrency"}


@pytest.mark.skipif(os.name != "posix", reason="symlink identity skip is POSIX-relevant")
def test_parse_all_notes_skips_symlinks_by_identity(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    _seed_repo(layout)
    # A symlink whose basename is NOT in the exempt set must still be skipped by symlink identity
    # (only the schema symlinks are allowed; the parser never follows any symlink).
    real = layout.root / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    link = layout.root / "wiki" / "ai-tech" / "themes" / "aliased-theme.md"
    os.symlink(real, link)
    bases = {n.basename for n in parse_all_notes(layout)}
    assert "aliased-theme" not in bases


def test_parse_all_notes_empty_repo(tmp_path: Path) -> None:
    assert parse_all_notes(RepoLayout(tmp_path)) == []


def test_parse_all_notes_missing_type_is_none(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    _write_note(layout, "index.md", {"title": "no type", "summary": "x"}, "body")
    (note,) = parse_all_notes(layout)
    assert note.type is None


def test_parse_all_notes_malformed_frontmatter_raises_with_path(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    (layout.root / "wiki" / "ai-tech").mkdir(parents=True)
    (layout.root / "wiki" / "ai-tech" / "broken.md").write_text(
        "no frontmatter at all", encoding="utf-8"
    )
    with pytest.raises(FrontmatterError) as exc:
        parse_all_notes(layout)
    assert "wiki/ai-tech/broken.md" in str(exc.value)


def test_note_basename_strips_md_suffix(tmp_path: Path) -> None:
    assert note_basename(tmp_path / "wiki" / "x" / "ai-tech-moc.md") == "ai-tech-moc"


# --- wikilinks (§3.1, frozen normalization) -------------------------------------------------------


def test_wikilinks_bare_and_display() -> None:
    # Display text is dropped; the key is the substring left of '|'.
    assert wikilinks("see [[curator-concurrency]] and [[cqrs|the CQRS page]]") == [
        "curator-concurrency",
        "cqrs",
    ]


def test_wikilinks_no_case_fold_no_slug_no_unicode_norm() -> None:
    # NO case folding, NO slugging, NO unicode normalization — byte-for-byte after whitespace strip.
    assert wikilinks("[[Curator Concurrency]] [[café]] [[A_B-C]]") == [
        "Curator Concurrency",
        "café",
        "A_B-C",
    ]


def test_wikilinks_strips_only_ascii_edge_whitespace() -> None:
    assert wikilinks("[[  spaced-key  ]] [[\ttabbed\t]]") == ["spaced-key", "tabbed"]
    # Interior whitespace is preserved (only leading/trailing is stripped).
    assert wikilinks("[[two words]]") == ["two words"]


def test_wikilinks_two_links_on_one_line_not_merged() -> None:
    assert wikilinks("[[a]][[b]]") == ["a", "b"]


def test_wikilinks_empty_key_dropped() -> None:
    assert wikilinks("[[]] [[ |display]] [[real]]") == ["real"]


def test_wikilinks_order_and_duplicates_preserved() -> None:
    assert wikilinks("[[a]] then [[b]] then [[a]]") == ["a", "b", "a"]


# --- child_bullets (§3.2 frozen MOC child-bullet grammar) -----------------------------------------


def test_child_bullets_matches_adr_index_example() -> None:
    # ADR-0010 §2.5 index.md body — children are ai-tech-moc and economy-moc.
    body = "- [[ai-tech-moc]] — models, tooling, architecture notes\n- [[economy-moc]] — macro"
    assert child_bullets(body) == {"ai-tech-moc", "economy-moc"}


def test_child_bullets_with_display_text_captures_base() -> None:
    assert child_bullets("- [[cqrs|the CQRS page]] some trailing prose") == {"cqrs"}


@pytest.mark.parametrize(
    "line",
    [
        "  - [[indented]]",  # indented (not at indent 0)
        "* [[star-marker]]",  # wrong marker
        "+ [[plus-marker]]",  # wrong marker
        "-[[no-space]]",  # missing the required hyphen-space
        "- prose then [[late-link]]",  # link is not the first token
        "- [[A]]",  # base must start [a-z0-9]; uppercase is not a valid child base
        "- [[a]] and [[b]]",  # second link on the line is ignored, only base 'a' would count…
        "text [[inline]] not a bullet",  # prose, not a bullet
        "> [[in-callout]]",  # blockquote, not a child bullet
        "- [[child]]xyz",  # trailing text must be whitespace-led (`(?:\\s.*)?$`), not glued to ]]
    ],
)
def test_child_bullets_non_matches(line: str) -> None:
    got = child_bullets(line)
    if line == "- [[a]] and [[b]]":
        # Exactly one link is captured (the first token); the second is ignored (§3.2).
        assert got == {"a"}
    else:
        assert got == set()


def test_child_bullets_bare_no_trailing_matches() -> None:
    # The trailing `(?:\s.*)?$` group is OPTIONAL: a bare `- [[base]]` (no trailing space) matches.
    assert child_bullets("- [[bare]]") == {"bare"}


def test_child_bullets_dedup_collapses() -> None:
    assert child_bullets("- [[x]]\n- [[x]]\n- [[y]]") == {"x", "y"}


def test_child_bullets_ignores_related_and_prose_links() -> None:
    body = (
        "- [[real-child]]\n"
        "Some prose mentioning [[other-theme]].\n"
        "  - [[nested-not-child]]\n"
        "Recent: [[ai-tech-2026-06-13]], [[ai-tech-2026-06-12]]\n"
    )
    assert child_bullets(body) == {"real-child"}


# --- heading_slug (§4.1 frozen anchor algorithm) --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Curator concurrency model", "curator-concurrency-model"),
        ("CQRS & the single writer", "cqrs-the-single-writer"),
        ("  Leading and trailing punctuation!!  ", "leading-and-trailing-punctuation"),
        ("Multiple   spaces", "multiple-spaces"),
        ("café déjà", "caf-d-j"),  # ASCII fold only: non-ASCII letters are not [a-z0-9]
        ("`code` and **bold** and _italic_", "code-and-bold-and-italic"),
        ("The run_id field", "the-run-id-field"),  # intraword `_` survives step 2; step 4 -> `-`
        ("snake_case_name", "snake-case-name"),  # snake_case `_`s are NOT emphasis (frozen, §4.1)
        ("a * b", "a-b"),  # a standalone `*` has no closing delimiter, so it collapses to `-`
        ("***triple***", "triple"),  # nested ***bold-italic*** spans unwrap to the inner text
        ("see [[curator-concurrency|the page]]", "see-the-page"),  # [[x|y]] -> y
        ("see [[git-as-audit-log]]", "see-git-as-audit-log"),  # [[x]] -> x
        ("a [link](https://example.com) here", "a-link-here"),  # [t](u) -> t
        ("---", ""),  # all-non-alnum trims to empty
    ],
)
def test_heading_slug_examples(text: str, expected: str) -> None:
    assert heading_slug(text) == expected


def test_heading_slug_duplicate_disambiguation_in_document_order() -> None:
    # Step 6: duplicate anchors within a file get -1, -2, … in document order (GitHub/Quartz).
    seen: dict[str, int] = {}
    assert heading_slug("Notes", seen=seen) == "notes"
    assert heading_slug("Notes", seen=seen) == "notes-1"
    assert heading_slug("Notes", seen=seen) == "notes-2"
    # A distinct heading is unaffected and starts its own counter.
    assert heading_slug("Other", seen=seen) == "other"
    assert heading_slug("Notes", seen=seen) == "notes-3"


def test_heading_slug_step6_counter_is_per_base_collision_documented() -> None:
    # Frozen step-6 edge (§4.1 "GitHub/Quartz convention"): the counter keys on the BASE slug, so a
    # heading whose own base slug already ends in `-1` ('Notes 1' -> base 'notes-1') is a DISTINCT
    # base from 'Notes' -> 'notes' / 'notes-1'. The two bases can therefore both emit the literal
    # string 'notes-1'. This test pins that known collision behavior so it cannot silently drift.
    seen: dict[str, int] = {}
    assert heading_slug("Notes", seen=seen) == "notes"  # base 'notes', count 0
    assert heading_slug("Notes", seen=seen) == "notes-1"  # base 'notes', count 1 -> 'notes-1'
    assert heading_slug("Notes 1", seen=seen) == "notes-1"  # base 'notes-1', count 0 -> 'notes-1'
    assert heading_slug("Notes 1", seen=seen) == "notes-1-1"  # base 'notes-1', count 1
