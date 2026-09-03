"""Tests for the deterministic §3 APPLY + §4.2 AUTHOR-diff + §4.6 stray-link strip (ADR-0011).

The INGEST core is "success = a pure function of (plan, diff, manifest, lint)" — so every plan here
is HAND-AUTHORED and applied to a tmp worktree with ZERO model in the loop. We assert the EXACT
files / frontmatter / sentinels / MOC-children / contested callout APPLY produces, that the result
passes the deterministic lint (the SAME gate the worker runs, ADR-0011 §4.4), that §4.2 accepts a
clean PASS-2 body edit and rejects every tampering class, that §4.6 strips stray links while keeping
planned ones, and that APPLY is byte-deterministic (same plan -> same bytes).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.core import frontmatter
from agora_kb.core.layout import RepoLayout
from agora_kb.curator.apply import (
    DEFAULT_MAX_BODY_BYTES,
    ApplyError,
    apply_plan,
    body_sentinels,
    region_sentinel_id,
    strip_stray_wikilinks,
    validate_author_diff,
)
from agora_kb.curator.plan import Disposition, Plan
from agora_kb.schema.emit import Taxonomy, emit_schema
from agora_kb.schema.lint import lint
from agora_kb.schema.notes import body_link_basenames, child_bullets

RUN_ID = "2026-06-13T03-00-00.000Z--7f31ab"
RUN_DATE = "2026-06-13"
E1 = "2026-06-13T02-40-10.000Z--a1b2c3"
E2 = "2026-06-13T02-41-00.000Z--d4e5f6"
E3 = "2026-06-13T02-42-00.000Z--beef01"

TAXONOMY = Taxonomy(
    schema_version=1,
    taxonomy_policy="open",
    allowed_tags=("curator", "concurrency", "architecture"),
    domains=("ai-tech", "economy", "general"),
)


# --- worktree fixtures --------------------------------------------------------------------------


def _worktree(tmp_path: Path) -> Path:
    """A repo worktree with the emitted schema + taxonomy and a populated raw/ for source refs."""
    layout = RepoLayout(tmp_path)
    emit_schema(layout, taxonomy=TAXONOMY)
    # Persist the raw/ artifacts that APPLY's sources: union cites (ADR-0010 D3), so lint L1-8
    # (source path exists) passes on the produced themes.
    for event in (E1, E2, E3):
        raw = tmp_path / "raw" / "ai-tech" / f"{event}.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f"raw capture {event}\n", encoding="utf-8")
    return tmp_path


def _provenance(
    candidate_id: str,
    *event_ids: str,
    source: str = "claude-code",
    raw_ref: str | None = None,
    body: str | None = None,
) -> dict:
    return {
        candidate_id: [
            {
                "event_id": e,
                "source": source,
                "writer": "dochan",
                "cwd": "/tmp/psa",
                "raw_ref": raw_ref,
                "created": "2026-06-13T02-40-10.000Z",
                **({"body": body} if body is not None else {}),
            }
            for e in event_ids
        ]
    }


def _plan(*dispositions: Disposition, finished: bool = True) -> Plan:
    return Plan(
        schema_version=1, run_id=RUN_ID, finished=finished, dispositions=tuple(dispositions)
    )


def _create_theme(**overrides: object) -> Disposition:
    base: dict[str, object] = {
        "candidate_id": "c1",
        "event_ids": (E1,),
        "op": "CREATE_THEME",
        "domain": "ai-tech",
        "basename": "curator-concurrency",
        "title": "Curator concurrency model",
        "summary": "One curator advances the curated branch under a per-repo lock.",
        "status": "active",
        "tags": ("curator", "concurrency"),
        "aliases": ("single-curator model",),
        "links": (),
        "needs_prose": True,
        "reason": "New concept.",
    }
    base.update(overrides)
    return Disposition(**base)


# --- CREATE_THEME -------------------------------------------------------------------------------


def test_create_theme_writes_full_c2_frontmatter_and_sentinels(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    theme = wt / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    assert theme.is_file()
    fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))

    # Full ADR-0010 C2 theme frontmatter, all worker-materialized.
    assert fm["title"] == "Curator concurrency model"
    assert fm["type"] == "theme"
    assert fm["aliases"] == ["single-curator model"]
    assert fm["tags"] == ["curator", "concurrency"]
    assert str(fm["created"]) == RUN_DATE
    assert str(fm["updated"]) == RUN_DATE
    assert fm["status"] == "active"
    assert fm["summary"] == "One curator advances the curated branch under a per-repo lock."
    # sources is the provenance UNION the WORKER writes (raw/<domain>/<event_id>.md, D3).
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md"]
    assert fm["related"] == []
    assert fm["confidence"] == "high"
    assert fm["body_status"] == "pending"
    assert "origin" not in fm  # not harvested

    # Body sentinel pair keyed by the RUN-SCOPED region id (§3, region_sentinel_id).
    start, end = body_sentinels(region_sentinel_id(RUN_ID, "c1"))
    assert start in body and end in body
    assert body.index(start) < body.index(end)


def test_create_theme_produces_exact_bytes(tmp_path: Path) -> None:
    # Byte-exact golden: pins frontmatter KEY ORDER (ADR-0010 C2 shape), date quoting, the blank
    # line, and the sentinel start/placeholder/end each on their own line. A reordering, a dropped
    # placeholder line, or whitespace drift would all fail here (anchors the determinism test to a
    # KNOWN-correct output, not merely run-to-run stability).
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))
    theme = wt / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    cid = region_sentinel_id(RUN_ID, "c1")  # the run-scoped persisted id, {run_id}--c1
    expected = (
        "---\n"
        "title: Curator concurrency model\n"
        "type: theme\n"
        "aliases:\n"
        "- single-curator model\n"
        "tags:\n"
        "- curator\n"
        "- concurrency\n"
        f"created: '{RUN_DATE}'\n"
        f"updated: '{RUN_DATE}'\n"
        # OKF v0.1 (ADR-0014 D2): timestamp right after updated, description right after summary;
        # NO okf_version on a theme (bundle-root index.md only).
        f"timestamp: '{RUN_DATE}T00:00:00Z'\n"
        "status: active\n"
        "summary: One curator advances the curated branch under a per-repo lock.\n"
        "description: One curator advances the curated branch under a per-repo lock.\n"
        "sources:\n"
        f"- raw/ai-tech/{E1}.md\n"
        "related: []\n"
        "confidence: high\n"
        "body_status: pending\n"
        "---\n"
        "\n"
        f"<!-- agora:body:start id={cid} -->\n"
        "_summary pending_\n"
        f"<!-- agora:body:end id={cid} -->\n"
    )
    assert theme.read_text(encoding="utf-8") == expected


def test_create_theme_no_prose_has_no_sentinels_no_body_status(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme(needs_prose=False))
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))
    theme = wt / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert "body_status" not in fm
    assert "agora:body:start" not in body


def test_create_theme_harvest_origin_stamped(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    prov = _provenance("c1", E1, source="harvest:basic-memory")
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
    theme = wt / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["origin"] == "harvest:basic-memory"


def test_create_theme_with_link_materializes_related(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    # A stub target so the link resolves and lint passes.
    plan = _plan(
        _create_theme(
            candidate_id="c0",
            basename="single-writer-invariant",
            title="Single-writer invariant",
            summary="One writer.",
            tags=(),
            aliases=(),
            links=(),
            event_ids=(E2,),
        ),
        _create_theme(links=("single-writer-invariant",), event_ids=(E1,)),
    )
    prov = {**_provenance("c0", E2), **_provenance("c1", E1)}
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
    theme = wt / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["related"] == ["[[single-writer-invariant]]"]


def test_create_theme_updates_moc_and_index(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    moc = wt / "wiki" / "ai-tech" / "ai-tech-moc.md"
    assert moc.is_file()
    mfm, mbody = frontmatter.parse(moc.read_text(encoding="utf-8"))
    assert mfm["type"] == "moc"
    # `children:` frontmatter STAYS [[basename]] (ADR-0014 D3 / Obsidian Properties native).
    assert mfm["children"] == ["[[curator-concurrency]]"]
    # The BODY child bullet is a STANDARD MARKDOWN LINK `- [Title](themes/<base>.md)` (ADR-0014 D3):
    # link TEXT == the theme's title; relative path from the MOC's dir == `themes/<base>.md`.
    assert "- [Curator concurrency model](themes/curator-concurrency.md)" in mbody
    # No bare wikilink survives in the MOC body — the body graph link is markdown-only now.
    assert "[[curator-concurrency]]" not in mbody

    index = wt / "index.md"
    assert index.is_file()
    ifm, ibody = frontmatter.parse(index.read_text(encoding="utf-8"))
    assert ifm["type"] == "index"
    assert ifm["children"] == ["[[ai-tech-moc]]"]
    # The index→MOC bullet path is `wiki/<domain>/<domain>-moc.md` (relative from the repo root).
    assert "- [ai-tech MOC](wiki/ai-tech/ai-tech-moc.md)" in ibody
    assert "[[ai-tech-moc]]" not in ibody


def test_create_theme_result_passes_lint(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))
    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


@pytest.mark.parametrize(
    "title",
    [
        "Edge ] case",  # a bracket terminates _CHILD_BULLET_RE's link-text group early
        "line1\nline2",  # a newline is forbidden in the frozen link-text class
        "[fully] ]bracketed[",  # every bracket char must be sanitized out of the TEXT
    ],
)
def test_create_theme_with_breaking_title_still_round_trips_and_lints(
    tmp_path: Path, title: str
) -> None:
    # REGRESSION (ADR-0014 D3 / ADR-0010 D5 round-trip): a model-decided theme `title` may legally
    # contain `]`, `[` or a newline (all valid YAML scalars), but the FROZEN `_CHILD_BULLET_RE`
    # link-text class `[^\]\r\n]*` forbids them. Emitting such a title RAW into the MOC body bullet
    # would produce a `- [..](themes/<base>.md)` line the curator's OWN L1-6/L1-2 lint can no longer
    # parse, silently dropping the child from `child_bullets` / `body_link_basenames`. `_link_text`
    # sanitizes the TEXT (never the slug-constrained PATH), so emit->parse still recovers the
    # basename and the post-APPLY tree the curator just wrote lints clean.
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme(title=title))
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    # The MOC body bullet round-trips: emit->parse recovers the theme's basename despite the title.
    moc = wt / "wiki" / "ai-tech" / "ai-tech-moc.md"
    _, mbody = frontmatter.parse(moc.read_text(encoding="utf-8"))
    assert child_bullets(mbody) == {"curator-concurrency"}
    assert body_link_basenames(mbody) == ["curator-concurrency"]

    # And the whole post-APPLY tree lints clean (L1-6 declared==body-bullets, L1-2 no broken links).
    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


def _bare_worktree(tmp_path: Path) -> Path:
    """A repo worktree with the schema/taxonomy but NO pre-seeded raw/ (the engine writes raw/)."""
    layout = RepoLayout(tmp_path)
    emit_schema(layout, taxonomy=TAXONOMY)
    return tmp_path


# --- ADR-0010 D3: the engine materializes the cited raw/ free-text source -----------------------


def test_create_theme_materializes_cited_raw_source_from_body(tmp_path: Path) -> None:
    # The deterministic engine (APPLY) — never the model — persists the free-text capture at the
    # cited raw/<domain>/<event_id>.md from the provenance tuple's immutable body (ADR-0010 D3), so
    # the curated commit holds raw/ + wiki/ consistently and lint L1-8 passes. raw/ is NOT
    # pre-seeded here: APPLY must create it.
    wt = _bare_worktree(tmp_path)
    plan = _plan(_create_theme())
    body = "One curator advances the branch under a per-repo lock."
    apply_plan(
        plan,
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("c1", E1, body=body),
    )

    theme = wt / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    # The theme cites the engine-materialized raw/ path.
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md"]
    # The raw/ artifact was WRITTEN at exactly that path, with the immutable body content.
    raw = wt / "raw" / "ai-tech" / f"{E1}.md"
    assert raw.is_file()
    assert raw.read_text(encoding="utf-8") == body
    # The post-APPLY tree (incl. the materialized raw/) lints clean — L1-8 is satisfied.
    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


def test_materialized_raw_source_is_written_and_recorded_as_exact_bytes(tmp_path: Path) -> None:
    # _materialize_raw_source writes BYTES, not text (write_bytes, never write_text): text mode
    # would translate "\n" to os.linesep on write, so on a platform where that differs from "\n"
    # the file on disk would NOT equal the bytes the §4.0 final-diff gate compares against in
    # `raw_writes` (#85) — a mismatch that cannot be provoked on POSIX (os.linesep == "\n" there),
    # so this asserts the CONTRACT directly rather than relying on a platform-specific repro.
    wt = _bare_worktree(tmp_path)
    plan = _plan(_create_theme())
    body = "line one\nline two\n"
    raw_writes = apply_plan(
        plan,
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("c1", E1, body=body),
    )
    ref = f"raw/ai-tech/{E1}.md"
    assert raw_writes[ref] == (wt / ref).read_bytes() == body.encode("utf-8")


def test_raw_source_is_immutable_not_overwritten(tmp_path: Path) -> None:
    # The raw/ source is immutable: APPLY writes it ONCE and NEVER overwrites a pre-existing file
    # (ADR-0010 D3). A pre-seeded raw/ artifact keeps its original content even when the provenance
    # body differs (e.g. a re-run or a cross-domain merge citing the same ref).
    wt = _bare_worktree(tmp_path)
    raw = wt / "raw" / "ai-tech" / f"{E1}.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("original immutable capture\n", encoding="utf-8")

    plan = _plan(_create_theme())
    apply_plan(
        plan,
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("c1", E1, body="a DIFFERENT body that must not clobber"),
    )
    assert raw.read_text(encoding="utf-8") == "original immutable capture\n"


def test_upload_raw_ref_tuple_cites_ref_and_is_not_written(tmp_path: Path) -> None:
    # A tuple WITH a raw_ref is an UPLOAD already persisted by core.ingest at capture time: APPLY
    # cites that ref verbatim and does NOT (re)write it (only free-text captures without a raw_ref
    # are materialized from the body, ADR-0010 D3). The cited upload path is NOT created by APPLY.
    wt = _bare_worktree(tmp_path)
    upload_ref = "raw/ai-tech/2026-06-13-uploaded-doc.md"
    plan = _plan(_create_theme())
    apply_plan(
        plan,
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("c1", E1, raw_ref=upload_ref, body="ignored when raw_ref present"),
    )

    theme = wt / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    # Cites the upload ref, NOT the event-id free-text path.
    assert fm["sources"] == [upload_ref]
    # APPLY did NOT write the upload (core.ingest owns uploads) nor a free-text raw/<event_id>.md.
    assert not (wt / upload_ref).exists()
    assert not (wt / "raw" / "ai-tech" / f"{E1}.md").exists()


def test_bodyless_provenance_cites_path_but_skips_write(tmp_path: Path) -> None:
    # A tuple with neither raw_ref nor body (a hand-authored unit-test fixture) keeps today's
    # behavior: cite the raw/<domain>/<event_id>.md path but skip the file write. raw/ is NOT
    # pre-seeded, so the path is cited but no file is created (the engine has no body to persist).
    wt = _bare_worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    theme = wt / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md"]  # path still cited
    assert not (wt / "raw" / "ai-tech" / f"{E1}.md").exists()  # but no file written


def test_two_themes_same_domain_moc_lists_both_sorted(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(
        _create_theme(),
        _create_theme(
            candidate_id="c2",
            basename="cqrs",
            title="CQRS",
            summary="Command/query split.",
            tags=("architecture",),
            aliases=(),
            event_ids=(E2,),
        ),
    )
    prov = {**_provenance("c1", E1), **_provenance("c2", E2)}
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
    moc = wt / "wiki" / "ai-tech" / "ai-tech-moc.md"
    mfm, _ = frontmatter.parse(moc.read_text(encoding="utf-8"))
    assert mfm["children"] == ["[[cqrs]]", "[[curator-concurrency]]"]
    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


# --- APPEND_DAILY -------------------------------------------------------------------------------


def _append_daily(**overrides: object) -> Disposition:
    base: dict[str, object] = {
        "candidate_id": "d1",
        "event_ids": (E1,),
        "op": "APPEND_DAILY",
        "domain": "ai-tech",
        "basename": f"ai-tech-{RUN_DATE}",
        "title": f"Daily {RUN_DATE}",
        "summary": "Daily consolidation.",
        "status": "active",
        "tags": (),
        "needs_prose": True,
        "reason": "Capture.",
    }
    base.update(overrides)
    return Disposition(**base)


def test_append_daily_creates_dated_section(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(_append_daily())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("d1", E1))
    daily = wt / "wiki" / "ai-tech" / "daily" / f"ai-tech-{RUN_DATE}.md"
    assert daily.is_file()
    fm, body = frontmatter.parse(daily.read_text(encoding="utf-8"))
    assert fm["type"] == "daily"
    assert str(fm["date"]) == RUN_DATE
    assert fm["run_id"] == RUN_ID
    assert f"## {RUN_DATE}" in body
    start, end = body_sentinels(region_sentinel_id(RUN_ID, "d1"))
    assert start in body and end in body


def test_two_daily_dispositions_one_file_stable_order(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    # d2's first event (E3) sorts AFTER d1's (E1); §3.1 orders sections by first event_id.
    plan = _plan(
        _append_daily(candidate_id="d2", event_ids=(E3,), summary="second"),
        _append_daily(candidate_id="d1", event_ids=(E1,), summary="first"),
    )
    prov = {**_provenance("d1", E1), **_provenance("d2", E3)}
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
    daily = wt / "wiki" / "ai-tech" / "daily" / f"ai-tech-{RUN_DATE}.md"
    fm, body = frontmatter.parse(daily.read_text(encoding="utf-8"))
    # Both sentinel regions present; d1's section appears before d2's (E1 < E3).
    start_d1, _ = body_sentinels(region_sentinel_id(RUN_ID, "d1"))
    start_d2, _ = body_sentinels(region_sentinel_id(RUN_ID, "d2"))
    assert body.index(start_d1) < body.index(start_d2)
    # sources unioned across both dispositions.
    assert f"raw/ai-tech/{E1}.md" in fm["sources"]
    assert f"raw/ai-tech/{E3}.md" in fm["sources"]


def test_append_daily_cross_run_preserves_prior_run_id(tmp_path: Path) -> None:
    # A daily already committed by a PRIOR run (different run_id + a prior sentinel region): a new
    # APPEND_DAILY adds a section, unions sources, bumps updated, but must PRESERVE the prior run_id
    # (apply.py keeps run_id on the append branch). Guards against a regression overwriting it.
    wt = _worktree(tmp_path)
    daily = wt / "wiki" / "ai-tech" / "daily" / f"ai-tech-{RUN_DATE}.md"
    daily.parent.mkdir(parents=True, exist_ok=True)
    prior_start, prior_end = body_sentinels("d0")
    prior_fm = {
        "title": f"Daily {RUN_DATE}",
        "type": "daily",
        "aliases": [],
        "tags": [],
        "created": RUN_DATE,
        "updated": RUN_DATE,
        "status": "active",
        "summary": "prior consolidation",
        "date": RUN_DATE,
        "run_id": "prior-run",
        "sources": [f"raw/ai-tech/{E1}.md"],
    }
    prior_body = f"## {RUN_DATE}\n\n{prior_start}\nprior prose\n{prior_end}"
    daily.write_text(frontmatter.render(prior_fm, prior_body), encoding="utf-8")

    apply_plan(
        _plan(_append_daily(candidate_id="d1", event_ids=(E2,))),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("d1", E2),
    )
    fm, body = frontmatter.parse(daily.read_text(encoding="utf-8"))
    assert fm["run_id"] == "prior-run"  # NOT overwritten
    assert str(fm["updated"]) == RUN_DATE  # bumped
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]  # unioned
    assert prior_start in body and prior_end in body  # prior region preserved
    new_start, _ = body_sentinels(region_sentinel_id(RUN_ID, "d1"))
    assert new_start in body  # new region appended


def test_append_daily_no_prose_places_no_region_and_no_flag(tmp_path: Path) -> None:
    """#131: a disposition nobody will author must not leave a region nobody will fill.

    APPLY placed the region unconditionally while ``worker._needs_prose_map`` skips
    ``needs_prose=False`` dispositions, so the region was built and then orphaned — a permanent
    ``_summary pending_`` in a published note. The region, the dated heading and the
    ``body_status`` stamp are now ONE decision.
    """
    wt = _worktree(tmp_path)
    plan = _plan(_append_daily(needs_prose=False))
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("d1", E1))

    daily = wt / "wiki" / "ai-tech" / "daily" / f"ai-tech-{RUN_DATE}.md"
    assert daily.is_file(), "the daily is still created — only its body section is withheld"
    fm, body = frontmatter.parse(daily.read_text(encoding="utf-8"))

    assert "body_status" not in fm
    assert "agora:body" not in body, "no sentinel region"
    assert f"## {RUN_DATE}" not in body, "the dated heading is the section's first line, not a peer"
    assert body.strip() == ""
    # The bytes are the shape _apply_create_theme already writes for a no-prose theme.
    assert daily.read_text(encoding="utf-8").endswith("---\n\n\n")

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


def test_append_daily_no_prose_still_records_provenance(tmp_path: Path) -> None:
    """The PROVENANCE half of the op is unconditional — this is not a silent DROP.

    ``_sources_union`` sits OUTSIDE the ``needs_prose`` gate on purpose: withholding the section
    must never discard the capture. If a future edit "simplifies" a no-prose APPEND_DAILY into a
    DROP, this is the test that fails.
    """
    wt = _worktree(tmp_path)
    plan = _plan(_append_daily(needs_prose=False))
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("d1", E1))

    daily = wt / "wiki" / "ai-tech" / "daily" / f"ai-tech-{RUN_DATE}.md"
    fm, _ = frontmatter.parse(daily.read_text(encoding="utf-8"))
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md"]
    assert (wt / "raw" / "ai-tech" / f"{E1}.md").is_file()


def test_append_daily_no_prose_append_branch_preserves_the_body_byte_for_byte(
    tmp_path: Path,
) -> None:
    """The append-to-existing branch: a provenance-only append must not touch prior prose.

    Also the trailing-whitespace answer the issue asked for — with no section there is nothing to
    join, so ``prior_body`` is carried through unchanged rather than gaining a blank separator.
    """
    wt = _worktree(tmp_path)
    daily = wt / "wiki" / "ai-tech" / "daily" / f"ai-tech-{RUN_DATE}.md"
    daily.parent.mkdir(parents=True, exist_ok=True)
    prior_start, prior_end = body_sentinels("d0")
    prior_fm = {
        "title": f"Daily {RUN_DATE}",
        "type": "daily",
        "aliases": [],
        "tags": [],
        "created": RUN_DATE,
        "updated": RUN_DATE,
        "status": "active",
        "summary": "prior consolidation",
        "date": RUN_DATE,
        "run_id": "prior-run",
        "sources": [f"raw/ai-tech/{E1}.md"],
    }
    prior_body = f"## {RUN_DATE}\n\n{prior_start}\nprior prose\n{prior_end}"
    daily.write_text(frontmatter.render(prior_fm, prior_body), encoding="utf-8")

    apply_plan(
        _plan(_append_daily(candidate_id="d1", event_ids=(E2,), needs_prose=False)),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("d1", E2),
    )
    fm, body = frontmatter.parse(daily.read_text(encoding="utf-8"))

    assert body == prior_body, "the existing body is carried through untouched"
    assert "body_status" not in fm, "no flag for a section that was never placed"
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]  # still unioned
    assert str(fm["updated"]) == RUN_DATE  # still bumped
    assert fm["run_id"] == "prior-run"  # still preserved


def test_append_daily_with_prose_bytes_are_unchanged(tmp_path: Path) -> None:
    """The half that must NOT move: pin the ``needs_prose=True`` bytes exactly (#131 lands
    before the ``v0.1.0b1`` tag precisely because it touches APPLY's output shape)."""
    wt = _worktree(tmp_path)
    apply_plan(
        _plan(_append_daily()),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("d1", E1),
    )
    daily = wt / "wiki" / "ai-tech" / "daily" / f"ai-tech-{RUN_DATE}.md"
    start, end = body_sentinels(region_sentinel_id(RUN_ID, "d1"))
    _, body = frontmatter.parse(daily.read_text(encoding="utf-8"))
    assert body == f"## {RUN_DATE}\n\n{start}\n_summary pending_\n{end}"


# --- MERGE_INTO_THEME ---------------------------------------------------------------------------


def _seed_theme(wt: Path, basename: str, *, sources: list[str], body: str = "Prior prose.") -> None:
    fm = {
        "title": basename,
        "type": "theme",
        "aliases": [],
        "tags": ["architecture"],
        "created": RUN_DATE,
        "updated": RUN_DATE,
        "status": "active",
        "summary": "seed",
        "sources": sources,
        "related": [],
        "confidence": "high",
    }
    path = wt / "wiki" / "ai-tech" / "themes" / f"{basename}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def test_merge_unions_sources_and_appends_sub_region(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="Existing CQRS prose.")
    disp = Disposition(
        candidate_id="m1",
        event_ids=(E2,),
        op="MERGE_INTO_THEME",
        target_basename="cqrs",
        summary="Adds flock detail.",
        status="active",
        links=(),
        needs_prose=True,
        reason="Overlaps cqrs.",
    )
    apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2))
    theme = wt / "wiki" / "ai-tech" / "themes" / "cqrs.md"
    fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
    # sources unioned (prior kept, new added).
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]
    # prior prose preserved; a NEW sentinel sub-region appended below it.
    assert "Existing CQRS prose." in body
    start, _ = body_sentinels(region_sentinel_id(RUN_ID, "m1"))
    assert start in body
    assert body.index("Existing CQRS prose.") < body.index(start)


