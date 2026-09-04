"""Tests for the Obsidian/markdown vault NORMALIZER (``agora import`` / ADR-0014 D5).

These build SYNTHETIC fixture vaults in ``tmp_path`` (the real ``~/knowledge`` is NEVER touched) and
assert both the :class:`~agora_kb.ingest.vault_import.ImportReport` contents AND that the normalized
notes PARSE under :func:`agora_kb.core.frontmatter.parse` — proving the tolerant-consumer boundary
(ADR-0014 D4) turns real-world Obsidian input into closer-to-ADR-0010-conformant output without
crashing or dropping content.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.core import frontmatter
from agora_kb.ingest.vault_import import ImportReport, import_vault

_IMPORT_DATE = "2026-06-17"


def _write(root: Path, rel: str, text: str) -> Path:
    """Write ``text`` to ``root/rel`` (creating parents); return the path. Source-vault helper."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _record(report: ImportReport, rel_path: str):
    """Return the NoteRecord whose destination ``rel_path`` matches, or fail the lookup."""
    for note in report.notes:
        if note.rel_path == rel_path:
            return note
    raise AssertionError(
        f"no imported note at {rel_path!r}; have {[n.rel_path for n in report.notes]}"
    )


def _parses(dest: Path, rel_path: str) -> dict:
    """Assert the normalized note at ``dest/rel_path`` parses; return its frontmatter."""
    fm, _ = frontmatter.parse((dest / rel_path).read_text(encoding="utf-8"))
    return fm


# --- auto-fix #1: tolerant frontmatter read + Obsidian inline-wikilink repair -------------------


