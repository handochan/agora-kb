"""Tests for the deterministic L1 lint ruleset (ADR-0010 §6, L1-1..L1-19; ADR-0011 §4.4).

For each L1 rule EVALUATED BY :func:`lint` there is a fixture repo that PASSES (no finding of that
code) and one that FAILS (the expected finding code is present). The exceptions are the rules that
are out of lint()'s single-worktree surface: L1-3 (ambiguous wikilink — belt-and-suspenders behind
L1-1/L1-15), L1-9 (path-escape / off-allowlist write — diff-scoped, enforced by the worker), and
L1-18 (taxonomy-evolution policy — needs a before/after taxonomy pair). A fully-valid repo lints
``ok``; finding order is deterministic (sorted by ``(path, code)``); and the run-relative date
checks (L1-12 no-future, L1-14 date/run_id) fire ONLY when ``run_date`` (and, for the full run_id
equality, ``run_id``) is provided — the dashboard path (no ``run_date``) does not flag them.

The fixtures write raw bytes via :func:`_write` (so encoding rules L1-16 can be exercised) rather
than going through ``frontmatter.render``, keeping the on-disk shape under the test's control.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agora_kb.core.frontmatter import render
from agora_kb.core.layout import RepoLayout
from agora_kb.schema.emit import Taxonomy, emit_schema
from agora_kb.schema.lint import LintFinding, lint

RUN_DATE = "2026-06-14"
RUN_ID = "2026-06-14T03-00-00.000Z--7f31ab"

_TAXONOMY = Taxonomy(
    schema_version=1,
    taxonomy_policy="open",
    allowed_tags=("architecture", "concurrency", "macro"),
    domains=("ai-tech", "economy", "general"),
)


# --- fixture helpers ------------------------------------------------------------------------------


def _write(layout: RepoLayout, rel: str, fm: dict[str, object], body: str = "") -> Path:
    path = layout.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(fm, body), encoding="utf-8")
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


def _raw_source(layout: RepoLayout, rel: str) -> None:
    """Create a real raw/ artifact so a theme's sources: entry exists (L1-8)."""
    path = layout.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("raw artifact body\n", encoding="utf-8")


def _theme_fm(**overrides: object) -> dict[str, object]:
    fm: dict[str, object] = {
        "title": "Curator concurrency model",
        "type": "theme",
        "aliases": [],
        "tags": ["architecture", "concurrency"],
        "created": RUN_DATE,
        "updated": RUN_DATE,
        "status": "active",
        "summary": "One curator advances the curated branch.",
        "sources": ["raw/ai-tech/2026-06-09-cqrs.md"],
        "related": [],
        "confidence": "high",
    }
    fm.update(overrides)
    return fm


def _valid_repo(tmp_path: Path) -> RepoLayout:
    """A fully-valid KB repo: index -> ai-tech-moc -> curator-concurrency theme (+ stub target)."""
    layout = RepoLayout(tmp_path)
    _write_taxonomy(layout)
    _raw_source(layout, "raw/ai-tech/2026-06-09-cqrs.md")
    _write(
        layout,
        "index.md",
        {
            "title": "KB index",
            "type": "index",
            "aliases": [],
            "tags": [],
            "created": RUN_DATE,
            "updated": RUN_DATE,
            "status": "active",
            "summary": "root",
            "children": ["[[ai-tech-moc]]"],
        },
        "- [[ai-tech-moc]] — models",
    )
    _write(
        layout,
        "wiki/ai-tech/ai-tech-moc.md",
        {
            "title": "AI/Tech",
            "type": "moc",
            "aliases": [],
            "tags": [],
            "created": RUN_DATE,
            "updated": RUN_DATE,
            "status": "active",
            "summary": "domain hub",
            "children": ["[[curator-concurrency]]"],
        },
        "- [[curator-concurrency]] — the curator",
    )
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(related=["[[single-writer-stub]]"]),
        "Exactly one curator advances the branch. See [[single-writer-stub]].",
    )
    # A forward-declared stub target (exempt from L1-7) keeps the [[single-writer-stub]] link valid.
    _write(
        layout,
        "wiki/ai-tech/themes/single-writer-stub.md",
        _theme_fm(
            title="Single writer",
            summary="placeholder",
            status="stub",
            sources=[],
            related=[],
        ),
        "",
    )
    return layout


