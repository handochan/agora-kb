"""Standard 5-field cron matching — compute ``cron_due`` for the curator scheduler (DESIGN §4).

:mod:`agora_kb.curator.triggers` is deliberately pure: it takes ``cron_due`` as an injected boolean
and never parses a cron expression. THIS module is the small, dependency-free (ADR-0005 OSS-pure, no
``croniter``) matcher the scheduler uses to compute that boolean from a repo's ``curator.triggers.
cron`` string and the current instant.

The grammar is the standard 5-field cron — ``minute hour day-of-month month day-of-week`` — with the
common syntax: ``*`` (any), ``N`` (a value), ``a-b`` (an inclusive range), ``*/n`` and ``a-b/n``
(steps), and ``a,b,c`` (lists). Day-of-week is ``0-6`` with ``0`` = Sunday; ``7`` is also
Sunday. Named months/days are NOT supported (the DATA-MODEL §3 default ``"0 3 * * *"`` is numeric).

**Day-of-month / day-of-week** follow the Vixie-cron rule: when BOTH fields are restricted (neither
is ``*``) a day matches if EITHER matches (OR); when only one is restricted, only that one must
match; when both are ``*`` every day matches.

**All evaluation is in UTC** (the curator reads no local clock; ADR-0010 D1): ``now`` is
converted to UTC before matching, so a cron schedule is interpreted against UTC wall-clock minutes.

:func:`is_cron_due` answers the scheduler's real question — "has a scheduled fire time elapsed since
the last run?" — by finding the most recent matching minute ``<= now`` and reporting it due iff it
is strictly after ``last_run`` (or there has never been a run). That makes the decision robust to a
COARSE poll interval: a daily 03:00 schedule still fires even if the scheduler only wakes every few
hours, and it fires AT MOST once per scheduled minute (a second poll in the same window is not due).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

__all__ = ["CronError", "CronSchedule", "parse_cron", "cron_matches", "is_cron_due"]

# (lo, hi) inclusive bounds per field: minute, hour, day-of-month, month, day-of-week.
_BOUNDS: tuple[tuple[int, int], ...] = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
# A scan window cap so a (mis)configured schedule that never matches terminates instead of looping
# forever; 366 days of minutes comfortably covers any real schedule (yearly being the rarest).
_DEFAULT_WINDOW_DAYS = 366


class CronError(ValueError):
    """A cron expression is malformed (wrong field count, out-of-range value, bad step/range).

    Raised by :func:`parse_cron` so the scheduler/CLI surfaces an operator typo in
    ``curator.triggers.cron`` as a clear message rather than silently never firing.
    """


@dataclass(frozen=True)
class _Field:
    """One parsed cron field: the allowed integer values + whether it was restricted (not ``*``)."""

    values: frozenset[int]
    restricted: bool


@dataclass(frozen=True)
class CronSchedule:
    """A parsed 5-field cron expression, ready for :func:`cron_matches`."""

    minute: _Field
    hour: _Field
    dom: _Field
    month: _Field
    dow: _Field


def _parse_field(spec: str, lo: int, hi: int, *, name: str, dow: bool = False) -> _Field:
    """Parse one comma-list cron field into its allowed-value set (``*`` → the full ``lo..hi``).

    Supports ``*``, ``N``, ``a-b``, and a ``/step`` suffix on ``*`` or a range. For day-of-week the
    input value ``7`` is normalized to ``0`` (both mean Sunday). ``restricted`` records whether the
    raw field was anything other than ``*`` (the Vixie dom/dow OR rule keys on it).
    """
    raw = spec.strip()
    if raw == "":
        raise CronError(f"empty {name} field")
    restricted = raw != "*"
    values: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if token == "":
            raise CronError(f"empty item in {name} field {spec!r}")
        rng, _, step_s = token.partition("/")
        step = 1
        if _:
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"invalid step {step_s!r} in {name} field {spec!r}")
            step = int(step_s)
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a_s, _, b_s = rng.partition("-")
            start, end = _int(a_s, name, spec, dow=dow), _int(b_s, name, spec, dow=dow)
        else:
            start = end = _int(rng, name, spec, dow=dow)
        if start > end:
            raise CronError(f"descending range {rng!r} in {name} field {spec!r}")
        for v in range(start, end + 1, step):
            values.add(0 if (dow and v == 7) else v)
    return _Field(values=frozenset(values), restricted=restricted)


def _int(text: str, name: str, spec: str, *, dow: bool) -> int:
    """Parse one integer cron token, validating it is within the field's bounds (dow allows 7)."""
    token = text.strip()
    if not (token.isdigit() or (token.startswith("-") and token[1:].isdigit())):
        raise CronError(f"non-integer {token!r} in {name} field {spec!r}")
    value = int(token)
    lo, hi = (0, 7) if dow else _BOUNDS[_FIELD_NAMES.index(name)]
    if not lo <= value <= hi:
        raise CronError(f"value {value} out of range {lo}-{hi} in {name} field {spec!r}")
    return value


