"""Tests for the Obsidian/markdown vault NORMALIZER (``agora import`` / ADR-0014 D5).

These build SYNTHETIC fixture vaults in ``tmp_path`` (the real ``~/knowledge`` is NEVER touched) and
assert both the :class:`~agora_kb.ingest.vault_import.ImportReport` contents AND that the normalized
notes PARSE under :func:`agora_kb.core.frontmatter.parse` — proving the tolerant-consumer boundary
(ADR-0014 D4) turns real-world Obsidian input into closer-to-ADR-0010-conformant output without
crashing or dropping content.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agora_kb.config import load_kb_identity
from agora_kb.core import frontmatter
from agora_kb.core.layout import RepoLayout
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


def _kb_id(dest: Path) -> str:
    """The ``_meta/kb.yaml`` ULID the import minted for ``dest`` (ADR-0041 D1.5)."""
    identity = load_kb_identity(RepoLayout(dest))
    assert identity is not None, "a schema-2 import must mint _meta/kb.yaml"
    return identity.kb_id


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

    rec = _record(report, "wiki/concepts/topic.md")
    assert rec.repaired_frontmatter is True
    # The normalized note PARSES (the whole point of the repair) and `related` survived as a list.
    fm = _parses(dest, "wiki/concepts/topic.md")
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

    fm = _parses(dest, "wiki/concepts/topic.md")
    assert fm["related"] == ["[[only]]"]  # resolvable -> kept, NOT dropped to []
    rec = _record(report, "wiki/concepts/topic.md")
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

    fm = _parses(dest, "wiki/maps/general.md")
    assert fm["children"] == []  # unresolved -> dropped from the array...
    rec = _record(report, "wiki/maps/general.md")
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
    rec = _record(report, "wiki/concepts/bad.md")
    assert any("not valid UTF-8" in w for w in rec.warnings)
    # The note still parses at the destination (content preserved, lossy bytes replaced).
    _parses(dest, "wiki/concepts/bad.md")


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

    rec = _record(report, "wiki/concepts/broken.md")
    assert any("could not be parsed" in w for w in rec.warnings)
    # Body content is preserved and the note parses (title inferred from the H1).
    fm = _parses(dest, "wiki/concepts/broken.md")
    assert fm["title"] == "Broken"
    assert "Still has a body." in (dest / "wiki/concepts/broken.md").read_text()


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

    rec = _record(report, "wiki/concepts/bare.md")
    for key in ("kind", "kb", "subjects", "title", "created", "updated", "status", "summary"):
        assert key in rec.inferred_fields, f"{key} should be inferred"

    fm = _parses(dest, "wiki/concepts/bare.md")
    # auto-fix #2: the KIND is materialized from the inferred layout and MIRRORED by the directory
    # (ADR-0041 D2.1); `type:` is the derived OKF mirror of it (OD-3), never the kind authority.
    assert fm["kind"] == "concept"
    assert fm["type"] == "concept"
    assert fm["title"] == "Bare Note Heading"  # from the H1
    assert fm["created"] == _IMPORT_DATE
    assert fm["updated"] == _IMPORT_DATE
    assert fm["status"] == "active"
    assert fm["summary"] == "This is the first paragraph that becomes the summary."
    # The rest of the D2 common base, which schema 1 had no place for at all.
    assert fm["kb"] == _kb_id(dest)  # the minted _meta/kb.yaml ULID, mirrored into every note
    assert fm["subjects"] == ["general"]  # the origin folder, a declared domain (D2.2 / D6 step 2)
    assert fm["aliases"] == []
    assert fm["derived"] is False  # D2.4 reserves `true` for the proposal plane
    assert fm["provenance"] == {"writers": [], "agents": []}  # D2.3: an import authenticates nobody
    assert rec.subjects == ("general",)


def test_inferred_type_resolves_l1_11_for_imported_theme(tmp_path: Path) -> None:
    """The materialized ``kind`` means an imported concept never fails L1-11 (only L1-7 remains)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    # A theme already in the right layout, missing only sources — the import should leave NO L1-11
    # finding (type is supplied), only the report-only L1-7 (empty sources).
    _write(src, "wiki/general/themes/topic.md", "# Topic\n\nA paragraph.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    codes = {f.code for f in report.lint.findings}
    assert "L1-11" not in codes  # the kind was materialized and matches its directory (D2.1)
    assert codes <= {"L1-7"}  # only the report-only empty-sources finding may remain
    assert _parses(dest, "wiki/concepts/topic.md")["kind"] == "concept"


def test_title_falls_back_to_filename_title_case_without_h1(tmp_path: Path) -> None:
    """With no H1 and no frontmatter title, the filename kebab is Title-Cased (ADR-0010 §2.1)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "wiki/general/themes/my-cool-note.md", "Just a body paragraph, no heading.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    fm = _parses(dest, "wiki/concepts/my-cool-note.md")
    assert fm["title"] == "My Cool Note"
    assert _record(report, "wiki/concepts/my-cool-note.md").type_inferred == "concept"


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

    fm = _parses(dest, "wiki/concepts/kept.md")
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

    theme_fm = _parses(dest, "wiki/concepts/topic.md")
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

    rec = _record(report, "wiki/concepts/topic.md")
    assert rec.converted_links == 1
    assert "does-not-exist" in rec.unresolved_links

    body = (dest / "wiki/concepts/topic.md").read_text(encoding="utf-8")
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
    assert "[deep-theme](wiki/concepts/deep-theme.md)" in body


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

    rec = _record(report, "wiki/concepts/tagged.md")
    assert "random-unknown" in rec.stripped_tags
    fm = _parses(dest, "wiki/concepts/tagged.md")
    assert set(fm["tags"]) == {"architecture", "concurrency"}
    assert report.summary["stripped_tags"] >= 1


# --- auto-fix #2: off-layout note moved + warned -----------------------------------------------


def test_off_layout_note_becomes_a_subjectless_concept_and_is_warned(tmp_path: Path) -> None:
    """A note outside any known layout becomes ``wiki/concepts/<slug>.md`` with NO subject.

    The schema-2 form of the ADR-0022 no-loss floor, and the leg that RETIRES (D2.2 leg 1): in v1
    the note had to be filed under ``wiki/<first-domain>/themes/`` because a note needed a path and
    a path needed a domain, so an unclassifiable note was ASSERTED to belong to ``domains[0]``. A
    concept now lands at ``wiki/concepts/<slug>.md`` whatever its subject, so the honest
    ``subjects: []`` replaces the possibly-false assertion — and nothing is dropped, which is what
    ADR-0022 actually guaranteed.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "notes/Loose Idea.md", "# Loose Idea\n\nA stray note at the vault root.\n")

    report = import_vault(src, dest, domains=["ai-tech", "general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/concepts/loose-idea.md")
    assert rec.type_inferred == "concept"
    assert any("moved to fit" in w for w in rec.warnings)
    assert "notes/Loose Idea.md" in " ".join(rec.warnings)
    assert report.summary["moved"] >= 1
    # `notes/` is not a declared domain, so NO subject is asserted — and none is invented.
    assert rec.subjects == ()
    assert _parses(dest, "wiki/concepts/loose-idea.md")["subjects"] == []
    # ...but the raw/ SHARD KEY still falls back to domains[0] (D2.2 leg 3: raw/ never moves, so a
    # snapshot still needs a directory). That is the one place the ADR-0022 catch-all survives.
    assert rec.synth_raw_source == "raw/ai-tech/loose-idea.md"
    assert (dest / "raw/ai-tech/loose-idea.md").is_file()


def test_declared_origin_folder_becomes_the_subject(tmp_path: Path) -> None:
    """An off-layout note whose origin FOLDER is a declared domain keeps it as its subject (D2.2).

    The other half of the rule above, and the reason it is not simply "off-layout means no subject":
    the folder a human filed a note in is a real assertion when the taxonomy declares it, and D2.2
    leg 1 says to record it. This is the same rule ``agora import --from-kb`` applies to the v1 path
    domain, so the two importers agree by construction.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "general/Filed Note.md", "# Filed Note\n\nFiled under a declared domain.\n")

    report = import_vault(src, dest, domains=["ai-tech", "general"], import_date=_IMPORT_DATE)

    rec = _record(report, "wiki/concepts/filed-note.md")
    assert rec.subjects == ("general",)
    assert _parses(dest, "wiki/concepts/filed-note.md")["subjects"] == ["general"]
    assert rec.synth_raw_source == "raw/general/filed-note.md"  # the subject IS the shard key


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

    rec = _record(report, "wiki/concepts/sourceless.md")
    # The synth raw/ snapshot is the theme's basename under its domain (a POSIX raw/ path).
    assert rec.synth_raw_source == "raw/general/sourceless.md"
    assert rec.stubbed_empty_theme is False
    # Frontmatter cites EXACTLY the synth source; no foreign / empty sources remain.
    fm = _parses(dest, "wiki/concepts/sourceless.md")
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

    rec = _record(report, "wiki/concepts/empty.md")
    assert rec.stubbed_empty_theme is True
    assert rec.synth_raw_source is None
    assert any("imported empty concept -> stub" in w for w in rec.warnings)
    fm = _parses(dest, "wiki/concepts/empty.md")
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
    """import_vault is a pure function of its inputs: two runs produce byte-identical notes.

    ``kb_id`` is passed explicitly because it is the ONE input the importer would otherwise mint
    itself, and a ULID is time-seeded (ADR-0041 D1.5: minted once at creation, never rewritten).
    That it is an INPUT rather than a hidden read of the clock is what keeps the rest of the run
    deterministic — asserted below by the second half of this test.
    """
    src = tmp_path / "vault"
    dest1 = tmp_path / "out1"
    dest2 = tmp_path / "out2"
    _write(src, "wiki/general/themes/topic.md", "# Topic\n\nA paragraph.\n\nSee [[other]].\n")
    _write(src, "wiki/general/themes/other.md", "# Other\n\nAnother.\n")

    pinned = "01J8ZQ3M4N5P6Q7R8S9T0V1W2X"
    import_vault(src, dest1, domains=["general"], import_date=_IMPORT_DATE, kb_id=pinned)
    import_vault(src, dest2, domains=["general"], import_date=_IMPORT_DATE, kb_id=pinned)

    a = (dest1 / "wiki/concepts/topic.md").read_bytes()
    b = (dest2 / "wiki/concepts/topic.md").read_bytes()
    assert a == b
    assert _kb_id(dest1) == _kb_id(dest2) == pinned

    # And with no id supplied, each destination is its OWN knowledge base — a minted id is never
    # shared between two repos, which is exactly what makes `kb:` an origin marker (D1.5).
    dest3, dest4 = tmp_path / "out3", tmp_path / "out4"
    import_vault(src, dest3, domains=["general"], import_date=_IMPORT_DATE)
    import_vault(src, dest4, domains=["general"], import_date=_IMPORT_DATE)
    assert _kb_id(dest3) != _kb_id(dest4)


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
    assert imported == {"wiki/concepts/real.md"}
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

    assert {n.rel_path for n in report.notes} == {"wiki/concepts/keep.md"}
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

    fm = _parses(dest, "wiki/maps/general.md")
    assert fm["children"] == ["[[present-theme]]"]  # EXACTLY the resolvable child-bullet set
    rec = _record(report, "wiki/maps/general.md")
    assert "ghost-theme" in rec.unresolved_links  # reported, never silently lost (D4)
    body = (dest / "wiki/maps/general.md").read_text(encoding="utf-8")
    # The resolvable bullet is now a markdown-link child bullet; the unresolvable target is NOT a
    # children entry and was never a graph edge (L1-2 / L1-6 both clean).
    assert "[present-theme](../concepts/present-theme.md)" in body
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

    assert _parses(dest, "wiki/maps/general.md")["children"] == ["[[alpha]]"]
    rec = _record(report, "wiki/maps/general.md")
    assert "ghost" in rec.unresolved_links
    _, body = frontmatter.parse((dest / "wiki/maps/general.md").read_text(encoding="utf-8"))
    # The whole unresolvable child-bullet LINE is gone from the BODY; the resolvable one survives —
    # RE-TARGETED at where alpha actually landed, since schema 2 files the map and the concept in
    # different kind directories and the source-relative `themes/alpha.md` resolves to nothing.
    assert "themes/ghost.md" not in body
    assert "[Alpha](../concepts/alpha.md)" in body
    assert _record(report, "wiki/maps/general.md").retargeted_links == 1
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

    rec = _record(report, "wiki/concepts/coded.md")
    assert "~/dev/analytics/psa @ 705f4a4 (2026-06-12)" in rec.stripped_sources
    assert any("stripped non-raw source" in w for w in rec.warnings)
    assert report.summary["stripped_sources"] >= 1
    fm = _parses(dest, "wiki/concepts/coded.md")
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

    rec = _record(report, "wiki/concepts/has-raw.md")
    assert rec.synth_raw_source is None  # the pre-existing artifact wins; no synth
    fm = _parses(dest, "wiki/concepts/has-raw.md")
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
    rec = _record(report, "wiki/concepts/evil.md")
    assert entry in rec.stripped_sources
    assert any("escapes raw/" in w for w in rec.warnings)

    # (3) the note itself still imports normally: the synth raw/ snapshot is its sole source.
    fm = _parses(dest, "wiki/concepts/evil.md")
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

    rec = _record(report, "wiki/concepts/odd.md")
    for entry in _NON_RAW_SOURCES:
        assert entry in rec.stripped_sources
    assert any("not under raw/" in w for w in rec.warnings)
    assert _parses(dest, "wiki/concepts/odd.md")["sources"] == ["raw/general/odd.md"]
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

    rec = _record(report, "wiki/concepts/dotted.md")
    assert rec.stripped_sources == ()
    assert rec.synth_raw_source is None  # the pre-existing artifact wins; no snapshot
    assert _parses(dest, "wiki/concepts/dotted.md")["sources"] == ["raw/general/kept-source.md"]
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

    cited = _record(report, "wiki/concepts/has-raw.md")
    assert cited.stripped_sources == ()
    assert not any("escapes raw/" in w for w in cited.warnings)
    assert _parses(dest, "wiki/concepts/has-raw.md")["sources"] == ["raw/general/kept-source.md"]
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
    rec = _record(report, "wiki/concepts/leak.md")
    assert any("escapes the source vault raw/" in w for w in rec.warnings)
    # The theme falls back to its own body snapshot — a normal import result, not a failure.
    fm = _parses(dest, "wiki/concepts/leak.md")
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
        "wiki/maps/general.md",
        "wiki/concepts/alpha.md",
        "wiki/concepts/beta.md",
    }
    # Both concepts are grounded in synth raw/ snapshots; the map's children == both concepts.
    assert report.summary["synth_raw_sources"] == 2
    moc_fm = _parses(dest, "wiki/maps/general.md")
    assert moc_fm["children"] == ["[[alpha]]", "[[beta]]"]
    # The vault's own MOC IS the domain's map: the importer completed nothing, overruled nothing.
    assert report.summary["synthesized_maps"] == 0
    assert report.summary["synthesized_index"] == 0
    # ADR-0041 D6 rule 3: the map's basename lost its `-moc` kind marker, so the index's
    # `[[general-moc]]` — the only spelling a schema-1 vault knows — is REWRITTEN, not reported
    # unresolved over a note that is plainly present.
    index_fm = _parses(dest, "index.md")
    assert index_fm["children"] == ["[[general]]"]
    assert not any(n.unresolved_links for n in report.notes)
    # Every note carries the same minted KB identity (D1.5).
    assert {_parses(dest, n.rel_path)["kb"] for n in report.notes} == {_kb_id(dest)}


def test_no_source_note_is_overwritten_by_a_colliding_destination(tmp_path: Path) -> None:
    """Every source note lands on its OWN destination — no silent overwrite (no-loss, ADR-0014 D5).

    Two ways two notes used to collapse into one file while the run still reported ``lint: clean``:

    * a stem the slugger emptied out took the literal name ``note``, so every such note in a vault
      overwrote the previous one. The curator path had already fixed exactly this with the
      deterministic ``note-<sha8>`` name (#57); the importer had not. Korean stems used to be the
      common shape here; since ADR-0041 D4.4 they slugify in their own script (asserted below) and
      the floor is reached only by a stem with no admissible character at all;
    * two distinct stems that slugify alike (``projects/Setup.md`` / ``archive/setup.md``) both
      inferred the same concept destination.

    Under schema 2 the claim is keyed on the destination BASENAME rather than its path, because the
    kind is now the directory: two notes can land on different paths (``wiki/concepts/x.md`` and
    ``wiki/maps/x.md``) while sharing the one identity every ``[[basename]]`` resolves on, and a
    path-keyed check would pass that pair through to an L1-1 failure after a completed import.

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

    themes = sorted(p.name for p in (dest / "wiki" / "concepts").glob("*.md"))
    assert len(themes) == 6, themes
    assert len({n.rel_path for n in report.notes if n.rel_path.startswith("wiki/concepts/")}) == 6
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
    bodies = {(dest / "wiki" / "concepts" / name).read_text(encoding="utf-8") for name in themes}
    assert len(bodies) == 6
    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]


# --- change E: the schema-2 NAVIGATION tier (ADR-0041 D1.2/D1.3/D5) -----------------------------


def test_a_map_is_synthesized_per_subject_and_the_index_is_the_root_map(tmp_path: Path) -> None:
    """A vault with no MOC still gets a map per subject, and a root map over the maps.

    Not cosmetic and not a courtesy. ADR-0041 D5 seeds the ranker's WHOLE structural term from
    ``wiki/maps/**`` (``core.wiki._is_map_path``), so a repo imported without maps would rank on
    lexical evidence alone; and every concept in it would be an orphan for the curator's own
    ``curator.lint.max_orphans`` gate (ADR-0022 §E). The importer therefore completes the tier the
    vault never had, rather than emitting a structurally inert repo that lints clean.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "wiki/finance/themes/ledger.md", "# Ledger\n\nA finance concept.\n")
    _write(src, "wiki/cooking/themes/braise.md", "# Braise\n\nA cooking concept.\n")
    _write(src, "loose.md", "# Loose\n\nNo declared domain in its path.\n")

    report = import_vault(src, dest, domains=["cooking", "finance"], import_date=_IMPORT_DATE)

    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]
    assert report.summary["synthesized_maps"] == 2
    assert report.summary["synthesized_index"] == 1
    # One map per subject that HAS concepts, children == exactly those concepts (L1-6 by
    # construction: the frontmatter array and the body bullets come from one list).
    finance = _parses(dest, "wiki/maps/finance.md")
    assert finance["kind"] == "map"
    assert finance["subjects"] == ["finance"]
    assert finance["children"] == ["[[ledger]]"]
    assert "- [Ledger](../concepts/ledger.md)" in (dest / "wiki/maps/finance.md").read_text("utf-8")
    assert _parses(dest, "wiki/maps/cooking.md")["children"] == ["[[braise]]"]
    # The root map: kind index, at the repo ROOT (D1.2), children == every map, subjects [].
    index = _parses(dest, "index.md")
    assert index["kind"] == "index"
    assert index["children"] == ["[[cooking]]", "[[finance]]"]
    assert index["subjects"] == []
    assert index["okf_version"] == "0.1"  # the OKF bundle root, still the index alone
    # The subjectless note is filed and searchable but joins no map — nothing invents a subject
    # for it just to give it a parent.
    assert (dest / "wiki/concepts/loose.md").is_file()
    assert "[[loose]]" not in finance["children"]
    # Synthesized navigation is NOT counted as an imported note: `notes` stays "what the vault had".
    assert {n.rel_path for n in report.notes} == {
        "wiki/concepts/ledger.md",
        "wiki/concepts/braise.md",
        "wiki/concepts/loose.md",
    }


def test_an_imported_index_is_extended_never_overruled(tmp_path: Path) -> None:
    """A vault's own ``index.md`` keeps its prose and bullets; missing maps are APPENDED."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "index.md", "---\ntitle: My Vault\n---\n\n# My Vault\n\nHand-written preamble.\n")
    _write(src, "wiki/finance/themes/ledger.md", "# Ledger\n\nA finance concept.\n")

    report = import_vault(src, dest, domains=["finance"], import_date=_IMPORT_DATE)

    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]
    assert report.summary["synthesized_index"] == 0  # the vault's own index was kept
    body = (dest / "index.md").read_text(encoding="utf-8")
    assert "Hand-written preamble." in body  # the operator's prose survives verbatim
    assert "- [finance map](wiki/maps/finance.md)" in body
    index = _parses(dest, "index.md")
    assert index["title"] == "My Vault"
    assert index["children"] == ["[[finance]]"]