def test_merge_no_prose_only_unions_sources(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="Existing prose.")
    disp = Disposition(
        candidate_id="m1",
        event_ids=(E2,),
        op="MERGE_INTO_THEME",
        target_basename="cqrs",
        summary="corroborate",
        needs_prose=False,
        reason="corroborate",
    )
    apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2))
    theme = wt / "wiki" / "ai-tech" / "themes" / "cqrs.md"
    fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]
    assert "agora:body:start" not in body  # no augmentation region when no prose


def test_merge_harvest_candidate_stamps_origin(tmp_path: Path) -> None:
    # MERGE_INTO_THEME is the only op a gated/harvested candidate may use to ADD content (§6), so a
    # harvested merge MUST tag origin: harvest:<agent> for loop-prevention (DATA-MODEL §7).
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="Existing prose.")
    disp = Disposition(
        candidate_id="m1",
        event_ids=(E2,),
        op="MERGE_INTO_THEME",
        target_basename="cqrs",
        summary="corroborate",
        needs_prose=False,
        reason="corroborate",
    )
    prov = _provenance("m1", E2, source="harvest:basic-memory")
    apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=prov)
    theme = wt / "wiki" / "ai-tech" / "themes" / "cqrs.md"
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["origin"] == "harvest:basic-memory"