def parse_cron(cron: str) -> CronSchedule:
    """Parse a 5-field cron string into a :class:`CronSchedule` (raises :class:`CronError`).

    Exactly five whitespace-separated fields are required (``minute hour day-of-month month
    day-of-week``); any other count, or a malformed field, raises :class:`CronError`.
    """
    fields = cron.split()
    if len(fields) != 5:
        raise CronError(
            f"cron expression must have exactly 5 fields "
            f"(minute hour day-of-month month day-of-week), got {len(fields)}: {cron!r}"
        )
    parsed = [
        _parse_field(fields[i], _BOUNDS[i][0], _BOUNDS[i][1], name=_FIELD_NAMES[i], dow=(i == 4))
        for i in range(5)
    ]
    return CronSchedule(
        minute=parsed[0], hour=parsed[1], dom=parsed[2], month=parsed[3], dow=parsed[4]
    )


def cron_matches(schedule: CronSchedule, when: datetime) -> bool:
    """Return whether ``when`` (a UTC instant) matches ``schedule`` at minute resolution.

    Minute/hour/month must each match. The day matches per the Vixie dom/dow rule: both restricted →
    OR; one restricted → that one; neither → any day. ``when`` is converted to UTC; seconds are
    ignored (cron is minute-resolution).
    """
    dt = when.astimezone(UTC)
    if dt.minute not in schedule.minute.values:
        return False
    if dt.hour not in schedule.hour.values:
        return False
    if dt.month not in schedule.month.values:
        return False
    dom_match = dt.day in schedule.dom.values
    # Python: Monday=0 … Sunday=6; cron: Sunday=0 … Saturday=6. Convert.
    cron_dow = (dt.weekday() + 1) % 7
    dow_match = cron_dow in schedule.dow.values
    if schedule.dom.restricted and schedule.dow.restricted:
        return dom_match or dow_match
    if schedule.dom.restricted:
        return dom_match
    if schedule.dow.restricted:
        return dow_match
    return True


def is_cron_due(
    cron: str,
    *,
    now: datetime,
    last_run: datetime | None,
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> bool:
    """Return whether a scheduled cron fire has elapsed since ``last_run`` (the scheduler's signal).

    Finds the most recent minute ``<= now`` that matches ``cron`` (scanning back at most
    ``window_days``) and reports due iff that fire is strictly AFTER ``last_run`` — so a coarse poll
    interval still fires a due schedule, and a schedule fires at most once per matching minute. When
    ``last_run`` is ``None`` (never run), any fire within the window counts as due. Both datetimes
    must be timezone-aware (matching the core convention); evaluation is in UTC.

    Args:
        cron: The 5-field cron expression (``curator.triggers.cron``).
        now: Current instant (timezone-aware).
        last_run: Instant of the last consolidation, or ``None``. Timezone-aware when provided.
        window_days: How far back to look for the most recent fire (default 366; bounds the scan).

    Raises:
        CronError: If ``cron`` is malformed.
        ValueError: If ``now`` (or a provided ``last_run``) is naive.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC)")
    if last_run is not None and last_run.tzinfo is None:
        raise ValueError("last_run must be timezone-aware (UTC)")

    schedule = parse_cron(cron)
    # Truncate to the current minute in UTC and scan backward for the most recent matching minute.
    cursor = now.astimezone(UTC).replace(second=0, microsecond=0)
    for _ in range(window_days * 24 * 60 + 1):
        if cron_matches(schedule, cursor):
            return last_run is None or cursor > last_run
        cursor -= timedelta(minutes=1)
    return False
