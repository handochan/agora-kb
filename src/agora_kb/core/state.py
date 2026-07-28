"""Curator state — ``_kb/state.json`` (DATA-MODEL §4).

Mutable engine state, owned by the curator and rewritten atomically **under the curator lock** (one
writer per repo, ADR-0002). It is a derived cache, NOT canonical knowledge: ``event_keys`` is
rebuildable from retained inbox events and ``published_runs`` from ``git log`` + ``_kb/processed/``
(invariant 1), so a lost ``state.json`` is recoverable via a deliberate rebuild rather than fatal.

Contents:
- ``last_run`` / ``last_commit`` — when the last consolidation ran and the commit it published.
- ``counters`` — cumulative ingested / merged / dropped / failed tallies (dashboard signal).
- ``last_batch`` — the last published run's claim/bundle shape (events claimed, tier-2 candidates,
  the ``max_candidates_per_run`` cap in effect, inbox depth left right after the claim) — the
  ADR-0024 §3 batch-observability signal the dashboard/metrics read (#60).
- ``last_attempt`` / ``last_failure`` — when the curator last CLAIMED work, and the last run that
  did NOT publish (issue #96). Together they are the only in-``state.json`` evidence of a failure
  INSIDE the §5.1 retry budget, which moves neither ``last_run`` nor ``counters.failed`` nor the
  inbox depth.
- ``event_keys`` — delivery-idempotency cache keyed ``"<writer>:<event_key>"`` → event id; the
  authoritative dedup happens here at claim time (the inbox write-path check is best-effort,
  ADR-0011).
- ``published_runs`` — ``run_id`` → published commit sha, so crash recovery finalizes a committed
  run without invoking the backend twice (DATA-MODEL §5 recovery).

:class:`StateStore` does load/save only; cross-process safety is the curator lock's job, so the
normal pattern is load → mutate in memory → save once at the end of a run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_serializer,
    field_validator,
)

from .atomicio import atomic_write_text
from .layout import RepoLayout

__all__ = ["Counters", "CuratorState", "LastBatch", "LastFailure", "StateStore"]

# How many reasons the state.json preview keeps. state.json is rewritten every run and re-read by
# `agora status`, `kb_status` and every /metrics scrape, so the preview must be O(1) in the size of
# the wiki: a LINT failure emits one reason per finding and a PLAN failure one per violated check —
# both unbounded. Each STRING is already capped at 400 chars upstream
# (curator.worker._bounded_reasons), so this caps the LIST ONLY and never re-clips — one truncation
# rule, and `reasons_total` therefore cannot lie. Worst case ≈ 2 KB.
MAX_FAILURE_REASONS = 5


class Counters(BaseModel):
    """Cumulative consolidation tallies (DATA-MODEL §4)."""

    model_config = ConfigDict(extra="forbid")

    ingested: int = 0
    merged: int = 0
    dropped: int = 0
    failed: int = 0


class LastBatch(BaseModel):
    """The last published run's claim/bundle shape (DATA-MODEL §4, ADR-0024 §3 / #60).

    Recorded by the worker at finalization so batch-size-vs-cap pressure is observable without
    re-reading run manifests: ``claimed`` events moved into the run, ``candidates`` after tier-2
    dedup (the capped unit), ``cap`` = the ``curator.limits.max_candidates_per_run`` in effect, and
    ``inbox_remaining`` = the queue depth left right after the claim (``claimed == candidates ==
    cap`` with a big remainder ⇒ the backlog is draining in capped slices). Plain ints only — this
    is core state, so it references no curator constant (the dependency stays core ← curator).
    """

    model_config = ConfigDict(extra="forbid")

    claimed: int = 0
    candidates: int = 0
    cap: int = 0
    inbox_remaining: int = 0


class LastFailure(BaseModel):
    """The most recent curator run that did NOT publish (DATA-MODEL §4, issue #96).

    The gap this closes: a failure INSIDE the §5.1 retry budget moves nothing an operator can see —
    ``last_run`` stays ``never`` (``mark_run`` fires only on publish), ``counters.failed`` bumps
    only at budget exhaustion, and the events return to ``inbox/`` so the depth is unchanged. This
    field is the ONLY place a non-terminal failure is visible without opening ``_kb/failed/``.

    ``reasons`` is a bounded PREVIEW; ``reasons_total`` is the FULL count so ``len(reasons)`` can
    never be mistaken for the whole; ``record_path`` is the repo-RELATIVE, POSIX-separated path of
    the run's ``error.json``, which holds the untruncated ``failed_checks``. Build via
    :meth:`from_run_failure` so the cap and the total can never disagree.

    NOT cleared by a later successful publish — it is a historical fact, like ``counters``. "Is it
    still broken?" is answered by :attr:`CuratorState.failure_is_current`. A CAS conflict is NOT a
    failure and never writes this (no budget burned, no error record written).
    """

    model_config = ConfigDict(extra="forbid")

    when: datetime
    run_id: str
    phase: str
    reasons: list[str] = Field(default_factory=list)
    reasons_total: int = Field(default=0, ge=0)
    record_path: str

    @field_validator("when")
    @classmethod
    def _check_when(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("last_failure.when must be timezone-aware (UTC)")
        return v.astimezone(UTC)

    @field_serializer("when")
    def _ser_when(self, v: datetime) -> str:
        # Same DATA-MODEL §4 form as last_run: 2026-06-13T03:00:12Z.
        return v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    @classmethod
    def from_run_failure(
        cls,
        *,
        when: datetime,
        run_id: str,
        phase: str,
        reasons: Sequence[str],
        record_path: str,
    ) -> LastFailure:
        """The SANCTIONED constructor. ``reasons`` MUST already be per-string bounded by
        ``curator.worker._bounded_reasons``; this caps the LIST and records the full count.

        Capping here (at construction), NOT in a validator, keeps load→save byte-idempotent — a
        mutating validator would re-truncate an already-truncated list and drift the file.
        """
        return cls(
            when=when,
            run_id=run_id,
            phase=phase,
            reasons=list(reasons[:MAX_FAILURE_REASONS]),
            reasons_total=len(reasons),
            record_path=record_path,
        )


class CuratorState(BaseModel):
    """In-memory view of ``_kb/state.json``. Mutate, then persist via :meth:`StateStore.save`."""

    model_config = ConfigDict(extra="forbid")

    last_run: datetime | None = None
    last_commit: str | None = None
    counters: Counters = Field(default_factory=Counters)
    # None ⇒ never recorded (a pre-#60 state.json loads unchanged — the field is additive) OR
    # cleared by crash-recovery finalize: a recovered run whose happy-path state save never landed
    # has an unknowable shape, so the worker clears the stale value rather than mislabel the
    # previous run's shape as the recovered run's (worker._finalize_recovered, same best-effort
    # posture as the un-replayed counters).
    last_batch: LastBatch | None = None
    # #96: failure observability. BOTH additive optionals — a pre-#96 state.json loads unchanged,
    # exactly like last_batch/#60. `last_attempt` is stamped once per run that actually CLAIMED
    # work — success, failure, or crash — so `last_attempt >> last_run` is the operator's "the
    # curator is trying and getting nowhere" signal.
    last_attempt: datetime | None = None
    last_failure: LastFailure | None = None
    event_keys: dict[str, str] = Field(default_factory=dict)
    published_runs: dict[str, str] = Field(default_factory=dict)

    @field_validator("last_run", "last_attempt")
    @classmethod
    def _check_timestamp(cls, v: datetime | None, info: ValidationInfo) -> datetime | None:
        # The message is built from the field name so BOTH stamps fail loudly in their own terms,
        # and "last_run must be timezone-aware (UTC)" stays byte-identical to the pre-#96 wording.
        if v is not None and v.tzinfo is None:
            raise ValueError(f"{info.field_name} must be timezone-aware (UTC)")
        return v if v is None else v.astimezone(UTC)

    @field_serializer("last_run", "last_attempt")
    def _ser_timestamp(self, v: datetime | None) -> str | None:
        # DATA-MODEL §4 form: 2026-06-13T03:00:12Z (second precision, explicit Z).
        return None if v is None else v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def failure_is_current(self) -> bool:
        """True iff :attr:`last_failure` has NOT been superseded by a later successful publish.

        ``last_failure`` is sticky, so this is what separates "currently broken" from "recovered".
        A never-published repo with a recorded failure is ALWAYS current — that is the #96
        criterion-7 state. Ties (failure and publish stamped at the same second) resolve to CURRENT:
        under-reporting a live failure is the disease #96 exists to cure, so the tie-break is
        fail-safe toward visibility.

        A plain @property, NOT a pydantic computed_field: it must never appear in state.json.
        """
        if self.last_failure is None:
            return False
        return self.last_run is None or self.last_failure.when >= self.last_run

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
