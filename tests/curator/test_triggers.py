"""Tests for consolidation triggers (DESIGN §4, DATA-MODEL §3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agora_kb.curator.triggers import TriggerConfig, TriggerDecision, evaluate

NOW = datetime(2026, 6, 13, 3, 0, 0, tzinfo=UTC)


def _cfg(*, threshold: int = 10, idle_minutes: int = 30) -> TriggerConfig:
    return TriggerConfig(threshold=threshold, idle_minutes=idle_minutes)


# --- config -------------------------------------------------------------------------------------
def test_config_defaults_match_data_model() -> None:
    c = TriggerConfig()
    assert c.cron == "0 3 * * *"
    assert c.threshold == 10
    assert c.idle_minutes == 30


def test_config_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        TriggerConfig(threshold=10, idle_minutes=30, bogus=1)  # type: ignore[call-arg]


def test_config_rejects_threshold_below_one() -> None:
    with pytest.raises(ValidationError):
        TriggerConfig(threshold=0)


def test_config_rejects_negative_idle_minutes() -> None:
    # Symmetric guard to the threshold check: idle_minutes has Field(ge=0).
    with pytest.raises(ValidationError):
        TriggerConfig(idle_minutes=-1)


def test_config_accepts_zero_idle_minutes() -> None:
    # idle_minutes=0 is a supported, load-bearing setting ("fire immediately on any backlog").
    assert TriggerConfig(idle_minutes=0).idle_minutes == 0


def test_decision_is_frozen() -> None:
    d = TriggerDecision(should_run=False, reason="none")
    with pytest.raises(ValidationError):
        d.should_run = True  # type: ignore[misc]


# --- threshold ----------------------------------------------------------------------------------
def test_threshold_met_exactly() -> None:
    d = evaluate(
        inbox_depth=10,
        now=NOW,
        last_write=None,
        last_run=None,
        config=_cfg(),
    )
    assert d == TriggerDecision(should_run=True, reason="threshold")


def test_threshold_exceeded() -> None:
    d = evaluate(
        inbox_depth=99,
        now=NOW,
        last_write=None,
        last_run=None,
        config=_cfg(),
    )
    assert d.should_run is True
    assert d.reason == "threshold"


def test_threshold_just_below_does_not_fire_on_depth_alone() -> None:
    # Depth 9 (< 10), no idle/cron signal → none.
    d = evaluate(
        inbox_depth=9,
        now=NOW,
        last_write=None,
        last_run=None,
        config=_cfg(),
    )
    assert d == TriggerDecision(should_run=False, reason="none")


# --- idle ---------------------------------------------------------------------------------------
def test_idle_met() -> None:
    d = evaluate(
        inbox_depth=3,
        now=NOW,
        last_write=NOW - timedelta(minutes=30),  # exactly idle_minutes elapsed
        last_run=None,
        config=_cfg(),
    )
    assert d == TriggerDecision(should_run=True, reason="idle")


def test_idle_exceeded() -> None:
    d = evaluate(
        inbox_depth=1,
        now=NOW,
        last_write=NOW - timedelta(hours=5),
        last_run=None,
        config=_cfg(),
    )
    assert d.should_run is True
    assert d.reason == "idle"


def test_idle_not_yet_elapsed() -> None:
    # 29 minutes < idle_minutes (30) → no idle fire, no other signal → none.
    d = evaluate(
        inbox_depth=3,
        now=NOW,
        last_write=NOW - timedelta(minutes=29),
        last_run=None,
        config=_cfg(),
    )
    assert d == TriggerDecision(should_run=False, reason="none")


def test_idle_minutes_zero_fires_immediately_on_backlog() -> None:
    # idle_minutes=0 means "fire on any backlog the instant writing stops": with last_write == now
    # the elapsed window is timedelta(0) >= timedelta(minutes=0), so idle fires.
    d = evaluate(
        inbox_depth=1,
        now=NOW,
        last_write=NOW,  # zero elapsed since last write
        last_run=None,
        config=_cfg(idle_minutes=0),
    )
    assert d == TriggerDecision(should_run=True, reason="idle")


def test_idle_minutes_zero_empty_inbox_is_none() -> None:
    # Even with idle_minutes=0, an empty inbox must not fire (idle requires a backlog).
    d = evaluate(
        inbox_depth=0,
        now=NOW,
        last_write=NOW,
        last_run=None,
        config=_cfg(idle_minutes=0),
    )
    assert d == TriggerDecision(should_run=False, reason="none")


def test_idle_requires_backlog() -> None:
    # Empty inbox: even a long quiet period does not fire idle.
    d = evaluate(
        inbox_depth=0,
        now=NOW,
        last_write=NOW - timedelta(hours=10),
        last_run=None,
        config=_cfg(),
    )
    assert d == TriggerDecision(should_run=False, reason="none")


def test_idle_requires_known_last_write() -> None:
    # Backlog present and idle window would be satisfied, but last_write is unknown → no idle.
    d = evaluate(
        inbox_depth=3,
        now=NOW,
        last_write=None,
        last_run=None,
        config=_cfg(),
    )
    assert d == TriggerDecision(should_run=False, reason="none")


# --- cron ---------------------------------------------------------------------------------------
def test_cron_due_with_backlog_fires() -> None:
    d = evaluate(
        inbox_depth=2,
        now=NOW,
        last_write=NOW - timedelta(minutes=1),  # too recent for idle
        last_run=None,
        config=_cfg(),
        cron_due=True,
    )
    assert d == TriggerDecision(should_run=True, reason="cron")


def test_cron_due_without_backlog_is_noop() -> None:
    # Scheduled wake-up over an empty inbox must not invoke the backend.
    d = evaluate(
        inbox_depth=0,
        now=NOW,
        last_write=None,
        last_run=None,
        config=_cfg(),
        cron_due=True,
    )
    assert d == TriggerDecision(should_run=False, reason="none")


def test_cron_not_due_does_not_fire() -> None:
    d = evaluate(
        inbox_depth=2,
        now=NOW,
        last_write=NOW - timedelta(minutes=1),
        last_run=None,
        config=_cfg(),
        cron_due=False,
    )
    assert d == TriggerDecision(should_run=False, reason="none")


# --- precedence ---------------------------------------------------------------------------------
def test_threshold_beats_idle_and_cron() -> None:
    # All three signals satisfied; threshold must win.
    d = evaluate(
        inbox_depth=10,
        now=NOW,
        last_write=NOW - timedelta(hours=2),
        last_run=None,
        config=_cfg(),
        cron_due=True,
    )
    assert d.reason == "threshold"


def test_idle_beats_cron() -> None:
    # Below threshold, but both idle and cron satisfied; idle must win.
    d = evaluate(
        inbox_depth=3,
        now=NOW,
        last_write=NOW - timedelta(hours=2),
        last_run=None,
        config=_cfg(),
        cron_due=True,
    )
    assert d.reason == "idle"


# --- none ---------------------------------------------------------------------------------------
def test_none_when_no_signal() -> None:
    d = evaluate(
        inbox_depth=0,
        now=NOW,
        last_write=None,
        last_run=None,
        config=_cfg(),
        cron_due=False,
    )
    assert d == TriggerDecision(should_run=False, reason="none")


def test_last_run_does_not_affect_decision() -> None:
    # last_run is accepted but not consulted; passing it changes nothing.
    a = evaluate(
        inbox_depth=5,
        now=NOW,
        last_write=NOW - timedelta(minutes=10),
        last_run=None,
        config=_cfg(),
        cron_due=False,
    )
    b = evaluate(
        inbox_depth=5,
        now=NOW,
        last_write=NOW - timedelta(minutes=10),
        last_run=NOW - timedelta(days=1),
        config=_cfg(),
        cron_due=False,
    )
    assert a == b == TriggerDecision(should_run=False, reason="none")


# --- naive-datetime rejection -------------------------------------------------------------------
def test_naive_now_rejected() -> None:
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        evaluate(
            inbox_depth=1,
            now=datetime(2026, 6, 13, 3, 0, 0),  # naive
            last_write=None,
            last_run=None,
            config=_cfg(),
        )


def test_naive_last_write_rejected() -> None:
    with pytest.raises(ValueError, match="last_write must be timezone-aware"):
        evaluate(
            inbox_depth=1,
            now=NOW,
            last_write=datetime(2026, 6, 13, 2, 0, 0),  # naive
            last_run=None,
            config=_cfg(),
        )


def test_naive_last_run_rejected() -> None:
    with pytest.raises(ValueError, match="last_run must be timezone-aware"):
        evaluate(
            inbox_depth=1,
            now=NOW,
            last_write=None,
            last_run=datetime(2026, 6, 13, 2, 0, 0),  # naive
            config=_cfg(),
        )
