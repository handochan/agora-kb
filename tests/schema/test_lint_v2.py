"""Tests for the KB wiki **schema 2** lint ruleset (ADR-0041, via ADR-0010's supersession banner).

The companion of ``tests/schema/test_lint.py``, which pins the schema-1 ruleset. Here the repo's
``_meta/taxonomy.yaml`` says ``schema_version: 2``, so :func:`agora_kb.schema.lint.lint` dispatches
to the ADR-0041 ruleset: the first segment under ``wiki/`` IS the kind, the subject lives in
``subjects:``, ``wiki/people/**`` is never graded, and L1-22 / L1-23 / L1-24 exist.

Every rule the banner AMENDS or ADDS gets a passing fixture and a failing one, and the fixtures are
written **by hand in this module** rather than through ``tests/support/kb_builder`` on purpose:
this file is the independent statement of what schema 2 looks like on disk, so a builder bug cannot
make the ruleset agree with itself.

The schema-1 side of the dispatch is asserted in ``tests/schema/test_lint.py`` (the whole suite
there runs unchanged, plus an explicit byte-identity test).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agora_kb.core.frontmatter import render
from agora_kb.core.layout import RepoLayout
from agora_kb.schema.emit import Taxonomy
from agora_kb.schema.lint import lint
from agora_kb.schema.notes import parse_all_notes

RUN_DATE = "2026-06-14"
RUN_ID = "2026-06-14T03-00-00.000Z--7f31ab"

#: The ``_meta/kb.yaml`` ``kb_id`` (ADR-0041 D1.5) every curator-written note mirrors into ``kb:``.
KB_ID = "01J8ZQ3M4N5P6Q7R8S9T0V1W2X"

_TAXONOMY = Taxonomy(
    schema_version=2,
    taxonomy_policy="open",
    allowed_tags=("architecture", "concurrency", "macro"),
    domains=("ai-tech", "economy", "general"),
)

_RAW = "raw/ai-tech/2026-06-09-cqrs.md"
_RAW_2 = "raw/ai-tech/2026-06-10-inbox.md"


# --- fixture helpers -----------------------------------------------------------------------------


def _write(layout: RepoLayout, rel: str, fm: dict[str, object], body: str = "") -> Path:
    path = layout.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(fm, body), encoding="utf-8", newline="\n")
    return path


def _write_bytes(layout: RepoLayout, rel: str, raw: bytes) -> Path:
    path = layout.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _write_taxonomy(layout: RepoLayout, taxonomy: Taxonomy = _TAXONOMY) -> None:
    doc = {
        "schema_version": taxonomy.schema_version,
        "taxonomy_policy": taxonomy.taxonomy_policy,
        "domains": list(taxonomy.domains),
        "allowed_tags": {t: {} for t in taxonomy.allowed_tags},
    }
    meta = layout.root / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "taxonomy.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _raw_source(layout: RepoLayout, rel: str = _RAW) -> None:
    path = layout.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("raw artifact body\n", encoding="utf-8")


def _concept_fm(**overrides: object) -> dict[str, object]:
    fm: dict[str, object] = {
        "title": "Curator concurrency model",
        "kind": "concept",
        "kb": KB_ID,
        "subjects": ["ai-tech"],
        "aliases": [],
        "tags": ["architecture", "concurrency"],
        "created": RUN_DATE,
        "updated": RUN_DATE,
        "status": "active",
        "summary": "One curator advances the curated branch.",
        "sources": [_RAW],
        "related": [],
        "confidence": "high",
    }
    fm.update(overrides)
    return fm


def _summary_fm(**overrides: object) -> dict[str, object]:
    fm = _concept_fm(
        title="CQRS deep dive",
        kind="summary",
        summary="A long-form pass over the write path.",
    )
    fm.update(overrides)
    return fm


def _entity_fm(**overrides: object) -> dict[str, object]:
    fm: dict[str, object] = {
        "title": "Anthropic",
        "kind": "entity",
        "kb": KB_ID,
        "subjects": ["ai-tech"],
        "aliases": [],
        "tags": [],
        "created": RUN_DATE,
        "updated": RUN_DATE,
        "status": "stub",
        "summary": "A registered entity.",
        "sources": [],
        "related": [],
    }
    fm.update(overrides)
    return fm


def _map_fm(**overrides: object) -> dict[str, object]:
    fm: dict[str, object] = {
        "title": "AI/Tech",
        "kind": "map",
        "kb": KB_ID,
        "subjects": ["ai-tech"],
        "aliases": [],
        "tags": [],
        "created": RUN_DATE,
        "updated": RUN_DATE,
        "status": "active",
        "summary": "subject hub",
        "children": ["[[curator-concurrency]]"],
    }
    fm.update(overrides)
    return fm


def _note_fm(**overrides: object) -> dict[str, object]:
    fm: dict[str, object] = {
        "title": f"Journal {RUN_DATE}",
        "kind": "note",
        "kb": KB_ID,
        "subjects": [],
        "aliases": [],
        "tags": [],
        "created": RUN_DATE,
        "updated": RUN_DATE,
        "status": "active",
        "summary": "what the run consolidated",
        "date": RUN_DATE,
        "run_id": RUN_ID,
        "sources": [],
    }
    fm.update(overrides)
    return fm


def _index_fm(**overrides: object) -> dict[str, object]:
    fm: dict[str, object] = {
        "title": "KB index",
        "kind": "index",
        "kb": KB_ID,
        "subjects": [],
        "aliases": [],
        "tags": [],
        "created": RUN_DATE,
        "updated": RUN_DATE,
        "status": "active",
        "summary": "root",
        "children": ["[[ai-tech]]"],
    }
    fm.update(overrides)
    return fm


def _valid_repo(tmp_path: Path) -> RepoLayout:
    """A fully-valid schema-2 repo: index -> maps/ai-tech -> concepts/curator-concurrency (+ note).

    Kind-first throughout: the root ``index.md`` is the root map (D1.2), ``wiki/maps/ai-tech.md``
    is a map whose subject is FRONTMATTER not path, and the journal sits on its ``<yyyy>/<mm>``
    date shard with a bare-date basename (D2.6).
    """
    layout = RepoLayout(tmp_path)
    _write_taxonomy(layout)
    _raw_source(layout)
    _write(layout, "index.md", _index_fm(), "- [AI/Tech](wiki/maps/ai-tech.md) — models")
    _write(
        layout,
        "wiki/maps/ai-tech.md",
        _map_fm(),
        "- [Curator concurrency model](../concepts/curator-concurrency.md)",
    )
    _write(layout, "wiki/concepts/curator-concurrency.md", _concept_fm(), "Body.[^1]")
    _write(layout, f"wiki/notes/2026/06/{RUN_DATE}.md", _note_fm(), "## consolidated")
    return layout


def _codes(result: object, path: str | None = None) -> list[str]:
    findings = result.findings  # type: ignore[attr-defined]
    return sorted(f.code for f in findings if path is None or f.path == path)


def _lint(layout: RepoLayout, **kwargs: object):  # noqa: ANN201 - test helper
    return lint(layout, taxonomy=_TAXONOMY, **kwargs)  # type: ignore[arg-type]


# --- the happy path + the dispatch itself --------------------------------------------------------


def test_a_fully_valid_schema2_repo_lints_ok(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    result = _lint(layout, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, result.findings
    assert result.findings == ()


def test_a_valid_schema2_repo_is_ok_without_a_run_date(tmp_path: Path) -> None:
    """The dashboard path: no run_date, so the run-relative halves of L1-12/L1-14 do not fire."""
    layout = _valid_repo(tmp_path)
    assert _lint(layout).ok


def test_the_ruleset_is_selected_by_the_taxonomy_on_disk(tmp_path: Path) -> None:
    """No ``schema_version=`` and no ``taxonomy=``: the version comes from ``_meta/taxonomy.yaml``.

    The v1 layout is REJECTED under it — ``wiki/ai-tech/`` is not a kind directory (L1-22) — which
    is what proves the dispatch actually read the file rather than defaulting to 1.
    """
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/ai-tech/themes/legacy.md", _concept_fm(title="Legacy"), "Body.")
    result = lint(layout)
    assert not result.ok
    assert "L1-22" in _codes(result, "wiki/ai-tech/themes/legacy.md")


def test_an_explicit_schema_version_kwarg_overrides_the_taxonomy(tmp_path: Path) -> None:
    """A converter writing a destination repo knows the version out of band; this is that seam."""
    layout = _valid_repo(tmp_path)
    # Graded as schema 1, a kind-first repo is a pile of findings (no `type:`, unknown domains).
    assert not lint(layout, taxonomy=_TAXONOMY, schema_version=1).ok
    assert lint(layout, taxonomy=_TAXONOMY, schema_version=2).ok


def test_lint_is_pure_and_repeatable_on_schema_2(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/broken.md", _concept_fm(title="Broken", sources=[]), "Body.")
    first = _lint(layout, run_date=RUN_DATE)
    second = _lint(layout, run_date=RUN_DATE)
    assert first == second
    assert [(f.path, f.code) for f in first.findings] == sorted(
        (f.path, f.code) for f in first.findings
    )


# --- L1-4 (required frontmatter, keyed on kind) --------------------------------------------------


def test_l1_4_missing_kb_fails(tmp_path: Path) -> None:
    """``kb:`` is the one NEW required common-base key in schema 2 (ADR-0041 D1.5/D2)."""
    layout = _valid_repo(tmp_path)
    fm = _concept_fm(title="No KB id")
    del fm["kb"]
    _write(layout, "wiki/concepts/no-kb.md", fm, "Body.")
    assert "L1-4" in _codes(_lint(layout), "wiki/concepts/no-kb.md")


def test_l1_4_kb_present_on_every_kind_of_the_valid_repo(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    assert all(n.kb == KB_ID for n in parse_all_notes(layout, schema_version=2))


def test_l1_4_map_missing_children_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    fm = _map_fm(title="Childless", subjects=["economy"])
    del fm["children"]
    _write(layout, "wiki/maps/childless.md", fm, "")
    assert "L1-4" in _codes(_lint(layout), "wiki/maps/childless.md")


def test_l1_4_note_missing_run_id_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    fm = _note_fm(title="No run id", date="2026-06-13")
    del fm["run_id"]
    _write(layout, "wiki/notes/2026/06/2026-06-13.md", fm, "## x")
    assert "L1-4" in _codes(_lint(layout), "wiki/notes/2026/06/2026-06-13.md")


def test_l1_4_bad_body_status_value_fails(tmp_path: Path) -> None:
    """The key exists ONLY as ``pending``; its ``absent`` state is the key being absent (§2.6)."""
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/concepts/flagged.md",
        _concept_fm(title="Flagged", body_status="absent"),
        "Body.",
    )
    assert "L1-4" in _codes(_lint(layout), "wiki/concepts/flagged.md")


def test_l1_4_pending_body_status_is_accepted(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/concepts/pending.md",
        _concept_fm(title="Pending", body_status="pending"),
        "<!-- agora:body:start id=r--c -->\n<one atomic idea>\n<!-- agora:body:end id=r--c -->",
    )
    assert "L1-4" not in _codes(_lint(layout), "wiki/concepts/pending.md")


def test_l1_4_malformed_provenance_block_fails(tmp_path: Path) -> None:
    """D2.3's two lists are SHAPE-checked; whether a writer is authenticated is not lint's call."""
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/concepts/prov.md",
        _concept_fm(title="Prov", provenance={"writers": "hando", "agents": []}),
        "Body.",
    )
    assert "L1-4" in _codes(_lint(layout), "wiki/concepts/prov.md")


def test_l1_4_well_formed_provenance_and_derived_are_accepted(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/concepts/prov-ok.md",
        _concept_fm(
            title="Prov ok",
            derived=False,
            provenance={"writers": ["hando"], "agents": ["claude-code"]},
        ),
        "Body.",
    )
    assert _codes(_lint(layout), "wiki/concepts/prov-ok.md") == []
    notes = {n.rel_path: n for n in parse_all_notes(layout, schema_version=2)}
    note = notes["wiki/concepts/prov-ok.md"]
    assert note.provenance.writers == ("hando",)
    assert note.provenance.agents == ("claude-code",)
    assert note.derived is False


def test_l1_4_non_bool_derived_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/d.md", _concept_fm(title="D", derived="yes"), "Body.")
    assert "L1-4" in _codes(_lint(layout), "wiki/concepts/d.md")


# --- L1-5 (subjects in the taxonomy domains; empty allowed) --------------------------------------


def test_l1_5_unknown_subject_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/concepts/off-vocab.md",
        _concept_fm(title="Off vocab", subjects=["ai-tech", "not-a-domain"]),
        "Body.",
    )
    result = _lint(layout)
    assert "L1-5" in _codes(result, "wiki/concepts/off-vocab.md")
    assert any("not-a-domain" in f.message for f in result.findings)


def test_l1_5_empty_subjects_is_legal(tmp_path: Path) -> None:
    """ADR-0041 D2.2: ``[]`` asserts nothing and loses nothing — the ADR-0022 floor's heir."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/unclassified.md", _concept_fm(title="Unc", subjects=[]), "Body.")
    assert _codes(_lint(layout), "wiki/concepts/unclassified.md") == []


