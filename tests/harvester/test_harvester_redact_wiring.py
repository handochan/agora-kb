"""Tests for the live redaction wiring the session connector uses (#25, ADR-0023 addendum §5/§6).

The connector redacts a fact at its boundary and reports the per-class hits on the HarvestedFact;
the orchestrator persists the (already-redacted) text and bumps the harvester-owned
``HarvestCursor.redacted`` counter once per class per WRITTEN fact. A curator load→save round-trips
the field untouched (symmetric with accepted/rejected). The file: connector reports no hits, so its
write path + cursor stay byte-identical (#39). These tests use a fake already-redacted connector so
they exercise the orchestrator plumbing independently of the SessionConnector distiller (Unit D).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agora_kb.config import HarvestPolicy
from agora_kb.core.frontmatter import parse
from agora_kb.core.hashing import content_sha256
from agora_kb.core.layout import RepoLayout
from agora_kb.core.redact import RedactionHit
from agora_kb.harvester.connectors import ConnectorScan, HarvestedFact, Scope
from agora_kb.harvester.harvester import CursorStore, HarvestCursor, Harvester

FIXED = datetime(2026, 7, 6, 9, 0, 0, tzinfo=UTC)
_POLICY = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")


class RedactingFakeConnector:
    """A connector that yields facts ALREADY redacted at its boundary (mimics session:)."""

    def __init__(self, name: str, agent: str, facts: list[HarvestedFact]) -> None:
        self._name, self._agent, self._facts = name, agent, facts

    @property
    def name(self) -> str:
        return self._name

    @property
    def agent(self) -> str:
        return self._agent

    @property
    def scope(self) -> Scope:
        return Scope.personal

    def scan(self, *, last_content_sha256: str | None) -> ConnectorScan:
        return ConnectorScan(
            unchanged=False,
            content_sha256="deadbeef",
            source_path=f"session://{self._agent}",
            facts=tuple(self._facts),
        )


def _redacted_fact(text: str, *hits: RedactionHit) -> HarvestedFact:
    # text is the ALREADY-redacted text; fact_key is over that redacted text (addendum §2).
    return HarvestedFact(text=text, fact_key=content_sha256(text), redaction_hits=tuple(hits))


def _read_inbox(layout: RepoLayout) -> list[tuple[dict, str]]:
    return [parse(p.read_text(encoding="utf-8")) for p in sorted(layout.inbox_dir.glob("*/*.md"))]


# --- counter bump: per class per WRITTEN fact ---------------------------------------------------


def test_cursor_redacted_bumps_once_per_class_per_written_fact(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    facts = [
        _redacted_fact("- lesson [REDACTED:jwt]", RedactionHit("jwt", 1)),
        _redacted_fact("- other [REDACTED:jwt]", RedactionHit("jwt", 1)),
        _redacted_fact(
            "- both [REDACTED:aws_access_key_id] [REDACTED:jwt]",
            RedactionHit("aws_access_key_id", 1),
            RedactionHit("jwt", 1),
        ),
    ]
    conn = RedactingFakeConnector("session:demo", "demo", facts)
    Harvester(layout).run([conn], policy=_POLICY, now=FIXED)

    cur = CursorStore(layout).load("session:demo")
    assert cur.proposed == 3
    # jwt appears in 3 facts, aws in 1 — once per class per fact (NOT per match).
    assert cur.redacted == {"jwt": 3, "aws_access_key_id": 1}


def test_multiple_matches_in_one_fact_count_once_for_the_class(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    # A hit with count=2 (two matches of one class in one fact) still bumps the cursor by 1 for it.
    fact = _redacted_fact("- two keys [REDACTED:jwt] [REDACTED:jwt]", RedactionHit("jwt", 2))
    Harvester(layout).run(
        [RedactingFakeConnector("session:demo", "demo", [fact])], policy=_POLICY, now=FIXED
    )
    assert CursorStore(layout).load("session:demo").redacted == {"jwt": 1}


def test_deduped_fact_does_not_recount_redaction(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    fact = _redacted_fact("- dupe [REDACTED:jwt]", RedactionHit("jwt", 1))
    conn = RedactingFakeConnector("session:demo", "demo", [fact, fact])  # same fact_key twice
    report = Harvester(layout).run([conn], policy=_POLICY, now=FIXED).connectors[0]
    assert report.written == 1 and report.deduped == 1
    # only the WRITTEN fact counts — the deduped duplicate does not inflate the counter.
    assert CursorStore(layout).load("session:demo").redacted == {"jwt": 1}


def test_file_connector_shape_never_bumps_redacted(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    fact = HarvestedFact(text="- plain fact", fact_key=content_sha256("- plain fact"))  # no hits
    Harvester(layout).run(
        [RedactingFakeConnector("file:demo", "demo", [fact])], policy=_POLICY, now=FIXED
    )
    assert CursorStore(layout).load("file:demo").redacted == {}


def test_written_note_reports_class_metadata_only(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    fact = _redacted_fact("- x [REDACTED:jwt]", RedactionHit("jwt", 1))
    report = (
        Harvester(layout)
        .run([RedactingFakeConnector("session:demo", "demo", [fact])], policy=_POLICY, now=FIXED)
        .connectors[0]
    )
    assert any("redacted secret/PII from written fact(s): jwt x1" in n for n in report.notes)


def test_already_redacted_text_persists_verbatim(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    redacted_text = "- credential [REDACTED:aws_access_key_id] noted"
    fact = _redacted_fact(redacted_text, RedactionHit("aws_access_key_id", 1))
    Harvester(layout).run(
        [RedactingFakeConnector("session:demo", "demo", [fact])], policy=_POLICY, now=FIXED
    )
    (_, body) = _read_inbox(layout)[0]
    assert "[REDACTED:aws_access_key_id]" in body  # the connector already redacted it


# --- HarvestCursor.redacted round-trip + curator preservation -----------------------------------


def test_cursor_redacted_round_trips_through_store(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    store = CursorStore(layout)
    store.save(HarvestCursor(connector="session:demo", proposed=2, redacted={"jwt": 2}))
    assert store.load("session:demo").redacted == {"jwt": 2}


def test_default_cursor_has_empty_redacted(tmp_path: Path) -> None:
    assert CursorStore(RepoLayout(tmp_path)).load("session:demo").redacted == {}


def test_curator_load_save_preserves_harvester_owned_redacted(tmp_path: Path) -> None:
    # Simulate the curator finalize path (_apply_harvest_cursor_deltas): load, bump accepted, save.
    layout = RepoLayout(tmp_path)
    store = CursorStore(layout)
    store.save(HarvestCursor(connector="session:demo", proposed=3, redacted={"jwt": 3}))
    cur = store.load("session:demo")
    cur.accepted += 2  # curator-owned field
    store.save(cur)
    reloaded = store.load("session:demo")
    assert reloaded.accepted == 2
    assert reloaded.redacted == {"jwt": 3}  # harvester-owned field preserved, not clobbered


# --- dry-run preview reconcile: already-redacted vs courtesy-redacted ----------------------------


def test_dry_run_preview_uses_reported_hits_for_already_redacted_fact(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    fact = _redacted_fact("- lesson [REDACTED:jwt] learned", RedactionHit("jwt", 1))
    cr = (
        Harvester(layout)
        .run(
            [RedactingFakeConnector("session:demo", "demo", [fact])],
            policy=_POLICY,
            now=FIXED,
            dry_run=True,
        )
        .connectors[0]
    )
    assert any("would redact 1 fact(s) before persistence: jwt x1" in n for n in cr.notes)
    assert cr.preview[0].text == "- lesson [REDACTED:jwt] learned"  # unchanged (already redacted)


def test_dry_run_touches_no_cursor_for_redacting_connector(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    fact = _redacted_fact("- x [REDACTED:jwt]", RedactionHit("jwt", 1))
    Harvester(layout).run(
        [RedactingFakeConnector("session:demo", "demo", [fact])],
        policy=_POLICY,
        now=FIXED,
        dry_run=True,
    )
    cur = CursorStore(layout).load("session:demo")
    assert cur.proposed == 0 and cur.redacted == {}