def test_map_synthesis_is_skipped_when_an_imported_note_owns_the_basename(tmp_path: Path) -> None:
    """A domain whose map basename is already an imported note's is SKIPPED with a warning.

    Never renamed: a renamed map is a map nothing links to, which is exactly the reason ADR-0041 D6
    rule 7 refuses silent renames for the converter. The concept keeps the basename it earned and
    the repo still lints clean; the operator is told which subject ended up without a map.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "wiki/finance/themes/finance.md", "# Finance\n\nA concept named for its domain.\n")

    report = import_vault(src, dest, domains=["finance"], import_date=_IMPORT_DATE)

    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]
    assert report.summary["synthesized_maps"] == 0
    assert not (dest / "wiki/maps/finance.md").exists()
    assert (dest / "wiki/concepts/finance.md").is_file()  # the concept keeps the basename
    rec = _record(report, "wiki/concepts/finance.md")
    assert any("no wiki/maps/finance.md was synthesized" in w for w in rec.warnings)


def test_a_vault_daily_becomes_a_concept_never_a_fabricated_journal(tmp_path: Path) -> None:
    """A ``wiki/<d>/daily/`` vault note becomes a CONCEPT, not a schema-2 journal.

    ADR-0041 D2.6 makes a journal a CURATOR RUN artifact: one per ``run_date``, basenamed that date,
    sharded by it, and carrying that run's ``run_id`` as a back-link (lint L1-14 asserts the whole
    identity). A vault note has no run behind it, so converting one would require MINTING a
    ``run_id`` naming a run that never happened — a fabricated entry in the permanent record. A real
    v1 journal, which HAS its run, crosses through ``agora import --from-kb`` instead.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(
        src,
        "wiki/finance/daily/finance-2026-01-12.md",
        "---\ntitle: finance daily\ntype: daily\ndate: 2026-01-12\n---\n\n"
        "# finance daily\n\nWork.\n",
    )

    report = import_vault(src, dest, domains=["finance"], import_date=_IMPORT_DATE)

    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]
    rec = _record(report, "wiki/concepts/finance-2026-01-12.md")
    assert rec.type_inferred == "concept"
    assert rec.subjects == ("finance",)
    fm = _parses(dest, "wiki/concepts/finance-2026-01-12.md")
    assert fm["kind"] == "concept"
    assert "run_id" not in fm  # nothing was fabricated
    # wiki/notes/ stays EMPTY: the journal tier belongs to the curator, not to an import.
    assert list((dest / "wiki" / "notes").rglob("*.md")) == []


