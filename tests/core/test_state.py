"""Tests for curator state (``_kb/state.json``, DATA-MODEL §4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agora_kb.core.layout import RepoLayout
from agora_kb.core.state import Counters, CuratorState, StateStore


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    return StateStore(RepoLayout(tmp_path))


def test_load_missing_returns_empty(store: StateStore) -> None:
    s = store.load()
    assert s.last_run is None
    assert s.last_commit is None
    assert s.counters == Counters()
    assert s.event_keys == {}
    assert s.published_runs == {}
    assert not store.path.exists()  # load must not create the file


def test_save_then_load_roundtrip(store: StateStore) -> None:
    s = CuratorState()
    s.mark_run(datetime(2026, 6, 13, 3, 0, 12, tzinfo=UTC), "705f4a4")
    s.counters.ingested = 142
    s.counters.merged = 38
    s.counters.dropped = 11
    s.counters.failed = 2
    s.record_event_key("dochan", "k1", "2026-06-13T02-40-10.000Z--a1b2c3")
    s.record_published_run("run-1", "abc1234")
    store.save(s)
    assert store.load() == s


def test_last_run_serialized_as_z(store: StateStore) -> None:
    s = CuratorState()
    s.mark_run(datetime(2026, 6, 13, 3, 0, 12, tzinfo=UTC), "abc")
    store.save(s)
    assert '"last_run": "2026-06-13T03:00:12Z"' in store.path.read_text(encoding="utf-8")


def test_event_key_lookup_and_namespacing() -> None:
    s = CuratorState()
    s.record_event_key("alice", "k1", "id-a")
    s.record_event_key("bob", "k1", "id-b")  # same key, different writer => distinct
    assert s.event_key_id("alice", "k1") == "id-a"
    assert s.event_key_id("bob", "k1") == "id-b"
    assert s.event_key_id("carol", "k1") is None


def test_record_event_key_keeps_first() -> None:
    s = CuratorState()
    s.record_event_key("alice", "k1", "first")
    s.record_event_key("alice", "k1", "second")  # idempotent: earliest wins
    assert s.event_key_id("alice", "k1") == "first"


def test_event_key_with_colon_in_key() -> None:
    # writer is colon-free, so a key containing ':' is still unambiguous.
    s = CuratorState()
    s.record_event_key("alice", "a:b:c", "id-x")
    assert s.event_key_id("alice", "a:b:c") == "id-x"


def test_published_run_ledger() -> None:
    s = CuratorState()
    assert not s.is_published("run-1")
    s.record_published_run("run-1", "sha1")
    assert s.is_published("run-1")
    assert s.published_commit("run-1") == "sha1"
    assert s.published_commit("run-x") is None


def test_mark_run_requires_aware() -> None:
    with pytest.raises(ValueError):
        CuratorState().mark_run(datetime(2026, 6, 13, 3, 0, 12), "abc")  # noqa: DTZ001 (naive)


def test_naive_last_run_rejected_on_load(store: StateStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"last_run": "2026-06-13T03:00:12"}', encoding="utf-8")  # no offset
    with pytest.raises(ValueError):
        store.load()


def test_extra_field_rejected_on_load(store: StateStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"unexpected": 1}', encoding="utf-8")
    with pytest.raises(ValueError):
        store.load()


def test_update_load_mutate_save(store: StateStore) -> None:
    result = store.update(lambda s: setattr(s.counters, "ingested", 5))
    assert result.counters.ingested == 5  # update returns the mutated state...
    assert result == store.load()  # ...and it was persisted
    store.update(lambda s: s.record_published_run("r2", "sha2"))
    assert store.load().is_published("r2")


def test_save_overwrites_and_leaves_no_temp(store: StateStore) -> None:
    store.save(CuratorState())
    s = store.load()
    s.counters.failed = 9
    store.save(s)
    assert store.load().counters.failed == 9
    assert list(store.path.parent.glob(".*tmp*")) == []


def test_to_json_shape() -> None:
    js = CuratorState().to_json()
    assert js.endswith("\n")
    for key in ('"counters"', '"event_keys"', '"published_runs"', '"last_run"'):
        assert key in js
    parsed = json.loads(js)  # empty state serializes the absent optionals as JSON null
    assert parsed["last_run"] is None
    assert parsed["last_commit"] is None


_KST = timezone(timedelta(hours=5))


def test_construct_non_utc_normalized() -> None:
    s = CuratorState(last_run=datetime(2026, 6, 13, 3, 0, 12, tzinfo=_KST))
    assert s.last_run == datetime(2026, 6, 12, 22, 0, 12, tzinfo=UTC)
    assert s.last_run.utcoffset() == timedelta(0)


def test_mark_run_normalizes_non_utc() -> None:
    s = CuratorState()
    s.mark_run(datetime(2026, 6, 13, 3, 0, 12, tzinfo=_KST), "abc")
    assert s.last_run == datetime(2026, 6, 12, 22, 0, 12, tzinfo=UTC)
    assert s.last_commit == "abc"


def test_non_utc_last_run_normalized_on_load(store: StateStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"last_run": "2026-06-13T03:00:12+05:00"}', encoding="utf-8")
    s = store.load()
    assert s.last_run == datetime(2026, 6, 12, 22, 0, 12, tzinfo=UTC)
    store.save(s)
    assert '"last_run": "2026-06-12T22:00:12Z"' in store.path.read_text(encoding="utf-8")


def test_last_run_truncates_fractional_seconds(store: StateStore) -> None:
    s = CuratorState()
    s.mark_run(datetime(2026, 6, 13, 3, 0, 12, 481_000, tzinfo=UTC), "abc")
    store.save(s)
    assert '"last_run": "2026-06-13T03:00:12Z"' in store.path.read_text(encoding="utf-8")


def test_corrupt_json_rejected_on_load(store: StateStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("not json at all {{", encoding="utf-8")
    # load RAISES on corruption (never silently discards published_runs/event_keys).
    with pytest.raises(ValueError):
        store.load()


def test_ek_assumes_colon_free_writer() -> None:
    # The "<writer>:<key>" cache key is only collision-free because writers are colon-free
    # (enforced by validate_writer upstream). Document the collapse if that ever breaks.
    s = CuratorState()
    s.record_event_key("a", "b:c", "id1")
    s.record_event_key("a:b", "c", "id2")
    assert s.event_keys == {"a:b:c": "id1"}  # both map to the same composite; first wins