def test_merge_non_harvest_leaves_origin_untouched(tmp_path: Path) -> None:
    # A non-harvest merge must NOT add an origin tag (origin is present iff a provenance source is
    # harvest:<agent>, ADR-0010); the seed theme has none, so none should appear.
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="Existing prose.")
    disp = Disposition(
        candidate_id="m1",
        event_ids=(E2,),
        op="MERGE_INTO_THEME",
        target_basename="cqrs",
        summary="corroborate",
        needs_prose=False,
        reason="corroborate",
    )
    apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2))
    theme = wt / "wiki" / "ai-tech" / "themes" / "cqrs.md"
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert "origin" not in fm


def test_merge_rejects_daily_target(tmp_path: Path) -> None:
    # MERGE_INTO_THEME is theme-scoped (§2 op table); a target_basename resolving to a daily note
    # must raise rather than mutate the daily (the §4.1 BASENAME check only verifies existence).
    wt = _worktree(tmp_path)
    daily = wt / "wiki" / "ai-tech" / "daily" / "ai-tech-2026-06-12.md"
    daily.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "title": "Daily",
        "type": "daily",
        "aliases": [],
        "tags": [],
        "created": RUN_DATE,
        "updated": RUN_DATE,
        "status": "active",
        "summary": "s",
        "date": "2026-06-12",
        "run_id": RUN_ID,
        "sources": [f"raw/ai-tech/{E1}.md"],
    }
    daily.write_text(frontmatter.render(fm, "## 2026-06-12"), encoding="utf-8")
    disp = Disposition(
        candidate_id="m1",
        event_ids=(E2,),
        op="MERGE_INTO_THEME",
        target_basename="ai-tech-2026-06-12",
        summary="merge",
        needs_prose=False,
        reason="merge",
    )
    with pytest.raises(ApplyError):
        apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2))