def _codes(result_findings: tuple[LintFinding, ...]) -> set[str]:
    return {f.code for f in result_findings}


# --- the happy path -------------------------------------------------------------------------------


def test_fully_valid_repo_lints_ok(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert result.ok, [f for f in result.findings]
    assert result.findings == ()


def test_valid_repo_ok_without_run_date(tmp_path: Path) -> None:
    # The dashboard path (no run_date) must also pass on a valid repo.
    layout = _valid_repo(tmp_path)
    result = lint(layout, taxonomy=_TAXONOMY)
    assert result.ok, [f for f in result.findings]


def test_taxonomy_loaded_from_disk_when_not_passed(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    result = lint(layout, run_date=RUN_DATE)  # taxonomy read from _meta/taxonomy.yaml
    assert result.ok, [f for f in result.findings]


# --- L1-1 duplicate basename ----------------------------------------------------------------------


def test_l1_1_duplicate_basename_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    # A second note with basename "curator-concurrency" in another domain dir.
    _write(
        layout,
        "wiki/economy/themes/curator-concurrency.md",
        _theme_fm(),
        "dup",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-1" in _codes(result.findings)
    assert not result.ok


# --- L1-2 broken wikilink (body + related:/children:) ---------------------------------------------


def test_l1_2_broken_link_in_body_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(related=["[[single-writer-stub]]"]),
        "See [[does-not-exist]].",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-2" in _codes(result.findings)


def test_l1_2_broken_link_in_related_array_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(related=["[[ghost-note]]"]),
        "body",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-2" in _codes(result.findings)


# --- L1-4 missing required frontmatter ------------------------------------------------------------


def test_l1_4_missing_summary_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    fm = _theme_fm()
    del fm["summary"]
    _write(layout, "wiki/ai-tech/themes/curator-concurrency.md", fm, "body")
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-4" in _codes(result.findings)


def test_l1_4_daily_missing_run_id_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/daily/ai-tech-2026-06-14.md",
        {
            "title": "Daily",
            "type": "daily",
            "aliases": [],
            "tags": [],
            "created": RUN_DATE,
            "updated": RUN_DATE,
            "status": "active",
            "summary": "today",
            "date": RUN_DATE,
            "sources": [],
        },
        "## 2026-06-14\n\nconsolidated",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-4" in _codes(result.findings)


# --- L1-5 tag / domain not in taxonomy ------------------------------------------------------------


def test_l1_5_unknown_tag_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(tags=["architecture", "not-a-real-tag"], related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-5" in _codes(result.findings)


def test_l1_5_unknown_domain_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    # A theme under a domain not declared in the taxonomy.
    _raw_source(layout, "raw/not-a-domain/x.md")
    _write(
        layout,
        "wiki/not-a-domain/themes/rogue.md",
        _theme_fm(title="Rogue", sources=["raw/not-a-domain/x.md"]),
        "body",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-5" in _codes(result.findings)


def test_l1_5_bad_tag_on_index_fails_type_agnostic(tmp_path: Path) -> None:
    # L1-5's tag check is type-agnostic: a bogus tag on the index note (not a theme) must fire it.
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "index.md",
        {
            "title": "KB index",
            "type": "index",
            "aliases": [],
            "tags": ["bogus"],
            "created": RUN_DATE,
            "updated": RUN_DATE,
            "status": "active",
            "summary": "root",
            "children": ["[[ai-tech-moc]]"],
        },
        "- [[ai-tech-moc]] — models",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-5" in _codes(result.findings)


def test_l1_5_root_index_domain_exempt(tmp_path: Path) -> None:
    # The root index.md has NO domain (_note_domain returns None), so it is exempt from the L1-5
    # domain-membership check — no L1-5 domain finding may be attributed to index.md here.
    layout = _valid_repo(tmp_path)
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert not any(f.code == "L1-5" and f.path == "index.md" for f in result.findings)


# --- L1-6 MOC children != child-bullet set --------------------------------------------------------


def test_l1_6_children_mismatch_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    # children: declares curator-concurrency but the body bullet points at single-writer-stub.
    _write(
        layout,
        "wiki/ai-tech/ai-tech-moc.md",
        {
            "title": "AI/Tech",
            "type": "moc",
            "aliases": [],
            "tags": [],
            "created": RUN_DATE,
            "updated": RUN_DATE,
            "status": "active",
            "summary": "domain hub",
            "children": ["[[curator-concurrency]]"],
        },
        "- [[single-writer-stub]] — wrong",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-6" in _codes(result.findings)


def test_l1_2_broken_link_in_children_array_fails(tmp_path: Path) -> None:
    # A broken link in a MOC's children: array that IS mirrored by a matching body bullet (so L1-6
    # passes) must still fire L1-2 — exercising the children:-array branch of L1-2 in isolation.
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/ai-tech-moc.md",
        {
            "title": "AI/Tech",
            "type": "moc",
            "aliases": [],
            "tags": [],
            "created": RUN_DATE,
            "updated": RUN_DATE,
            "status": "active",
            "summary": "domain hub",
            "children": ["[[curator-concurrency]]", "[[ghost]]"],
        },
        "- [[curator-concurrency]] — the curator\n- [[ghost]] — missing target",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-2" in _codes(result.findings)
    # The matching bullet keeps L1-6 satisfied, so the failure is L1-2 (broken link), not L1-6.
    assert "L1-6" not in _codes(result.findings)


# --- L1-7 non-stub theme with empty sources -------------------------------------------------------


def test_l1_7_nonstub_empty_sources_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(sources=[], related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-7" in _codes(result.findings)


def test_l1_7_stub_empty_sources_ok(tmp_path: Path) -> None:
    # The valid repo already contains a stub with empty sources; it must not trigger L1-7.
    layout = _valid_repo(tmp_path)
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-7" not in _codes(result.findings)


# --- L1-8 / L1-8b sources path -------------------------------------------------------------------


def test_l1_8_missing_source_path_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(sources=["raw/ai-tech/nope.md"], related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-8" in _codes(result.findings)


def test_l1_8b_sidecar_cited_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _raw_source(layout, "raw/ai-tech/foo.pdf.meta.yaml")
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(sources=["raw/ai-tech/foo.pdf.meta.yaml"], related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-8b" in _codes(result.findings)


# --- L1-10 contested shape ------------------------------------------------------------------------


def _contested_theme(**overrides: object) -> dict[str, object]:
    fm = _theme_fm(
        status="contested",
        sources=["raw/ai-tech/2026-06-09-cqrs.md", "raw/ai-tech/other.md"],
        contested_by=["competing-theme"],
        contested_at=RUN_DATE,
        related=["[[competing-theme]]"],
    )
    fm.update(overrides)
    return fm


def _seed_contested(layout: RepoLayout, fm: dict[str, object], body: str) -> None:
    _raw_source(layout, "raw/ai-tech/other.md")
    _raw_source(layout, "raw/ai-tech/competing.md")
    _write(
        layout,
        "wiki/ai-tech/themes/competing-theme.md",
        _theme_fm(title="Competing", sources=["raw/ai-tech/competing.md"], related=[]),
        "the competing claim",
    )
    _write(layout, "wiki/ai-tech/themes/curator-concurrency.md", fm, body)


def test_l1_10_well_formed_contested_ok(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _seed_contested(
        layout,
        _contested_theme(),
        "Claim A.\n\n> [!contested] Competing claim (recorded 2026-06-14)\n> Claim B.\n",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-10" not in _codes(result.findings), [f for f in result.findings]


def test_l1_10_missing_callout_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _seed_contested(layout, _contested_theme(), "Claim A only, no callout.")
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-10" in _codes(result.findings)


def test_l1_10_one_source_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _seed_contested(
        layout,
        _contested_theme(sources=["raw/ai-tech/2026-06-09-cqrs.md"]),  # only one source
        "> [!contested] Competing claim\n> B.",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-10" in _codes(result.findings)


def test_l1_10_empty_contested_by_fails(tmp_path: Path) -> None:
    # The cb_ok term in isolation: every other term is satisfied, only contested_by is empty.
    layout = _valid_repo(tmp_path)
    _seed_contested(
        layout,
        _contested_theme(contested_by=[]),
        "> [!contested] Competing claim (recorded 2026-06-14)\n> B.\n",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-10" in _codes(result.findings)


def test_l1_10_contested_at_ne_run_date_gated(tmp_path: Path) -> None:
    # The at_ok term: contested_at is a valid date but != run_date. With run_date the equality half
    # fires; WITHOUT run_date only date-format validity is required, so a valid date passes L1-10.
    layout = _valid_repo(tmp_path)
    _seed_contested(
        layout,
        _contested_theme(contested_at="2026-06-13"),
        "> [!contested] Competing claim (recorded 2026-06-13)\n> B.\n",
    )
    assert "L1-10" in _codes(lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE).findings)
    assert "L1-10" not in _codes(lint(layout, taxonomy=_TAXONOMY).findings)


# --- L1-11 unknown type / status ------------------------------------------------------------------


def test_l1_11_unknown_status_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(status="verified", related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-11" in _codes(result.findings)


def test_l1_11_unknown_type_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(type="wikipage", related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-11" in _codes(result.findings)


# --- L1-12 dates ----------------------------------------------------------------------------------


def test_l1_12_bad_date_format_fails_always(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(created="06/14/2026", related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    # Format check is structural — fires with AND without run_date.
    assert "L1-12" in _codes(lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE).findings)
    assert "L1-12" in _codes(lint(layout, taxonomy=_TAXONOMY).findings)


def test_l1_12_future_date_gated_on_run_date(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(updated="2099-01-01", related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    # With run_date: a future 'updated' is a no-future violation.
    assert "L1-12" in _codes(lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE).findings)
    # Without run_date (dashboard): a well-formed but future date does NOT fire (no "today").
    assert "L1-12" not in _codes(lint(layout, taxonomy=_TAXONOMY).findings)


def test_l1_12_unquoted_future_date_still_fires(tmp_path: Path) -> None:
    # ADR-0010 §2 shows dates UNQUOTED; yaml.safe_load coerces them to datetime.date. Writing raw
    # bytes (NOT via frontmatter.render, which quotes dates) proves the date gate fires on the
    # spec's own on-disk shape rather than fail-open on a non-string value.
    layout = _valid_repo(tmp_path)
    path = layout.root / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    path.write_text(
        "---\n"
        "title: Curator concurrency model\n"
        "type: theme\n"
        "aliases: []\n"
        "tags: [architecture, concurrency]\n"
        "created: 2026-06-14\n"
        "updated: 2099-01-01\n"  # UNQUOTED future date -> datetime.date
        "status: active\n"
        "summary: s\n"
        'sources: ["raw/ai-tech/2026-06-09-cqrs.md"]\n'
        'related: ["[[single-writer-stub]]"]\n'
        "confidence: high\n"
        "---\n"
        "See [[single-writer-stub]].\n",
        encoding="utf-8",
    )
    # With run_date: the unquoted future date must still be a no-future L1-12 violation.
    assert "L1-12" in _codes(lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE).findings)
    # Without run_date: a well-formed (date-typed) value is not flagged (no "today").
    assert "L1-12" not in _codes(lint(layout, taxonomy=_TAXONOMY).findings)


# --- L1-13 second index ---------------------------------------------------------------------------


def test_l1_13_second_index_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/index.md",
        {
            "title": "Rogue index",
            "type": "index",
            "aliases": [],
            "tags": [],
            "created": RUN_DATE,
            "updated": RUN_DATE,
            "status": "active",
            "summary": "x",
            "children": [],
        },
        "",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-13" in _codes(result.findings)


# --- L1-14 daily date mismatch --------------------------------------------------------------------


def _write_daily(layout: RepoLayout, rel: str, **overrides: object) -> None:
    fm: dict[str, object] = {
        "title": "Daily",
        "type": "daily",
        "aliases": [],
        "tags": [],
        "created": RUN_DATE,
        "updated": RUN_DATE,
        "status": "active",
        "summary": "today",
        "date": RUN_DATE,
        "run_id": RUN_ID,
        "sources": [],
    }
    fm.update(overrides)
    _write(layout, rel, fm, "## 2026-06-14\n\nconsolidated")


def test_l1_14_valid_daily_ok(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write_daily(layout, "wiki/ai-tech/daily/ai-tech-2026-06-14.md")
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-14" not in _codes(result.findings), [f for f in result.findings]


def test_l1_14_date_ne_basename_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    # basename date is 2026-06-13, but frontmatter date is RUN_DATE (2026-06-14).
    _write_daily(layout, "wiki/ai-tech/daily/ai-tech-2026-06-13.md")
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-14" in _codes(result.findings)


def test_l1_14_date_ne_run_date_gated(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    # date/basename agree on 2026-06-13 but != run_date (2026-06-14).
    _write_daily(
        layout,
        "wiki/ai-tech/daily/ai-tech-2026-06-13.md",
        date="2026-06-13",
        run_id="2026-06-13T03-00-00.000Z--7f31ab",
    )
    # With run_date: the date != run_date branch fires.
    assert "L1-14" in _codes(lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE).findings)
    # Without run_date: date == basename date, so the structural half is satisfied — no L1-14.
    assert "L1-14" not in _codes(lint(layout, taxonomy=_TAXONOMY).findings)


def test_l1_14_run_id_full_mismatch_fails_when_injected(tmp_path: Path) -> None:
    # date == basename == run_date, but run_id's TIME/suffix disagrees with the injected run_id
    # (same calendar date). With the full injected run_id, byte-for-byte equality must fail (the
    # curator gate enforces ADR-0008 replay idempotency, ADR-0010 L1-14 / ADR-0011 §4.4).
    layout = _valid_repo(tmp_path)
    _write_daily(
        layout,
        "wiki/ai-tech/daily/ai-tech-2026-06-14.md",
        run_id="2026-06-14T18-22-00.000Z--DEADBEEF",  # valid date prefix, wrong time/suffix
    )
    findings = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID).findings
    assert "L1-14" in _codes(findings)
    # The finding pins to the run_id branch, not the date branch.
    assert any(f.code == "L1-14" and "run_id" in f.message for f in findings)
    # Without the injected run_id (dashboard) only the date-portion is checked, which matches here.
    assert "L1-14" not in _codes(lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE).findings)


def test_l1_14_run_id_date_portion_mismatch_without_injected_run_id(tmp_path: Path) -> None:
    # date == basename == run_date, but run_id's DATE-PORTION disagrees (a copy-paste error). The
    # dashboard path (no injected run_id) still catches this via run_id[:10] != run_date.
    layout = _valid_repo(tmp_path)
    _write_daily(
        layout,
        "wiki/ai-tech/daily/ai-tech-2026-06-14.md",
        run_id="2026-06-13T03-00-00.000Z--7f31ab",  # date-prefix 2026-06-13 != run_date 2026-06-14
    )
    findings = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE).findings
    assert "L1-14" in _codes(findings)
    assert any(f.code == "L1-14" and "date-portion" in f.message for f in findings)


# --- L1-15 alias / basename collision -------------------------------------------------------------


def test_l1_15_alias_collides_with_basename_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    # The theme aliases itself to an existing basename ("ai-tech-moc").
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(aliases=["ai-tech-moc"], related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-15" in _codes(result.findings)


# --- L1-16 encoding -------------------------------------------------------------------------------


def test_l1_16_crlf_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    path = layout.root / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    text = render(_theme_fm(related=["[[single-writer-stub]]"]), "See [[single-writer-stub]].")
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))  # CRLF endings
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-16" in _codes(result.findings)


def test_l1_16_bom_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    path = layout.root / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    text = render(_theme_fm(related=["[[single-writer-stub]]"]), "See [[single-writer-stub]].")
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))  # UTF-8 BOM
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-16" in _codes(result.findings)


# --- L1-17 schema_version drift -------------------------------------------------------------------


def test_l1_17_repo_yaml_drift_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    kb = layout.kb_dir
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "repo.yaml").write_text("schema_version: 2\n", encoding="utf-8")
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-17" in _codes(result.findings)
    assert any(f.code == "L1-17" and f.path == "_kb/repo.yaml" for f in result.findings)


def test_l1_17_no_drift_ok(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    kb = layout.kb_dir
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "repo.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-17" not in _codes(result.findings)


def test_l1_17_schema_doc_header_in_sync_ok(tmp_path: Path) -> None:
    # The schema-doc-header branch of L1-17 (real AGENTS.md emitted by emit_schema): header v1 ==
    # canonical v1 must NOT fire L1-17 on the schema doc.
    layout = _valid_repo(tmp_path)
    emit_schema(layout, taxonomy=Taxonomy(schema_version=1))
    result = lint(layout, taxonomy=Taxonomy(schema_version=1), run_date=RUN_DATE)
    rel = layout.schema_file.relative_to(layout.root).as_posix()
    assert not any(f.code == "L1-17" and f.path == rel for f in result.findings)


def test_l1_17_schema_doc_header_drift_fails(tmp_path: Path) -> None:
    # The schema-doc-header branch of L1-17: emit a header saying 1 but lint with canonical 2 (the
    # taxonomy.yaml is also v2), so the schema-doc header drifts and L1-17 must fire on AGENTS.md.
    # This also guards the template/regex coupling — if the header wording drifts so the regex stops
    # matching, this FAIL test stops finding L1-17 and surfaces the break.
    layout = _valid_repo(tmp_path)
    emit_schema(layout, taxonomy=Taxonomy(schema_version=1))  # header says 1
    _write_taxonomy(layout, Taxonomy(schema_version=2))  # canonical becomes 2
    result = lint(layout, run_date=RUN_DATE)  # taxonomy (v2) read from disk
    rel = layout.schema_file.relative_to(layout.root).as_posix()
    assert any(f.code == "L1-17" and f.path == rel for f in result.findings)


# --- L1-19 origin not in inbox source enum --------------------------------------------------------


def test_l1_19_bad_origin_fails(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(origin="some-random-tool", related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-19" in _codes(result.findings)


def test_l1_19_harvest_origin_ok(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(origin="harvest:claude-code", related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-19" not in _codes(result.findings)


def test_l1_19_web_origin_ok(tmp_path: Path) -> None:
    # The parameterized web:<user> branch must pass (a non-empty parameter after the prefix).
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(origin="web:alice", related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-19" not in _codes(result.findings)


def test_l1_19_empty_parameter_origin_fails(tmp_path: Path) -> None:
    # A bare prefix with no parameter ("harvest:") must fail — locks the len(origin) > len(prefix)
    # guard so a regression accepting a parameterless harvest:/web: prefix is caught.
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(origin="harvest:", related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert "L1-19" in _codes(result.findings)


# --- determinism ----------------------------------------------------------------------------------


def test_findings_sorted_by_path_then_code(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    # Introduce two distinct failures in two files so ordering across (path, code) is observable.
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(status="verified", sources=[], related=[]),  # L1-7 + L1-11 on this path
        "body",
    )
    _write(
        layout,
        "wiki/ai-tech/themes/another.md",
        _theme_fm(title="Another", tags=["bogus-tag"], sources=["raw/ai-tech/2026-06-09-cqrs.md"]),
        "body",
    )
    # another.md is referenced nowhere — that is L2 orphan (derived), not L1, so fine for this test.
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    ordered = [(f.path, f.code) for f in result.findings]
    assert ordered == sorted(ordered)


def test_lint_is_pure_repeatable(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    _write(
        layout,
        "wiki/ai-tech/themes/curator-concurrency.md",
        _theme_fm(status="verified", related=["[[single-writer-stub]]"]),
        "See [[single-writer-stub]].",
    )
    first = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    second = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert first == second


def test_malformed_frontmatter_is_l1_reject(tmp_path: Path) -> None:
    layout = _valid_repo(tmp_path)
    (layout.root / "wiki" / "ai-tech" / "themes" / "broken.md").write_text(
        "no frontmatter here", encoding="utf-8"
    )
    result = lint(layout, taxonomy=_TAXONOMY, run_date=RUN_DATE)
    assert not result.ok
    assert any("broken.md" in f.path for f in result.findings)