def test_l1_5_the_kind_directory_is_never_read_as_a_subject(tmp_path: Path) -> None:
    """``concepts`` is no declared domain; under v1's path rule the whole repo would fail L1-5."""
    layout = _valid_repo(tmp_path)
    assert "concepts" not in _TAXONOMY.domains
    assert _lint(layout).ok


def test_l1_5_unknown_tag_still_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/tagged.md", _concept_fm(title="T", tags=["nope"]), "Body.")
    assert "L1-5" in _codes(_lint(layout), "wiki/concepts/tagged.md")


# --- L1-6 / L1-24 (map children) -----------------------------------------------------------------


def test_l1_6_children_mismatch_fails_on_a_map(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/maps/ai-tech.md",
        _map_fm(children=["[[curator-concurrency]]"]),
        "",  # declared child, no bullet
    )
    assert "L1-6" in _codes(_lint(layout), "wiki/maps/ai-tech.md")


def test_l1_24_a_journal_note_may_never_be_a_map_child(tmp_path: Path) -> None:
    """v1 said this in prose with no rule; L1-24 is where it finally gets enforced (D1.3)."""
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/maps/ai-tech.md",
        _map_fm(children=["[[curator-concurrency]]", f"[[{RUN_DATE}]]"]),
        f"- [Curator concurrency model](../concepts/curator-concurrency.md)\n"
        f"- [Journal]( ../notes/2026/06/{RUN_DATE}.md )",
    )
    result = _lint(layout)
    assert "L1-24" in _codes(result, "wiki/maps/ai-tech.md")
    assert "L1-6" not in _codes(result, "wiki/maps/ai-tech.md")


