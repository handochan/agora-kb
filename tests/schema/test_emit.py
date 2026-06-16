"""Tests for the schema emitter (ADR-0010 §1, §4, §5; ADR-0011 §4.0 — emit is NOT an INGEST write).

Emit into a tmp ``RepoLayout`` then assert: ``AGENTS.md`` exists, the symlinks resolve to it,
``_meta/taxonomy.yaml`` round-trips, ``_templates/`` is present, and re-emitting is idempotent.
"""

from __future__ import annotations

import os
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
