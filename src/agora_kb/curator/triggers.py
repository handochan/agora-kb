"""Consolidation triggers — when should a curator run fire (DESIGN §4, DATA-MODEL §3).

A repo's curator is woken by three independent signals, configured under ``curator.triggers`` in
``_kb/repo.yaml`` (DATA-MODEL §3):

- **cron** — a wall-clock schedule (e.g. ``"0 3 * * *"`` = 03:00 daily).
- **threshold** — inbox depth has reached ``threshold`` pending events.
- **idle** — no writes for ``idle_minutes`` while a backlog exists (``inbox_depth > 0``).

This module owns only the *policy* decision and is deliberately pure: it reads no wall clock and
parses no cron expression. All time is **injected** by the caller (the scheduler), and whether the
cron schedule is currently due is passed in as the ``cron_due`` boolean. That keeps the decision
fully deterministic and trivially testable; cron-expression parsing/matching is the scheduler's job.

Precedence (highest first) — exactly one reason wins, evaluated top to bottom:

1. ``threshold`` — fire when ``inbox_depth >= config.threshold``. The backlog is large enough that
   we should drain it regardless of timing; this is the strongest signal.
2. ``idle`` — fire when there *is* a backlog (``inbox_depth > 0``), we know the last write time, and
   at least ``idle_minutes`` have elapsed since it (``now - last_write >= idle_minutes``). The
   writer has gone quiet, so consolidating now is cheap and improves freshness.
3. ``cron`` — fire when the schedule is due (``cron_due``) **and** there is a backlog
   (``inbox_depth > 0``). A scheduled wake-up with nothing pending is a no-op, so we never run the
   (potentially paid) backend over an empty inbox.
4. ``none`` — otherwise do not run (``should_run = False``).

Because threshold is checked first, a met threshold always wins over idle and cron; a met idle wins
over cron. ``last_run`` is accepted for symmetry with the scheduler's state and future policies
(e.g. min-interval damping) but is not consulted by the current rules.

Note: DESIGN §4 treats the three triggers as an unordered OR of independent signals — *any* one
firing means run. The precedence above does not change that ``should_run`` (which is true iff at
least one signal fires); it exists solely to pick a single deterministic ``reason`` label for
diagnostics and routing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["TriggerConfig", "TriggerDecision", "TriggerReason", "evaluate"]

TriggerReason = Literal["cron", "threshold", "idle", "none"]


class TriggerConfig(BaseModel):
    """Curator trigger thresholds (``curator.triggers`` in ``_kb/repo.yaml``, DATA-MODEL §3).

    Defaults mirror the DATA-MODEL §3 example: 03:00-daily cron, a depth of 10, 30 idle minutes.
    """

    model_config = ConfigDict(extra="forbid")

    cron: str = "0 3 * * *"
    threshold: int = Field(default=10, ge=1)
    idle_minutes: int = Field(default=30, ge=0)


class TriggerDecision(BaseModel):
    """The outcome of :func:`evaluate`: whether to run and which signal won (the precedence)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    should_run: bool
    reason: TriggerReason


def _require_aware(value: datetime, name: str) -> None:
    # Match core conventions (models.py / state.py): reject naive datetimes outright so all time
    # comparisons here are unambiguous instants.
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")


def evaluate(
    *,
    inbox_depth: int,
    now: datetime,
    last_write: datetime | None,
    last_run: datetime | None,
    config: TriggerConfig,
    cron_due: bool = False,
) -> TriggerDecision:
    """Decide whether a consolidation run should fire, and why (DESIGN §4).

    Pure and deterministic: reads no wall clock and parses no cron expression. The scheduler injects
    ``now`` and computes ``cron_due`` (whether ``config.cron`` matches the current time) itself.

    Precedence (highest first): ``threshold`` > ``idle`` > ``cron`` > ``none`` (see module docstring
    for the full rules). Returns a frozen :class:`TriggerDecision`.

    Args:
        inbox_depth: Number of pending events in the inbox. Assumed non-negative (the caller's
            ``Inbox.depth()`` already guarantees this); it is not re-validated here, and a negative
            value can never make a signal fire, so it falls through to ``reason="none"``.
        now: Current instant; **must be timezone-aware**.
        last_write: Instant of the most recent inbox write, or ``None`` if nothing has ever been
            written. Must be timezone-aware when provided.
        last_run: Instant of the last consolidation, or ``None``. Must be timezone-aware when
            provided. Accepted for symmetry/future policy; not consulted by the current rules.
        config: The repo's :class:`TriggerConfig`.
        cron_due: Whether the cron schedule is currently due (computed by the scheduler).

    Raises:
        ValueError: If any provided datetime is naive (timezone-unaware).
    """
    _require_aware(now, "now")
    if last_write is not None:
        _require_aware(last_write, "last_write")
    if last_run is not None:
        _require_aware(last_run, "last_run")

    # 1. threshold — strongest signal: drain a large backlog regardless of timing.
    if inbox_depth >= config.threshold:
        return TriggerDecision(should_run=True, reason="threshold")

    # 2. idle — backlog present, writer has been quiet for >= idle_minutes.
    if (
        inbox_depth > 0
        and last_write is not None
        and (now - last_write) >= timedelta(minutes=config.idle_minutes)
    ):
        return TriggerDecision(should_run=True, reason="idle")

    # 3. cron — scheduled wake-up, but never run over an empty inbox.
    if cron_due and inbox_depth > 0:
        return TriggerDecision(should_run=True, reason="cron")

    # 4. none — nothing to do.
    return TriggerDecision(should_run=False, reason="none")
