"""Regression: cross-run body-sentinel id collisions (the integrity-critical dogfood bug).

Body-sentinel regions are keyed by the PER-RUN ``candidate_id`` ("c1","c2",…), which
:func:`agora_kb.curator.bundle._dedup_tier2` reassigns every run. A ``MERGE_INTO_THEME`` or a
cross-run ``APPEND_DAILY`` appends a NEW region to a note that may ALREADY hold a region with the
SAME bare candidate_id from a PRIOR run — producing two identical ``agora:body:start id=c1`` markers
in ONE note. Live, that:

* made :func:`agora_kb.curator.apply._extract_sentinel_regions` return ``None`` ("tampering"), so
  :func:`validate_author_diff` rejected the note and the worker CLOBBERED the prior run's already
  published prose back to ``> _summary pending_``; and
* still PUBLISHED with the duplicated sentinels, because the §4.4 lint had NO sentinel-integrity
  check (ADR-0011 §4.4 check 6 was unimplemented).

The fix makes the PERSISTED region id RUN-SCOPED via
:func:`agora_kb.curator.apply.region_sentinel_id` (``{run_id}--{candidate_id}``) — globally unique
across runs — and ADDS the missing lint check as L1-20. These tests reproduce the bug's exact
shape with ZERO model in the loop (hand-authored plans + ``apply_plan`` over a tmp worktree) and
assert it can no longer occur.
"""

from __future__ import annotations

from pathlib import Path

from agora_kb.core import frontmatter
from agora_kb.core.layout import RepoLayout
from agora_kb.curator.apply import (
    _extract_sentinel_regions,
    apply_plan,
    body_sentinels,
    region_sentinel_id,
    validate_author_diff,
)
from agora_kb.curator.plan import Disposition, Plan
from agora_kb.schema.emit import Taxonomy, emit_schema
from agora_kb.schema.lint import lint

# Two DISTINCT, globally-unique run ids whose dispositions both get the per-run candidate_id "c1"
# (the exact cross-run collision that produced two identical id=c1 markers before the fix).
RUN_ID_A = "2026-06-13T03-00-00.000Z--aaaaaa"
RUN_ID_B = "2026-06-14T03-00-00.000Z--bbbbbb"
RUN_DATE_A = RUN_ID_A[:10]
RUN_DATE_B = RUN_ID_B[:10]
E1 = "2026-06-13T02-40-10.000Z--a1b2c3"
E2 = "2026-06-14T02-41-00.000Z--d4e5f6"

TAXONOMY = Taxonomy(
    schema_version=1,
    taxonomy_policy="open",
    allowed_tags=("curator", "concurrency", "architecture"),
    domains=("ai-tech", "economy", "general"),
)


def _worktree(tmp_path: Path) -> Path:
    """A repo worktree with the emitted schema + taxonomy and populated raw/ for source refs."""
    layout = RepoLayout(tmp_path)
    emit_schema(layout, taxonomy=TAXONOMY)
    for event in (E1, E2):
        raw = tmp_path / "raw" / "ai-tech" / f"{event}.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f"raw capture {event}\n", encoding="utf-8")
    return tmp_path


def _provenance(candidate_id: str, *event_ids: str) -> dict:
    return {
        candidate_id: [
            {
                "event_id": e,
                "source": "claude-code",
                "writer": "dochan",
                "cwd": "/tmp/psa",
                "raw_ref": None,
                "created": "2026-06-13T02-40-10.000Z",
            }
            for e in event_ids
        ]
    }


def _plan(run_id: str, *dispositions: Disposition) -> Plan:
    return Plan(schema_version=1, run_id=run_id, finished=True, dispositions=tuple(dispositions))


def _author_region(path: Path, sentinel_id: str, prose: str) -> None:
    """Fill the ``sentinel_id`` region body with ``prose`` in place (deterministic PASS-2 stand-in).

    Pure string surgery between the exact start/end markers (markers + frontmatter + out-of-region
    text byte-preserved), exactly as :class:`agora_kb.curator.worker.FakeBackend` does — so the
    authored note is a faithful, model-free PASS-2 output.
    """
    start, end = body_sentinels(sentinel_id)
    text = path.read_text(encoding="utf-8")
    si = text.index(start) + len(start)
    ei = text.index(end)
    path.write_text(f"{text[:si]}\n{prose}\n{text[ei:]}", encoding="utf-8")


