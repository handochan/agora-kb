"""Curator state — ``_kb/state.json`` (DATA-MODEL §4).

Mutable engine state, owned by the curator and rewritten atomically **under the curator lock** (one
writer per repo, ADR-0002). It is a derived cache, NOT canonical knowledge: ``event_keys`` is
rebuildable from retained inbox events and ``published_runs`` from ``git log`` + ``_kb/processed/``
(invariant 1), so a lost ``state.json`` is recoverable via a deliberate rebuild rather than fatal.

Contents:
- ``last_run`` / ``last_commit`` — when the last consolidation ran and the commit it published.
- ``counters`` — cumulative ingested / merged / dropped / failed tallies (dashboard signal).
- ``event_keys`` — delivery-idempotency cache keyed ``"<writer>:<event_key>"`` → event id; the
  authoritative dedup happens here at claim time (the inbox write-path check is best-effort,
  ADR-0011).
- ``published_runs`` — ``run_id`` → published commit sha, so crash recovery finalizes a committed
  run without invoking the backend twice (DATA-MODEL §5 recovery).

:class:`StateStore` does load/save only; cross-process safety is the curator lock's job, so the
normal pattern is load → mutate in memory → save once at the end of a run.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from .atomicio import atomic_write_text
from .layout import RepoLayout

__all__ = ["Counters", "CuratorState", "StateStore"]


class Counters(BaseModel):
    """Cumulative consolidation tallies (DATA-MODEL §4)."""

    model_config = ConfigDict(extra="forbid")

    ingested: int = 0
    merged: int = 0
    dropped: int = 0
    failed: int = 0


class CuratorState(BaseModel):
    """In-memory view of ``_kb/state.json``. Mutate, then persist via :meth:`StateStore.save`."""

    model_config = ConfigDict(extra="forbid")

    last_run: datetime | None = None
    last_commit: str | None = None
    counters: Counters = Field(default_factory=Counters)
    event_keys: dict[str, str] = Field(default_factory=dict)
    published_runs: dict[str, str] = Field(default_factory=dict)

    @field_validator("last_run")
    @classmethod
    def _check_last_run(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("last_run must be timezone-aware (UTC)")
        return v if v is None else v.astimezone(UTC)

    @field_serializer("last_run")
    def _ser_last_run(self, v: datetime | None) -> str | None:
        # DATA-MODEL §4 form: 2026-06-13T03:00:12Z (second precision, explicit Z).
        return None if v is None else v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- idempotency cache ----------------------------------------------------------------------
    @staticmethod
    def _ek(writer: str, event_key: str) -> str:
        # writer is colon-free (validated upstream), so "<writer>:<key>" is unambiguous even if the
        # caller-scoped key itself contains ':'.
        return f"{writer}:{event_key}"

    def event_key_id(self, writer: str, event_key: str) -> str | None:
        """Return the event id previously recorded for this writer+event_key, or None."""
        return self.event_keys.get(self._ek(writer, event_key))

    def record_event_key(self, writer: str, event_key: str, event_id: str) -> None:
        """Record writer+event_key → event_id idempotently (the earliest write wins)."""
        self.event_keys.setdefault(self._ek(writer, event_key), event_id)

    # --- published-run ledger -------------------------------------------------------------------
    def is_published(self, run_id: str) -> bool:
        return run_id in self.published_runs

    def published_commit(self, run_id: str) -> str | None:
        return self.published_runs.get(run_id)

    def record_published_run(self, run_id: str, commit: str) -> None:
        self.published_runs[run_id] = commit

    # --- run bookkeeping ------------------------------------------------------------------------
    def mark_run(self, when: datetime, commit: str) -> None:
        """Record the timestamp + published commit of the run that just finished."""
        if when.tzinfo is None:
            raise ValueError("run timestamp must be timezone-aware (UTC)")
        self.last_run = when.astimezone(UTC)
        self.last_commit = commit

    def to_json(self) -> str:
        return self.model_dump_json(indent=2) + "\n"


class StateStore:
    """Atomic load/save of one repo's ``_kb/state.json`` (caller holds the curator lock)."""

    def __init__(self, layout: RepoLayout) -> None:
        self._layout = layout

    @property
    def path(self) -> Path:
        return self._layout.state_file

    def load(self) -> CuratorState:
        """Load state. A missing file returns a fresh empty state; a corrupt/invalid file RAISES
        (the curator rebuilds deliberately from git + events — ``load`` never silently discards
        ``published_runs``/``event_keys``, which would risk double-publish or duplicate events).
        """
        path = self._layout.state_file
        if not path.exists():
            return CuratorState()
        return CuratorState.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, state: CuratorState) -> None:
        """Atomically rewrite ``state.json`` (overwrite; durable). Caller must hold the lock."""
        path = self._layout.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, state.to_json(), exclusive=False)

    def update(self, mutate: Callable[[CuratorState], None]) -> CuratorState:
        """Convenience load → mutate → save (single process; the lock guards concurrency)."""
        state = self.load()
        mutate(state)
        self.save(state)
        return state
