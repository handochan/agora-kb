"""Tests for inbox event ids (DATA-MODEL §10)."""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest

from agora_kb.core.ids import (
    EVENT_ID_RE,
    event_id_timestamp,
    is_valid_event_id,
    new_event_id,
)


def test_format_matches_spec() -> None:
    eid = new_event_id(
        now=datetime(2026, 6, 13, 10, 22, 33, 481_000, tzinfo=UTC),
        rand_hex="a1b2c3",
    )
    assert eid == "2026-06-13T10-22-33.481Z--a1b2c3"
    assert EVENT_ID_RE.match(eid)
    assert is_valid_event_id(eid)


def test_millisecond_truncation() -> None:
    # microseconds floor to milliseconds
    eid = new_event_id(
        now=datetime(2026, 6, 13, 10, 22, 33, 481_999, tzinfo=UTC),
        rand_hex="000000",
    )
    assert eid.startswith("2026-06-13T10-22-33.481Z--")


def test_non_utc_normalized_to_utc() -> None:
    from datetime import timedelta

    # A tz-aware non-UTC instant is converted to UTC before formatting.
    tz = timezone(timedelta(hours=9))
    eid = new_event_id(now=datetime(2026, 6, 13, 19, 22, 33, tzinfo=tz), rand_hex="abcdef")
    assert eid.startswith("2026-06-13T10-22-33.000Z--")


def test_naive_now_rejected() -> None:
    # A naive datetime would be silently read as local time; reject it (matches InboxItem.created).
    with pytest.raises(ValueError):
        new_event_id(now=datetime(2026, 6, 13, 10, 22, 33))  # noqa: DTZ001


def test_sortable_is_chronological() -> None:
    early = new_event_id(now=datetime(2026, 6, 13, 1, 0, 0, tzinfo=UTC), rand_hex="ffffff")
    late = new_event_id(now=datetime(2026, 6, 13, 2, 0, 0, tzinfo=UTC), rand_hex="000000")
    assert early < late  # lexicographic == chronological despite the random suffix


def test_uniqueness_default_random() -> None:
    ids = {new_event_id() for _ in range(200)}
    assert len(ids) == 200


def test_roundtrip_timestamp() -> None:
    moment = datetime(2026, 6, 13, 10, 22, 33, 481_000, tzinfo=UTC)
    eid = new_event_id(now=moment, rand_hex="a1b2c3")
    assert event_id_timestamp(eid) == moment


@pytest.mark.parametrize(
    "bad",
    [
        "2026-06-13T10:22:33.481Z--a1b2c3",  # colons not allowed
        "2026-06-13T10-22-33Z--a1b2c3",  # missing millis
        "2026-06-13T10-22-33.481Z--A1B2C3",  # uppercase hex
        "2026-06-13T10-22-33.481Z--a1b2c",  # 5 hex
        "2026-06-13T10-22-33.481Z--a1b2c3\n",  # trailing newline rejected (\\Z, not $)
        "not-an-id",
        "",
    ],
)
def test_invalid_ids_rejected(bad: str) -> None:
    assert not is_valid_event_id(bad)
    with pytest.raises(ValueError):
        event_id_timestamp(bad)


def test_bad_rand_hex_rejected() -> None:
    with pytest.raises(ValueError):
        new_event_id(rand_hex="xyz")
