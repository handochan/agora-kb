"""Tests for the vault ``tags:`` reader behind ``--tags-from-vault`` (issue #174).

Every fixture is synthesized under ``tmp_path``. The owner's real vault is never read here — a unit
suite that depends on one machine's ``~/knowledge`` is not a unit suite — but the shapes below are
the ones a live walk of it produced: Obsidian inline-wikilink frontmatter, an ``.obsidian/`` config
dir, symlinked agent guides, and (in a vault that is itself an Agora repo) a ``raw/`` capture
sharing its basename with the concept that cites it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agora_kb.ingest.vault_tags import build_vault_tag_index, iter_vault_notes


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _note(title: str, *, tags: str | None = None, extra: str = "") -> str:
    lines = ["---", f"title: {title}"]
    if tags is not None:
        lines.append(f"tags: {tags}")
    if extra:
        lines.append(extra)
    lines += ["---", "", f"# {title}", ""]
    return "\n".join(lines)


# --- the walk -------------------------------------------------------------------------------------
def test_dot_directories_are_not_indexed(tmp_path: Path) -> None:
    """``.obsidian/`` and ``.git/`` are configuration and history, never knowledge."""
    _write(tmp_path, ".obsidian/x.md", _note("X", tags="[a]"))
    _write(tmp_path, ".git/y.md", _note("Y", tags="[b]"))
    _write(tmp_path, "real.md", _note("Real", tags="[c]"))

    index = build_vault_tag_index(tmp_path)

    assert set(index.by_basename) == {"real"}
    assert index.lookup("x").status == "unmatched"
    assert index.lookup("y").status == "unmatched"


def test_raw_is_excluded_so_a_vault_that_is_an_agora_repo_still_matches(tmp_path: Path) -> None:
    """The NET-NEW exclusion, and the reason it is load-bearing.

    A vault that is itself an Agora repo holds ``raw/<domain>/<slug>.md`` next to
    ``wiki/**/<slug>.md`` BY CONSTRUCTION — the concept cites the capture it was distilled from. The
    importer's own walk does not exclude ``raw/`` (it is absent from ``_STRUCTURAL_DIRS``), so
    without this rule the two would collide and every such note would come back ``ambiguous``,
    recovering nothing at all from exactly the vaults this command exists for.
    """
    _write(tmp_path, "raw/general/foo.md", "extracted body, no frontmatter\n")
    _write(tmp_path, "wiki/general/themes/foo.md", _note("Foo", tags="[infra]"))

    index = build_vault_tag_index(tmp_path)

    assert [p.name for p in iter_vault_notes(tmp_path)] == ["foo.md"]
    match = index.lookup("foo")
    assert match.status == "matched"
    assert match.tags == ("infra",)
    assert match.source == "wiki/general/themes/foo.md"


def test_wiki_people_is_excluded_so_a_person_note_never_tags_a_concept(tmp_path: Path) -> None:
    """The other half of the ADR-0041 D3.3 fence, on the READ side.

    ``wiki/people/**`` basenames are outside the global ``[[basename]]`` identity space, so a person
    note is not a candidate for any curated basename. Indexing one would hand a same-named concept
    that person's tags and union private vocabulary into the destination's public ``allowed_tags``
    — while the engine's own guard proves only that it never WRITES the namespace. Reachable
    exactly when the vault is itself a schema-2 Agora repo, which is the shape D5 was written for.
    """
    _write(tmp_path, "wiki/people/hando/psa-hca.md", _note("Hando on PSA", tags="[private]"))
    _write(tmp_path, "wiki/general/themes/psa-hca.md", _note("PSA HCA", tags="[statistics]"))

    index = build_vault_tag_index(tmp_path)

    assert [p.parts[-3:] for p in iter_vault_notes(tmp_path)] == [
        ("general", "themes", "psa-hca.md")
    ]
    match = index.lookup("psa-hca")
    assert match.status == "matched"  # not `ambiguous`: the person note is not a candidate
    assert match.tags == ("statistics",)


def test_exempt_stems_are_not_indexed(tmp_path: Path) -> None:
    """The schema doc, its agent-guide aliases and the run log are structure, not notes."""
    for stem in ("AGENTS", "SCHEMA", "CLAUDE", "QWEN", "GEMINI", "log"):
        _write(tmp_path, f"{stem}.md", _note(stem, tags="[a]"))
    _write(tmp_path, "kept.md", _note("Kept", tags="[a]"))

    assert set(build_vault_tag_index(tmp_path).by_basename) == {"kept"}


def test_structural_directories_are_excluded_at_any_depth(tmp_path: Path) -> None:
    _write(tmp_path, "_templates/concept.md", _note("T", tags="[a]"))
    _write(tmp_path, "deep/nest/_meta/x.md", _note("M", tags="[a]"))
    _write(tmp_path, "_kb/inbox/w/e.md", _note("E", tags="[a]"))
    _write(tmp_path, "ok.md", _note("Ok", tags="[a]"))

    assert set(build_vault_tag_index(tmp_path).by_basename) == {"ok"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_symlinks_are_never_notes(tmp_path: Path) -> None:
    """``CLAUDE.md -> AGENTS.md`` and friends: a symlink is never imported, so never indexed."""
    target = _write(tmp_path, "target.md", _note("Target", tags="[a]"))
    (tmp_path / "alias.md").symlink_to(target)

    assert set(build_vault_tag_index(tmp_path).by_basename) == {"target"}


# --- reading ----------------------------------------------------------------------------------
def test_obsidian_inline_wikilink_frontmatter_still_yields_its_tags(tmp_path: Path) -> None:
    """THE regression that decides whether this feature works on a real vault.

    ``links: [[a]], [[b]]`` is not valid YAML; ``core.frontmatter.parse`` raises on it. On the
    owner's own vault seven of the nine matching notes have that shape, so a reader built on
    ``parse`` silently recovers one note in nine while reporting the rest as honestly tag-less.
    """
    _write(
        tmp_path,
        "linked.md",
        "---\ntitle: Linked\ntags: [agent, infra]\nlinks: [[a]], [[b]]\n---\n\n# Linked\n",
    )

    match = build_vault_tag_index(tmp_path).lookup("linked")

    assert match.status == "matched"
    assert match.tags == ("agent", "infra")


def test_no_frontmatter_and_scalar_tags_both_read_as_no_tags(tmp_path: Path) -> None:
    """The importer dropped both shapes, so the inverse does not resurrect either.

    ``_filter_tags`` iterates a LIST and skips non-strings, and replaces a non-list ``tags`` value
    with ``[]`` — a scalar ``tags: research`` never survived the import, so recovering it would be
    adding a tag the destination never had rather than restoring one it lost.
    """
    _write(tmp_path, "bare.md", "# Bare\n\nno frontmatter at all\n")
    _write(tmp_path, "scalar.md", _note("Scalar", tags="research"))
    _write(tmp_path, "mixed.md", "---\ntitle: M\ntags: [ok, 7, null]\n---\n\n# M\n")

    index = build_vault_tag_index(tmp_path)

    assert index.lookup("bare").status == "no-tags"
    assert index.lookup("scalar").status == "no-tags"
    assert index.lookup("mixed").tags == ("ok",)


def test_duplicate_tags_are_de_duplicated_but_order_is_preserved(tmp_path: Path) -> None:
    _write(tmp_path, "dup.md", _note("Dup", tags="[b, a, b]"))

    assert build_vault_tag_index(tmp_path).lookup("dup").tags == ("b", "a")


def test_two_notes_of_one_basename_are_ambiguous_and_both_are_named(tmp_path: Path) -> None:
    """Never guess. The candidates go in ``source`` so the vault can be tidied and the run repeated
    — the tag bridge is re-entrant, so an ambiguous note is deferred, not lost."""
    _write(tmp_path, "a/dupe.md", _note("One", tags="[x]"))
    _write(tmp_path, "b/dupe.md", _note("Two", tags="[y]"))

    match = build_vault_tag_index(tmp_path).lookup("dupe")

    assert match.status == "ambiguous"
    assert match.tags == ()
    assert match.source is not None
    assert "a/dupe.md" in match.source
    assert "b/dupe.md" in match.source


def test_a_slugified_destination_that_collides_with_another_stem_is_ambiguous(
    tmp_path: Path,
) -> None:
    """The join key is the DESTINATION basename, and the importer slugifies it.

    ``notes/Foo Bar.md`` is imported to ``wiki/concepts/foo-bar.md`` (``_infer_layout``'s catch-all
    calls ``_slugify``), while ``wiki/general/themes/foo-bar.md`` keeps its stem verbatim — so the
    destination ``foo-bar`` has TWO honest candidates. Indexing raw stems alone would answer it
    ``matched`` from whichever file happens to spell the key literally and attach the other note's
    vocabulary with no sign in the report; indexing both forms makes it the skip it always was.
    """
    _write(tmp_path, "wiki/general/themes/foo-bar.md", _note("Foo Bar theme", tags="[alpha]"))
    _write(tmp_path, "notes/Foo Bar.md", _note("Foo Bar note", tags="[beta]"))

    match = build_vault_tag_index(tmp_path).lookup("foo-bar")

    assert match.status == "ambiguous"
    assert match.source is not None
    assert "notes/Foo Bar.md" in match.source
    assert "wiki/general/themes/foo-bar.md" in match.source


def test_a_note_answers_to_its_slug_as_well_as_its_stem(tmp_path: Path) -> None:
    """The recovery half of the same rule: a vault filename the importer renamed is still found."""
    _write(tmp_path, "notes/Foo Bar.md", _note("Foo Bar", tags="[alpha]"))

    index = build_vault_tag_index(tmp_path)

    assert index.lookup("foo-bar").tags == ("alpha",)  # the destination basename
    assert index.lookup("Foo Bar").tags == ("alpha",)  # the vault's own stem


def test_a_stem_that_is_already_a_slug_is_indexed_once(tmp_path: Path) -> None:
    """A note indexed under two spellings of ITSELF would make every lookup ambiguous."""
    _write(tmp_path, "already-a-slug.md", _note("Already", tags="[alpha]"))

    match = build_vault_tag_index(tmp_path).lookup("already-a-slug")

    assert match.status == "matched"
    assert match.tags == ("alpha",)


def test_a_basename_the_vault_does_not_hold_is_unmatched(tmp_path: Path) -> None:
    """Distinct from ``no-tags``: "the matcher found nothing" and "the answer is honestly empty"
    are different operator problems and must not share a bucket."""
    _write(tmp_path, "present.md", _note("Present", tags="[a]"))

    assert build_vault_tag_index(tmp_path).lookup("absent").status == "unmatched"


def test_non_utf8_bytes_do_not_raise(tmp_path: Path) -> None:
    """A vault saved as latin-1/cp1252 degrades to a mangled character, never to an exception."""
    path = tmp_path / "latin.md"
    path.write_bytes(b"---\ntitle: caf\xe9\ntags: [ok]\n---\n\n# caf\xe9\n")

    assert build_vault_tag_index(tmp_path).lookup("latin").tags == ("ok",)


def test_non_kebab_tags_are_reported_never_transformed(tmp_path: Path) -> None:
    """Lint L1-5 checks MEMBERSHIP only — it has no rule about tag SHAPE — so a tag like ``Agent``
    or ``local llm`` would sail into ``allowed_tags`` and leave a repo whose own ``InboxItem``
    validator and web upload face then refuse every capture carrying it. The reader flags them; it
    does not repair them, because the importer applied no normalisation to invert."""
    _write(tmp_path, "shouty.md", _note("Shouty", tags="[Agent, 'local llm', fine]"))

    match = build_vault_tag_index(tmp_path).lookup("shouty")

    assert match.status == "matched"
    assert match.tags == ("Agent", "local llm", "fine")  # verbatim, unrepaired
    assert match.invalid_tags == ("Agent", "local llm")
    assert match.usable is False


def test_a_clean_match_is_usable(tmp_path: Path) -> None:
    _write(tmp_path, "clean.md", _note("Clean", tags="[local-llm, agent]"))

    assert build_vault_tag_index(tmp_path).lookup("clean").usable is True
