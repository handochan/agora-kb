"""Inbox event identifiers (DATA-MODEL §10).

Format: ``YYYY-MM-DDTHH-MM-SS.mmmZ--<6 hex>`` — a UTC timestamp with millisecond precision (``:``
and the date/time separators rendered as ``-`` so the id is a safe filename) plus a short random
suffix. This makes ids **time-sortable** (lexicographic order == chronological order, giving FIFO
ordering of the inbox) and **globally unique** (the random suffix breaks ties within the same
millisecond).

The WRITE path mints ids from a real clock: captures are real-time events, so wall-clock use is
correct here. (The no-wall-clock determinism rule of ADR-0010 D1 applies to the *curator*, not to
capture.)
``now`` and ``rand_hex`` are injectable for deterministic tests.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

__all__ = ["new_event_id", "is_valid_event_id", "event_id_timestamp", "EVENT_ID_RE"]

# 2026-06-13T10-22-33.481Z--a1b2c3
EVENT_ID_RE = re.compile(
    r"\A(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.\d{3}Z)--(?P<rand>[0-9a-f]{6})\Z"
)


def new_event_id(*, now: datetime | None = None, rand_hex: str | None = None) -> str:
    """Mint a new globally-unique, time-sortable inbox event id.

    ``now`` defaults to the current UTC time; ``rand_hex`` defaults to 6 random hex chars. Both are
    injectable so tests can pin a deterministic id.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        # Reject naive datetimes: astimezone() would silently assume local time and shift the id,
        # diverging from InboxItem.created (which requires tz-awareness). Match that contract.
        raise ValueError("now must be timezone-aware (UTC)")
    moment = moment.astimezone(UTC)
    ts = moment.strftime("%Y-%m-%dT%H-%M-%S") + f".{moment.microsecond // 1000:03d}Z"
    rand = rand_hex if rand_hex is not None else secrets.token_hex(3)
    if not re.fullmatch(r"[0-9a-f]{6}", rand):
        raise ValueError(f"rand_hex must be 6 lowercase hex chars, got {rand!r}")
    return f"{ts}--{rand}"


def is_valid_event_id(event_id: str) -> bool:
    """True iff ``event_id`` matches the canonical format (also a filename-safety guard)."""
    return isinstance(event_id, str) and EVENT_ID_RE.match(event_id) is not None


def event_id_timestamp(event_id: str) -> datetime:
    """Parse the UTC timestamp embedded in an event id. Raises ``ValueError`` if malformed."""
    m = EVENT_ID_RE.match(event_id)
    if not m:
        raise ValueError(f"malformed event id: {event_id!r}")
    ts = m.group("ts")  # YYYY-MM-DDTHH-MM-SS.mmmZ
    date_part, time_part = ts[:10], ts[11:-1]  # drop 'T' and trailing 'Z'
    hh, mm, rest = time_part.split("-")
    ss, millis = rest.split(".")
    return datetime(
        int(date_part[0:4]),
        int(date_part[5:7]),
        int(date_part[8:10]),
        int(hh),
        int(mm),
        int(ss),
        int(millis) * 1000,
        tzinfo=UTC,
    )
