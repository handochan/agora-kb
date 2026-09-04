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
    body_link_basenames,
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
        # ADR-0014 D3: BODY child bullets are markdown links (the produced form).
        "- [AI/Tech MOC](wiki/ai-tech/ai-tech-moc.md) — models",
    )
    _write_note(
        layout,
        "wiki/ai-tech/ai-tech-moc.md",
        {"title": "AI/Tech", "type": "moc", "status": "active", "summary": "domain hub"},
        "- [Curator concurrency](themes/curator-concurrency.md) — the curator",
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


def test_parse_all_notes_strict_malformed_frontmatter_raises_with_path(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    (layout.root / "wiki" / "ai-tech").mkdir(parents=True)
    (layout.root / "wiki" / "ai-tech" / "broken.md").write_text(
        "no frontmatter at all", encoding="utf-8"
    )
    with pytest.raises(FrontmatterError) as exc:
        parse_all_notes(layout, strict=True)
    assert "wiki/ai-tech/broken.md" in str(exc.value)


def test_parse_all_notes_tolerant_default_degrades_fenceless_note(tmp_path: Path) -> None:
    """The DEFAULT (tolerant) read must not raise on a fenceless note; it degrades to empty
    frontmatter + the full text as body (ADR-0014 D1), so the browse face stays up."""
    layout = RepoLayout(tmp_path)
    (layout.root / "wiki" / "general").mkdir(parents=True)
    (layout.root / "wiki" / "general" / "nofm.md").write_text(
        "# No frontmatter here\n\nbody.\n", encoding="utf-8"
    )
    (note,) = parse_all_notes(layout)  # must NOT raise
    assert note.rel_path == "wiki/general/nofm.md"
    assert note.type is None
    assert note.frontmatter == {}
    assert note.body == "# No frontmatter here\n\nbody.\n"


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


# --- child_bullets (§3.2 frozen MOC child-bullet grammar — ADR-0014 D3 markdown links) -----------
# Since ADR-0014 D3 the BODY child bullet is a STANDARD MARKDOWN LINK `- [Title](relative.md)`; the
# child basename is parsed from the link PATH (filename minus dir minus `.md`). The `children:`
# FRONTMATTER stays `[[basename]]` (exercised by wikilinks() above). Identity is still the basename.


def test_child_bullets_matches_adr_index_example_markdown_links() -> None:
    # index.md body — children are the domain MOCs, linked as `[Title](wiki/<dom>/<dom>-moc.md)`
    # (ADR-0014 D3). The basename is parsed from the path → the child set is the two MOC basenames.
    body = (
        "- [AI/Tech MOC](wiki/ai-tech/ai-tech-moc.md) — models, tooling, architecture\n"
        "- [Economy MOC](wiki/economy/economy-moc.md) — macro"
    )
    assert child_bullets(body) == {"ai-tech-moc", "economy-moc"}


def test_child_bullets_theme_child_path_yields_basename() -> None:
    # A MOC links a theme child as `themes/<base>.md`; the basename is parsed from that path
    # (ADR-0014 D3 relative path form: from the MOC's dir, no leading `/` or `./`).
    assert child_bullets("- [Curator concurrency](themes/curator-concurrency.md)") == {
        "curator-concurrency"
    }


def test_child_bullets_index_to_moc_path_yields_basename() -> None:
    # The root index links a domain MOC as `wiki/<domain>/<domain>-moc.md`; basename == `<dom>-moc`.
    assert child_bullets("- [AI/Tech MOC](wiki/ai-tech/ai-tech-moc.md)") == {"ai-tech-moc"}


def test_child_bullets_link_text_is_ignored_for_identity() -> None:
    # The link TEXT (the title) is display-only; identity is the path's basename. A title with
    # spaces/punctuation does not change the parsed basename.
    assert child_bullets("- [Some Fancy Title!](themes/cqrs.md) some trailing prose") == {"cqrs"}


@pytest.mark.parametrize(
    "raw_text",
    [
        "Edge ] case",  # a `]` terminates the link-text group before the real `](`
        "[bracketed]",  # any bracket char breaks the frozen `[^\]\r\n]*` text class
    ],
)
def test_child_bullets_raw_bracket_in_text_does_not_round_trip(raw_text: str) -> None:
    # The FROZEN `_CHILD_BULLET_RE` link-text class `[^\]\r\n]*` CANNOT contain `]` or a newline.
    # A bullet whose TEXT carries a raw `]` therefore FAILS to parse — silently dropping the child.
    # This is exactly why the curator emitter must sanitize a model-decided title before emit
    # (ADR-0014 D3 / ADR-0010 D5 round-trip): the emitter-driven emit->parse round-trip is asserted
    # in tests/curator/test_apply.py (test_create_theme_with_breaking_title_still_round_trips...).
    assert child_bullets(f"- [{raw_text}](themes/edge-case.md)") != {"edge-case"}
    # The sanitized TEXT (brackets removed) round-trips back to the same basename.
    sanitized = raw_text.replace("]", "").replace("[", "")
    assert child_bullets(f"- [{sanitized}](themes/edge-case.md)") == {"edge-case"}


@pytest.mark.parametrize(
    "line",
    [
        "  - [Indented](themes/indented.md)",  # indented (not at indent 0)
        "* [Star](themes/star-marker.md)",  # wrong marker
        "+ [Plus](themes/plus-marker.md)",  # wrong marker
        "-[NoSpace](themes/no-space.md)",  # missing the required hyphen-space
        "- prose then [Late](themes/late-link.md)",  # link is not the first token
        "text [Inline](themes/inline.md) not a bullet",  # prose, not a bullet
        "> [InCallout](themes/in-callout.md)",  # blockquote, not a child bullet
        "- [Child](themes/child.md)xyz",  # trailing text must be whitespace-led, not glued to )
        "- [[wikilink-in-body]]",  # a bare wikilink is NO LONGER a child bullet (ADR-0014 D3)
    ],
)
def test_child_bullets_non_matches(line: str) -> None:
    assert child_bullets(line) == set()


def test_child_bullets_second_link_on_line_ignored() -> None:
    # Exactly one markdown link is captured (the first token); a second link on the line is ignored.
    assert child_bullets("- [A](themes/a.md) and [B](themes/b.md)") == {"a"}


def test_child_bullets_bare_no_trailing_matches() -> None:
    # The trailing `(?:\s.*)?$` group is OPTIONAL: a bare `- [T](themes/base.md)` (no trailing
    # text) matches.
    assert child_bullets("- [Bare](themes/bare.md)") == {"bare"}


def test_child_bullets_dedup_collapses() -> None:
    body = "- [X](themes/x.md)\n- [X again](themes/x.md)\n- [Y](themes/y.md)"
    assert child_bullets(body) == {"x", "y"}


def test_child_bullets_ignores_wikilinks_prose_and_nested_links() -> None:
    body = (
        "- [Real child](themes/real-child.md)\n"
        "Some prose mentioning [Other](themes/other-theme.md).\n"  # not a bullet (prose)
        "  - [Nested](themes/nested-not-child.md)\n"  # indented (not at indent 0)
        "Recent: [[ai-tech-2026-06-13]], [[ai-tech-2026-06-12]]\n"  # wikilinks, not graph bullets
    )
    assert child_bullets(body) == {"real-child"}


# --- body_link_basenames (ADR-0014 D3 BODY markdown graph edges, for L1-2 + read-path seeding) ----


def test_body_link_basenames_resolves_theme_and_moc_paths() -> None:
    # Resolves every body markdown `.md` link to its basename (path → file minus dir minus `.md`).
    body = (
        "- [Curator concurrency](themes/curator-concurrency.md)\n"
        "- [AI/Tech MOC](wiki/ai-tech/ai-tech-moc.md)\n"
    )
    assert body_link_basenames(body) == ["curator-concurrency", "ai-tech-moc"]


def test_body_link_basenames_skips_external_urls_and_images() -> None:
    # An external URL (non-`.md`) is a citation, an `![alt](assets/…)` is an asset — neither is a
    # note graph edge (ADR-0010 §3.5 / ADR-0014 D3), so both are skipped; only the `.md` link wins.
    body = (
        "See [the OKF spec](https://example.com/okf) and the diagram "
        "![arch](assets/arch.png) plus [a theme](themes/single-writer.md)."
    )
    assert body_link_basenames(body) == ["single-writer"]


def test_body_link_basenames_preserves_order_and_duplicates() -> None:
    body = "[a](themes/a.md) [b](themes/b.md) [a again](themes/a.md)"
    assert body_link_basenames(body) == ["a", "b", "a"]


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


# --- ADR-0041 D2.2 / D3.2: where a subject comes from, and where it does NOT ---------------------
@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        ("wiki/finance/themes/budget.md", ("finance",)),  # a real v1 domain directory
        ("wiki/finance/finance-moc.md", ("finance",)),
        ("wiki/stray.md", ()),  # tolerated directly under wiki/ — NO domain directory
        ("index.md", ()),  # the root index belongs to no domain
        ("README.md", ()),
    ],
)
def test_schema_1_subjects_come_from_a_domain_directory_never_a_filename(
    rel_path: str, expected: tuple[str, ...]
) -> None:
    """A stray note under ``wiki/`` has no subject — reporting its FILENAME would invent one.

    ADR-0014 D1's tolerant read exists for exactly that file (``wiki/README.md``, an un-normalized
    vault file), and the fabricated value would reach every read payload: a heading on the web home
    page, a chip on ``/graph``, and a member of ``GET /api/notes``' ``subjects`` union.
    """
    from agora_kb.schema.notes import _derive_subjects

    assert _derive_subjects(rel_path, {}, 1) == expected


