"""Curator run manifest — ``_kb/processing/<run-id>/run.json`` (DATA-MODEL §5, ADR-0008 step 1).

The manifest is the lifecycle + recovery record for one transactional curator run (ADR-0008). It is
written under the per-repo lock the instant a FIFO snapshot is claimed (phase ``claimed``) and is
ATOMICALLY rewritten in place as the run advances ``claimed → applied → published → finalized``
(ADR-0011 §0 / DATA-MODEL §5). The claimed event files alongside it stay byte-for-byte unchanged
throughout; the manifest — never the events — carries the mutable state, so recovery (ADR-0011 §9)
is driven purely by ``(phase, prose_complete, published_runs, git ref)``.

This module owns only the typed model + its on-disk (de)serialization and discovery:

* :class:`RunManifest` — pydantic, ``extra='forbid'``, the EXACT DATA-MODEL §5 field set. ``phase``
  is the flat enum (DATA-MODEL §5 as amended by ADR-0011 §10) and ``prose_complete`` distinguishes
  the two recovery entry points at ``applied``.
* :func:`manifest_path` — the canonical ``processing/<run-id>/run.json`` path.
* :func:`write_manifest` — ATOMIC overwrite (``core.atomic_write_text``, ``exclusive=False``), since
  the same path is rewritten on every phase transition under the lock.
* :func:`read_manifest` / :func:`list_processing` — load one manifest / discover every in-flight run
  in a DETERMINISTIC order (run-id is time-sortable, so directory order == chronological order).

The model and its JSON are MODEL-FREE and fully unit-testable: round-trip + phase transitions are a
pure function of the on-disk text, with zero git/backend in the loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..core.atomicio import atomic_write_text
from ..core.layout import RepoLayout

__all__ = [
    "RunManifest",
    "manifest_path",
    "write_manifest",
    "read_manifest",
    "list_processing",
]

# The flat phase enum (DATA-MODEL §5 as amended by ADR-0011 §10): the orchestrator rewrites the
# manifest under the lock as claimed → applied → published → finalized. ``prose_complete`` (a bool)
# distinguishes the two recovery entry points at ``applied`` (ADR-0011 §9).
Phase = Literal["claimed", "applied", "published", "finalized"]


class RunManifest(BaseModel):
    """One curator run's ``run.json`` (DATA-MODEL §5).

    EXACTLY the §5 field set, in the §5 order. ``run_id`` is a time-sortable inbox-style id; the
    manifest is created at claim time (``phase='claimed'``, ``prose_complete=False``,
    ``published_commit=None``) and rewritten in place as the run advances. ``event_ids`` is the SOLE
    coverage universe for the run (ADR-0011 §4.1 check 2) and is byte-frozen once claimed.

    Frozen + ``extra='forbid'`` so the on-disk shape cannot drift and a stray/renamed field is a
    hard parse error rather than a silently-ignored no-op.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    base_commit: str
    event_ids: tuple[str, ...]
    phase: Phase = "claimed"
    prose_complete: bool = False
    schema_version: int = 1
    published_commit: str | None = None
    started: str

    def to_json(self) -> str:
        """Render the manifest as pretty JSON with a trailing newline (matches state.json form)."""
        return self.model_dump_json(indent=2) + "\n"


def manifest_path(layout: RepoLayout, run_id: str) -> Path:
    """Canonical manifest path ``_kb/processing/<run-id>/run.json`` (DATA-MODEL §5)."""
    return layout.processing_dir / run_id / "run.json"


def write_manifest(layout: RepoLayout, m: RunManifest) -> None:
    """ATOMICALLY (over)write ``processing/<run-id>/run.json``.

    Uses ``core.atomic_write_text`` with ``exclusive=False`` because the SAME path is rewritten on
    every phase transition under the curator lock (DATA-MODEL §5) — a phase advance must atomically
    replace the prior manifest, never fail on its existence. The run directory is created on demand.
    """
    path = manifest_path(layout, m.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, m.to_json(), exclusive=False)


def read_manifest(path: Path) -> RunManifest:
    """Load + validate one ``run.json`` from disk into a :class:`RunManifest`.

    Raises pydantic ``ValidationError`` on an unknown/extra/missing field and ``OSError`` if the
    file is absent — a corrupt manifest must surface, never be silently coerced (DATA-MODEL §5
    drives recovery, so a misread phase would risk double-publish/double-finalize).
    """
    return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def list_processing(layout: RepoLayout) -> list[RunManifest]:
    """Every in-flight run manifest under ``_kb/processing/``, in DETERMINISTIC chronological order.

    ``run_id`` is time-sortable (DATA-MODEL §10 inbox-id form), so sorting the
    ``processing/<run-id>/`` directory names lexicographically == sorting by start time. A
    processing directory with no readable ``run.json`` is skipped (it is not yet a claimed run).
    Drives startup recovery (ADR-0011 §9): the caller decides an action per manifest from
    ``(phase, prose_complete, published_runs)``.
    """
    base = layout.processing_dir
    if not base.exists():
        return []
    manifests: list[RunManifest] = []
    for run_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        path = run_dir / "run.json"
        if not path.is_file():
            continue
        manifests.append(read_manifest(path))
    return manifests