def test_the_imported_repo_accepts_a_write(tmp_path: Path) -> None:
    """The repo ``agora import`` mints is CURATE-ABLE — the whole reason the flip landed here.

    This is the defect the schema bump closes, asserted end to end rather than by reading the
    declared version. While the importer emitted schema 1, ADR-0041 D6 made every write path refuse
    the repo it had just produced: ``Inbox.write`` — the one call that covers ``kb_remember``, the
    web upload and every future writer — raised ``ReadOnlySchemaVersionError`` the moment the import
    finished, over a run that had printed ``lint: clean``. A repo whose inbox can never drain is
    silent data loss dressed as success.
    """
    from agora_kb.core.inbox import Inbox

    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "wiki/general/themes/topic.md", "# Topic\n\nReal prose.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)
    assert report.lint.ok is True

    item = Inbox(RepoLayout(dest)).write(
        text="A fact captured right after the import.",
        writer="test-agent",
        source="agent:test",
    )
    assert item is not None
    assert Inbox(RepoLayout(dest)).depth() == 1


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


def test_import_into_a_schema1_repo_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    """The importer is a DIRECT ``wiki/`` writer, so it inherits none of D6's write refusal.

    It emits the schema-2 kind-first layout, so importing into a repo that declares schema **1**
    would commit ``wiki/concepts/…`` beside ``wiki/<domain>/themes/…`` — two layouts in one repo,
    verbatim what D6's refusal exists to prevent — and the importer would never SEE it, because it
    lints its own output with its own schema-2 taxonomy. The guard is the mirror image of the one
    that stood here while the importer wrote schema 1; the rule ("never mix layouts") is unchanged.
    """
    from agora_kb.core.repo import Repo
    from agora_kb.schema.emit import Taxonomy, emit_schema

    src = tmp_path / "vault"
    dest = tmp_path / "kb1"
    _write(src, "wiki/general/themes/alpha.md", "# Alpha\n\nProse.\n")

    layout = RepoLayout(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (layout.wiki_dir / "general" / "themes").mkdir(parents=True, exist_ok=True)
    emit_schema(
        layout,
        taxonomy=Taxonomy(
            schema_version=1, taxonomy_policy="open", allowed_tags=(), domains=("general",)
        ),
        schema_version=1,
    )
    Repo(layout)  # not initialized; the guard runs before any git work

    with pytest.raises(ValueError, match="schema-1 KB"):
        import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert not (dest / "wiki" / "concepts").exists(), "refused BEFORE any note is written"
    # And the refusal NAMES the one crossing D6 authorises, so the operator is not left guessing.
    with pytest.raises(ValueError, match="--from-kb"):
        import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)


