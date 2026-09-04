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

This module ALSO mints the second identifier shape Agora uses, and the two must not be confused:

* an **event id** (above) names ONE inbox capture and is a filename;
* a **ULID** (:func:`new_ulid`) names ONE KNOWLEDGE BASE — the ``kb_id`` minted once at
  ``agora repo init`` into ``_meta/kb.yaml`` and mirrored into every note's ``kb:`` frontmatter
  (ADR-0041 D1.5), so a note copied out of a repo still names its origin.

The ULID is implemented INLINE (Crockford base32 over a 48-bit millisecond timestamp plus 80
random bits) rather than taken from a package: ADR-0041 OD-1 records the decision — a third-party
ULID would be a T1 dependency admission under the ADR-0005 T0-T4 addendum (permissive transitive
closure + a ``docs/BOM.md`` ledger entry) for a thirty-line algorithm, and inline keeps
``agora repo init`` dependency-free.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta

__all__ = [
    "new_event_id",
    "is_valid_event_id",
    "event_id_timestamp",
    "EVENT_ID_RE",
    "new_ulid",
    "is_ulid",
    "ULID_ALPHABET",
    "ULID_RE",
]

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


# --- KB identity: ULID (ADR-0041 D1.5 / OD-1) ---------------------------------------------------
#: Crockford base32 — the ULID alphabet. ``I``, ``L``, ``O`` and ``U`` are deliberately absent
#: (``I``/``L`` confuse with ``1``, ``O`` with ``0``, and ``U`` is excluded to avoid accidental
#: obscenities), which is exactly why a ULID survives being read aloud or retyped out of a note.
ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: 26 Crockford characters == 130 bits of encoding space for a 128-bit value, so the two high bits
#: of the FIRST character are always zero: a canonical ULID starts with ``0``-``7``. Anchored with
#: ``\A``/``\Z`` (not ``^``/``$``) so a trailing newline is rejected, matching :data:`EVENT_ID_RE`.
ULID_RE = re.compile(r"\A[0-7][0-9A-HJKMNP-TV-Z]{25}\Z")

_ULID_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ULID_TIME_BITS = 48
_ULID_RANDOM_BITS = 80
_ULID_LENGTH = 26
_ULID_MAX_MS = 1 << _ULID_TIME_BITS
_ULID_MAX_RANDOM = 1 << _ULID_RANDOM_BITS


def new_ulid(*, now: datetime | None = None, randomness: int | None = None) -> str:
    """Mint a ULID — the ``kb_id`` of one knowledge base (ADR-0041 D1.5).

    128 bits: a 48-bit UNIX millisecond timestamp (most significant, so ULIDs sort
    chronologically as plain strings) followed by 80 bits of randomness from :mod:`secrets`,
    rendered as 26 Crockford base32 characters. Minted ONCE at ``agora repo init`` and never
    rewritten; it is display/join identity, NEVER an authorisation input (ADR-0041 D1.5 / R3 — a
    ``kb_id`` on a KB that was not created locally is a self-claim).

    ``now`` (must be timezone-aware, as for :func:`new_event_id`) and ``randomness`` are injectable
    so a test can pin an exact string; both default to a real clock and real entropy. The
    millisecond conversion is integer arithmetic against the epoch, never ``timestamp() * 1000``,
    so no float rounding can move the timestamp field by a millisecond.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        # Same contract as new_event_id: a naive datetime would be silently read as local time.
        raise ValueError("now must be timezone-aware (UTC)")
    milliseconds = (moment.astimezone(UTC) - _ULID_EPOCH) // timedelta(milliseconds=1)
    if not 0 <= milliseconds < _ULID_MAX_MS:
        raise ValueError(
            f"now is outside the 48-bit ULID timestamp range (1970-01-01 .. 10889-08-02): {now!r}"
        )
    if randomness is None:
        randomness = secrets.randbits(_ULID_RANDOM_BITS)
    elif isinstance(randomness, bool) or not isinstance(randomness, int):
        raise ValueError(f"randomness must be an int, got {randomness!r}")
    elif not 0 <= randomness < _ULID_MAX_RANDOM:
        raise ValueError(f"randomness must fit in {_ULID_RANDOM_BITS} bits, got {randomness!r}")
    return _encode_crockford((milliseconds << _ULID_RANDOM_BITS) | randomness, _ULID_LENGTH)


def is_ulid(value: object) -> bool:
    """True iff ``value`` is a canonical 26-character uppercase Crockford ULID.

    STRICT on purpose: lowercase and the ``I``/``L``/``O``/``U`` decode aliases the Crockford
    spec tolerates on INPUT are rejected here, because a ``kb_id`` is a join key mirrored into
    every note's frontmatter — two spellings of one KB identity would silently split it.
    """
    return isinstance(value, str) and ULID_RE.match(value) is not None


def _encode_crockford(value: int, length: int) -> str:
    """Render ``value`` as ``length`` Crockford base32 chars, most significant first."""
    out = [""] * length
    for i in range(length - 1, -1, -1):
        out[i] = ULID_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)