def test_lint_still_reads_a_stray_notes_domain_so_l1_5_can_reject_it() -> None:
    """The two v1 readings differ ON PURPOSE — this pins the half the read facet must not copy."""
    from agora_kb.schema.notes import v1_path_domain

    assert v1_path_domain("wiki/stray.md") == "stray.md"
    assert v1_path_domain("wiki/finance/themes/budget.md") == "finance"
    assert v1_path_domain("index.md") is None


def test_the_people_exclusion_is_one_schema_gated_predicate() -> None:
    """ADR-0041 D3.3's tree exists only on schema 2 (``lint``'s ``skip_people = version >= 2``).

    ``core.gold`` and ``faces.mcp_server`` both ask this question, and an unconditional path test
    in either would make a schema-1 repo that merely owns a ``people`` DOMAIN answer one way for
    the dashboard and another for the pack.
    """
    from agora_kb.schema.notes import Note, is_people_path, is_ungraded_people_note

    v2 = Note(rel_path="wiki/people/hando/desk.md", basename="desk", type=None, schema_version=2)
    v1 = Note(rel_path="wiki/people/hando/desk.md", basename="desk", type=None, schema_version=1)
    concept = Note(rel_path="wiki/concepts/a.md", basename="a", type=None, schema_version=2)

    assert is_ungraded_people_note(v2) is True
    assert is_ungraded_people_note(v1) is False  # an ordinary v1 domain, graded like any other
    assert is_ungraded_people_note(concept) is False
    # The bare path predicate is schema-blind by design; the gate above is what callers use.
    assert is_people_path("wiki/people/hando/desk.md") is True
    assert is_people_path("wiki/People/hando/desk.md") is False  # exact-case, D3.1 closed set
