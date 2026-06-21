"""PURE unit tests for the harvest-cursor delta helper (ADR-0017 §7, curator-owned counters).

These tests exercise :func:`agora_kb.curator.worker.compute_harvest_cursor_deltas` with HAND-BUILT
plans + bundle provenance and ZERO model / ZERO disk: the helper is a deterministic, model-free
function of ``(plan, bundle, connector_specs)``, so the accepted/rejected/NOOP semantics, the
per-harvested-event granularity, the mixed-provenance attribution, and the configured-only connector
mapping are all gradable in isolation (mirroring how plan.py is unit-tested against canned plans).
"""

from __future__ import annotations

from pathlib import Path

from agora_kb.config import ConnectorSpec
from agora_kb.curator.bundle import BundleResult
from agora_kb.curator.plan import Disposition, Plan
from agora_kb.curator.worker import HarvestCursorDelta, compute_harvest_cursor_deltas


def _disp(candidate_id: str, op: str, **kw: object) -> Disposition:
    """A minimal valid disposition for the given candidate/op (only the fields the helper reads)."""
    base: dict[str, object] = {
        "candidate_id": candidate_id,
        "event_ids": ("e1",),
        "op": op,
        "reason": "test",
    }
    base.update(kw)
    return Disposition(**base)  # type: ignore[arg-type]


def _plan(*dispositions: Disposition) -> Plan:
    return Plan(schema_version=1, run_id="run", finished=True, dispositions=tuple(dispositions))


def _harvest_tuple(agent: str, event_id: str = "e1") -> dict[str, object]:
    """A provenance tuple as the harvester writes it: source=harvest:<agent> (bundle.py shape)."""
    return {"event_id": event_id, "source": f"harvest:{agent}", "writer": f"harvest-{agent}"}


def _local_tuple(event_id: str = "e1") -> dict[str, object]:
    """A LOCAL capture provenance tuple (a non-harvest source) — must NEVER be counted."""
    return {"event_id": event_id, "source": "claude-code", "writer": "dochan"}


def _bundle(provenance: dict[str, list[dict[str, object]]]) -> BundleResult:
    """A BundleResult carrying only the provenance map the helper reads (other fields are inert)."""
    return BundleResult(
        candidates=(),
        bundle_dir=Path("/unused"),
        gated_candidate_ids=set(),
        provenance=provenance,
    )


def _spec(name: str) -> ConnectorSpec:
    return ConnectorSpec(name=name, scope="personal", path="x/*.md")


# --- counting semantics: MERGE/CONTEST -> accepted, DROP -> rejected, NOOP -> skip --------------


def test_merge_into_theme_counts_accepted() -> None:
    deltas = compute_harvest_cursor_deltas(
        _plan(_disp("c1", "MERGE_INTO_THEME", target_basename="t")),
        _bundle({"c1": [_harvest_tuple("claude-code")]}),
        [_spec("file:claude-code")],
    )
    assert deltas == {"file:claude-code": HarvestCursorDelta(accepted=1, rejected=0)}


def test_mark_contested_counts_accepted() -> None:
    deltas = compute_harvest_cursor_deltas(
        _plan(_disp("c1", "MARK_CONTESTED", target_basename="t")),
        _bundle({"c1": [_harvest_tuple("claude-code")]}),
        [_spec("file:claude-code")],
    )
    assert deltas == {"file:claude-code": HarvestCursorDelta(accepted=1, rejected=0)}


def test_drop_counts_rejected() -> None:
    deltas = compute_harvest_cursor_deltas(
        _plan(_disp("c1", "DROP")),
        _bundle({"c1": [_harvest_tuple("claude-code")]}),
        [_spec("file:claude-code")],
    )
    assert deltas == {"file:claude-code": HarvestCursorDelta(accepted=0, rejected=1)}


def test_noop_is_skipped_neither_accepted_nor_rejected() -> None:
    deltas = compute_harvest_cursor_deltas(
        _plan(_disp("c1", "NOOP")),
        _bundle({"c1": [_harvest_tuple("claude-code")]}),
        [_spec("file:claude-code")],
    )
    assert deltas == {}


# --- provenance filtering -----------------------------------------------------------------------