def test_create_theme_confidence_mirrors_candidate(tmp_path: Path) -> None:
    # confidence is MIRRORED from the candidate's worst-case value (ADR-0011 §2), NOT a literal
    # 'high'. A low-confidence candidate must materialize confidence: low so lint/dashboard surface
    # it (§6); the model can never inflate it because it is not a plan field.
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(
        plan,
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("c1", E1),
        confidence={"c1": "low"},
    )
    theme = wt / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["confidence"] == "low"


# --- MARK_CONTESTED -----------------------------------------------------------------------------


def test_mark_contested_renders_callout_and_frontmatter(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="The original CQRS claim.")
    # A competing note must exist for the [[competing]] link to resolve (lint L1-2).
    _seed_theme(wt, "event-sourcing", sources=[f"raw/ai-tech/{E3}.md"], body="Alt claim.")
    disp = Disposition(
        candidate_id="x1",
        event_ids=(E2,),
        op="MARK_CONTESTED",
        target_basename="cqrs",
        summary="Curator uses two writers, not one.",
        links=("event-sourcing",),
        needs_prose=False,
        reason="Contradiction.",
    )
    apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("x1", E2))
    theme = wt / "wiki" / "ai-tech" / "themes" / "cqrs.md"
    fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))

    # §2.1 frontmatter shape.
    assert fm["status"] == "contested"
    assert fm["contested_by"] == ["event-sourcing"]
    assert str(fm["contested_at"]) == RUN_DATE
    # >=2 sources (prior + new), kept BOTH.
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]
    # §2.1 callout: assert the EXACT contiguous 3-line block (not just substrings), pinning the
    # recorded-date line, the verbatim claim line, and the competing-link + sources line byte-exact.
    expected_block = (
        f"> [!contested] Competing claim (recorded {RUN_DATE})\n"
        "> Curator uses two writers, not one.\n"
        f"> — see [[event-sourcing]] · sources: raw/ai-tech/{E2}.md"
    )
    assert expected_block in body
    # prior prose preserved, callout appended below it.
    assert "The original CQRS claim." in body
    assert body.index("The original CQRS claim.") < body.index(expected_block)

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


