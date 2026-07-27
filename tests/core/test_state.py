"""Tests for curator state (``_kb/state.json``, DATA-MODEL §4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agora_kb.core.layout import RepoLayout
from agora_kb.core.state import (
    MAX_FAILURE_REASONS,
    Counters,
    CuratorState,
    LastBatch,
    LastFailure,
    StateStore,
)


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


def test_last_batch_roundtrip(store: StateStore) -> None:
    """(#60) The per-run batch shape persists and loads; a fresh state carries None."""
    assert store.load().last_batch is None
    s = CuratorState()
    s.last_batch = LastBatch(claimed=3, candidates=2, cap=2, inbox_remaining=7)
    store.save(s)
    loaded = store.load()
    assert loaded.last_batch == LastBatch(claimed=3, candidates=2, cap=2, inbox_remaining=7)


def test_pre_batch_state_json_loads_with_none(store: StateStore) -> None:
    """(#60) A pre-#60 state.json (no last_batch key) loads unchanged — the field is additive."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "last_run": "2026-06-13T03:00:12Z",
                "last_commit": "705f4a4",
                "counters": {"ingested": 1, "merged": 0, "dropped": 0, "failed": 0},
                "event_keys": {},
                "published_runs": {},
            }
        ),
        encoding="utf-8",
    )
    loaded = store.load()
    assert loaded.last_batch is None
    assert loaded.counters.ingested == 1


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
    for key in (
        '"counters"',
        '"event_keys"',
        '"published_runs"',
        '"last_run"',
        '"last_attempt"',
        '"last_failure"',
    ):
        assert key in js
    parsed = json.loads(js)  # empty state serializes the absent optionals as JSON null
    assert parsed["last_run"] is None
    assert parsed["last_commit"] is None
    # (#96) both new fields are additive optionals: absent ⇒ null, never a fabricated default.
    assert parsed["last_attempt"] is None
    assert parsed["last_failure"] is None


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


# --- (#96) failure observability: last_attempt + last_failure ------------------------------------

_WHEN = datetime(2026, 6, 13, 3, 0, 12, tzinfo=UTC)
_RECORD = "_kb/failed/2026-06-13/2026-06-13T03-00-12.000Z--3f2a1b/error.json"


def _failure(**overrides: object) -> LastFailure:
    """A canonical :class:`LastFailure` built through the sanctioned constructor."""
    kwargs: dict[str, object] = {
        "when": _WHEN,
        "run_id": "2026-06-13T03-00-12.000Z--3f2a1b",
        "phase": "claimed",
        "reasons": ["TAXONOMY: unknown domain 'not-a-real-domain'"],
        "record_path": _RECORD,
    }
    kwargs.update(overrides)
    return LastFailure.from_run_failure(**kwargs)  # type: ignore[arg-type]


def test_last_failure_roundtrip(store: StateStore) -> None:
    """(#96) The failure record persists and loads back equal, in the DATA-MODEL §4 time form."""
    s = CuratorState()
    s.last_failure = _failure()
    store.save(s)
    loaded = store.load()
    assert loaded.last_failure == s.last_failure
    assert loaded.last_failure is not None
    assert loaded.last_failure.record_path == _RECORD
    assert loaded.last_failure.reasons_total == 1
    assert '"when": "2026-06-13T03:00:12Z"' in store.path.read_text(encoding="utf-8")


def test_pre_failure_state_json_loads_with_none(store: StateStore) -> None:
    """(#96 crit 10) A pre-#96 state.json loads unchanged — both fields are additive optionals.

    ``extra='forbid'`` rejects unknown keys PRESENT in the input, never absent ones, so an operator
    upgrading agora keeps their published_runs/event_keys without any migration step.
    """
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "last_run": "2026-06-13T03:00:12Z",
                "last_commit": "705f4a4",
                "counters": {"ingested": 1, "merged": 0, "dropped": 0, "failed": 0},
                "last_batch": {"claimed": 1, "candidates": 1, "cap": 32, "inbox_remaining": 0},
                "event_keys": {},
                "published_runs": {},
            }
        ),
        encoding="utf-8",
    )
    loaded = store.load()
    assert loaded.last_attempt is None
    assert loaded.last_failure is None
    assert loaded.counters.ingested == 1