def test_import_into_an_existing_schema2_repo_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """A populated schema-2 destination is refused, and refused BEFORE anything is written.

    The importer is not an incremental writer: it mints ``_meta/kb.yaml`` and composes the root map
    from this vault's maps alone. Run twice against the same destination it re-stamped the KB with
    a NEW ``kb_id`` (ADR-0041 D1.5 mints one ONCE), dropped ``declared_kind``, and emptied the root
    map's ``children:`` — and printed ``lint: clean`` over it. Pinned as a byte-comparison so the
    guard cannot regress into "refuses, but only after the damage".
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "wiki/general/themes/alpha.md", "# Alpha\n\nProse.\n")
    import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    before = {
        path.relative_to(dest).as_posix(): path.read_bytes()
        for path in sorted(dest.rglob("*"))
        if path.is_file() and ".git/" not in path.relative_to(dest).as_posix()
    }
    with pytest.raises(ValueError, match="already a schema-2 KB"):
        import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    after = {
        path.relative_to(dest).as_posix(): path.read_bytes()
        for path in sorted(dest.rglob("*"))
        if path.is_file() and ".git/" not in path.relative_to(dest).as_posix()
    }
    assert after == before, "the refused import must leave the destination byte-identical"


def test_import_into_a_repo_that_only_declares_an_identity_is_refused(tmp_path: Path) -> None:
    """A half-initialized destination (``_meta/kb.yaml``, no taxonomy) is refused on the identity.

    The schema check cannot see this one — nothing declares a version — but minting over it would
    still rewrite a ``kb_id`` ADR-0041 D1.5 says is stamped once and never rewritten.
    """
    from agora_kb.config import KbIdentity, write_kb_identity

    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "wiki/general/themes/alpha.md", "# Alpha\n\nProse.\n")
    identity = KbIdentity(kb_id="01J8ZQ3M4N5P6Q7R8S9T0V1W2X", name="existing")
    write_kb_identity(RepoLayout(dest), identity)

    with pytest.raises(ValueError, match="already declares a KB identity"):
        import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)
    assert _kb_id(dest) == "01J8ZQ3M4N5P6Q7R8S9T0V1W2X"


def test_a_fresh_import_declares_the_schema_the_importer_writes(tmp_path: Path) -> None:
    """The declared version and the written tree are ONE fact — never a half-migration."""
    from agora_kb.config import MAX_SUPPORTED_KB_SCHEMA_VERSION, read_canonical_kb_schema_version
    from agora_kb.ingest.vault_import import IMPORTER_SCHEMA_VERSION

    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "wiki/general/themes/alpha.md", "# Alpha\n\nProse.\n")
    import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert read_canonical_kb_schema_version(RepoLayout(dest)) == IMPORTER_SCHEMA_VERSION
    assert IMPORTER_SCHEMA_VERSION == MAX_SUPPORTED_KB_SCHEMA_VERSION, (
        "the importer must emit the schema this build WRITES; emitting an older one mints a repo "
        "that is read-only the moment it is created (ADR-0041 D6)"
    )
    # The declared version and the written TREE are one fact: the kind-first layout, on disk.
    assert (dest / "wiki" / "concepts" / "alpha.md").is_file()
    assert not (dest / "wiki" / "general").exists()
    # ...and the identity every schema-2 note mirrors into `kb:` exists (D1.5).
    assert _kb_id(dest) == _parses(dest, "wiki/concepts/alpha.md")["kb"]


def test_an_imported_index_keeps_its_concept_children_when_a_map_is_appended(
    tmp_path: Path,
) -> None:
    """Change E EXTENDS the root map's ``children:``; it never replaces them (L1-6).

    ``index.md`` is the root of the map tier, and D1.3 admits a CONCEPT as a child of it. Change C
    syncs ``children:`` to the body's own bullets; change E then appends a bullet for each
    synthesized map. Overwriting ``children:`` with the map list alone deleted a child whose bullet
    was still in the body, and the import announced ``lint: clean`` over a repo L1-6 rejects.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(
        src,
        "index.md",
        "---\ntitle: Home\nchildren: ['[[alpha]]']\n---\n\n# Home\n\n"
        "- [Alpha](wiki/general/themes/alpha.md)\n",
    )
    _write(src, "wiki/general/themes/alpha.md", "# Alpha\n\nProse.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]
    assert _parses(dest, "index.md")["children"] == ["[[alpha]]", "[[general]]"]
    _, body = frontmatter.parse((dest / "index.md").read_text(encoding="utf-8"))
    assert "wiki/concepts/alpha.md" in body  # ...and the concept bullet is still there, retargeted
    assert "wiki/maps/general.md" in body


def test_a_non_root_note_basenamed_index_is_renamed_not_left_to_shadow_the_root_map(
    tmp_path: Path,
) -> None:
    """``index`` is reserved for the ROOT map, whether or not the root map is an imported note.

    An Obsidian folder note ``general/index.md`` lands at ``wiki/concepts/index.md``; change E then
    synthesizes the root ``index.md`` the vault has none of. The claim pass keyed only on the
    destination PATH, so it never saw the collision — the same path-key-vs-basename-key blind spot
    it was rewritten to close, one branch further along — and the import emitted an L1-1 duplicate
    basename plus an L1-13 second ``index``.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "general/index.md", "# General\n\nA folder note.\n")
    _write(src, "general/alpha.md", "# Alpha\n\nProse.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]
    assert (dest / "index.md").is_file()  # the SYNTHESIZED root map keeps the reserved basename
    renamed = [n for n in report.notes if n.rel_path.startswith("wiki/concepts/index-")]
    assert len(renamed) == 1, [n.rel_path for n in report.notes]
    assert any("RESERVED for the ROOT map" in w for w in renamed[0].warnings)


def test_an_imported_root_index_keeps_the_reserved_basename(tmp_path: Path) -> None:
    """The reservation never costs the operator their own root map: it wins the claim."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "index.md", "# Home\n\nThe operator's own root map.\n")
    _write(src, "general/index.md", "# General\n\nA folder note.\n")

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert _record(report, "index.md").rel_path == "index.md"
    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]


