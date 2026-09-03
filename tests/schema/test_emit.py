"""Tests for the schema emitter (ADR-0010 §1, §4, §5; ADR-0011 §4.0 — emit is NOT an INGEST write).

Emit into a tmp ``RepoLayout`` then assert: ``AGENTS.md`` exists, the symlinks resolve to it,
``_meta/taxonomy.yaml`` round-trips, ``_templates/`` is present, and re-emitting is idempotent.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import agora_kb.schema.emit as emit_mod
from agora_kb.core.layout import RepoLayout
from agora_kb.schema.emit import Taxonomy, emit_schema

_SYMLINKS = ("CLAUDE.md", "QWEN.md", "GEMINI.md")


def test_taxonomy_defaults() -> None:
    t = Taxonomy()
    assert t.schema_version == 1
    assert t.taxonomy_policy == "open"
    assert t.allowed_tags == ()
    assert t.domains == ()


def test_taxonomy_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):  # extra='forbid' rejects unknown keys
        Taxonomy(bogus="x")  # type: ignore[call-arg]


def test_emit_writes_schema_doc_from_template(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    emit_schema(layout)
    assert layout.schema_file.exists()
    text = layout.schema_file.read_text(encoding="utf-8")
    # The emitted doc is the packaged frozen v1 schema template, not the engine's source AGENTS.md.
    assert "KB Wiki Schema v1" in text
    assert "schema_version" in text


@pytest.mark.skipif(os.name != "posix", reason="real symlinks require privilege on Windows")
def test_emit_symlinks_resolve_to_schema_doc(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    emit_schema(layout)
    schema_text = layout.schema_file.read_text(encoding="utf-8")
    for name in _SYMLINKS:
        p = layout.root / name
        assert p.is_symlink()
        # Relative symlink target keeps the repo portable.
        assert os.readlink(p) == "AGENTS.md"
        assert p.resolve() == layout.schema_file.resolve()
        assert p.read_text(encoding="utf-8") == schema_text


def test_emit_symlink_oserror_falls_back_to_plain_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-symlink-privilege fallback: on OSError, write a plain copy of the schema doc.

    Exercises the branch in :func:`agora_kb.schema.emit._link_or_copy` that keeps CLAUDE / QWEN /
    GEMINI resolving to the schema content when symlinks are unavailable — otherwise untested on
    POSIX CI (where real symlinks always succeed).
    """

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("symlinks not permitted")

    monkeypatch.setattr(emit_mod.os, "symlink", _raise)
    layout = RepoLayout(tmp_path)
    emit_schema(layout)
    schema_text = layout.schema_file.read_text(encoding="utf-8")
    for name in _SYMLINKS:
        p = layout.root / name
        assert p.exists()
        assert not p.is_symlink()  # the fallback wrote a plain copy, not a symlink
        assert p.read_text(encoding="utf-8") == schema_text


def test_emit_force_over_dangling_symlink(tmp_path: Path) -> None:
    """A dangling symlink occupant must not crash force re-emit (the _write_text is_symlink guard).

    ``Path.exists()`` follows symlinks and is False for a dangling one, so without the
    ``or dest.is_symlink()`` half the atomic exclusive write would raise FileExistsError on the
    path entry. Here _meta/taxonomy.yaml starts as a dangling symlink; force=True must replace it.
    """
    layout = RepoLayout(tmp_path)
    meta = layout.root / "_meta"
    meta.mkdir(parents=True)
    os.symlink("nonexistent-target", meta / "taxonomy.yaml")  # dangling
    emit_schema(layout, force=True)  # must not raise
    taxonomy_path = meta / "taxonomy.yaml"
    assert taxonomy_path.is_file()
    assert not taxonomy_path.is_symlink()