def test_repairs_invalid_obsidian_inline_links_frontmatter(tmp_path: Path) -> None:
    """An Obsidian ``links: [[a]], [[b]]`` line is invalid YAML — repaired to a valid list (D4)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    # This frontmatter is invalid YAML (an unquoted [[ starts a flow seq that never closes); the
    # un-repaired reader raises FrontmatterError.
    _write(
        src,
        "wiki/general/themes/topic.md",
        "---\n"
        "title: Topic\n"
        "links: [[a]], [[b]], [[c]]\n"
        "related: [[a]]\n"
        "---\n\n"
        "# Topic\n\nA body paragraph about the topic.\n",
    )
    _write(src, "wiki/general/themes/a.md", "---\ntitle: A\n---\n\n# A\n\nAlpha.\n")
    _write(src, "wiki/general/themes/b.md", "---\ntitle: B\n---\n\n# B\n\nBeta.\n")
    _write(src, "wiki/general/themes/c.md", "---\ntitle: C\n---\n\n# C\n\nGamma.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/general/themes/topic.md")
    assert rec.repaired_frontmatter is True
    # The normalized note PARSES (the whole point of the repair) and `related` survived as a list.
    fm = _parses(dest, "wiki/general/themes/topic.md")
    assert isinstance(fm["related"], list)
    assert fm["related"] == ["[[a]]"]  # resolvable -> kept as a canonical [[basename]]
    assert report.summary["repaired_frontmatter"] >= 1


def test_standalone_single_token_inline_link_survives_resolvable(tmp_path: Path) -> None:
    """A standalone ``related: [[only]]`` is VALID YAML (a nested list) — the resolvable edge must
    not be silently dropped (ADR-0014 D4: content is never lost)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    # `related: [[only]]` stands alone as valid YAML, so the parse never raises and the line-repair
    # never fires; the nested-list shape must still be recovered to its basename.
    _write(
        src,
        "wiki/general/themes/topic.md",
        "---\ntitle: Topic\nrelated: [[only]]\n---\n\n# Topic\n\nA body paragraph.\n",
    )
    _write(src, "wiki/general/themes/only.md", "---\ntitle: Only\n---\n\n# Only\n\nOnly.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    fm = _parses(dest, "wiki/general/themes/topic.md")
    assert fm["related"] == ["[[only]]"]  # resolvable -> kept, NOT dropped to []
    rec = _record(report, "wiki/general/themes/topic.md")
    assert "only" not in rec.unresolved_links


def test_standalone_single_token_children_unknown_is_reported(tmp_path: Path) -> None:
    """A standalone ``children: [[ghost]]`` on a MOC with no such note lands in unresolved_links,
    never silently empty (ADR-0014 D4)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(
        src,
        "wiki/general/general-moc.md",
        "---\ntitle: General MOC\nchildren: [[ghost]]\n---\n\n# General MOC\n\nA map.\n",
    )

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    fm = _parses(dest, "wiki/general/general-moc.md")
    assert fm["children"] == []  # unresolved -> dropped from the array...
    rec = _record(report, "wiki/general/general-moc.md")
    assert "ghost" in rec.unresolved_links  # ...but reported, never silently lost


def test_non_utf8_source_note_is_tolerated_and_reported(tmp_path: Path) -> None:
    """A non-UTF8 source note is decoded with replacement + reported — never a crash (D4)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    # A note with a stray non-UTF8 byte (the common latin-1/cp1252 real-world case). The un-tolerant
    # reader would raise UnicodeDecodeError and abort the whole import.
    note = src / "wiki/general/themes/bad.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_bytes(b"# Title\n\nbody \xff\xfe not utf8\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert isinstance(report, ImportReport)
    assert report.summary["notes"] == 1
    rec = _record(report, "wiki/general/themes/bad.md")
    assert any("not valid UTF-8" in w for w in rec.warnings)
    # The note still parses at the destination (content preserved, lossy bytes replaced).
    _parses(dest, "wiki/general/themes/bad.md")


def test_unparseable_frontmatter_is_treated_as_empty_and_reported(tmp_path: Path) -> None:
    """Frontmatter that no repair can rescue becomes empty + a warning — never a crash (D4)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    # A genuinely broken YAML block (unbalanced bracket, not the inline-wikilink shape we repair).
    _write(
        src,
        "wiki/general/themes/broken.md",
        "---\ntitle: Broken\nweird: [1, 2, {unclosed\n---\n\n# Broken\n\nStill has a body.\n",
    )

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/general/themes/broken.md")
    assert any("could not be parsed" in w for w in rec.warnings)
    # Body content is preserved and the note parses (title inferred from the H1).
    fm = _parses(dest, "wiki/general/themes/broken.md")
    assert fm["title"] == "Broken"
    assert "Still has a body." in (dest / "wiki/general/themes/broken.md").read_text()


# --- auto-fix #3: missing required frontmatter is inferred --------------------------------------


def test_infers_all_required_frontmatter_when_missing(tmp_path: Path) -> None:
    """A note with NO frontmatter gets title (H1), dates (import_date), status, summary (¶1)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(
        src,
        "wiki/general/themes/bare.md",
        "# Bare Note Heading\n\nThis is the first paragraph that becomes the summary.\n\nSecond.\n",
    )

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/general/themes/bare.md")
    for key in ("type", "title", "created", "updated", "status", "summary"):
        assert key in rec.inferred_fields, f"{key} should be inferred"

    fm = _parses(dest, "wiki/general/themes/bare.md")
    assert fm["type"] == "theme"  # auto-fix #2: type materialized from the inferred layout
    assert fm["title"] == "Bare Note Heading"  # from the H1
    assert fm["created"] == _IMPORT_DATE
    assert fm["updated"] == _IMPORT_DATE
    assert fm["status"] == "active"
    assert fm["summary"] == "This is the first paragraph that becomes the summary."


def test_inferred_type_resolves_l1_11_for_imported_theme(tmp_path: Path) -> None:
    """The materialized ``type`` means an imported theme never fails L1-11 (only L1-7 remains)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    # A theme already in the right layout, missing only sources — the import should leave NO L1-11
    # finding (type is supplied), only the report-only L1-7 (empty sources).
    _write(src, "wiki/general/themes/topic.md", "# Topic\n\nA paragraph.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    codes = {f.code for f in report.lint.findings}
    assert "L1-11" not in codes  # type was materialized
    assert codes <= {"L1-7"}  # only the report-only empty-sources finding may remain
    assert _parses(dest, "wiki/general/themes/topic.md")["type"] == "theme"


def test_title_falls_back_to_filename_title_case_without_h1(tmp_path: Path) -> None:
    """With no H1 and no frontmatter title, the filename kebab is Title-Cased (ADR-0010 §2.1)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "wiki/general/themes/my-cool-note.md", "Just a body paragraph, no heading.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    fm = _parses(dest, "wiki/general/themes/my-cool-note.md")
    assert fm["title"] == "My Cool Note"
    assert _record(report, "wiki/general/themes/my-cool-note.md").type_inferred == "theme"


def test_preserves_valid_existing_dates_and_status(tmp_path: Path) -> None:
    """A valid existing date/status is kept (not overwritten with import_date/active)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(
        src,
        "wiki/general/themes/kept.md",
        "---\n"
        "title: Kept\n"
        "created: 2025-01-02\n"
        "updated: 2025-03-04\n"
        "status: deprecated\n"
        "summary: A precise existing summary.\n"
        "---\n\n# Kept\n\nBody.\n",
    )

    import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    fm = _parses(dest, "wiki/general/themes/kept.md")
    assert fm["created"] == "2025-01-02"
    assert fm["updated"] == "2025-03-04"
    assert fm["status"] == "deprecated"
    assert fm["summary"] == "A precise existing summary."


# --- auto-fix #4: OKF fields -------------------------------------------------------------------


def test_okf_fields_added_okf_version_only_on_index(tmp_path: Path) -> None:
    """description mirrors summary; timestamp == <updated>T00:00:00Z; okf_version ONLY on index."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "index.md", "# Home\n\nThe top of the vault.\n")
    _write(src, "wiki/general/themes/topic.md", "# Topic\n\nA theme paragraph.\n")

    import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    index_fm = _parses(dest, "index.md")
    assert index_fm["okf_version"] == "0.1"
    assert index_fm["description"] == index_fm["summary"]
    assert index_fm["timestamp"] == f"{index_fm['updated']}T00:00:00Z"

    theme_fm = _parses(dest, "wiki/general/themes/topic.md")
    assert "okf_version" not in theme_fm  # bundle-root only
    assert theme_fm["description"] == theme_fm["summary"]
    assert theme_fm["timestamp"] == f"{_IMPORT_DATE}T00:00:00Z"


# --- auto-fix #5: body wikilink -> markdown link, unresolvable reported -------------------------


def test_body_links_resolvable_converted_unresolvable_reported(tmp_path: Path) -> None:
    """[[resolvable]] -> markdown link (relative); [[missing]] left verbatim + warned (D3/D4)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(
        src,
        "wiki/general/themes/topic.md",
        "# Topic\n\nSee [[other-theme]] and also [[does-not-exist]] for context.\n",
    )
    _write(src, "wiki/general/themes/other-theme.md", "# Other Theme\n\nThe other one.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/general/themes/topic.md")
    assert rec.converted_links == 1
    assert "does-not-exist" in rec.unresolved_links

    body = (dest / "wiki/general/themes/topic.md").read_text(encoding="utf-8")
    # The resolvable link became a standard markdown link with a relative path (same dir -> bare).
    assert "[other-theme](other-theme.md)" in body
    # The unresolvable wikilink is left verbatim (content never dropped, D4).
    assert "[[does-not-exist]]" in body


def test_body_link_relative_path_crosses_directories(tmp_path: Path) -> None:
    """A converted link's path is RELATIVE from the linking note's dir (increment-2 form)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "index.md", "# Home\n\nGo to [[deep-theme]].\n")
    _write(src, "wiki/general/themes/deep-theme.md", "# Deep Theme\n\nDeep.\n")

    import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    body = (dest / "index.md").read_text(encoding="utf-8")
    assert "[deep-theme](wiki/general/themes/deep-theme.md)" in body