# --- (a) cross-run MERGE into a CREATE_THEME body -----------------------------------------------


def test_cross_run_merge_keeps_distinct_ids_and_preserves_prior_prose(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    theme = wt / "wiki" / "ai-tech" / "themes" / "curator-concurrency.md"

    # RUN A: CREATE_THEME for candidate c1, authored with REAL prose.
    create = Disposition(
        candidate_id="c1",
        event_ids=(E1,),
        op="CREATE_THEME",
        domain="ai-tech",
        basename="curator-concurrency",
        title="Curator concurrency model",
        summary="One curator advances the curated branch under a per-repo lock.",
        status="active",
        tags=("curator", "concurrency"),
        aliases=(),
        links=(),
        needs_prose=True,
        reason="New concept.",
    )
    apply_plan(
        _plan(RUN_ID_A, create),
        worktree=wt,
        run_date=RUN_DATE_A,
        provenance=_provenance("c1", E1),
    )
    id_a = region_sentinel_id(RUN_ID_A, "c1")
    prior_prose = "The curator holds a per-repo flock while advancing the branch."
    _author_region(theme, id_a, prior_prose)
    # The prior region's EXACT body (between the markers) — the merge must never touch it.
    prior_regions = _extract_sentinel_regions(frontmatter.parse(theme.read_text())[1])
    assert prior_regions is not None
    prior_region_body = prior_regions[id_a]

    # RUN B: a NEW run whose candidate is ALSO "c1" MERGEs into that same theme. Pre-fix this placed
    # a SECOND id=c1 region; now it is run-scoped, so the ids differ.
    merge = Disposition(
        candidate_id="c1",
        event_ids=(E2,),
        op="MERGE_INTO_THEME",
        target_basename="curator-concurrency",
        summary="Adds flock detail.",
        status="active",
        links=(),
        needs_prose=True,
        reason="Overlaps curator-concurrency.",
    )
    apply_plan(
        _plan(RUN_ID_B, merge), worktree=wt, run_date=RUN_DATE_B, provenance=_provenance("c1", E2)
    )
    id_b = region_sentinel_id(RUN_ID_B, "c1")

    text = theme.read_text(encoding="utf-8")
    fm, body = frontmatter.parse(text)

    # TWO DISTINCT run-scoped sentinel ids in one note (the bare candidate_id would have collided).
    assert id_a != id_b
    start_a, _ = body_sentinels(id_a)
    start_b, _ = body_sentinels(id_b)
    assert start_a in body and start_b in body

    # No duplicate-id tampering: _extract_sentinel_regions returns both regions (NOT None).
    regions = _extract_sentinel_regions(body)
    assert regions is not None
    assert set(regions) == {id_a, id_b}
    # The prior region's body is preserved BYTE-FOR-BYTE (no clobber-to-pending).
    assert regions[id_a] == prior_region_body
    assert regions[id_a].strip() == prior_prose

    # The §4.2 validator accepts authoring the NEW region while the prior one is untouched (the
    # exact path that pre-fix degraded the prior prose). sentinels must list BOTH live region ids.
    old = text
    _author_region(theme, id_b, "Acquiring the flock is non-blocking; a held lock exits early.")
    new = theme.read_text(encoding="utf-8")
    rel = "wiki/ai-tech/themes/curator-concurrency.md"
    errors = validate_author_diff(
        changed_paths=[rel],
        per_file_old={rel: old},
        per_file_new={rel: new},
        sentinels={rel: {id_a, id_b}},
    )
    assert errors == []
    # Prior prose still intact after authoring the new region.
    assert prior_prose in new

    # The published tree lints clean — including the new L1-20 sentinel-integrity check.
    assert lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE_B).ok


# --- (b) cross-run APPEND_DAILY into the same daily file ----------------------------------------