def test_a_pre_existing_body_markdown_link_is_retargeted_to_where_the_note_landed(
    tmp_path: Path,
) -> None:
    """Schema 2 moves EVERY note, so a link written against the source tree resolves to nothing.

    ``[[wikilink]]`` tokens were already emitted destination-correct; links a vault already carries
    in the emitted markdown form were not, and lint cannot see it — L1-2/L1-6 key on basenames, so
    the import reported ``lint: clean`` and ``unresolved_links: 0`` over dead navigation.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(
        src,
        "wiki/general/general-moc.md",
        "---\ntitle: General MOC\n---\n\n# General MOC\n\n- [Alpha](themes/alpha.md)\n",
    )
    _write(
        src,
        "wiki/general/themes/alpha.md",
        "# Alpha\n\nSee [the map](../general-moc.md) and [nowhere](../themes/gone.md).\n",
    )

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    _, map_body = frontmatter.parse((dest / "wiki/maps/general.md").read_text(encoding="utf-8"))
    assert "[Alpha](../concepts/alpha.md)" in map_body
    _, concept_body = frontmatter.parse(
        (dest / "wiki/concepts/alpha.md").read_text(encoding="utf-8")
    )
    # The `-moc` rename (D6 rule 3) travels with the retarget: the map is `wiki/maps/general.md`.
    assert "[the map](../maps/general.md)" in concept_body
    # A target that resolves NOWHERE is left byte-identical and reported, never guessed at.
    assert "[nowhere](../themes/gone.md)" in concept_body
    assert "gone" in _record(report, "wiki/concepts/alpha.md").unresolved_links
    assert report.summary["retargeted_links"] == 2


def test_a_body_link_to_a_real_non_note_file_is_left_verbatim(tmp_path: Path) -> None:
    """A link to a file that is NOT imported as a note (the schema doc, an excluded structural
    file) keeps its target: re-pointing it at whatever note shares its stem would rewrite the
    operator's own text, and the file it names is not a note edge at all."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    _write(src, "AGENTS.md", "# Schema\n\nStructural; never imported as a note.\n")
    _write(
        src,
        "wiki/general/themes/alpha.md",
        "# Alpha\n\nThe schema lives at [the schema](../../../AGENTS.md).\n",
    )

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    _, body = frontmatter.parse((dest / "wiki/concepts/alpha.md").read_text(encoding="utf-8"))
    assert "[the schema](../../../AGENTS.md)" in body
    assert not _record(report, "wiki/concepts/alpha.md").unresolved_links