# --- auto-fix #6: unknown tags stripped + reported ---------------------------------------------


def test_unknown_tags_stripped_and_reported_known_kept(tmp_path: Path) -> None:
    """Tags outside the --tag taxonomy are stripped + recorded; declared ones survive (L1-5)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(
        src,
        "wiki/general/themes/tagged.md",
        "---\ntitle: Tagged\ntags: [architecture, random-unknown, concurrency]\n---\n\n"
        "# Tagged\n\nBody.\n",
    )

    report = import_vault(
        src,
        dest,
        domains=["general"],
        import_date=_IMPORT_DATE,
        tags=["architecture", "concurrency"],
    )

    rec = _record(report, "wiki/general/themes/tagged.md")
    assert "random-unknown" in rec.stripped_tags
    fm = _parses(dest, "wiki/general/themes/tagged.md")
    assert set(fm["tags"]) == {"architecture", "concurrency"}
    assert report.summary["stripped_tags"] >= 1


# --- auto-fix #2: off-layout note moved + warned -----------------------------------------------


def test_off_layout_note_moved_to_first_domain_theme_and_warned(tmp_path: Path) -> None:
    """A note outside the ADR-0010 layout is moved under wiki/<first-domain>/themes/ + warned."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "notes/Loose Idea.md", "# Loose Idea\n\nA stray note at the vault root.\n")

    report = import_vault(src, dest, domains=["ai-tech", "general"], import_date=_IMPORT_DATE)

    # Moved under the FIRST domain (ai-tech), slugged.
    rec = _record(report, "wiki/ai-tech/themes/loose-idea.md")
    assert rec.type_inferred == "theme"
    assert any("moved to fit" in w for w in rec.warnings)
    assert "notes/Loose Idea.md" in " ".join(rec.warnings)
    assert report.summary["moved"] >= 1
    # And it parses cleanly at the new location.
    _parses(dest, "wiki/ai-tech/themes/loose-idea.md")


# --- v2 change B: theme with no sources is GROUNDED in a synth raw/ snapshot (L1-7/L1-8) --------


def test_theme_with_body_gets_synth_raw_source(tmp_path: Path) -> None:
    """A theme with a body but no sources is GROUNDED: its body is snapshotted to
    raw/<domain>/<slug> and cited as the only source (ADR-0014 D5 v2 change B / ADR-0010 D3)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(
        src,
        "wiki/general/themes/sourceless.md",
        "# Sourceless\n\nNo sources cited but it has real prose content.\n",
    )

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/general/themes/sourceless.md")
    # The synth raw/ snapshot is the theme's basename under its domain (a POSIX raw/ path).
    assert rec.synth_raw_source == "raw/general/sourceless.md"
    assert rec.stubbed_empty_theme is False
    # Frontmatter cites EXACTLY the synth source; no foreign / empty sources remain.
    fm = _parses(dest, "wiki/general/themes/sourceless.md")
    assert fm["sources"] == ["raw/general/sourceless.md"]
    assert fm["status"] == "active"  # NOT stubbed — it has a body
    # The raw/ file EXISTS on disk and carries the (verbatim) body content (L1-8 satisfiable).
    raw = (dest / "raw/general/sourceless.md").read_text(encoding="utf-8")
    assert "No sources cited but it has real prose content." in raw
    assert report.summary["synth_raw_sources"] >= 1
    # And the whole repo lints CLEAN — the theme satisfies L1-7 + L1-8 by construction.
    assert report.lint.ok is True


def test_empty_body_theme_becomes_stub(tmp_path: Path) -> None:
    """An EMPTY-body theme cannot be honestly grounded, so it becomes status: stub (L1-7-exempt)
    with empty sources + an 'imported empty theme -> stub' note (ADR-0014 D5 v2 change B)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    # A note that is frontmatter-only (or empty body) — nothing to snapshot into raw/.
    _write(src, "wiki/general/themes/empty.md", "---\ntitle: Empty Theme\n---\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/general/themes/empty.md")
    assert rec.stubbed_empty_theme is True
    assert rec.synth_raw_source is None
    assert any("imported empty theme -> stub" in w for w in rec.warnings)
    fm = _parses(dest, "wiki/general/themes/empty.md")
    assert fm["status"] == "stub"
    assert fm["sources"] == []
    assert report.summary["stubbed_empty_themes"] >= 1
    # A stub is L1-7-exempt, so the repo still lints clean.
    assert report.lint.ok is True


# --- robustness: a previously-crashing vault now imports + dest is git-inited -------------------


def test_previously_crashing_vault_imports_without_raising(tmp_path: Path) -> None:
    """The Obsidian frontmatter that crashed the un-tolerant reader now imports cleanly (D4/D5)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    # The exact ADR-0014-cited crash trigger plus a mix of off-layout + missing-field notes.
    _write(
        src,
        "Inbox.md",
        "---\nlinks: [[Project A]], [[Project B]]\n---\n\n# Inbox\n\nMy obsidian inbox.\n",
    )
    _write(src, "Project A.md", "# Project A\n\nFirst project.\n")
    _write(src, "Project B.md", "# Project B\n\nSecond project.\n")
    _write(src, ".obsidian/app.json", "{}")  # must be ignored, not parsed
    _write(src, "diagram.canvas", "{}")  # non-.md, must be ignored

    # Must not raise.
    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert isinstance(report, ImportReport)
    assert report.summary["notes"] == 3  # the three .md notes; canvas + .obsidian ignored
    # dest is a real git repo (Repo.init ran) and the schema was emitted.
    assert (dest / ".git").exists()
    assert (dest / "AGENTS.md").is_file()
    assert (dest / "_meta" / "taxonomy.yaml").is_file()
    # The lint result is attached (best-effort; v1 does not promise clean).
    assert report.lint is not None
    # Every normalized note parses.
    for note in report.notes:
        _parses(dest, note.rel_path)


def test_src_is_never_modified(tmp_path: Path) -> None:
    """The source vault is read-only: its bytes are identical before and after import (D5)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    note = _write(src, "wiki/general/themes/topic.md", "# Topic\n\nBody.\n")
    before = note.read_bytes()
    before_listing = sorted(p.relative_to(src).as_posix() for p in src.rglob("*"))

    import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert note.read_bytes() == before
    assert sorted(p.relative_to(src).as_posix() for p in src.rglob("*")) == before_listing


