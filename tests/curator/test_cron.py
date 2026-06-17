"""Tests for the standard 5-field cron matcher (:mod:`agora_kb.curator.cron`).

Pure + deterministic: every instant is an explicit timezone-aware UTC datetime, so these never read
a wall clock. Covers parsing (``*``, values, ranges, lists, steps, dow ``7``→Sunday), the Vixie
dom/dow OR rule, ``is_cron_due`` window/last-run semantics, UTC evaluation, and error cases.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from agora_kb.curator.cron import CronError, cron_matches, is_cron_due, parse_cron


def _utc(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# --- parsing -----------------------------------------------------------------------------------


def test_parse_wildcards_are_unrestricted() -> None:
    s = parse_cron("* * * * *")
    assert s.minute.restricted is False
    assert s.dom.restricted is False and s.dow.restricted is False
    assert 0 in s.minute.values and 59 in s.minute.values


def test_parse_value_range_list_step() -> None:
    s = parse_cron("0,30 9-17 * * *")
    assert s.minute.values == {0, 30}
    assert s.hour.values == set(range(9, 18))
    assert s.minute.restricted is True
    step = parse_cron("*/15 * * * *")
    assert step.minute.values == {0, 15, 30, 45}


def test_parse_dow_seven_is_sunday() -> None:
    assert 0 in parse_cron("0 0 * * 7").dow.values  # 7 normalized to 0 (Sunday)
    assert 0 in parse_cron("0 0 * * 0").dow.values


@pytest.mark.parametrize(
    "bad",
    [
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "5-1 * * * *",
        "a * * * *",
        "",
    ],
)
def test_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(CronError):
        parse_cron(bad)


# --- cron_matches ------------------------------------------------------------------------------


def test_matches_daily_three_am() -> None:
    s = parse_cron("0 3 * * *")
    assert cron_matches(s, _utc(2026, 6, 17, 3, 0)) is True
    assert cron_matches(s, _utc(2026, 6, 17, 3, 1)) is False
    assert cron_matches(s, _utc(2026, 6, 17, 2, 0)) is False


def test_matches_evaluates_in_utc() -> None:
    s = parse_cron("0 3 * * *")
    # 03:00 in a +09:00 zone is 18:00 the previous day UTC → must NOT match the UTC 03:00 schedule.
    kst = timezone(timedelta(hours=9))
    assert cron_matches(s, datetime(2026, 6, 17, 3, 0, tzinfo=kst)) is False
    # 12:00 KST == 03:00 UTC → matches.
    assert cron_matches(s, datetime(2026, 6, 17, 12, 0, tzinfo=kst)) is True


def test_vixie_dom_dow_or_when_both_restricted() -> None:
    # "00:00 on the 1st OR on Monday": pick a date that is one but not the other to prove OR (not
    # AND). 2026-06-01 is itself a Monday.
    s = parse_cron("0 0 1 * 1")  # day-of-month 1 OR Monday
    assert cron_matches(s, _utc(2026, 6, 1, 0, 0)) is True  # the 1st (a Monday too)
    assert cron_matches(s, _utc(2026, 6, 8, 0, 0)) is True  # a Monday, not the 1st → OR matches
    assert cron_matches(s, _utc(2026, 6, 2, 0, 0)) is False  # Tue the 2nd → neither


def test_dom_only_restricted_ignores_dow() -> None:
    s = parse_cron("0 0 15 * *")  # the 15th, any weekday
    assert cron_matches(s, _utc(2026, 6, 15, 0, 0)) is True
    assert cron_matches(s, _utc(2026, 6, 16, 0, 0)) is False


# --- is_cron_due -------------------------------------------------------------------------------


def test_due_when_fire_elapsed_since_last_run() -> None:
    cron = "0 3 * * *"
    now = _utc(2026, 6, 17, 5, 0)  # past today's 03:00
    last = _utc(2026, 6, 16, 4, 0)  # before today's 03:00 fire
    assert is_cron_due(cron, now=now, last_run=last) is True


def test_not_due_when_already_ran_after_last_fire() -> None:
    cron = "0 3 * * *"
    now = _utc(2026, 6, 17, 5, 0)
    last = _utc(2026, 6, 17, 3, 30)  # already ran after today's 03:00 fire
    assert is_cron_due(cron, now=now, last_run=last) is False


def test_due_exactly_at_fire_minute() -> None:
    cron = "0 3 * * *"
    now = _utc(2026, 6, 17, 3, 0)
    assert is_cron_due(cron, now=now, last_run=_utc(2026, 6, 16, 3, 0)) is True


def test_due_with_no_last_run() -> None:
    # Never run: a fire within the window counts as due.
    assert is_cron_due("0 3 * * *", now=_utc(2026, 6, 17, 9, 0), last_run=None) is True


def test_every_15_minutes_fires_each_quarter_hour() -> None:
    cron = "*/15 * * * *"
    now = _utc(2026, 6, 17, 10, 31)  # most recent fire is 10:30
    assert is_cron_due(cron, now=now, last_run=_utc(2026, 6, 17, 10, 20)) is True
    assert is_cron_due(cron, now=now, last_run=_utc(2026, 6, 17, 10, 30)) is False


def test_no_fire_in_window_is_not_due() -> None:
    # Feb 30 never occurs → no fire ever; a tiny window must terminate and report not due.
    assert is_cron_due("0 0 30 2 *", now=_utc(2026, 6, 17, 0, 0), last_run=None, window_days=2) is (
        False
    )


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        is_cron_due("0 3 * * *", now=datetime(2026, 6, 17, 5, 0), last_run=None)  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        is_cron_due(
            "0 3 * * *",
            now=_utc(2026, 6, 17, 5, 0),
            last_run=datetime(2026, 6, 16, 3, 0),  # noqa: DTZ001
        )