def test_mark_contested_empty_links_raises(tmp_path: Path) -> None:
    # A MARK_CONTESTED with empty links carries no competing basename: contested_by would be empty
    # (un-publishable per lint L1-10) and the callout would self-reference the target. APPLY treats
    # this as a precondition violation and raises rather than fabricating a self-link.
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="The original claim.")
    disp = Disposition(
        candidate_id="x1",
        event_ids=(E2,),
        op="MARK_CONTESTED",
        target_basename="cqrs",
        summary="Contradicts.",
        links=(),
        needs_prose=False,
        reason="Contradiction.",
    )
    with pytest.raises(ApplyError):
        apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("x1", E2))


# --- DROP / NOOP --------------------------------------------------------------------------------


def test_drop_and_noop_write_nothing(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    before = sorted(p.relative_to(wt).as_posix() for p in wt.rglob("*") if p.is_file())
    plan = _plan(
        Disposition(candidate_id="c1", event_ids=(E1,), op="DROP", reason="noise"),
        Disposition(candidate_id="c2", event_ids=(E2,), op="NOOP", reason="dup"),
    )
    prov = {**_provenance("c1", E1), **_provenance("c2", E2)}
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
    after = sorted(p.relative_to(wt).as_posix() for p in wt.rglob("*") if p.is_file())
    assert before == after  # no wiki edit


# --- determinism --------------------------------------------------------------------------------


def test_apply_is_byte_deterministic(tmp_path: Path) -> None:
    plan = _plan(
        _create_theme(),
        _create_theme(
            candidate_id="c2",
            basename="cqrs",
            title="CQRS",
            summary="split.",
            tags=("architecture",),
            aliases=(),
            event_ids=(E2,),
        ),
    )
    prov = {**_provenance("c1", E1), **_provenance("c2", E2)}

    def _run(root: Path) -> dict[str, str]:
        wt = _worktree(root)
        apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
        return {
            p.relative_to(wt).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(wt.rglob("*.md"))
            if p.is_file()
        }

    a = _run(tmp_path / "a")
    b = _run(tmp_path / "b")
    assert a == b


def test_create_theme_missing_domain_raises(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    # Bypass the §4.1 gate via model_construct to feed APPLY a malformed disposition.
    disp = Disposition.model_construct(
        candidate_id="c1",
        event_ids=(E1,),
        op="CREATE_THEME",
        domain=None,
        basename=None,
        title="x",
        summary="s",
        status="active",
        aliases=(),
        tags=(),
        links=(),
        needs_prose=True,
        reason="bad",
    )
    with pytest.raises(ApplyError):
        apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))