def test_cross_run_append_daily_same_file_keeps_distinct_ids(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    daily = wt / "wiki" / "ai-tech" / "daily" / f"ai-tech-{RUN_DATE_A}.md"

    def _append(run_id: str, run_date: str, event_id: str) -> None:
        disp = Disposition(
            candidate_id="c1",
            event_ids=(event_id,),
            op="APPEND_DAILY",
            domain="ai-tech",
            basename=f"ai-tech-{RUN_DATE_A}",  # SAME daily file across runs (date-named, exempt)
            summary="Daily capture.",
            status="active",
            tags=(),
            aliases=(),
            links=(),
            needs_prose=True,
            reason="Capture.",
        )
        apply_plan(
            _plan(run_id, disp),
            worktree=wt,
            run_date=run_date,
            provenance=_provenance("c1", event_id),
        )

    # RUN A creates the daily with a c1 region; RUN B (also "c1") appends INTO the same file.
    _append(RUN_ID_A, RUN_DATE_A, E1)
    id_a = region_sentinel_id(RUN_ID_A, "c1")
    _author_region(daily, id_a, "Run A: a curator advanced the branch.")
    _append(RUN_ID_B, RUN_DATE_B, E2)
    id_b = region_sentinel_id(RUN_ID_B, "c1")

    body = daily.read_text(encoding="utf-8")
    assert id_a != id_b

    # Both regions present, distinct, and not a duplicated id.
    regions = _extract_sentinel_regions(frontmatter.parse(body)[1])
    assert regions is not None
    assert set(regions) == {id_a, id_b}
    # The prior run's authored prose survives the second run's append.
    assert "Run A: a curator advanced the branch." in body


# --- (c) the L1-20 lint check itself ------------------------------------------------------------


def _write_theme(wt: Path, basename: str, body: str) -> Path:
    fm = {
        "title": basename,
        "type": "theme",
        "aliases": [],
        "tags": ["architecture"],
        "created": RUN_DATE_A,
        "updated": RUN_DATE_A,
        "status": "active",
        "summary": "seed",
        "sources": [f"raw/ai-tech/{E1}.md"],
        "related": [],
        "confidence": "high",
    }
    path = wt / "wiki" / "ai-tech" / "themes" / f"{basename}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.render(fm, body), encoding="utf-8")
    return path


def test_lint_l1_20_fails_duplicate_sentinel_id(tmp_path: Path) -> None:
    # A note hand-authored with TWO IDENTICAL agora:body ids (the corrupted dogfood shape) must
    # FAIL the new L1-20 sentinel-integrity check.
    wt = _worktree(tmp_path)
    s, e = body_sentinels("c1")  # bare, COLLIDING id used twice
    body = f"{s}\nfirst\n{e}\n\n{s}\nsecond\n{e}"
    _write_theme(wt, "dup-sentinels", body)

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE_A)
    assert not result.ok
    dup = [
        f
        for f in result.findings
        if f.code == "L1-20" and f.path == "wiki/ai-tech/themes/dup-sentinels.md"
    ]
    assert dup, f"expected an L1-20 duplicate-sentinel finding, got {result.findings}"
    assert "duplicated" in dup[0].message


def test_lint_l1_20_passes_well_formed_multi_region_note(tmp_path: Path) -> None:
    # A well-formed note with TWO distinct run-scoped regions passes the L1-20 check.
    wt = _worktree(tmp_path)
    id_a = region_sentinel_id(RUN_ID_A, "c1")
    id_b = region_sentinel_id(RUN_ID_B, "c1")
    sa, ea = body_sentinels(id_a)
    sb, eb = body_sentinels(id_b)
    body = f"{sa}\nfirst prose\n{ea}\n\n{sb}\nsecond prose\n{eb}"
    _write_theme(wt, "multi-region", body)

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE_A)
    l1_20 = [f for f in result.findings if f.code == "L1-20"]
    assert l1_20 == [], f"unexpected L1-20 findings on a well-formed note: {l1_20}"


def test_lint_l1_20_fails_unmatched_start(tmp_path: Path) -> None:
    # An unmatched start (no closing end) is also sentinel tampering → L1-20.
    wt = _worktree(tmp_path)
    s, _ = body_sentinels(region_sentinel_id(RUN_ID_A, "c1"))
    _write_theme(wt, "unmatched", f"{s}\norphan prose with no end marker")

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE_A)
    l1_20 = [f for f in result.findings if f.code == "L1-20"]
    assert l1_20, f"expected an L1-20 unmatched-start finding, got {result.findings}"
    assert "unmatched" in l1_20[0].message