def test_emit_schema_version_bump_keeps_header_in_sync(tmp_path: Path) -> None:
    """Bumping Taxonomy.schema_version templates the value into the schema-doc header (§5.1).

    Without the placeholder substitution, the header would still say `1` after a v2 bump and the
    next lint would fail L1-17 (schema-doc-header drift). See the L1-17 test for the lint side.
    """
    layout = RepoLayout(tmp_path)
    emit_schema(layout, taxonomy=Taxonomy(schema_version=2))
    header = layout.schema_file.read_text(encoding="utf-8")
    assert "is **`2`**" in header
    assert "is **`1`**" not in header
    doc = yaml.safe_load((layout.root / "_meta" / "taxonomy.yaml").read_text(encoding="utf-8"))
    assert doc["schema_version"] == 2


def test_emit_writes_taxonomy_that_round_trips(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    tax = Taxonomy(
        schema_version=1,
        taxonomy_policy="capped:5",
        allowed_tags=("architecture", "concurrency", "macro"),
        domains=("ai-tech", "economy", "general"),
    )
    emit_schema(layout, taxonomy=tax)
    doc = yaml.safe_load((layout.root / "_meta" / "taxonomy.yaml").read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1
    assert doc["taxonomy_policy"] == "capped:5"
    assert doc["domains"] == ["ai-tech", "economy", "general"]
    # allowed_tags is a mapping of tag-key -> {} (§5 shape: kebab-case keys, descriptors omitted).
    assert set(doc["allowed_tags"]) == {"architecture", "concurrency", "macro"}
    assert all(isinstance(v, dict) for v in doc["allowed_tags"].values())


def test_emit_default_taxonomy_shape(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    emit_schema(layout)
    doc = yaml.safe_load((layout.root / "_meta" / "taxonomy.yaml").read_text(encoding="utf-8"))
    assert doc == {
        "schema_version": 1,
        "taxonomy_policy": "open",
        "domains": [],
        "allowed_tags": {},
    }


def test_emit_writes_templates(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    emit_schema(layout)
    templates = layout.root / "_templates"
    assert (templates / "theme.md").exists()
    assert (templates / "daily.md").exists()
    theme = (templates / "theme.md").read_text(encoding="utf-8")
    daily = (templates / "daily.md").read_text(encoding="utf-8")
    assert "type: theme" in theme
    assert "type: daily" in daily
    # Daily template carries the run-clock-injected keys (ADR-0010 §0.1 / §2.3).
    assert "run_id:" in daily
    assert "date:" in daily


def test_emit_is_idempotent(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    emit_schema(layout)
    snapshot = {
        p.relative_to(layout.root).as_posix(): (
            os.readlink(p) if p.is_symlink() else p.read_bytes()
        )
        for p in layout.root.rglob("*")
        if p.is_file() or p.is_symlink()
    }
    emit_schema(layout)  # second run must be a no-op
    after = {
        p.relative_to(layout.root).as_posix(): (
            os.readlink(p) if p.is_symlink() else p.read_bytes()
        )
        for p in layout.root.rglob("*")
        if p.is_file() or p.is_symlink()
    }
    assert after == snapshot


def test_emit_idempotent_preserves_manual_schema_edits_without_force(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    emit_schema(layout)
    # Without force, an existing schema doc is left untouched (idempotent skip).
    layout.schema_file.write_text("LOCAL EDIT", encoding="utf-8")
    emit_schema(layout)
    assert layout.schema_file.read_text(encoding="utf-8") == "LOCAL EDIT"


def test_emit_force_overwrites(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    emit_schema(layout)
    layout.schema_file.write_text("LOCAL EDIT", encoding="utf-8")
    emit_schema(layout, force=True)
    assert "KB Wiki Schema v1" in layout.schema_file.read_text(encoding="utf-8")


def test_emit_force_updates_taxonomy(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    emit_schema(layout)
    emit_schema(
        layout,
        taxonomy=Taxonomy(allowed_tags=("architecture",), domains=("ai-tech",)),
        force=True,
    )
    doc = yaml.safe_load((layout.root / "_meta" / "taxonomy.yaml").read_text(encoding="utf-8"))
    assert set(doc["allowed_tags"]) == {"architecture"}
    assert doc["domains"] == ["ai-tech"]


def test_emitted_meta_and_templates_are_off_ingest_allowlist(tmp_path: Path) -> None:
    """Emit writes _meta/ and _templates/, which are NOT on the INGEST allowlist (ADR-0011 §4.0).

    This is a documentation-as-test guard: emit is the repo-init/admin path, distinct from a curator
    INGEST write. The files it creates here are exactly the ones the curator may never write.
    """
    layout = RepoLayout(tmp_path)
    emit_schema(layout)
    # These paths exist after emit but live outside { wiki/**, index.md, <domain>-moc.md, log.md,
    # assets/** } — the curator-writable allowlist.
    assert (layout.root / "_meta" / "taxonomy.yaml").exists()
    assert (layout.root / "_templates").is_dir()
    assert layout.schema_file.exists()


# --- KB wiki schema 2 (ADR-0041 D6): the emitter is version-selecting ----------------------------
def test_emit_schema_2_writes_the_v2_doc(tmp_path: Path) -> None:
    """`schema_version: 2` emits ADR-0041's doc, not ADR-0010's, with the header substituted."""
    layout = RepoLayout(tmp_path)
    emit_schema(layout, taxonomy=Taxonomy(schema_version=2))
    text = layout.schema_file.read_text(encoding="utf-8")
    assert "KB Wiki Schema v2" in text
    assert "KB Wiki Schema v1" not in text
    assert "is **`2`**" in text  # the L1-17 header mirror


def test_emit_schema_2_doc_states_the_kind_first_contract(tmp_path: Path) -> None:
    """The v2 doc is the BRAIN's contract, so the load-bearing schema-2 rules must be IN it.

    Not a spell-check: each assertion below is a rule the curator brain cannot follow if the
    emitted doc omits it — the kind directories it must write into, the frontmatter keys APPLY
    materializes, the three new lint rules that reject a run, and the human-owned tree it may
    never touch.
    """
    layout = RepoLayout(tmp_path)
    emit_schema(layout, taxonomy=Taxonomy(schema_version=2))
    text = layout.schema_file.read_text(encoding="utf-8")
    for kind_dir in ("concepts/", "summaries/", "notes/", "maps/", "entities/", "people/"):
        assert f"wiki/{kind_dir}" in text or kind_dir in text
    for key in ("kind:", "subjects:", "kb:", "provenance:", "derived:"):
        assert key in text
    for rule in ("L1-22", "L1-23", "L1-24", "L1-20"):
        assert rule in text
    # L1-21 is PRE-RESERVED for the L2-6 promotion and must not be handed out to a new rule.
    assert "pre-reserved" in text
    # One journal per run_date, repo-wide (D2.6) — the `-moc` filename marker is gone (D5).
    assert "wiki/notes/<yyyy>/<mm>/<yyyy>-<mm>-<dd>.md" in text
    assert "-moc.md" not in text


def test_emit_schema_2_keeps_the_section_numbering_and_h0_wording(tmp_path: Path) -> None:
    """Both docs share section structure, so a prompt or reviewer citing a § survives the flip.

    The `related/` view wording is asserted verbatim because it is the H0 (#144/#152) correction:
    the PASS-1 view comes from the model-free lexical oracle and lists only curator-produced notes,
    which is what makes every hit in it a legal MERGE/CONTEST target.
    """
    v1 = RepoLayout(tmp_path / "one")
    v2 = RepoLayout(tmp_path / "two")
    emit_schema(v1, taxonomy=Taxonomy(schema_version=1))
    emit_schema(v2, taxonomy=Taxonomy(schema_version=2))
    v1_text = v1.schema_file.read_text(encoding="utf-8")
    v2_text = v2.schema_file.read_text(encoding="utf-8")

    def sections(text: str) -> list[str]:
        return re.findall(r"^## (\d+)\. ", text, re.MULTILINE)

    # Every numbered top-level section of the v1 doc still exists in the v2 doc, in the same order.
    assert sections(v1_text) == sections(v2_text) == [str(n) for n in range(10)]
    for text in (v1_text, v2_text):
        assert "lexical oracle `query_lexical`" in text
        assert "graded = curator_produced(notes)" in text


def test_emit_schema_2_writes_per_kind_templates(tmp_path: Path) -> None:
    """`_templates/` holds one template per KIND under schema 2 (ADR-0041 D1)."""
    layout = RepoLayout(tmp_path)
    emit_schema(layout, taxonomy=Taxonomy(schema_version=2))
    templates = layout.root / "_templates"
    concept = (templates / "concept.md").read_text(encoding="utf-8")
    note = (templates / "note.md").read_text(encoding="utf-8")
    assert "kind: concept" in concept and "subjects: []" in concept and "kb:" in concept
    assert "kind: note" in note and "run_id:" in note and "date:" in note
    # The v1 per-TYPE pair is NOT emitted into a v2 repo…
    assert not (templates / "theme.md").exists()
    assert not (templates / "daily.md").exists()
    # …and the empty tiers get no template: nothing may produce one (OD-7 / OD-8).
    assert not (templates / "summary.md").exists()
    assert not (templates / "entity.md").exists()


def test_emit_schema_1_is_unchanged_by_the_v2_addition(tmp_path: Path) -> None:
    """The schema-1 emit is byte-identical whether or not the version is stated explicitly."""
    implicit = RepoLayout(tmp_path / "implicit")
    explicit = RepoLayout(tmp_path / "explicit")
    emit_schema(implicit)
    emit_schema(explicit, taxonomy=Taxonomy(schema_version=1), schema_version=1)

    def snapshot(layout: RepoLayout) -> dict[str, bytes | str]:
        return {
            p.relative_to(layout.root).as_posix(): (
                os.readlink(p) if p.is_symlink() else p.read_bytes()
            )
            for p in layout.root.rglob("*")
            if p.is_file() or p.is_symlink()
        }

    assert snapshot(explicit) == snapshot(implicit)
    assert "KB Wiki Schema v1" in implicit.schema_file.read_text(encoding="utf-8")
    assert (implicit.root / "_templates" / "theme.md").exists()


def test_emit_rejects_a_schema_version_contradicting_the_taxonomy(tmp_path: Path) -> None:
    """Emit writes BOTH the header and the canonical taxonomy, so it must not manufacture drift.

    A silently-honoured override would produce exactly the L1-17 condition (schema-doc header !=
    `_meta/taxonomy.yaml: schema_version`) that lint exists to reject — written by the one
    component that owns both sides.
    """
    layout = RepoLayout(tmp_path)
    with pytest.raises(ValueError, match="contradicts taxonomy.schema_version"):
        emit_schema(layout, taxonomy=Taxonomy(schema_version=1), schema_version=2)
    assert not layout.schema_file.exists()  # refused BEFORE any write


def test_emit_refuses_a_version_with_no_packaged_doc(tmp_path: Path) -> None:
    """An unknown schema version raises rather than silently shipping the v1 contract.

    The emitted doc is copied verbatim into the curator's run bundle, so a fallback would hand the
    brain the rules of a layout the repo is not on — and the resulting plan failures would be
    blamed on the model.
    """
    layout = RepoLayout(tmp_path)
    with pytest.raises(ValueError, match="no packaged KB schema doc"):
        emit_schema(layout, taxonomy=Taxonomy(schema_version=99))
    assert not layout.schema_file.exists()


def test_emit_schema_2_is_idempotent(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    tax = Taxonomy(schema_version=2, domains=("ai-tech",))
    emit_schema(layout, taxonomy=tax)
    layout.schema_file.write_text("LOCAL EDIT", encoding="utf-8")
    emit_schema(layout, taxonomy=tax)  # idempotent skip, exactly as on schema 1
    assert layout.schema_file.read_text(encoding="utf-8") == "LOCAL EDIT"
    emit_schema(layout, taxonomy=tax, force=True)
    assert "KB Wiki Schema v2" in layout.schema_file.read_text(encoding="utf-8")