# --- §4.6 stray-wikilink stripping --------------------------------------------------------------


def test_strip_stray_wikilinks_strips_unplanned_keeps_planned() -> None:
    text = "See [[planned]] and also [[stray]] plus [[other|Display]]."
    out = strip_stray_wikilinks(text, allowed={"planned"})
    assert "[[planned]]" in out  # planned kept verbatim
    assert "[[stray]]" not in out and "stray" in out  # delimiters dropped, meaning kept
    # stray display token unwrapped to its inner text:
    assert "[[other|Display]]" not in out and "other|Display" in out


def test_strip_stray_wikilinks_keeps_planned_display_token() -> None:
    # Resolution keys off the basename left of '|', matching wikilinks() normalization.
    text = "[[planned|Nice Name]]"
    out = strip_stray_wikilinks(text, allowed={"planned"})
    assert out == "[[planned|Nice Name]]"


def test_strip_stray_wikilinks_is_byte_deterministic() -> None:
    text = "[[a]] [[b]] [[a]]"
    assert strip_stray_wikilinks(text, {"a"}) == strip_stray_wikilinks(text, {"a"})
    assert strip_stray_wikilinks(text, {"a"}) == "[[a]] b [[a]]"


def test_strip_stray_wikilinks_nested_brackets_no_survivor() -> None:
    from agora_kb.schema.notes import wikilinks

    # Doubled delimiters must not SYNTHESIZE a surviving link: a single pass would leave
    # [[victim]] from [[[[victim]]]]. The fixed-point loop guarantees no non-allowed key survives.
    out = strip_stray_wikilinks("[[[[victim]]]]", allowed=set())
    assert not (set(wikilinks(out)) - set())  # no surviving link at all

    out2 = strip_stray_wikilinks("prose [[x[[victim]]]] more", allowed=set())
    assert set(wikilinks(out2)) == set()  # neither 'xvictim' nor 'victim' survives