def test_a_child_title_holding_a_bracket_still_yields_a_grammar_conforming_bullet(
    tmp_path: Path,
) -> None:
    """A note titled ``Alpha [draft] notes`` must not make the importer emit an L1-6 finding.

    The synthesized navigation bullets interpolate the child's ``title:`` as the link label, and a
    title is operator prose that has been through no slugger. The FROZEN §3.2 child-bullet grammar
    admits ``[^\\]\\r\\n]*`` as link text, so a ``]`` in the title makes the emitted line invisible
    to ``child_bullets()`` — ``children:`` and the body bullet set diverge and the importer reports
    L1-6 against the repo it just minted, contradicting its own "holds BY CONSTRUCTION" contract.
    The label degrades to the child's basename; the bullet, and the invariant, survive.
    """
    src = tmp_path / "vault"
    (src / "general").mkdir(parents=True)
    (src / "general" / "alpha.md").write_text(
        "---\ntitle: 'Alpha [draft] notes'\n---\n\n# Alpha\n\nProse about alpha.\n",
        encoding="utf-8",
    )
    dest = tmp_path / "kb"

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]
    _, body = frontmatter.parse((dest / "wiki/maps/general.md").read_text(encoding="utf-8"))
    assert "- [alpha](../concepts/alpha.md)" in body  # basename label, not the bracketed title
    # The title itself is untouched where it lives — only the LABEL degraded.
    fm, _ = frontmatter.parse((dest / "wiki/concepts/alpha.md").read_text(encoding="utf-8"))
    assert fm["title"] == "Alpha [draft] notes"


