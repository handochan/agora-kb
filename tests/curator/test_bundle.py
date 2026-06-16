"""Tests for the read-only backend input bundle + tier-2 content dedup (ADR-0011 §1, §5 tier-2).

MODEL-FREE: events are written through the real :class:`Inbox.write` and claimed through the real
:func:`agora_kb.curator.claim.claim` (so the bundle is graded over genuine immutable claimed
events), then :func:`build_bundle` is asserted deterministically. We cover: byte-equivalent bodies
collapsing to ONE candidate with BOTH provenance tuples unioned (tier-2, §5); distinct bodies stay
distinct; the §6 ``is_gated`` flag (kind==candidate OR confidence==low); ``candidates.json`` +
``related/`` + the read-only schema/taxonomy copies written; and deterministic ``c1``/``c2`` ids.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agora_kb.core.inbox import Inbox
from agora_kb.core.layout import RepoLayout
from agora_kb.core.models import Confidence, Kind
from agora_kb.core.repo import Repo
from agora_kb.core.state import CuratorState
from agora_kb.curator.bundle import build_bundle
from agora_kb.curator.claim import claim, curator_lock
from agora_kb.curator.manifest import RunManifest

RUN_ID = "2026-06-13T03-00-00.000Z--7f31ab"
BASE = "705f4a4"
STARTED = "2026-06-13T03:00:00Z"


def _write(
    inbox: Inbox,
    *,
    text: str,
    writer: str = "dochan",
    source: str = "claude-code",
    second: int,
    domain: str | None = None,
    kind: Kind = Kind.capture,
    confidence: Confidence | None = None,
) -> str:
    """Write one inbox event at a pinned wall-clock second (deterministic FIFO id)."""
    now = datetime(2026, 6, 13, 2, 40, second, tzinfo=UTC)
    return inbox.write(
        text=text,
        writer=writer,
        source=source,
        domain=domain,
        kind=kind,
        confidence=confidence,
        now=now,
    ).id


def _claim(layout: RepoLayout) -> RunManifest:
    """Claim the current inbox into ``processing/<run-id>/`` and return the manifest."""
    with curator_lock(layout):
        manifest = claim(
            layout, base_commit=BASE, run_id=RUN_ID, started=STARTED, state=CuratorState()
        )
    assert manifest is not None
    return manifest


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- tier-2: byte-equivalent bodies collapse to ONE candidate -----------------------------------
def test_identical_bodies_collapse_to_one_candidate_with_unioned_provenance(
    tmp_path: Path,
) -> None:
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    # SAME body, DIFFERENT writers/sources — tier-2 collapses to one candidate (§5), but BOTH
    # distinct provenance tuples are unioned (§5 "always preserve provenance").
    e1 = _write(inbox, text="shared fact", writer="dochan", source="claude-code", second=10)
    e2 = _write(inbox, text="shared fact", writer="alice", source="codex", second=11)
    manifest = _claim(layout)

    result = build_bundle(layout, Repo(layout), manifest)

    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert cand.candidate_id == "c1"
    # Both manifest events collapsed into this one candidate, in FIFO order.
    assert cand.event_ids == (e1, e2)
    assert cand.text == "shared fact"
    # Provenance UNION preserves both distinct {event_id, writer, source, ...} tuples.
    assert len(cand.provenance) == 2
    writers = {tup["writer"] for tup in cand.provenance}
    sources = {tup["source"] for tup in cand.provenance}
    assert writers == {"dochan", "alice"}
    assert sources == {"claude-code", "codex"}
    event_ids = {tup["event_id"] for tup in cand.provenance}
    assert event_ids == {e1, e2}
    # Each provenance tuple carries the immutable event BODY (threaded so deterministic APPLY can
    # materialize the cited raw/<domain>/<event_id>.md free-text source, ADR-0010 D3).
    assert all(tup["body"] == "shared fact" for tup in cand.provenance)
    # The worker-side provenance handle mirrors the candidate (the apply_plan sources: input, §2).
    assert {tup["event_id"] for tup in result.provenance["c1"]} == {e1, e2}
    assert all(tup["body"] == "shared fact" for tup in result.provenance["c1"])


def test_distinct_bodies_stay_distinct_candidates(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    e1 = _write(inbox, text="alpha fact", second=10)
    e2 = _write(inbox, text="beta fact", second=11)
    manifest = _claim(layout)

    result = build_bundle(layout, Repo(layout), manifest)

    assert [c.candidate_id for c in result.candidates] == ["c1", "c2"]
    # FIFO: the earliest event seeds c1.
    assert result.candidates[0].event_ids == (e1,)
    assert result.candidates[1].event_ids == (e2,)
    assert result.candidates[0].text == "alpha fact"
    assert result.candidates[1].text == "beta fact"


def test_candidate_ids_are_deterministic_fifo_order(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    # Write out of chronological order; the claim FIFO-orders by id, so c1==earliest body.
    _write(inbox, text="third", second=12)
    _write(inbox, text="first", second=10)
    _write(inbox, text="second", second=11)
    manifest = _claim(layout)

    result = build_bundle(layout, Repo(layout), manifest)
    assert [(c.candidate_id, c.text) for c in result.candidates] == [
        ("c1", "first"),
        ("c2", "second"),
        ("c3", "third"),
    ]


# --- §6 candidate gate --------------------------------------------------------------------------
def test_is_gated_for_candidate_kind(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    _write(
        inbox, text="harvested claim", second=10, kind=Kind.candidate, confidence=Confidence.high
    )
    _write(inbox, text="normal capture", second=11, kind=Kind.capture, confidence=Confidence.high)
    manifest = _claim(layout)

    result = build_bundle(layout, Repo(layout), manifest)
    by_id = {c.candidate_id: c for c in result.candidates}
    assert by_id["c1"].kind == "candidate"
    assert by_id["c1"].is_gated is True
    assert by_id["c2"].kind == "capture"
    assert by_id["c2"].is_gated is False
    assert result.gated_candidate_ids == {"c1"}


def test_is_gated_for_low_confidence(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    _write(inbox, text="low conf capture", second=10, kind=Kind.capture, confidence=Confidence.low)
    manifest = _claim(layout)

    result = build_bundle(layout, Repo(layout), manifest)
    cand = result.candidates[0]
    assert cand.confidence == "low"
    assert cand.kind == "capture"
    # is_gated flips on low confidence even for a (non-candidate) capture (§6).
    assert cand.is_gated is True
    assert result.gated_candidate_ids == {cand.candidate_id}


def test_merged_kind_confidence_are_worst_case(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    # Identical body, but one member is a low-confidence candidate -> the merged candidate is gated.
    _write(
        inbox,
        text="merge me",
        writer="dochan",
        second=10,
        kind=Kind.capture,
        confidence=Confidence.high,
    )
    _write(
        inbox,
        text="merge me",
        writer="alice",
        source="codex",
        second=11,
        kind=Kind.candidate,
        confidence=Confidence.low,
    )
    manifest = _claim(layout)

    result = build_bundle(layout, Repo(layout), manifest)
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    # Worst-case roll-up across the tier-2 group (§1).
    assert cand.kind == "candidate"
    assert cand.confidence == "low"
    assert cand.is_gated is True


# --- bundle materialization (§1) ----------------------------------------------------------------
def test_candidates_json_and_related_written(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    _write(
        inbox,
        text="shared fact",
        writer="dochan",
        source="claude-code",
        second=10,
        domain="ai-tech",
    )
    _write(inbox, text="shared fact", writer="alice", source="codex", second=11, domain="ai-tech")
    _write(inbox, text="another fact", second=12, domain="economy")
    manifest = _claim(layout)

    result = build_bundle(layout, Repo(layout), manifest)
    bundle_dir = result.bundle_dir
    assert bundle_dir == layout.processing_dir / RUN_ID / "bundle"

    # candidates.json — ONE entry per dedup'd candidate (NOT per raw event), §1.
    doc = _read_json(bundle_dir / "candidates.json")
    assert doc["run_id"] == RUN_ID
    assert [c["candidate_id"] for c in doc["candidates"]] == ["c1", "c2"]
    c1 = doc["candidates"][0]
    assert c1["text"] == "shared fact"
    assert c1["domain"] == "ai-tech"
    assert c1["is_gated"] is False
    assert c1["related_ref"] == "related/c1.json"
    # content_sha256 is present and stable for byte-equivalent bodies.
    assert isinstance(c1["content_sha256"], str) and len(c1["content_sha256"]) == 64
    # Provenance union recorded in the written JSON too.
    assert len(c1["provenance"]) == 2

    # related/<cand-id>.json — one per candidate, a serialized QueryResult (status + hits).
    for cid in ("c1", "c2"):
        related = _read_json(bundle_dir / "related" / f"{cid}.json")
        assert related["status"] in {"ok", "not_found"}
        assert isinstance(related["hits"], list)


def test_schema_and_taxonomy_copied_into_bundle(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    # Materialize the read-only schema doc + _meta/taxonomy.yaml the worker copies into the bundle.
    schema_text = "# KB schema\nrules here\n"
    layout.schema_file.write_text(schema_text, encoding="utf-8")
    meta_dir = layout.root / "_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    taxonomy_text = "schema_version: 1\nallowed_tags: [curator]\ndomains: [ai-tech]\n"
    (meta_dir / "taxonomy.yaml").write_text(taxonomy_text, encoding="utf-8")

    inbox = Inbox(layout)
    _write(inbox, text="a fact", second=10)
    manifest = _claim(layout)

    result = build_bundle(layout, Repo(layout), manifest)
    # Copied verbatim (a self-contained read-only mount; originals untouched).
    assert (result.bundle_dir / "schema.md").read_text(encoding="utf-8") == schema_text
    assert (result.bundle_dir / "taxonomy.yaml").read_text(encoding="utf-8") == taxonomy_text


def test_build_bundle_is_deterministic(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    _write(inbox, text="shared fact", writer="dochan", source="claude-code", second=10)
    _write(inbox, text="shared fact", writer="alice", source="codex", second=11)
    _write(inbox, text="distinct", second=12)
    manifest = _claim(layout)

    result = build_bundle(layout, Repo(layout), manifest)
    first_candidates_json = (result.bundle_dir / "candidates.json").read_text(encoding="utf-8")

    # Re-running over the same claimed events yields byte-identical bundle output.
    result2 = build_bundle(layout, Repo(layout), manifest)
    second_candidates_json = (result2.bundle_dir / "candidates.json").read_text(encoding="utf-8")
    assert first_candidates_json == second_candidates_json
    assert [c.candidate_id for c in result.candidates] == [
        c.candidate_id for c in result2.candidates
    ]
    assert [c.event_ids for c in result.candidates] == [c.event_ids for c in result2.candidates]