def test_strip_stray_wikilinks_nested_keeps_allowed() -> None:
    from agora_kb.schema.notes import wikilinks

    # An allowed key nested with a stray must keep ONLY the allowed link, no synthesized stray.
    out = strip_stray_wikilinks("[[stray[[planned]]]]", allowed={"planned"})
    surviving = set(wikilinks(out))
    assert surviving - {"planned"} == set()


# --- §4.2 AUTHOR-diff validation ----------------------------------------------------------------


def _note(fm_block: str, region_body: str, cid: str = "c1") -> str:
    start, end = body_sentinels(cid)
    return f"{fm_block}\n\n{start}\n{region_body}\n{end}\n"


_FM_BLOCK = (
    "---\ntitle: T\ntype: theme\ntags: []\nstatus: active\n"
    "summary: s\ncreated: '2026-06-13'\nupdated: '2026-06-13'\n"
    "sources:\n- raw/ai-tech/e1.md\nrelated: []\nconfidence: high\n"
    "body_status: pending\n---"
)


def test_author_diff_accepts_clean_body_edit() -> None:
    old = _note(_FM_BLOCK, "_summary pending_")
    new = _note(_FM_BLOCK, "The curator holds a per-repo flock while advancing the branch.")
    errors = validate_author_diff(
        changed_paths=["wiki/ai-tech/themes/t.md"],
        per_file_old={"wiki/ai-tech/themes/t.md": old},
        per_file_new={"wiki/ai-tech/themes/t.md": new},
        sentinels={"wiki/ai-tech/themes/t.md": {"c1"}},
    )
    assert errors == []