def test_last_attempt_serialization_and_tz(store: StateStore) -> None:
    """(#96) last_attempt obeys the SAME §4 grammar as last_run: aware-only, Z-form, UTC."""
    s = CuratorState(last_attempt=_WHEN)
    store.save(s)
    assert '"last_attempt": "2026-06-13T03:00:12Z"' in store.path.read_text(encoding="utf-8")

    # A naive stamp is rejected on load, and the message names the field that is wrong.
    store.path.write_text('{"last_attempt": "2026-06-13T03:00:12"}', encoding="utf-8")
    with pytest.raises(ValueError, match="last_attempt must be timezone-aware"):
        store.load()

    # A non-UTC offset is normalized on load, exactly like last_run.
    store.path.write_text('{"last_attempt": "2026-06-13T03:00:12+05:00"}', encoding="utf-8")
    assert store.load().last_attempt == datetime(2026, 6, 12, 22, 0, 12, tzinfo=UTC)


def test_last_failure_reasons_list_is_capped_and_total_is_honest(store: StateStore) -> None:
    """(#96) ``from_run_failure`` caps the LIST only — it never re-clips an already-bounded string.

    One truncation rule (``curator.worker._bounded_reasons`` owns the per-string 400-char bound),
    so ``reasons_total`` can be trusted as the true count behind the preview.
    """
    long_reason = "Z" * 400  # already at the upstream per-string bound
    reasons = [long_reason, *[f"LINT L1-{i}" for i in range(10)]]
    lf = _failure(reasons=reasons)

    assert len(lf.reasons) == MAX_FAILURE_REASONS == 5
    assert lf.reasons_total == 11
    assert lf.reasons[0] == long_reason  # byte-identical: no second truncation pass
    store.save(CuratorState(last_failure=lf))
    assert store.load().last_failure == lf


def test_last_failure_save_load_save_is_byte_idempotent(store: StateStore) -> None:
    """(#96) Capping at CONSTRUCTION (not in a validator) keeps the file stable across saves.

    A mutating validator would re-truncate an already-truncated list on every load, so state.json
    would drift on each run of a permanently-broken repo.
    """
    store.save(CuratorState(last_failure=_failure(reasons=[f"r{i}" for i in range(11)])))
    first = store.path.read_text(encoding="utf-8")
    store.save(store.load())
    assert store.path.read_text(encoding="utf-8") == first


def test_failure_is_current_without_a_failure() -> None:
    """(#96) No recorded failure ⇒ nothing to be current about."""
    assert CuratorState().failure_is_current is False
    assert CuratorState(last_run=_WHEN, last_commit="abc").failure_is_current is False


def test_failure_is_current_when_never_published() -> None:
    """(#96 crit 7) A repo that has NEVER published but HAS failed is unambiguously broken."""
    s = CuratorState(last_failure=_failure())
    assert s.last_run is None
    assert s.failure_is_current is True


def test_failure_is_current_is_false_when_superseded() -> None:
    """(#96) A later successful publish supersedes the sticky failure — 'was broken', not 'is'."""
    s = CuratorState(last_failure=_failure())
    s.mark_run(_WHEN + timedelta(seconds=60), "abc1234")
    assert s.last_failure is not None  # sticky: the historical fact survives the publish
    assert s.failure_is_current is False


def test_failure_is_current_on_a_tie_reports_current() -> None:
    """(#96) Same-second tie resolves to CURRENT — under-reporting a live failure is the disease."""
    s = CuratorState(last_failure=_failure())
    s.mark_run(_WHEN, "abc1234")
    assert s.failure_is_current is True


def test_failure_is_current_is_not_serialized(store: StateStore) -> None:
    """(#96) It is a plain @property, never a pydantic field — state.json must not carry it."""
    store.save(CuratorState(last_failure=_failure()))
    assert "failure_is_current" not in store.path.read_text(encoding="utf-8")