def test_non_harvested_provenance_is_not_counted() -> None:
    """A candidate with ONLY local-capture provenance contributes nothing to any cursor."""
    deltas = compute_harvest_cursor_deltas(
        _plan(_disp("c1", "MERGE_INTO_THEME", target_basename="t")),
        _bundle({"c1": [_local_tuple()]}),
        [_spec("file:claude-code")],
    )
    assert deltas == {}


def test_mixed_provenance_counts_only_harvested_tuples() -> None:
    """A mixed candidate (one harvested + one local tuple) counts ONLY the harvested tuple."""
    deltas = compute_harvest_cursor_deltas(
        _plan(_disp("c1", "MERGE_INTO_THEME", target_basename="t")),
        _bundle({"c1": [_harvest_tuple("claude-code"), _local_tuple("e2")]}),
        [_spec("file:claude-code")],
    )
    assert deltas == {"file:claude-code": HarvestCursorDelta(accepted=1, rejected=0)}


def test_multi_event_candidate_counts_per_harvested_event() -> None:
    """K harvested tuples from one connector on one disposition contribute K (per-event count)."""
    deltas = compute_harvest_cursor_deltas(
        _plan(_disp("c1", "DROP")),
        _bundle(
            {
                "c1": [
                    _harvest_tuple("claude-code", "e1"),
                    _harvest_tuple("claude-code", "e2"),
                    _harvest_tuple("claude-code", "e3"),
                ]
            }
        ),
        [_spec("file:claude-code")],
    )
    assert deltas == {"file:claude-code": HarvestCursorDelta(accepted=0, rejected=3)}


# --- connector mapping --------------------------------------------------------------------------


def test_agent_not_in_config_is_skipped_no_stray_cursor() -> None:
    """A harvested tuple whose connector was removed from config creates NO cursor entry."""
    deltas = compute_harvest_cursor_deltas(
        _plan(_disp("c1", "MERGE_INTO_THEME", target_basename="t")),
        _bundle({"c1": [_harvest_tuple("removed-agent")]}),
        [_spec("file:claude-code")],  # 'removed-agent' is NOT configured
    )
    assert deltas == {}


def test_no_connectors_configured_is_empty() -> None:
    deltas = compute_harvest_cursor_deltas(
        _plan(_disp("c1", "DROP")),
        _bundle({"c1": [_harvest_tuple("claude-code")]}),
        [],
    )
    assert deltas == {}


def test_two_connectors_are_attributed_separately() -> None:
    """Harvested tuples from two agents land on their own cursors (mapped by configured name)."""
    deltas = compute_harvest_cursor_deltas(
        _plan(
            _disp("c1", "MERGE_INTO_THEME", target_basename="t"),
            _disp("c2", "DROP"),
        ),
        _bundle(
            {
                "c1": [_harvest_tuple("claude-code")],
                "c2": [_harvest_tuple("hermes")],
            }
        ),
        [_spec("file:claude-code"), _spec("file:hermes")],
    )
    assert deltas == {
        "file:claude-code": HarvestCursorDelta(accepted=1, rejected=0),
        "file:hermes": HarvestCursorDelta(accepted=0, rejected=1),
    }


def test_accepted_and_rejected_accumulate_per_connector_across_dispositions() -> None:
    """One connector seeing a MERGE and a DROP accumulates accepted=1 AND rejected=1."""
    deltas = compute_harvest_cursor_deltas(
        _plan(
            _disp("c1", "MERGE_INTO_THEME", target_basename="t"),
            _disp("c2", "DROP"),
        ),
        _bundle(
            {
                "c1": [_harvest_tuple("claude-code")],
                "c2": [_harvest_tuple("claude-code")],
            }
        ),
        [_spec("file:claude-code")],
    )
    assert deltas == {"file:claude-code": HarvestCursorDelta(accepted=1, rejected=1)}


def test_spec_name_without_colon_has_no_agent_and_is_ignored() -> None:
    """A connector spec name with no '<type>:' prefix has no agent and never matches a source."""
    deltas = compute_harvest_cursor_deltas(
        _plan(_disp("c1", "DROP")),
        _bundle({"c1": [_harvest_tuple("claude-code")]}),
        [_spec("noprefix")],
    )
    assert deltas == {}