def test_author_diff_rejects_frontmatter_edit() -> None:
    old = _note(_FM_BLOCK, "_summary pending_")
    tampered_fm = _FM_BLOCK.replace("status: active", "status: deprecated")
    new = _note(tampered_fm, "_summary pending_")
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert any("frontmatter changed" in e for e in errors)


def test_author_diff_rejects_out_of_sentinel_edit() -> None:
    start, end = body_sentinels("c1")
    old = _note(_FM_BLOCK, "_summary pending_")
    # Inject prose OUTSIDE the sentinel region (after the end marker).
    new = old.rstrip("\n") + "\nrogue prose outside the region\n"
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert any("outside the sentinel" in e for e in errors)


def test_author_diff_rejects_log_md_change() -> None:
    errors = validate_author_diff(
        changed_paths=["log.md"],
        per_file_old={"log.md": "base log\n"},
        per_file_new={"log.md": "base log\ntampered\n"},
        sentinels={},
    )
    assert any("log.md changed" in e for e in errors)


def test_author_diff_rejects_unexpected_file() -> None:
    errors = validate_author_diff(
        changed_paths=["wiki/ai-tech/themes/other.md"],
        per_file_old={"wiki/ai-tech/themes/other.md": "x"},
        per_file_new={"wiki/ai-tech/themes/other.md": "y"},
        sentinels={"wiki/ai-tech/themes/t.md": {"c1"}},
    )
    assert any("not a declared needs_prose note" in e for e in errors)


def test_author_diff_rejects_oversized_body() -> None:
    old = _note(_FM_BLOCK, "_summary pending_")
    new = _note(_FM_BLOCK, "x" * (DEFAULT_MAX_BODY_BYTES + 1))
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert any("exceeds" in e for e in errors)


def test_author_diff_rejects_new_wikilink() -> None:
    old = _note(_FM_BLOCK, "_summary pending_")
    new = _note(_FM_BLOCK, "Now references [[some-other-theme]] which APPLY never linked.")
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert any("new wikilink" in e for e in errors)


def test_author_diff_rejects_sentinel_tampering() -> None:
    old = _note(_FM_BLOCK, "_summary pending_")
    start, end = body_sentinels("c1")
    # Delete the end marker -> unmatched start -> tampering.
    new = old.replace(end + "\n", "")
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert errors  # rejected (tampering or region-set mismatch)


def test_author_diff_rejects_missing_frontmatter() -> None:
    # A declared needs_prose note whose PASS-2 text lacks a proper '---' frontmatter fence is
    # rejected as missing/malformed frontmatter (apply.py _split_frontmatter_and_body branch).
    start, end = body_sentinels("c1")
    no_fm = f"not frontmatter\n\n{start}\n_summary pending_\n{end}\n"
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": no_fm},
        per_file_new={"t.md": no_fm},
        sentinels={"t.md": {"c1"}},
    )
    assert any("missing/malformed frontmatter" in e for e in errors)


def test_author_diff_rejects_embedded_fake_sentinel() -> None:
    # A model writing a NEW agora:body sentinel pair (each marker on its OWN line, the matched
    # grammar) INSIDE its prose region nests a foreign region -> tampering / a foreign region id.
    old = _note(_FM_BLOCK, "_summary pending_")
    fake_start, fake_end = body_sentinels("evil")
    new = _note(_FM_BLOCK, f"prose\n{fake_start}\nsmuggled\n{fake_end}\nmore")
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert errors  # rejected: an embedded sentinel pair is tampering / a foreign region


def test_author_diff_accepts_two_region_clean_edit() -> None:
    # A note with TWO declared regions {c1,c2}, both edited cleanly and nothing out-of-region
    # touched, validates clean — the multi-region accept path (CREATE wraps c1; a later MERGE
    # appended c2), confirming set(regions) == sentinels[path] holds for prior + this-run regions.
    s1, e1 = body_sentinels("c1")
    s2, e2 = body_sentinels("c2")
    old = f"{_FM_BLOCK}\n\n{s1}\n_summary pending_\n{e1}\n\n{s2}\n_summary pending_\n{e2}\n"
    new = f"{_FM_BLOCK}\n\n{s1}\nFirst region prose.\n{e1}\n\n{s2}\nSecond region prose.\n{e2}\n"
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1", "c2"}},
    )
    assert errors == []


def test_author_diff_end_to_end_from_apply_output(tmp_path: Path) -> None:
    # APPLY -> AUTHOR contract on REAL bytes: apply a CREATE_THEME(needs_prose), read the produced
    # file as base, simulate a prose edit inside the candidate region, and assert §4.2 accepts it.
    # This guards against APPLY's emitted frontmatter/sentinel format drifting from what the §4.2
    # validator expects (the hand-rolled _FM_BLOCK tests cannot catch such drift).
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))
    theme = wt / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"
    rel = "wiki/ai-tech/themes/curator-concurrency.md"
    old = theme.read_text(encoding="utf-8")
    new = old.replace(
        "_summary pending_", "The curator holds a per-repo flock while advancing the branch."
    )
    errors = validate_author_diff(
        changed_paths=[rel],
        per_file_old={rel: old},
        per_file_new={rel: new},
        sentinels={rel: {region_sentinel_id(RUN_ID, "c1")}},
    )
    assert errors == []
