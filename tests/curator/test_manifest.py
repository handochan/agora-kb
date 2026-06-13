"""Tests for the curator run manifest (DATA-MODEL §5, ADR-0008/0011).

The manifest is the lifecycle + recovery record for one transactional run. These tests are
MODEL-FREE and assert it is a pure function of its on-disk JSON: the EXACT §5 field set, a clean
round-trip through ``write_manifest``/``read_manifest``, the flat phase enum advancing
``claimed → applied → published → finalized`` as an ATOMIC in-place rewrite, and the deterministic
chronological order of ``list_processing``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agora_kb.core.layout import RepoLayout
from agora_kb.curator.manifest import (
    RunManifest,
    list_processing,
    manifest_path,
    read_manifest,
    write_manifest,
)

RUN_ID = "2026-06-13T03-00-00.000Z--7f31ab"
BASE = "705f4a4"
E1 = "2026-06-13T02-40-10.000Z--a1b2c3"
E2 = "2026-06-13T02-41-00.000Z--d4e5f6"
STARTED = "2026-06-13T03:00:00Z"


def _manifest(**overrides: object) -> RunManifest:
    base: dict[str, object] = {
        "run_id": RUN_ID,
        "base_commit": BASE,
        "event_ids": (E1, E2),
        "started": STARTED,
    }
    base.update(overrides)
    return RunManifest(**base)  # type: ignore[arg-type]


# --- model defaults + field set -----------------------------------------------------------------
def test_defaults_match_data_model_section_5() -> None:
    m = _manifest()
    assert m.phase == "claimed"
    assert m.prose_complete is False
    assert m.schema_version == 1
    assert m.published_commit is None
    # The exact §5 field set, no more, no less.
    assert set(m.model_dump().keys()) == {
        "run_id",
        "base_commit",
        "event_ids",
        "phase",
        "prose_complete",
        "schema_version",
        "published_commit",
        "started",
    }


def test_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        RunManifest(  # type: ignore[call-arg]
            run_id=RUN_ID,
            base_commit=BASE,
            event_ids=(E1,),
            started=STARTED,
            bogus=1,
        )


def test_is_frozen() -> None:
    m = _manifest()
    with pytest.raises(ValidationError):
        m.phase = "applied"  # type: ignore[misc]


def test_rejects_unknown_phase() -> None:
    with pytest.raises(ValidationError):
        RunManifest(
            run_id=RUN_ID,
            base_commit=BASE,
            event_ids=(E1,),
            started=STARTED,
            phase="bogus",  # type: ignore[arg-type]
        )


# --- JSON shape + round-trip --------------------------------------------------------------------
def test_to_json_is_valid_and_trailing_newline() -> None:
    text = _manifest().to_json()
    assert text.endswith("\n")
    data = json.loads(text)
    assert data["run_id"] == RUN_ID
    assert data["event_ids"] == [E1, E2]
    assert data["phase"] == "claimed"
    assert data["published_commit"] is None


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    m = _manifest()
    write_manifest(layout, m)
    assert manifest_path(layout, RUN_ID) == layout.processing_dir / RUN_ID / "run.json"
    assert read_manifest(manifest_path(layout, RUN_ID)) == m


def test_read_manifest_rejects_corrupt(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    path = manifest_path(layout, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError):
        read_manifest(path)


# --- phase transitions (atomic in-place rewrite) ------------------------------------------------
def test_phase_transitions_rewrite_in_place(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    path = manifest_path(layout, RUN_ID)

    write_manifest(layout, _manifest())
    assert read_manifest(path).phase == "claimed"

    # claimed -> applied (prose not yet authored)
    write_manifest(layout, _manifest(phase="applied", prose_complete=False))
    m = read_manifest(path)
    assert m.phase == "applied" and m.prose_complete is False

    # applied -> applied (prose_complete flips true after PASS 2)
    write_manifest(layout, _manifest(phase="applied", prose_complete=True))
    assert read_manifest(path).prose_complete is True

    # applied -> published (the CAS commit is recorded — the durable publish point)
    write_manifest(
        layout, _manifest(phase="published", prose_complete=True, published_commit="abc123")
    )
    m = read_manifest(path)
    assert m.phase == "published" and m.published_commit == "abc123"

    # published -> finalized
    write_manifest(
        layout, _manifest(phase="finalized", prose_complete=True, published_commit="abc123")
    )
    assert read_manifest(path).phase == "finalized"


def test_write_manifest_overwrites_not_appends(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    write_manifest(layout, _manifest())
    write_manifest(layout, _manifest(phase="applied"))
    # Atomic overwrite: exactly one well-formed manifest on disk, not a duplicated/appended file.
    assert read_manifest(manifest_path(layout, RUN_ID)).phase == "applied"
    # Non-tautological: the on-disk BYTES equal the single rendered JSON document (one object,
    # trailing newline) — an appended/duplicated doc would differ even if read_manifest parses the
    # first object. And the run dir holds exactly ONE file (run.json), with no leftover atomic temp.
    path = manifest_path(layout, RUN_ID)
    assert path.read_text(encoding="utf-8") == _manifest(phase="applied").to_json()
    assert [p.name for p in sorted(path.parent.iterdir())] == ["run.json"]


# --- list_processing (deterministic order) ------------------------------------------------------
def test_list_processing_empty_when_no_dir(tmp_path: Path) -> None:
    assert list_processing(RepoLayout(tmp_path)) == []


def test_list_processing_chronological_order(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    later = "2026-06-13T04-00-00.000Z--bbbbbb"
    earlier = "2026-06-13T02-00-00.000Z--aaaaaa"
    # Write out of chronological order; list_processing must return them sorted by run_id (time).
    write_manifest(layout, _manifest(run_id=later))
    write_manifest(layout, _manifest(run_id=earlier))
    write_manifest(layout, _manifest(run_id=RUN_ID))
    ids = [m.run_id for m in list_processing(layout)]
    assert ids == sorted([later, earlier, RUN_ID])


def test_list_processing_skips_dir_without_run_json(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    write_manifest(layout, _manifest())
    # A bare processing/<id>/ dir with no run.json is not yet a claimed run — skip it.
    (layout.processing_dir / "2026-06-13T05-00-00.000Z--cccccc" / "events").mkdir(parents=True)
    ids = [m.run_id for m in list_processing(layout)]
    assert ids == [RUN_ID]
