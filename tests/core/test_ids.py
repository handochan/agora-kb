"""Tests for the two identifier shapes: inbox event ids (DATA-MODEL §10) and KB ULIDs."""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest

import agora_kb.core.ids as ids_mod
from agora_kb.core.ids import (
    EVENT_ID_RE,
    ULID_ALPHABET,
    event_id_timestamp,
    is_ulid,
    is_valid_event_id,
    new_event_id,
    new_ulid,
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


# --- ULID: the KB identity minted into _meta/kb.yaml (ADR-0041 D1.5 / OD-1) ---------------------


def _decode_crockford(text: str) -> int:
    """Independent decoder — the test does NOT reuse the encoder it is checking."""
    value = 0
    for ch in text:
        value = value * 32 + ULID_ALPHABET.index(ch)
    return value


def test_ulid_is_26_crockford_chars_and_self_validates() -> None:
    ulid = new_ulid()
    assert len(ulid) == 26
    assert set(ulid) <= set(ULID_ALPHABET)
    assert is_ulid(ulid)


def test_ulid_layout_is_48_bit_ms_timestamp_then_80_random_bits() -> None:
    """The exact bit layout, decoded independently: the timestamp is recoverable from the string."""
    moment = datetime(2026, 9, 4, 12, 34, 56, 789_000, tzinfo=UTC)
    ulid = new_ulid(now=moment, randomness=0)

    value = _decode_crockford(ulid)
    assert value >> 80 == int(moment.timestamp() * 1000)  # the 48-bit millisecond field
    assert value & ((1 << 80) - 1) == 0  # the 80-bit randomness field, as injected


def test_ulid_is_deterministic_when_both_inputs_are_pinned() -> None:
    moment = datetime(2026, 9, 4, tzinfo=UTC)
    assert new_ulid(now=moment, randomness=0) == new_ulid(now=moment, randomness=0)
    # …and the low field really is the randomness (one bit apart ⇒ one character apart).
    assert new_ulid(now=moment, randomness=0) != new_ulid(now=moment, randomness=1)


def test_ulid_sorts_chronologically_as_a_plain_string() -> None:
    """Timestamp-high is the whole reason for the layout: lexicographic == chronological."""
    early = new_ulid(now=datetime(2026, 9, 4, 1, 0, tzinfo=UTC), randomness=(1 << 80) - 1)
    late = new_ulid(now=datetime(2026, 9, 4, 2, 0, tzinfo=UTC), randomness=0)
    assert early < late  # despite maximal randomness on the earlier one


def test_ulid_defaults_to_secrets_for_its_randomness(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default entropy source is :mod:`secrets`, not :mod:`random` (ADR-0041 OD-1)."""
    calls: list[int] = []

    def fake_randbits(bits: int) -> int:
        calls.append(bits)
        return 0

    monkeypatch.setattr(ids_mod.secrets, "randbits", fake_randbits)
    ulid = new_ulid(now=datetime(2026, 9, 4, tzinfo=UTC))

    assert calls == [80]
    assert ulid.endswith("0" * 16)  # 80 bits == 16 Crockford characters


def test_ulid_uses_a_real_clock_by_default() -> None:
    ulid = new_ulid(randomness=0)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    assert abs((_decode_crockford(ulid) >> 80) - now_ms) < 60_000


def test_ulid_default_mint_is_unique() -> None:
    assert len({new_ulid() for _ in range(200)}) == 200


def test_ulid_naive_now_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        new_ulid(now=datetime(2026, 9, 4))  # noqa: DTZ001


@pytest.mark.parametrize("bad", [-1, 1 << 80, True, 1.5, "0"])
def test_ulid_bad_randomness_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        new_ulid(randomness=bad)  # type: ignore[arg-type]


def test_ulid_timestamp_out_of_48_bit_range_rejected() -> None:
    with pytest.raises(ValueError, match="48-bit"):
        new_ulid(now=datetime(1969, 12, 31, tzinfo=UTC))


@pytest.mark.parametrize(
    "bad",
    [
        "01J8Z0000000000000000000",  # 24 chars
        "01J8Z00000000000000000000000",  # 28 chars
        "81J8Z000000000000000000000",  # first char > '7' ⇒ overflows 128 bits
        "01j8z000000000000000000000",  # lowercase is NOT canonical
        "01I8Z000000000000000000000",  # 'I' is not in the Crockford alphabet
        "01L8Z000000000000000000000",  # 'L' likewise
        "01O8Z000000000000000000000",  # 'O' likewise
        "01U8Z000000000000000000000",  # 'U' likewise
        "01J8Z00000000000000000000\n",  # a trailing newline is rejected (\\Z, not $)
        "",
        "not-a-ulid",
    ],
)
def test_is_ulid_rejects_non_canonical(bad: str) -> None:
    assert not is_ulid(bad)


@pytest.mark.parametrize("bad", [None, 123, b"01J8Z000000000000000000000"])
def test_is_ulid_rejects_non_strings(bad: object) -> None:
    assert not is_ulid(bad)


def test_ulid_and_event_id_are_different_vocabularies() -> None:
    """The two identifier shapes in this module must never validate each other's values."""
    assert not is_ulid(new_event_id())
    assert not is_valid_event_id(new_ulid())