def test_missing_src_raises(tmp_path: Path) -> None:
    """A missing source vault is a HARD error (FileNotFoundError), not a silent empty import."""
    dest = tmp_path / "out"
    try:
        import_vault(tmp_path / "nope", dest, domains=["general"], import_date=_IMPORT_DATE)
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError for a missing source vault")


def test_determinism_same_inputs_same_output(tmp_path: Path) -> None:
    """import_vault is a pure function of inputs: two runs produce byte-identical notes."""
    src = tmp_path / "vault"
    dest1 = tmp_path / "out1"
    dest2 = tmp_path / "out2"
    _write(src, "wiki/general/themes/topic.md", "# Topic\n\nA paragraph.\n\nSee [[other]].\n")
    _write(src, "wiki/general/themes/other.md", "# Other\n\nAnother.\n")

    import_vault(src, dest1, domains=["general"], import_date=_IMPORT_DATE)
    import_vault(src, dest2, domains=["general"], import_date=_IMPORT_DATE)

    a = (dest1 / "wiki/general/themes/topic.md").read_bytes()
    b = (dest2 / "wiki/general/themes/topic.md").read_bytes()
    assert a == b


# --- v2 change A: structural / non-knowledge files are NOT imported as themes -------------------


def test_structural_files_are_not_imported_as_themes(tmp_path: Path) -> None:
    """The schema doc + a CLAUDE.md symlink + log.md + _templates/* are NOT imported as notes —
    the dest emits its OWN schema, so importing them would only make junk themes (v2 change A)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    # The schema doc and its agent-tool symlink (CLAUDE.md -> AGENTS.md), the run log, a template,
    # and one REAL knowledge theme that MUST survive.
    agents = _write(src, "AGENTS.md", "# Schema\n\nThe KB schema doc.\n")
    (src / "CLAUDE.md").symlink_to(agents.name)  # a symlink, never imported
    _write(src, "log.md", "# Log\n\n2026-06-17 ran curate.\n")
    _write(src, "_templates/theme.md", "---\ntype: theme\n---\n\n# {{title}}\n")
    _write(src, "wiki/general/themes/real.md", "# Real Note\n\nActual knowledge content.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    imported = {n.rel_path for n in report.notes}
    # Only the real knowledge note is imported.
    assert imported == {"wiki/general/themes/real.md"}
    # None of the structural junk themes from v1 exist.
    for junk in ("agents", "claude", "schema", "log", "theme"):
        assert not (dest / f"wiki/general/themes/{junk}.md").exists(), f"{junk} leaked as a theme"
    # The four structural files (AGENTS.md, CLAUDE.md symlink, log.md, _templates/theme.md) counted.
    assert report.summary["excluded_structural_files"] == 4
    assert report.summary["notes"] == 1


def test_meta_and_kb_subtrees_are_excluded(tmp_path: Path) -> None:
    """Files under _meta/ and _kb/ (any depth) are structural and never imported (v2 change A)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "_meta/taxonomy.md", "# taxonomy\n")
    _write(src, "_kb/inbox/event.md", "# spool\n")
    _write(src, "wiki/general/themes/keep.md", "# Keep\n\nKnowledge.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert {n.rel_path for n in report.notes} == {"wiki/general/themes/keep.md"}
    assert report.summary["excluded_structural_files"] == 2


# --- v2 change C: MOC children synced to resolvable child-bullet set ----------------------------


def test_moc_children_synced_to_resolvable_set_obsidian_bullets(tmp_path: Path) -> None:
    """A MOC whose Obsidian child bullets target a resolvable AND an unresolvable note ends with
    children: == the resolvable set; the unresolvable one is reported (v2 change C / L1-6)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    # Obsidian child bullets; present-theme exists, ghost-theme does not. The resolvable bullet is
    # converted to a markdown link (a child bullet); the unresolvable one stays a bare [[ ]] (NOT a
    # child bullet, never a graph edge), so children: ends up exactly the resolvable set.
    _write(
        src,
        "wiki/general/general-moc.md",
        "---\ntitle: General MOC\n---\n\n# General MOC\n\n"
        "## Themes\n"
        "- [[present-theme]] — a real theme\n"
        "- [[ghost-theme]] — a theme that was never written\n",
    )
    _write(src, "wiki/general/themes/present-theme.md", "# Present\n\nReal content.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    fm = _parses(dest, "wiki/general/general-moc.md")
    assert fm["children"] == ["[[present-theme]]"]  # EXACTLY the resolvable child-bullet set
    rec = _record(report, "wiki/general/general-moc.md")
    assert "ghost-theme" in rec.unresolved_links  # reported, never silently lost (D4)
    body = (dest / "wiki/general/general-moc.md").read_text(encoding="utf-8")
    # The resolvable bullet is now a markdown-link child bullet; the unresolvable target is NOT a
    # children entry and was never a graph edge (L1-2 / L1-6 both clean).
    assert "[present-theme](themes/present-theme.md)" in body
    assert report.lint.ok is True


def test_moc_unresolvable_markdown_child_bullet_line_dropped(tmp_path: Path) -> None:
    """A MOC whose body already has a STANDARD-markdown child bullet to a non-existent note has that
    bullet LINE dropped (it would fail L1-2 / break L1-6) + reported (v2 change C line-drop)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    # A markdown-vault MOC: child bullets are already standard markdown links. ghost.md was never
    # written, so its child-bullet line must be dropped to keep L1-6 / L1-2 clean.
    _write(
        src,
        "wiki/general/general-moc.md",
        "---\ntitle: General MOC\n---\n\n# General MOC\n\n"
        "- [Alpha](themes/alpha.md) — a real theme\n"
        "- [Ghost](themes/ghost.md) — never written\n",
    )
    _write(src, "wiki/general/themes/alpha.md", "# Alpha\n\nReal content.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert _parses(dest, "wiki/general/general-moc.md")["children"] == ["[[alpha]]"]
    rec = _record(report, "wiki/general/general-moc.md")
    assert "ghost" in rec.unresolved_links
    _, body = frontmatter.parse((dest / "wiki/general/general-moc.md").read_text(encoding="utf-8"))
    # The whole unresolvable child-bullet LINE is gone from the BODY; the resolvable one survives.
    assert "themes/ghost.md" not in body
    assert "[Alpha](themes/alpha.md)" in body
    assert report.lint.ok is True


# --- v2 change D: non-raw/ source entries stripped + reported -----------------------------------


def test_non_raw_source_path_is_stripped_and_reported(tmp_path: Path) -> None:
    """A pre-existing sources: entry that is NOT a raw/ path is removed + recorded; the synth raw/
    snapshot becomes the authoritative source (v2 change D / L1-8)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(
        src,
        "wiki/general/themes/coded.md",
        "---\ntitle: Coded\n"
        'sources: ["~/dev/analytics/psa @ 705f4a4 (2026-06-12)"]\n'
        "---\n\n# Coded\n\nA project analysis with a foreign locator source.\n",
    )

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/general/themes/coded.md")
    assert "~/dev/analytics/psa @ 705f4a4 (2026-06-12)" in rec.stripped_sources
    assert any("stripped non-raw source" in w for w in rec.warnings)
    assert report.summary["stripped_sources"] >= 1
    fm = _parses(dest, "wiki/general/themes/coded.md")
    # The foreign locator is gone; the synth raw/ snapshot is the sole authoritative source.
    assert fm["sources"] == ["raw/general/coded.md"]
    assert rec.synth_raw_source == "raw/general/coded.md"
    assert report.lint.ok is True


def test_existing_valid_raw_source_is_kept_and_copied(tmp_path: Path) -> None:
    """A theme already citing a raw/ artifact that EXISTS in the vault keeps it (no synth) and the
    artifact is copied into dest so L1-8 passes (v2 change B/D pre-existing-source branch)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "raw/general/kept-source.md", "the immutable captured source body\n")
    _write(
        src,
        "wiki/general/themes/has-raw.md",
        "---\ntitle: Has Raw\nsources: [raw/general/kept-source.md]\n---\n\n# Has Raw\n\nProse.\n",
    )

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/general/themes/has-raw.md")
    assert rec.synth_raw_source is None  # the pre-existing artifact wins; no synth
    fm = _parses(dest, "wiki/general/themes/has-raw.md")
    assert fm["sources"] == ["raw/general/kept-source.md"]
    # The artifact was copied into dest verbatim (L1-8 satisfiable).
    assert (dest / "raw/general/kept-source.md").read_text(encoding="utf-8") == (
        "the immutable captured source body\n"
    )
    assert report.lint.ok is True


# --- raw/ containment: an untrusted vault can never write outside dest (issue #108) -------------


# Entries a ``startswith("raw/")`` gate ACCEPTED that nonetheless address a file outside
# ``dest/raw/`` — these are the WRITE-ESCAPE locks. Each gets its OWN vault: the copy branch
# ``return``s after the first successful copy, so several escaping entries on one note would leave
# every entry but the first unexercised (the assertion would pass with or without the fix). Each
# case therefore names the file the pre-fix copy READ (it must exist, or the branch never fires)
# and the file it WROTE. The grammar is enforced on ALL platforms — a KB imported on macOS must
# behave the same on Windows — so the Windows spelling is rejected by POSIX-side code too, which
# here means "never copied anywhere" rather than "escaped".
_WRITE_ESCAPES = [
    # climbs clean out of dest: src/raw/../../pwned.md → dest/raw/../../pwned.md
    pytest.param("raw/../../pwned.md", "pwned.md", "out/pwned.md", id="climbs-out-of-dest"),
    # stays inside dest but leaves dest/raw/: src/sibling.md → dest/sibling.md
    pytest.param("raw/../sibling.md", "vault/sibling.md", "out/kb/sibling.md", id="out-of-raw"),
    # a backslash is a separator on Windows and a filename character on POSIX; either way the
    # entry must never be copied, so the POSIX-side write target is asserted absent too.
    pytest.param(
        "raw/..\\..\\pwned-win.md",
        "vault/raw/..\\..\\pwned-win.md",
        "out/kb/raw/..\\..\\pwned-win.md",
        id="backslash-separator",
    ),
]

# Entries the grammar alone rejects and that the OLD ``startswith("raw/")`` gate already dropped —
# they lock the STRIP behaviour (recorded, never silently kept), not a write escape.
_NON_RAW_SOURCES = [
    "/etc/passwd",  # absolute
    "C:/raw/pwned-drive.md",  # drive letter
    "raw",  # the root itself, not an artifact under it
]


@pytest.mark.parametrize(("entry", "read_rel", "write_rel"), _WRITE_ESCAPES)
def test_traversal_raw_source_cannot_write_outside_dest(
    tmp_path: Path, entry: str, read_rel: str, write_rel: str
) -> None:
    """A ``sources:`` entry that traverses out of ``dest/raw/`` is DROPPED and never copied (#108).

    ``dest`` is nested one level deeper than ``src`` so a traversal's read target and write target
    differ: pre-fix, ``entry.startswith("raw/")`` passed the gate, ``(src / entry)`` resolved to a
    real file, and ``dest / entry`` wrote OUTSIDE the destination repo. The assertion inspects that
    write path directly, not just the warning text.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out" / "kb"
    # ``src/raw/`` must EXIST: the kernel resolves ``raw/../x`` component by component, so without
    # it the pre-fix ``(src / entry).is_file()`` probe would fail and the copy branch would never
    # fire — the escape assertion below would then hold with or without the fix.
    (src / "raw").mkdir(parents=True, exist_ok=True)
    read_target = tmp_path / read_rel
    read_target.parent.mkdir(parents=True, exist_ok=True)
    read_target.write_text("SECRET\n", encoding="utf-8")
    # A single-quoted YAML scalar gets no escape processing, so the entry arrives verbatim (in
    # particular the backslash spelling stays a backslash).
    _write(
        src,
        "wiki/general/themes/evil.md",
        f"---\ntitle: Evil\nsources: ['{entry}']\n---\n\n# Evil\n\nUntrusted vault prose.\n",
    )

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    # (1) nothing was copied to the escaping write target, and the read target is untouched.
    assert not (tmp_path / write_rel).exists()
    assert read_target.read_text(encoding="utf-8") == "SECRET\n"

    # (2) the entry was dropped WITH a record — warn + drop, never a silent pass.
    rec = _record(report, "wiki/general/themes/evil.md")
    assert entry in rec.stripped_sources
    assert any("escapes raw/" in w for w in rec.warnings)

    # (3) the note itself still imports normally: the synth raw/ snapshot is its sole source.
    fm = _parses(dest, "wiki/general/themes/evil.md")
    assert fm["sources"] == ["raw/general/evil.md"]
    assert "Untrusted vault prose." in (dest / "raw/general/evil.md").read_text(encoding="utf-8")
    assert report.lint.ok is True


def test_non_raw_sources_are_stripped_with_a_record(tmp_path: Path) -> None:
    """Grammar-only rejects (absolute, drive letter, bare ``raw``) are stripped + recorded (#108).

    These never reached the copy branch even before the containment fix, so they lock the no-loss
    STRIP contract rather than a write escape: whatever is refused is visible in
    ``stripped_sources`` and in a warning, and the theme still gets its own snapshot.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    fm_sources = ", ".join(f"'{s}'" for s in _NON_RAW_SOURCES)
    _write(
        src,
        "wiki/general/themes/odd.md",
        f"---\ntitle: Odd\nsources: [{fm_sources}]\n---\n\n# Odd\n\nProse.\n",
    )

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/general/themes/odd.md")
    for entry in _NON_RAW_SOURCES:
        assert entry in rec.stripped_sources
    assert any("not under raw/" in w for w in rec.warnings)
    assert _parses(dest, "wiki/general/themes/odd.md")["sources"] == ["raw/general/odd.md"]
    assert report.lint.ok is True


def test_dot_slash_raw_source_is_kept_and_normalized(tmp_path: Path) -> None:
    """``./raw/<...>`` is a real raw artifact in an unnormalized spelling — kept, not stripped.

    The old ``startswith("raw/")`` gate dropped it as "not under raw/" and the theme fell back to a
    body snapshot, losing a valid provenance link. Containment (issue #108) judges the RESOLVED
    path, so the entry survives and is rewritten in canonical POSIX form — the spelling every
    downstream consumer (lint L1-8, the graph) expects. This test pins that widened acceptance so
    it cannot drift back silently.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "raw/general/kept-source.md", "the immutable captured source body\n")
    _write(
        src,
        "wiki/general/themes/dotted.md",
        "---\ntitle: Dotted\nsources: ['./raw/general/kept-source.md']\n---\n\n# Dotted\n\nP.\n",
    )

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/general/themes/dotted.md")
    assert rec.stripped_sources == ()
    assert rec.synth_raw_source is None  # the pre-existing artifact wins; no snapshot
    assert _parses(dest, "wiki/general/themes/dotted.md")["sources"] == [
        "raw/general/kept-source.md"
    ]
    assert (dest / "raw/general/kept-source.md").read_text(encoding="utf-8") == (
        "the immutable captured source body\n"
    )
    assert report.lint.ok is True


def test_relocated_dest_raw_symlink_keeps_a_cited_artifact(tmp_path: Path) -> None:
    """An operator who parks ``dest/raw/`` on another volume keeps working (issue #108 review).

    ``dest`` belongs to the operator, not to the untrusted vault, and the body-snapshot fallback in
    ``_synth_raw_source`` writes ``dest/raw/...`` through such a symlink unconditionally. Demanding
    that a CITED artifact ALSO resolve under ``dest`` itself would therefore drop valid provenance
    without preventing a single write. Both branches must agree: the cited artifact and the synth
    snapshot both land in the relocated store, and nothing is reported as an escape.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "kb"
    store = tmp_path / "bigstore"  # the operator's other volume
    store.mkdir()
    dest.mkdir()
    try:
        (dest / "raw").symlink_to(store, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # unprivileged Windows / symlink-less filesystem
        pytest.skip(f"filesystem cannot create symlinks: {exc}")
    _write(src, "raw/general/kept-source.md", "the immutable captured source body\n")
    _write(
        src,
        "wiki/general/themes/has-raw.md",
        "---\ntitle: Has Raw\nsources: [raw/general/kept-source.md]\n---\n\n# Has Raw\n\nProse.\n",
    )
    _write(src, "wiki/general/themes/no-raw.md", "---\ntitle: No Raw\n---\n\n# No Raw\n\nProse.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    cited = _record(report, "wiki/general/themes/has-raw.md")
    assert cited.stripped_sources == ()
    assert not any("escapes raw/" in w for w in cited.warnings)
    assert _parses(dest, "wiki/general/themes/has-raw.md")["sources"] == [
        "raw/general/kept-source.md"
    ]
    # Both write branches followed the operator's link into the relocated store.
    assert (store / "general/kept-source.md").read_text(encoding="utf-8") == (
        "the immutable captured source body\n"
    )
    assert (store / "general/no-raw.md").is_file()
    assert report.lint.ok is True


@pytest.mark.parametrize("planted", ["inside-raw", "raw-itself"])
def test_symlinked_raw_source_cannot_exfiltrate_into_dest(tmp_path: Path, planted: str) -> None:
    """A ``raw/`` entry whose path escapes through a SYMLINK planted in the vault is not copied.

    The grammar layer cannot see either of these: the cited paths have no ``..`` and are relative.
    The resolve layer catches them on the READ end of the copy, which would otherwise pull an
    arbitrary host file into the imported KB (issue #108). Both plantings are covered because they
    fail DIFFERENT containment checks — a link inside ``raw/`` escapes the resolved ``raw/`` root,
    while a symlinked ``raw/`` root escapes only the vault root.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("SECRET\n", encoding="utf-8")
    src.mkdir(parents=True, exist_ok=True)
    link = (src / "raw" / "link") if planted == "inside-raw" else (src / "raw")
    cited = "raw/link/secret.md" if planted == "inside-raw" else "raw/secret.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # unprivileged Windows / symlink-less filesystem
        pytest.skip(f"filesystem cannot create symlinks: {exc}")
    _write(
        src,
        "wiki/general/themes/leak.md",
        f"---\ntitle: Leak\nsources: [{cited}]\n---\n\n# Leak\n\nOwn prose.\n",
    )

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert not (dest / cited).exists()
    assert not any(
        p.is_file() and "SECRET" in p.read_text(encoding="utf-8", errors="ignore")
        for p in (dest / "raw").rglob("*")
    )
    rec = _record(report, "wiki/general/themes/leak.md")
    assert any("escapes the source vault raw/" in w for w in rec.warnings)
    # The theme falls back to its own body snapshot — a normal import result, not a failure.
    fm = _parses(dest, "wiki/general/themes/leak.md")
    assert fm["sources"] == ["raw/general/leak.md"]
    assert report.lint.ok is True


# --- v2 end-to-end: a small multi-note vault imports LINT-CLEAN ---------------------------------


def test_end_to_end_small_vault_imports_lint_clean(tmp_path: Path) -> None:
    """A small but representative vault (index + a domain MOC + 2 themes + a structural file)
    imports to a LINT-CLEAN repo — the v2 acceptance bar (ADR-0014 D5)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    # An index (Obsidian-style type) with a domain MOC link in the body.
    _write(
        src,
        "index.md",
        "---\ntitle: Home\ntype: theme\n---\n\n# Home\n\n"
        "## Domains\n- [[general-moc]] — the general domain\n",
    )
    # A domain MOC whose body lists its two themes as Obsidian child bullets.
    _write(
        src,
        "wiki/general/general-moc.md",
        "---\ntitle: General MOC\ntype: theme\n---\n\n# General\n\n"
        "## Themes\n"
        "- [[alpha]] — the first theme\n"
        "- [[beta]] — the second theme, see [[alpha]]\n",
    )
    _write(src, "wiki/general/themes/alpha.md", "# Alpha\n\nThe first concept, real prose.\n")
    _write(
        src,
        "wiki/general/themes/beta.md",
        "# Beta\n\nThe second concept; it relates to [[alpha]] for context.\n",
    )
    # A structural file that must NOT become a note.
    _write(src, "log.md", "# Log\n\n2026-06-17 import.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    # The hard acceptance bar: the imported repo lints CLEAN (zero findings).
    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]
    assert len(report.lint.findings) == 0
    # log.md was excluded; the four real notes were imported.
    assert report.summary["excluded_structural_files"] == 1
    assert {n.rel_path for n in report.notes} == {
        "index.md",
        "wiki/general/general-moc.md",
        "wiki/general/themes/alpha.md",
        "wiki/general/themes/beta.md",
    }
    # Both themes are grounded in synth raw/ snapshots; the MOC children == both themes.
    assert report.summary["synth_raw_sources"] == 2
    moc_fm = _parses(dest, "wiki/general/general-moc.md")
    assert moc_fm["children"] == ["[[alpha]]", "[[beta]]"]
    index_fm = _parses(dest, "index.md")
    assert index_fm["children"] == ["[[general-moc]]"]


def test_no_source_note_is_overwritten_by_a_colliding_destination(tmp_path: Path) -> None:
    """Every source note lands on its OWN destination — no silent overwrite (no-loss, ADR-0014 D5).

    Two ways two notes used to collapse into one file while the run still reported ``lint: clean``:

    * a stem the slugger emptied out took the literal name ``note``, so every such note in a vault
      overwrote the previous one. The curator path had already fixed exactly this with the
      deterministic ``note-<sha8>`` name (#57); the importer had not. Korean stems used to be the
      common shape here; since ADR-0041 D4.4 they slugify in their own script (asserted below) and
      the floor is reached only by a stem with no admissible character at all;
    * two distinct stems that slugify alike (``projects/Setup.md`` / ``archive/setup.md``) both
      inferred ``wiki/<domain>/themes/setup.md``.

    In both cases the losers no longer existed, so no duplicate basename remained for L1-1 to
    report — the import announced a clean lint over notes it had just destroyed. Measured on the
    pre-fix code: five sources produced two files.
    """
    src, dest = tmp_path / "vault", tmp_path / "kb"
    _write(src, "큐레이터 설계.md", "# 큐레이터 설계\n\n한국어 노트 하나.\n")
    _write(src, "하베스터 안전.md", "# 하베스터 안전\n\n두 번째 한국어 노트.\n")
    _write(src, "골드 팩.md", "# 골드 팩\n\n세 번째 한국어 노트.\n")
    _write(src, "projects/Setup.md", "# Setup A\n\nfirst setup note.\n")
    _write(src, "archive/setup.md", "# Setup B\n\nsecond setup note.\n")
    _write(src, "+++.md", "# +++\n\nsymbol-only stem, nothing safe survives.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    themes = sorted(p.name for p in (dest / "wiki" / "general" / "themes").glob("*.md"))
    assert len(themes) == 6, themes
    assert len({n.rel_path for n in report.notes if "/themes/" in n.rel_path}) == 6
    # The Korean stems keep their meaning in the filename (ADR-0041 D4.4) — the import lane and
    # the curator lane run the same slugger, so they agree by construction.
    assert {"큐레이터-설계.md", "하베스터-안전.md", "골드-팩.md"} <= set(themes)
    # The one stem nothing survives from takes the #57 content-keyed name, never the literal
    # "note" — the floor is intact, it is just no longer where non-Latin knowledge lands.
    assert "note.md" not in themes
    assert sum(name.startswith("note-") for name in themes) == 1
    # The slug collision is disambiguated rather than overwritten, and says so out loud.
    assert "setup.md" in themes
    assert sum(name.startswith("setup-") for name in themes) == 1
    renamed = [n for n in report.notes if any("already claimed" in w for w in n.warnings)]
    assert len(renamed) == 1
    # Distinct content keeps distinct bodies — nothing was clobbered on the way in.
    bodies = {
        (dest / "wiki" / "general" / "themes" / name).read_text(encoding="utf-8") for name in themes
    }
    assert len(bodies) == 6
    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]


# --- the slugger mirror (ADR-0041 D4.4) ---------------------------------------------------------


# Inputs chosen to separate the two lanes wherever they COULD disagree: the byte cap (the import
# lane had none), the re-verify (the import lane had none), the reserved prefix, the Windows device
# stems, traversal, and the scripts the swap exists to admit.
_MIRROR_CORPUS = [
    "Hello, World!",
    "  --Foo__Bar--  ",
    "v1.2 release",
    "한국어 지식",
    "에이전트 Memory 설계",
    "日本語",
    "Привет Мир",
    "가" * 100,
    "a" * 100,
    "_blob",
    "_blob notes",
    "___",
    "CON",
    "com1.md",
    "../etc/passwd",
    ".hidden",
    "a/b",
    "???",
    "",
]


@pytest.mark.parametrize("raw", _MIRROR_CORPUS, ids=lambda v: repr(v)[:32])
def test_import_slugger_mirrors_the_curator_slugger(raw: str) -> None:
    """The two lanes must name the same note identically (ADR-0041 D4.4's "producers mirror it").

    ADR-0022 §A's no-loss floor turns on the import lane and the curator lane agreeing, and before
    this swap they did NOT: the import ``_slugify`` had no length cap and never re-verified its own
    output, so a long or reserved stem produced a different basename in each lane. Asserting the
    equality directly is what stops that divergence from re-opening — a copied implementation with
    no test between them is exactly how it opened the first time.
    """
    from agora_kb.adapters.ollama_brain import _slugify as curator_slugify
    from agora_kb.ingest.vault_import import _slugify as import_slugify

    assert import_slugify(raw) == curator_slugify(raw)


def test_import_slugger_caps_at_60_utf8_bytes() -> None:
    """The cap the import lane never had: a long stem must stay inside a filesystem component."""
    from agora_kb.ingest.vault_import import _slugify as import_slugify

    assert import_slugify("a" * 300) == "a" * 60
    assert len(import_slugify("가" * 300).encode("utf-8")) <= 60


# --- the ADR-0041 D6 destination boundary --------------------------------------------------------


def test_import_into_a_schema2_repo_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    """The importer is a DIRECT ``wiki/`` writer, so it inherits none of D6's write refusal.

    It still emits the schema-1 layout (``wiki/<domain>/themes/<slug>.md``); the kind-first layout
    arrives with the ``agora import --from-kb`` converter. Until then, importing into a repo that
    declares schema 2 would commit v1 paths beside ``wiki/concepts/`` — two layouts in one repo,
    verbatim what D6's refusal exists to prevent — and the importer would never SEE it, because it
    lints its own output with its own schema-1 taxonomy.
    """
    from agora_kb.core.layout import RepoLayout
    from agora_kb.core.repo import Repo
    from agora_kb.schema.emit import Taxonomy, emit_schema

    src = tmp_path / "vault"
    dest = tmp_path / "kb2"
    _write(src, "wiki/general/themes/alpha.md", "# Alpha\n\nProse.\n")

    layout = RepoLayout(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (layout.wiki_dir / "concepts").mkdir(parents=True, exist_ok=True)
    emit_schema(
        layout,
        taxonomy=Taxonomy(
            schema_version=2, taxonomy_policy="open", allowed_tags=(), domains=("general",)
        ),
        schema_version=2,
    )
    Repo(layout)  # not initialized; the guard runs before any git work

    with pytest.raises(ValueError, match="schema-2 KB"):
        import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert not (dest / "wiki" / "general").exists(), "refused BEFORE any note is written"


def test_import_into_a_schema1_repo_is_allowed(tmp_path: Path) -> None:
    """The guard is about a MISMATCH, not about re-importing: schema 1 is what this writer emits."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "wiki/general/themes/alpha.md", "# Alpha\n\nProse.\n")
    import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)
    # A second import into the repo the first one produced still works.
    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)
    assert report.summary["notes"] >= 1


def test_a_fresh_import_declares_the_schema_the_importer_writes(tmp_path: Path) -> None:
    """The declared version and the written tree are ONE fact — never a half-migration."""
    from agora_kb.config import read_canonical_kb_schema_version
    from agora_kb.core.layout import RepoLayout
    from agora_kb.ingest.vault_import import IMPORTER_SCHEMA_VERSION

    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "wiki/general/themes/alpha.md", "# Alpha\n\nProse.\n")
    import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert read_canonical_kb_schema_version(RepoLayout(dest)) == IMPORTER_SCHEMA_VERSION
    assert (dest / "wiki" / "general" / "themes" / "alpha.md").is_file()
