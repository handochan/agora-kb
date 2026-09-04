"""Tests for the OKF v0.1 producer frontmatter fields (ADR-0014 D2, increment 1).

ADR-0014 makes Agora a STRICT OKF *producer*: every curator-emitted note (and the `repo init` seed
index) is a conformant OKF v0.1 bundle node. Increment 1 is PURELY ADDITIVE — it adds frontmatter
fields without touching any link grammar, required-field rule, or the integrity gate. These tests
pin the additive contract:

* `timestamp` == `<updated>T00:00:00Z` EXACTLY — the deterministic, wall-clock-free
  datetime (ADR-0014 ratified decision #5 / ADR-0010 D1 replay determinism);
* `okf_version: '0.1'` appears ONLY on the bundle-root `index.md`, never on theme/daily/moc;
* `description` carries the SAME value as `summary` (ratified decision #2);
* a fresh `repo init` seed `index.md` is itself a conformant OKF bundle root;
* the OKF-augmented notes still lint CLEAN (the additive fields do not break the L1 gate).

Like `test_apply.py`, every plan is HAND-AUTHORED and applied with ZERO model in the loop, so the
assertions are over deterministic worker output.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agora_kb.config import KbIdentity, write_kb_identity
from agora_kb.core import Repo, frontmatter
from agora_kb.core.layout import RepoLayout
from agora_kb.curator.apply import apply_plan
from agora_kb.curator.plan import Disposition, Plan
from agora_kb.schema.emit import Taxonomy, emit_schema
from agora_kb.schema.lint import lint

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

RUN_ID = "2026-06-13T03-00-00.000Z--7f31ab"
RUN_DATE = "2026-06-13"
E1 = "2026-06-13T02-40-10.000Z--a1b2c3"
E2 = "2026-06-13T02-41-00.000Z--d4e5f6"

#: The fixture `_meta/kb.yaml` identity (ADR-0041 D1.5): APPLY stamps it into every note's `kb:`,
#: so it is FIXED rather than minted — a per-run id would make these frontmatter assertions
#: non-reproducible.
KB_ID = "01J8ZQ3M4N5P6Q7R8S9T0V1W2X"

TAXONOMY = Taxonomy(
    schema_version=2,
    taxonomy_policy="open",
    allowed_tags=("curator", "concurrency", "architecture"),
    domains=("ai-tech", "economy", "general"),
)


def _worktree(tmp_path: Path) -> Path:
    """A schema-2 worktree with the emitted schema + taxonomy and a populated raw/ for source refs.

    `_meta/kb.yaml` is written through the production writer: APPLY refuses to write any note when
    the identity file is missing (ADR-0041 D1.5), so a worktree without one is not a schema-2 repo.
    """
    layout = RepoLayout(tmp_path)
    layout.root.mkdir(parents=True, exist_ok=True)
    write_kb_identity(layout, KbIdentity(kb_id=KB_ID, name="agora-fixture"))
    emit_schema(layout, taxonomy=TAXONOMY, schema_version=2)
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


def _plan(*dispositions: Disposition) -> Plan:
    return Plan(schema_version=1, run_id=RUN_ID, finished=True, dispositions=tuple(dispositions))


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


def _append_daily(**overrides: object) -> Disposition:
    base: dict[str, object] = {
        "candidate_id": "d1",
        "event_ids": (E2,),
        "op": "APPEND_DAILY",
        "domain": "ai-tech",
        # ADR-0041 D2.6: ONE journal per run_date, repo-wide, BASENAMED by that date.
        "basename": RUN_DATE,
        "title": f"Daily {RUN_DATE}",
        "summary": "Daily consolidation.",
        "status": "active",
        "tags": (),
        "needs_prose": True,
        "reason": "Capture.",
    }
    base.update(overrides)
    return Disposition(**base)


def _apply_full_bundle(wt: Path) -> dict[str, dict[str, object]]:
    """Apply a CREATE_THEME (+ its lazily-created MAP + root index) and an APPEND_DAILY.

    Returns ``{note_kind: frontmatter}`` for the concept/journal/map/index so a single APPLY
    exercises every emitted note kind. The CREATE_THEME triggers the lazy map + index creation
    (ADR-0041 D1.3 — the map is created at the first concept of its subject).
    """
    plan = _plan(_create_theme(), _append_daily())
    prov = {**_provenance("c1", E1), **_provenance("d1", E2)}
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
    theme = wt / "wiki" / "concepts" / "curator-concurrency.md"
    daily = wt / "wiki" / "notes" / "2026" / "06" / f"{RUN_DATE}.md"
    moc = wt / "wiki" / "maps" / "ai-tech.md"
    index = wt / "index.md"
    return {
        "theme": frontmatter.parse(theme.read_text(encoding="utf-8"))[0],
        "daily": frontmatter.parse(daily.read_text(encoding="utf-8"))[0],
        "moc": frontmatter.parse(moc.read_text(encoding="utf-8"))[0],
        "index": frontmatter.parse(index.read_text(encoding="utf-8"))[0],
    }


# (a) timestamp == <updated> + "T00:00:00Z" exactly ---------------------------------------------


def test_timestamp_is_updated_at_midnight_utc_exactly(tmp_path: Path) -> None:
    # The OKF timestamp is the DETERMINISTIC <updated>T00:00:00Z (no wall clock, ADR-0010 D1): every
    # emitted note carries it, derived purely from updated (== run_date).
    wt = _worktree(tmp_path)
    fms = _apply_full_bundle(wt)
    for kind, fm in fms.items():
        assert fm["timestamp"] == f"{fm['updated']}T00:00:00Z", kind
        assert fm["timestamp"] == f"{RUN_DATE}T00:00:00Z", kind


# (a.2) timestamp stays in lock-step with updated across a SECOND run (UPDATE branches) ---------

RUN_ID_2 = "2026-06-20T03-00-00.000Z--9c44de"
RUN_DATE_2 = "2026-06-20"
E3 = "2026-06-20T02-40-10.000Z--aa11bb"
E4 = "2026-06-20T02-41-00.000Z--cc22dd"


def test_timestamp_re_derived_on_every_update_branch(tmp_path: Path) -> None:
    # The OKF invariant timestamp == <updated>T00:00:00Z must hold after a RE-TOUCH, not just at
    # CREATE: a stale timestamp (updated advances, timestamp frozen) breaks OKF "last meaningful
    # change". Run 1 creates the bundle; run 2 (a LATER run_date) re-touches the merge target
    # concept, the subject map and the root index via their UPDATE branches, and exercises the
    # JOURNAL's append branch by writing TWO APPEND_DAILY dispositions into the one journal of that
    # run date (ADR-0041 D2.6 — the journal is per run_date, repo-wide, so "append into an existing
    # journal" is a within-run-date fact now, not a cross-run one).
    wt = _worktree(tmp_path)

    # Run 1 — create the theme (+ lazy MOC + index) and the daily.
    _apply_full_bundle(wt)

    # Run 2 raw captures for the new provenance.
    for event in (E3, E4):
        raw = wt / "raw" / "ai-tech" / f"{event}.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f"raw capture {event}\n", encoding="utf-8")

    # Run 2 — two APPEND_DAILY dispositions land in ONE journal (the second takes the append
    # branch); MERGE_INTO_THEME re-touches the concept; a second concept keeps the map + index on
    # the create-or-update path.
    merge = Disposition(
        candidate_id="m2",
        event_ids=(E4,),
        op="MERGE_INTO_THEME",
        target_basename="curator-concurrency",
        summary="Corroborates the curator-concurrency theme.",
        status="active",
        links=(),
        needs_prose=False,
        reason="Overlaps curator-concurrency.",
    )
    plan2 = _plan(
        _append_daily(candidate_id="d2", event_ids=(E3,), basename=RUN_DATE_2),
        _append_daily(
            candidate_id="d3",
            event_ids=(E4,),
            basename=RUN_DATE_2,
            summary="A second section in the same journal.",
        ),
        merge,
        _create_theme(
            candidate_id="c2",
            event_ids=(E4,),
            basename="curator-locking-2",
            title="A second theme",
            summary="Forces MOC + index re-render on run 2.",
            aliases=(),
        ),
    )
    prov2 = {
        **_provenance("d2", E3),
        **_provenance("d3", E4),
        **_provenance("m2", E4),
        **_provenance("c2", E4),
    }
    apply_plan(plan2, worktree=wt, run_date=RUN_DATE_2, provenance=prov2)

    daily = wt / "wiki" / "notes" / "2026" / "06" / f"{RUN_DATE_2}.md"
    theme = wt / "wiki" / "concepts" / "curator-concurrency.md"
    moc = wt / "wiki" / "maps" / "ai-tech.md"
    index = wt / "index.md"
    re_touched = {
        "daily": frontmatter.parse(daily.read_text(encoding="utf-8"))[0],
        "merge-target": frontmatter.parse(theme.read_text(encoding="utf-8"))[0],
        "moc": frontmatter.parse(moc.read_text(encoding="utf-8"))[0],
        "index": frontmatter.parse(index.read_text(encoding="utf-8"))[0],
    }
    for kind, fm in re_touched.items():
        # updated advanced to run 2; timestamp moved WITH it (the invariant, not frozen at run 1).
        assert fm["updated"] == RUN_DATE_2, kind
        assert fm["timestamp"] == f"{fm['updated']}T00:00:00Z", kind
        assert fm["timestamp"] == f"{RUN_DATE_2}T00:00:00Z", kind


# (b) okf_version "0.1" on index ONLY -----------------------------------------------------------


def test_okf_version_appears_only_on_bundle_root_index(tmp_path: Path) -> None:
    # OKF marks the BUNDLE ROOT with its version; per the OKF spec (ADR-0014 D2) okf_version lives
    # on index.md alone — never on a concept/journal/map.
    wt = _worktree(tmp_path)
    fms = _apply_full_bundle(wt)
    assert fms["index"]["okf_version"] == "0.1"
    for kind in ("theme", "daily", "moc"):
        assert "okf_version" not in fms[kind], kind


# (c) description == summary --------------------------------------------------------------------


def test_description_mirrors_summary_on_every_note(tmp_path: Path) -> None:
    # description carries the SAME value as summary (ratified decision #2) on every emitted note.
    wt = _worktree(tmp_path)
    fms = _apply_full_bundle(wt)
    for kind, fm in fms.items():
        assert fm["description"] == fm["summary"], kind
    # And the values are the concrete plan/worker summaries (not empty / not swapped).
    assert (
        fms["theme"]["description"]
        == "One curator advances the curated branch under a per-repo lock."
    )
    assert fms["daily"]["description"] == "Daily consolidation."


# (d) a fresh repo init's seed index carries the OKF fields --------------------------------------


@requires_git
def test_fresh_repo_init_seed_index_is_okf_conformant(tmp_path: Path) -> None:
    # OKF conformance is the DEFAULT posture at repo init (ratified decision #4): the seed
    # index.md is itself a conformant OKF bundle root the moment the repo is initialized.
    target = tmp_path / "kb"
    repo = Repo.resolve(target)
    repo.init(schema_version=2, kb_id=KB_ID)
    fm, _ = frontmatter.parse(repo.layout.index_file.read_text(encoding="utf-8"))
    # `type:` survives ONLY as the OKF mirror of `kind` (ADR-0041 D2.5 / OD-3), which is exactly
    # what makes this test still about OKF conformance rather than about the retired authority.
    assert fm["kind"] == "index"
    assert fm["type"] == "index"
    assert fm["okf_version"] == "0.1"
    assert fm["description"] == fm["summary"]
    # timestamp == <created/updated date>T00:00:00Z — the repo-init date pinned to midnight UTC.
    assert str(fm["created"]) == str(fm["updated"])
    assert fm["timestamp"] == f"{fm['updated']}T00:00:00Z"


# (e) the OKF-augmented note still lints clean --------------------------------------------------


def test_okf_augmented_notes_lint_clean(tmp_path: Path) -> None:
    # The additive OKF fields are OPTIONAL/unknown to lint (no new L1 rule, no required-field
    # change): the full emitted bundle (concept + journal + lazy map + index) lints CLEAN.
    wt = _worktree(tmp_path)
    _apply_full_bundle(wt)
    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]