def test_a_child_title_that_fakes_a_link_cannot_hijack_the_bullet_target(tmp_path: Path) -> None:
    """A title spelling its own ``](path)`` must not make the bullet point somewhere else.

    This is the sharper half of the same defect: ``title: 'a](evil.md) b'`` renders a line the §3.2
    regex DOES match — it stops at the inner ``]``, reads ``evil.md`` as the target and swallows the
    real link into the optional trailing-prose group. The bullet would then claim a child the map
    never meant, so the grammar check must verify the parsed path resolves back to the intended
    basename, not merely that the line matches.
    """
    src = tmp_path / "vault"
    (src / "general").mkdir(parents=True)
    (src / "general" / "alpha.md").write_text(
        "---\ntitle: 'a](evil.md) b'\n---\n\n# Alpha\n\nProse about alpha.\n", encoding="utf-8"
    )
    dest = tmp_path / "kb"

    report = import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]
    _, body = frontmatter.parse((dest / "wiki/maps/general.md").read_text(encoding="utf-8"))
    assert "- [alpha](../concepts/alpha.md)" in body
    assert "evil.md" not in body


def test_the_imported_repo_git_tracks_every_kind_container(tmp_path: Path) -> None:
    """The six ``wiki/<kind>/`` containers survive a git round-trip, exactly as ``repo init``'s do.

    The directory IS the kind under schema 2, so the containers are the schema's own statement of
    what kinds exist. Git cannot track an empty directory, so a bare ``mkdir`` would leave the
    unpopulated ones out of the import's own commit — a different tree from an init'd repo at the
    same schema, and containers that vanish on ``agora sync`` + clone.
    """
    src = tmp_path / "vault"
    (src / "general").mkdir(parents=True)
    (src / "general" / "alpha.md").write_text("# Alpha\n\nProse.\n", encoding="utf-8")
    dest = tmp_path / "kb"

    import_vault(src, dest, domains=["general"], import_date=_IMPORT_DATE)

    tracked = subprocess.run(
        ["git", "ls-files", "wiki/"], cwd=dest, capture_output=True, text=True, check=True
    ).stdout.split()
    for name in ("concepts", "summaries", "notes", "maps", "entities", "people"):
        assert f"wiki/{name}/.gitkeep" in tracked, f"wiki/{name}/ is not in the import's commit"