def test_l1_24_an_entity_may_not_be_a_map_child_on_day_one(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/entities/anthropic.md", _entity_fm(), "")
    _write(
        layout,
        "wiki/maps/ai-tech.md",
        _map_fm(children=["[[curator-concurrency]]", "[[anthropic]]"]),
        "- [Curator concurrency model](../concepts/curator-concurrency.md)\n"
        "- [Anthropic](../entities/anthropic.md)",
    )
    assert "L1-24" in _codes(_lint(layout), "wiki/maps/ai-tech.md")


def test_l1_24_concept_summary_and_map_children_are_admitted(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/summaries/cqrs-deep-dive.md", _summary_fm(), "Long body.")
    _write(
        layout,
        "wiki/maps/nested.md",
        _map_fm(title="Nested", children=[], subjects=["economy"]),
        "",
    )
    _write(
        layout,
        "wiki/maps/ai-tech.md",
        _map_fm(
            children=["[[curator-concurrency]]", "[[cqrs-deep-dive]]", "[[nested]]"],
        ),
        "- [Curator concurrency model](../concepts/curator-concurrency.md)\n"
        "- [CQRS deep dive](../summaries/cqrs-deep-dive.md)\n"
        "- [Nested](nested.md)",
    )
    result = _lint(layout)
    assert result.ok, result.findings


# --- L1-7 / L1-8 / L1-8b / L1-10 / L1-19 (the sourced kinds) -------------------------------------


def test_l1_7_non_stub_concept_with_empty_sources_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/thin.md", _concept_fm(title="Thin", sources=[]), "Body.")
    assert "L1-7" in _codes(_lint(layout), "wiki/concepts/thin.md")


def test_l1_7_non_stub_summary_with_empty_sources_fails(tmp_path: Path) -> None:
    """The predicate widened from ``theme`` to ``{concept, summary}`` — summary is graded too."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/summaries/thin.md", _summary_fm(title="Thin", sources=[]), "Body.")
    assert "L1-7" in _codes(_lint(layout), "wiki/summaries/thin.md")


def test_l1_7_stub_concept_with_empty_sources_is_exempt(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/concepts/stub.md",
        _concept_fm(title="Stub", status="stub", sources=[]),
        "",
    )
    assert "L1-7" not in _codes(_lint(layout), "wiki/concepts/stub.md")


def test_l1_7_an_entity_with_empty_sources_is_excluded_from_the_rule(tmp_path: Path) -> None:
    """ADR-0010's banner excludes ``entity`` from L1-7 BY NAME, not just via the stub exemption."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/entities/anthropic.md", _entity_fm(status="active", sources=[]), "")
    assert _codes(_lint(layout), "wiki/entities/anthropic.md") == []


def test_l1_8_missing_source_path_fails_on_a_concept(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/concepts/ghost.md",
        _concept_fm(title="Ghost", sources=["raw/ai-tech/nope.md"]),
        "Body.",
    )
    assert "L1-8" in _codes(_lint(layout), "wiki/concepts/ghost.md")


def test_l1_8b_sidecar_citation_fails_on_a_summary(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/summaries/sidecar.md",
        _summary_fm(title="Sidecar", sources=[f"{_RAW}.meta.yaml"]),
        "Body.",
    )
    assert "L1-8b" in _codes(_lint(layout), "wiki/summaries/sidecar.md")


def test_l1_10_contested_shape_is_graded_on_a_concept(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _raw_source(layout, _RAW_2)
    good = _concept_fm(
        title="Contested",
        status="contested",
        sources=[_RAW, _RAW_2],
        contested_by=["curator-concurrency"],
        contested_at=RUN_DATE,
    )
    _write(layout, "wiki/concepts/contested.md", good, "> [!contested]\n> two claims")
    assert "L1-10" not in _codes(_lint(layout, run_date=RUN_DATE), "wiki/concepts/contested.md")

    bad = dict(good)
    bad["contested_by"] = []
    _write(layout, "wiki/concepts/contested.md", bad, "> [!contested]\n> two claims")
    assert "L1-10" in _codes(_lint(layout, run_date=RUN_DATE), "wiki/concepts/contested.md")


def test_l1_19_bad_origin_fails_on_a_concept(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/org.md", _concept_fm(title="Org", origin="upload"), "Body.")
    assert "L1-19" in _codes(_lint(layout), "wiki/concepts/org.md")


def test_l1_19_harvest_origin_is_accepted(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/concepts/org.md",
        _concept_fm(title="Org", origin="harvest:claude-code"),
        "Body.",
    )
    assert _codes(_lint(layout), "wiki/concepts/org.md") == []


# --- L1-11 (kind enum + directory cross-check) ---------------------------------------------------


def test_l1_11_unknown_kind_fails(tmp_path: Path) -> None:
    """The retired v1 value ``theme`` is not a schema-2 kind."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/old.md", _concept_fm(title="Old", kind="theme"), "Body.")
    assert "L1-11" in _codes(_lint(layout), "wiki/concepts/old.md")


def test_l1_11_person_may_not_be_declared(tmp_path: Path) -> None:
    """``person`` is DERIVED from ``wiki/people/`` and never authored (D2.5/D3.3)."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/p.md", _concept_fm(title="P", kind="person"), "Body.")
    assert "L1-11" in _codes(_lint(layout), "wiki/concepts/p.md")


def test_l1_11_kind_contradicting_its_directory_fails(tmp_path: Path) -> None:
    """D2.1: where the two disagree the DIRECTORY wins and lint hard-rejects the note."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/liar.md", _concept_fm(title="Liar", kind="map"), "Body.")
    result = _lint(layout)
    assert "L1-11" in _codes(result, "wiki/concepts/liar.md")
    assert any("DIRECTORY is authoritative" in f.message for f in result.findings)


def test_l1_11_the_directory_selects_the_required_key_set_not_the_declared_kind(
    tmp_path: Path,
) -> None:
    """A note in ``concepts/`` claiming ``kind: map`` is graded as the CONCEPT it structurally is.

    If the liar's declaration chose the rule set, ``children:`` would be demanded and the concept
    rules skipped — i.e. the lie would pick its own grader.
    """
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/concepts/liar.md",
        _concept_fm(title="Liar", kind="map", sources=[]),
        "Body.",
    )
    codes = _codes(_lint(layout), "wiki/concepts/liar.md")
    assert "L1-7" in codes  # graded as a concept: non-stub with empty sources
    assert codes.count("L1-4") == 0  # NOT graded as a map: no "missing required 'children'"


def test_l1_11_unknown_status_still_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/s.md", _concept_fm(title="S", status="verified"), "Body.")
    assert "L1-11" in _codes(_lint(layout), "wiki/concepts/s.md")


# --- L1-14 (the note date/shard rule) ------------------------------------------------------------


def test_l1_14_a_well_placed_journal_passes(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    assert "L1-14" not in _codes(_lint(layout, run_date=RUN_DATE, run_id=RUN_ID))


def test_l1_14_a_wrong_month_shard_fails(tmp_path: Path) -> None:
    """D1.1: ``<yyyy>/<mm>`` MUST equal the year and month of the frontmatter ``date:``."""
    layout = _valid_repo(tmp_path)
    (layout.root / f"wiki/notes/2026/06/{RUN_DATE}.md").unlink()
    _write(layout, f"wiki/notes/2026/07/{RUN_DATE}.md", _note_fm(), "## x")
    result = _lint(layout, run_date=RUN_DATE, run_id=RUN_ID)
    assert "L1-14" in _codes(result, f"wiki/notes/2026/07/{RUN_DATE}.md")


def test_l1_14_a_v1_shaped_domain_prefixed_basename_fails(tmp_path: Path) -> None:
    """D2.6 retires ``<domain>-YYYY-MM-DD``: one journal per run_date, basename is the bare date."""
    layout = _valid_repo(tmp_path)
    (layout.root / f"wiki/notes/2026/06/{RUN_DATE}.md").unlink()
    _write(layout, f"wiki/notes/2026/06/ai-tech-{RUN_DATE}.md", _note_fm(), "## x")
    assert "L1-14" in _codes(_lint(layout), f"wiki/notes/2026/06/ai-tech-{RUN_DATE}.md")


def test_l1_14_date_ne_basename_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    (layout.root / f"wiki/notes/2026/06/{RUN_DATE}.md").unlink()
    _write(layout, "wiki/notes/2026/06/2026-06-13.md", _note_fm(date="2026-06-13"), "## x")
    assert _codes(_lint(layout), "wiki/notes/2026/06/2026-06-13.md") == []
    _write(layout, "wiki/notes/2026/06/2026-06-13.md", _note_fm(), "## x")
    assert "L1-14" in _codes(_lint(layout), "wiki/notes/2026/06/2026-06-13.md")


def test_l1_14_run_relative_halves_are_gated_on_run_date(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    (layout.root / f"wiki/notes/2026/06/{RUN_DATE}.md").unlink()
    _write(
        layout,
        "wiki/notes/2026/06/2026-06-13.md",
        _note_fm(title="Yesterday", date="2026-06-13", run_id="2026-06-13T03-00-00.000Z--aaaa"),
        "## x",
    )
    assert _lint(layout).ok  # no run_date: structural halves only
    assert "L1-14" in _codes(
        _lint(layout, run_date=RUN_DATE, run_id=RUN_ID), "wiki/notes/2026/06/2026-06-13.md"
    )


# --- L1-22 (the closed kind vocabulary, at the directory level) ----------------------------------


def test_l1_22_an_unknown_wiki_directory_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/themes/stray.md", _concept_fm(title="Stray"), "Body.")
    assert _codes(_lint(layout), "wiki/themes/stray.md") == ["L1-22"]


def test_l1_22_a_note_directly_under_wiki_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/loose.md", _concept_fm(title="Loose"), "Body.")
    assert _codes(_lint(layout), "wiki/loose.md") == ["L1-22"]


def test_l1_22_every_declared_kind_directory_is_accepted(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/summaries/cqrs-deep-dive.md", _summary_fm(), "Long body.")
    _write(layout, "wiki/entities/anthropic.md", _entity_fm(), "")
    _write(layout, "wiki/people/hando/scratch.md", _concept_fm(title="Scratch"), "Mine.")
    assert "L1-22" not in _codes(_lint(layout))


def test_l1_22_a_free_sub_folder_under_a_kind_is_allowed(tmp_path: Path) -> None:
    """D1.1: depth below the kind directory is free and no code reads the intermediate segments."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/eng/team/deep.md", _concept_fm(title="Deep"), "Body.")
    assert _codes(_lint(layout), "wiki/concepts/eng/team/deep.md") == []


# --- L1-23 (the reserved `_` domain namespace) ---------------------------------------------------


def test_l1_23_an_underscore_prefixed_domain_fails(tmp_path: Path) -> None:
    """D1.4 layer 2: ``raw/<domain>/`` and ``raw/_blob/`` share ONE namespace."""
    reserved = Taxonomy(
        schema_version=2,
        taxonomy_policy="open",
        allowed_tags=_TAXONOMY.allowed_tags,
        domains=(*_TAXONOMY.domains, "_blob"),
    )
    layout = _valid_repo(tmp_path)
    _write_taxonomy(layout, reserved)
    result = lint(layout, taxonomy=reserved)
    assert "L1-23" in _codes(result, "_meta/taxonomy.yaml")
    assert not result.ok


def test_l1_23_is_silent_on_a_clean_taxonomy(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    assert "L1-23" not in _codes(_lint(layout))


def test_l1_23_does_not_exist_under_schema_1(tmp_path: Path) -> None:
    """The rule is ADDED by ADR-0041; a v1 repo's verdict must not move."""
    reserved = Taxonomy(
        schema_version=1,
        taxonomy_policy="open",
        allowed_tags=_TAXONOMY.allowed_tags,
        domains=("_blob",),
    )
    layout = RepoLayout(tmp_path)
    _write_taxonomy(layout, reserved)
    assert "L1-23" not in _codes(lint(layout, taxonomy=reserved))


# --- L1-9 / D3.3 (the `wiki/people/**` carve-out) ------------------------------------------------


def test_people_notes_never_carry_a_finding_of_any_code(tmp_path: Path) -> None:
    """A human's Obsidian note is READ but never GRADED — it is not a producer artifact (D3.3)."""
    layout = _valid_repo(tmp_path)
    _write_bytes(
        layout,
        "wiki/people/hando/rough.md",
        b"\xef\xbb\xbf---\r\ntitle: Rough\r\nkind: nonsense\r\n---\r\n\r\nno kb, CRLF, BOM\r\n",
    )
    result = _lint(layout, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, result.findings
    assert [f for f in result.findings if f.path.startswith("wiki/people/")] == []


def test_a_malformed_people_note_does_not_abort_the_whole_pass(tmp_path: Path) -> None:
    """A fenceless people note must not become the fail-fast L1-4 that kills a curator run."""
    layout = _valid_repo(tmp_path)
    _write_bytes(layout, "wiki/people/hando/no-fence.md", b"just prose, no frontmatter\n")
    _write(layout, "wiki/concepts/thin.md", _concept_fm(title="Thin", sources=[]), "Body.")
    result = _lint(layout)
    assert "L1-7" in _codes(result, "wiki/concepts/thin.md")  # the pass still reached the concept
    assert _codes(result, "wiki/people/hando/no-fence.md") == []


def test_a_people_basename_does_not_collide_with_a_curator_basename(tmp_path: Path) -> None:
    """D3.3: people basenames are OUTSIDE the global identity space, so L1-1 must stay silent."""
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/people/hando/curator-concurrency.md",
        _concept_fm(title="My own take"),
        "Mine.",
    )
    result = _lint(layout)
    assert result.ok, result.findings
    assert "L1-1" not in _codes(result)


def test_a_curator_alias_may_take_a_people_basename(tmp_path: Path) -> None:
    """The inverse of the #152 reservation: a human tree may not acquire veto power over naming."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/people/hando/scratchpad.md", _concept_fm(title="Scratch"), "Mine.")
    _write(
        layout,
        "wiki/concepts/curator-concurrency.md",
        _concept_fm(aliases=["scratchpad"]),
        "Body.",
    )
    assert "L1-15" not in _codes(_lint(layout))


def test_a_wikilink_into_people_does_not_resolve(tmp_path: Path) -> None:
    """Stated by the ADR, not discovered: a people note is addressed by PATH, never [[base]]."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/people/hando/scratchpad.md", _concept_fm(title="Scratch"), "Mine.")
    _write(
        layout,
        "wiki/concepts/curator-concurrency.md",
        _concept_fm(related=["[[scratchpad]]"]),
        "Body.",
    )
    assert "L1-2" in _codes(_lint(layout), "wiki/concepts/curator-concurrency.md")


def test_people_notes_are_still_parsed_and_carry_a_derived_person_kind(tmp_path: Path) -> None:
    """Read is FIRST CLASS: the tree is ungraded, not invisible (D3.3)."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/people/hando/scratchpad.md", _concept_fm(title="Scratch"), "Mine.")
    notes = {n.rel_path: n for n in parse_all_notes(layout, schema_version=2)}
    assert notes["wiki/people/hando/scratchpad.md"].kind == "person"


def test_parse_all_notes_resolves_the_schema_version_from_the_repo_by_default(
    tmp_path: Path,
) -> None:
    """A read-side caller that passes no version must be CORRECT on a schema-2 repo, not v1-shaped.

    The trap a hardcoded default of 1 sets: every production reader (`Wiki.list_notes`, the gold
    assembler, the curator's own classification pass, `agora doctor`) takes the default, so on a
    schema-2 repo they would silently see `subjects` derived FROM THE PATH — the kind directory —
    which is the path-derived subject ADR-0041 D3.2 exists to abolish, and `kind = None` because
    the OD-3 `type:` mirror does not map back through the v1 table. `lint()` already resolves the
    version from the taxonomy; this is the same answer for the same question.
    """
    layout = _valid_repo(tmp_path)  # writes _meta/taxonomy.yaml with schema_version: 2

    notes = {n.rel_path: n for n in parse_all_notes(layout)}  # NO schema_version argument
    concept = notes["wiki/concepts/curator-concurrency.md"]
    assert concept.schema_version == 2
    assert concept.kind == "concept"
    assert concept.subjects == ("ai-tech",)  # frontmatter, NOT the "concepts" path segment
    # An explicit value still overrides the repo — the converter/test escape hatch.
    assert parse_all_notes(layout, schema_version=1)[0].schema_version == 1


def test_parse_all_notes_on_a_schema_1_repo_is_byte_identical_under_the_new_default(
    tmp_path: Path,
) -> None:
    """The additivity contract: resolving from a schema-1 taxonomy yields the v1 derivation."""
    layout = RepoLayout(tmp_path)
    _write_taxonomy(layout, Taxonomy(schema_version=1, domains=("ai-tech",)))
    _write(
        layout,
        "wiki/ai-tech/themes/cqrs.md",
        {"title": "CQRS", "type": "theme", "status": "active", "summary": "s"},
        "Body.",
    )

    (note,) = parse_all_notes(layout)
    assert note.schema_version == 1
    assert note.kind == "concept"  # the frozen D2.5 type->kind table
    assert note.subjects == ("ai-tech",)  # v1: the path IS the subject carrier


def test_parse_all_notes_defaults_to_v1_when_the_repo_declares_nothing(tmp_path: Path) -> None:
    """An indeterminate version resolves to 1 — the conservative pre-ADR-0041 answer."""
    layout = RepoLayout(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/cqrs.md",
        {"title": "CQRS", "type": "theme", "status": "active", "summary": "s"},
        "Body.",
    )
    (note,) = parse_all_notes(layout)
    assert note.schema_version == 1


# --- L2-1 (the orphan population) ----------------------------------------------------------------


def test_l2_1_counts_concepts_and_summaries_and_exempts_entities_and_people(
    tmp_path: Path,
) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/orphan-a.md", _concept_fm(title="A"), "Body.")
    _write(layout, "wiki/summaries/orphan-b.md", _summary_fm(title="B"), "Body.")
    _write(layout, "wiki/entities/anthropic.md", _entity_fm(), "")
    _write(layout, "wiki/people/hando/orphan-c.md", _concept_fm(title="C"), "Mine.")
    # 2 graded orphans (the concept + the summary); the entity and the people note are exempt.
    assert "L2-1" not in _codes(_lint(layout, max_orphans=2))
    result = _lint(layout, max_orphans=1)
    assert "L2-1" in _codes(result, "index.md")
    assert result.ok  # warning severity: never flips the verdict


def test_l2_1_a_people_link_does_not_suppress_a_concepts_orphan_count(tmp_path: Path) -> None:
    """Links OUT of a people note are ungraded (D3.3), so they do not feed the orphan universe.

    Otherwise the exclusion is one-sided: the human tree cannot BE counted, but one human file
    linking a concept would silently un-orphan it — a human-owned subtree acquiring a vote on the
    signal that gates a curator run (``curator.lint.max_orphans``, ADR-0022).
    """
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/orphan-a.md", _concept_fm(title="A"), "Body.")
    assert "L2-1" in _codes(_lint(layout, max_orphans=0), "index.md")

    # A human links the orphan from their own tree, by body link AND by frontmatter wikilink.
    _write(
        layout,
        "wiki/people/hando/reading.md",
        _concept_fm(title="Reading", related=["[[orphan-a]]"]),
        "- [A](../../concepts/orphan-a.md)",
    )
    assert "L2-1" in _codes(_lint(layout, max_orphans=0), "index.md")


def test_l2_1_off_by_default(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/orphan-a.md", _concept_fm(title="A"), "Body.")
    assert "L2-1" not in _codes(_lint(layout))


# --- rules kept verbatim (spot checks that the v2 path did not drop them) -------------------------


def test_l1_1_duplicate_basename_between_two_concepts_still_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/eng/curator-concurrency.md", _concept_fm(), "Body.")
    assert "L1-1" in _codes(_lint(layout), "wiki/concepts/eng/curator-concurrency.md")


def test_l1_13_a_second_index_still_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/maps/index.md", _map_fm(title="Fake", children=[]), "")
    assert "L1-13" in _codes(_lint(layout), "wiki/maps/index.md")


def test_l1_16_crlf_still_fails_on_a_curated_note(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    raw = render(_concept_fm(title="CRLF"), "Body.").replace("\n", "\r\n").encode("utf-8")
    _write_bytes(layout, "wiki/concepts/crlf.md", raw)
    assert "L1-16" in _codes(_lint(layout), "wiki/concepts/crlf.md")


def test_l1_12_future_date_still_gated_on_run_date(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/future.md", _concept_fm(title="F", updated="2099-01-01"), "B.")
    assert "L1-12" not in _codes(_lint(layout), "wiki/concepts/future.md")
    assert "L1-12" in _codes(_lint(layout, run_date=RUN_DATE), "wiki/concepts/future.md")


def test_l1_20_body_sentinel_integrity_still_fires(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/concepts/unbalanced.md",
        _concept_fm(title="Unbalanced"),
        "<!-- agora:body:start id=r--c -->\nprose",
    )
    assert "L1-20" in _codes(_lint(layout), "wiki/concepts/unbalanced.md")


def test_scope_still_narrows_the_graded_population_under_schema_2(tmp_path: Path) -> None:
    """The #152 producer scope and the D3.3 people exclusion are independent narrowings."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/human.md", _concept_fm(title="Human", sources=[]), "Body.")
    assert "L1-7" in _codes(_lint(layout), "wiki/concepts/human.md")
    scoped = _lint(layout, scope=["wiki/concepts/curator-concurrency.md"])
    assert _codes(scoped, "wiki/concepts/human.md") == []
    assert scoped.ok


def test_a_stale_type_key_never_selects_a_rule_under_schema_2(tmp_path: Path) -> None:
    """D2.5 retires ``type:`` as the kind AUTHORITY while OD-3 keeps emitting it as an OKF mirror.

    A concept carrying ``type: daily`` (a bad mirror, an imported v1 note, a brain's leftover) must
    NOT be graded by the v1 daily rules — that would let a retired key pick its own grader through
    the back door.
    """
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/mirrored.md", _concept_fm(title="M", type="daily"), "Body.")
    assert (
        _codes(_lint(layout, run_date=RUN_DATE, run_id=RUN_ID), "wiki/concepts/mirrored.md") == []
    )


def test_the_derived_okf_type_mirror_is_accepted(tmp_path: Path) -> None:
    """OD-3's recommendation: ``type:`` is still EMITTED as a derived mirror of ``kind``."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/concepts/mirror.md", _concept_fm(title="M", type="concept"), "Body.")
    assert _codes(_lint(layout), "wiki/concepts/mirror.md") == []


def test_the_note_reading_layer_exposes_the_schema_2_vocabulary(tmp_path: Path) -> None:
    """One kind vocabulary for read-side callers, taken from the DIRECTORY under schema 2."""
    layout = _valid_repo(tmp_path)
    _write(layout, "wiki/summaries/cqrs-deep-dive.md", _summary_fm(), "Long body.")
    _write(layout, "wiki/entities/anthropic.md", _entity_fm(), "")
    _write(layout, "wiki/people/hando/scratchpad.md", _concept_fm(title="Scratch"), "Mine.")
    notes = {n.rel_path: n for n in parse_all_notes(layout, schema_version=2)}

    assert {p: n.kind for p, n in notes.items()} == {
        "index.md": "index",
        "wiki/concepts/curator-concurrency.md": "concept",
        "wiki/entities/anthropic.md": "entity",
        "wiki/maps/ai-tech.md": "map",
        f"wiki/notes/2026/06/{RUN_DATE}.md": "note",
        "wiki/people/hando/scratchpad.md": "person",
        "wiki/summaries/cqrs-deep-dive.md": "summary",
    }
    # The SUBJECT comes from frontmatter, never from the path (D3.2) — `maps` is not a subject.
    assert notes["wiki/maps/ai-tech.md"].subjects == ("ai-tech",)
    assert notes[f"wiki/notes/2026/06/{RUN_DATE}.md"].subjects == ()
    assert notes["index.md"].schema_version == 2
