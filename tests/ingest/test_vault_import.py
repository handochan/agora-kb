"""Tests for the Obsidian/markdown vault NORMALIZER (``agora import`` / ADR-0014 D5).

These build SYNTHETIC fixture vaults in ``tmp_path`` (the real ``~/knowledge`` is NEVER touched) and
assert both the :class:`~agora_kb.ingest.vault_import.ImportReport` contents AND that the normalized
notes PARSE under :func:`agora_kb.core.frontmatter.parse` — proving the tolerant-consumer boundary
(ADR-0014 D4) turns real-world Obsidian input into closer-to-ADR-0010-conformant output without
crashing or dropping content.
"""

from __future__ import annotations

from pathlib import Path

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


# --- report-only: theme with no sources (L1-7) -------------------------------------------------


def test_theme_without_sources_is_reported_not_autofixed(tmp_path: Path) -> None:
    """A theme with no raw/ sources is REPORTED (needs sources or status: stub), not auto-fixed."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "wiki/general/themes/sourceless.md", "# Sourceless\n\nNo sources cited.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/general/themes/sourceless.md")
    assert any("needs sources" in w for w in rec.warnings)
    assert report.summary["themes_without_sources"] >= 1


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
