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

import pytest

from agora_kb.core.inbox import Inbox
from agora_kb.core.layout import RepoLayout
from agora_kb.core.models import Confidence, Kind
from agora_kb.core.repo import Repo
from agora_kb.core.state import CuratorState
from agora_kb.core.wiki import Wiki
from agora_kb.curator import bundle
from agora_kb.curator.bundle import build_bundle
from agora_kb.curator.claim import claim, curator_lock
from agora_kb.curator.constants import DEFAULT_RELATED_K
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


def test_build_bundle_threads_related_k(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """related_k reaches wiki.query_lexical(limit=…); default = DEFAULT_RELATED_K.

    The spied method is ``query_lexical``, not ``query`` — the write path is pinned to the
    model-free oracle (#144); see ``test_bundle_never_touches_the_read_face_query``.
    """
    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    _write(inbox, text="a fact worth relating", second=10)
    manifest = _claim(layout)

    seen: list[int | None] = []
    orig = bundle.Wiki.query_lexical

    def spy(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(kwargs.get("limit"))
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(bundle.Wiki, "query_lexical", spy)

    build_bundle(layout, Repo(layout), manifest, related_k=3)
    assert seen == [3]  # one candidate → one query at the operator's breadth

    seen.clear()
    build_bundle(layout, Repo(layout), manifest)  # default
    assert seen == [DEFAULT_RELATED_K]


def test_schema_and_taxonomy_copied_into_bundle(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    # Materialize the read-only schema doc + _meta/taxonomy.yaml the worker copies into the bundle.
    schema_text = "# KB schema\nrules here\n"
    layout.schema_file.write_text(schema_text, encoding="utf-8")
    meta_dir = layout.root / "_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    # schema_version 2: the version this build WRITES (ADR-0041 D6). A `_meta/taxonomy.yaml`
    # declaring 1 makes the repo READ-ONLY, and `Inbox.write` below then refuses the capture this
    # test needs — which is the refusal working, not a bundle failure.
    taxonomy_text = "schema_version: 2\nallowed_tags: [curator]\ndomains: [ai-tech]\n"
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


# --- #144: the related/ view is pinned to the model-free oracle ---------------------------------
def _build_wiki_corpus(layout: RepoLayout) -> None:
    """Materialize a small real wiki under ``layout`` so the related/ view has genuine hits.

    Without notes every ``related/<cid>.json`` is a trivially-identical ``not_found``, and the
    determinism lock below would pass vacuously.
    """
    (layout.root / "index.md").write_text(
        "# personal\n\n- [AI Tech map](wiki/maps/ai-tech.md)\n", encoding="utf-8"
    )
    themes = layout.root / "wiki" / "concepts"
    themes.mkdir(parents=True, exist_ok=True)
    maps = layout.root / "wiki" / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    (maps / "ai-tech.md").write_text(
        "---\nstatus: active\n---\n# AI Tech\n\n"
        "- [Curator concurrency](../concepts/curator-concurrency.md) — the single-writer curator\n"
        "- [Inbox design](../concepts/inbox-design.md) — append-only per-writer inbox\n",
        encoding="utf-8",
    )
    (themes / "curator-concurrency.md").write_text(
        "---\nstatus: active\ntags: [single-writer, concurrency]\n---\n"
        "# Curator Concurrency\n\n"
        "The curator acquires a per-repo flock so exactly one writer advances the branch.\n",
        encoding="utf-8",
    )
    (themes / "inbox-design.md").write_text(
        "---\nstatus: active\ntags: [inbox, append-only]\n---\n"
        "# Inbox Design\n\n"
        "The inbox is append-only and per-writer namespaced; items are never edited.\n",
        encoding="utf-8",
    )


# The candidate text is chosen to actually retrieve against the corpus above, so the locked bytes
# carry real hits (path/anchor/line/excerpt/match_reason/score) rather than an empty result.
_RELATED_CANDIDATE_TEXT = "the curator is a single-writer that serializes concurrency"


def _bundle_once(tmp_path: Path, name: str) -> tuple[Path, dict[str, bytes]]:
    """Build one bundle in its own repo and return (bundle_dir, related/<cid>.json bytes).

    ``name`` only varies the PARENT directory: the repo directory itself is always ``personal``
    because ``SearchHit.repo`` is derived from the layout root's name, and the point here is to
    lock retrieval, not to prove that two differently-named repos disagree about their own name.
    """
    layout = RepoLayout(tmp_path / name / "personal")
    layout.root.mkdir(parents=True, exist_ok=True)
    _build_wiki_corpus(layout)
    inbox = Inbox(layout)
    _write(inbox, text=_RELATED_CANDIDATE_TEXT, second=10)
    _write(inbox, text="the inbox is append-only and per-writer namespaced", second=11)
    manifest = _claim(layout)

    result = build_bundle(layout, Repo(layout), manifest)
    related = {
        path.name: path.read_bytes() for path in sorted((result.bundle_dir / "related").iterdir())
    }
    return result.bundle_dir, related


def test_related_view_bytes_are_deterministic(tmp_path: Path) -> None:
    """The ARTEFACT is locked, not just the code path (#144 'test gap').

    Two independent runs — separate repos, separate ``Wiki`` instances, separate claims — must
    write byte-identical ``related/<cid>.json``. This file is the planning brain's tier-3 input
    and decides MERGE_INTO_THEME targets, so a drift here is a drift in what the curator merges.
    """
    _, first = _bundle_once(tmp_path, "run-a")
    _, second = _bundle_once(tmp_path, "run-b")

    assert sorted(first) == sorted(second) == ["c1.json", "c2.json"]
    assert first == second

    # The lock is not vacuous: at least one candidate really retrieved something.
    hits = [json.loads(blob.decode("utf-8"))["hits"] for blob in first.values()]
    assert any(hits), "corpus/candidate text no longer retrieves — determinism lock is vacuous"


def test_bundle_never_touches_the_read_face_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a future model tier on ``Wiki.query``: the curator must not notice it exists.

    ``query`` is the read face and MAY grow a tier above the lexical floor; the write path is
    pinned to ``query_lexical`` forever (#144, ADR-0012 §0a). If the pin regresses, this raises
    inside ``build_bundle``.
    """

    def exploded(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the curator write path must never call Wiki.query")

    monkeypatch.setattr(bundle.Wiki, "query", exploded)

    _, related = _bundle_once(tmp_path, "no-query")
    assert sorted(related) == ["c1.json", "c2.json"]
    assert any(json.loads(blob.decode("utf-8"))["hits"] for blob in related.values())


# --- #152: the related/ view offers only LEGAL merge targets ------------------------------------
def _related_with(tmp_path: Path, name: str, mergeable: set[str] | None) -> dict[str, dict]:
    """Build one bundle over the shared corpus and return the parsed ``related/`` views."""
    layout = RepoLayout(tmp_path / name / "personal")
    layout.root.mkdir(parents=True, exist_ok=True)
    _build_wiki_corpus(layout)
    inbox = Inbox(layout)
    _write(inbox, text=_RELATED_CANDIDATE_TEXT, second=10)
    manifest = _claim(layout)
    result = build_bundle(layout, Repo(layout), manifest, mergeable_paths=mergeable)
    return {
        path.name: _read_json(path) for path in sorted((result.bundle_dir / "related").iterdir())
    }


def test_related_view_is_unfiltered_by_default(tmp_path: Path) -> None:
    """``mergeable_paths=None`` is today's behaviour — no path is dropped."""
    views = _related_with(tmp_path, "unfiltered", None)
    hits = views["c1.json"]["hits"]
    assert views["c1.json"]["status"] == "ok"
    assert "wiki/concepts/curator-concurrency.md" in {h["path"] for h in hits}


def test_mergeable_paths_withholds_a_target_the_plan_gate_would_reject(tmp_path: Path) -> None:
    """A note the curator did not write is not a legal MERGE target, so it is not offered as one.

    PASS 1 picks ``target_basename`` out of these hits (kb_schema.md §7.1) and naming a
    human-written note is a BASENAME plan error that fails the WHOLE run (#152). The engine must
    not hand the model a menu item it will then be punished for choosing.
    """
    unfiltered = _related_with(tmp_path, "before", None)["c1.json"]
    assert unfiltered["hits"], "corpus no longer retrieves — the filter test would be vacuous"

    keep = "wiki/concepts/inbox-design.md"
    filtered = _related_with(tmp_path, "after", {keep})["c1.json"]
    assert {h["path"] for h in filtered["hits"]} <= {keep}
    # Everything else about the surviving hits is untouched: this is a path-set drop, not a re-rank
    # (ADR-0012 §0a — nothing outside core computes lex/struct/fm/score/match_reason).
    by_path = {h["path"]: h for h in unfiltered["hits"]}
    for hit in filtered["hits"]:
        assert hit == by_path[hit["path"]]


def test_an_emptied_related_view_reports_not_found(tmp_path: Path) -> None:
    """The brain reads ONE shape for "no eligible evidence" — never ``ok`` with zero hits."""
    view = _related_with(tmp_path, "empty", set())["c1.json"]
    assert view["hits"] == []
    assert view["status"] == "not_found"


def test_the_filter_over_fetches_so_the_view_still_carries_k_eligible_hits(tmp_path: Path) -> None:
    """Filtering AFTER retrieval must not silently shrink the view below ``related_k``.

    The failure this locks is a RECALL loss with a real consequence: a candidate whose top
    ``related_k`` hits are all ineligible would hand PASS 1 an EMPTY related view, and an empty
    view is exactly what tells the planning brain "genuinely new -> CREATE_THEME" (kb_schema.md
    §7.1 rule 6) — a duplicate theme beside the curator note it should have merged into.

    The eligible set is derived FROM the real ranking (the second hit), so the test is
    non-vacuous by construction: with ``related_k=1`` the un-over-fetched call would retrieve only
    the FIRST hit, which is ineligible here, and the view would come back empty.
    """
    layout = RepoLayout(tmp_path / "overfetch" / "personal")
    layout.root.mkdir(parents=True, exist_ok=True)
    _build_wiki_corpus(layout)
    inbox = Inbox(layout)
    _write(inbox, text=_RELATED_CANDIDATE_TEXT, second=10)
    manifest = _claim(layout)

    full = Wiki(layout).query_lexical(_RELATED_CANDIDATE_TEXT, limit=20).hits
    assert len(full) >= 2, "corpus no longer retrieves 2+ notes — the over-fetch test is vacuous"
    second_best = full[1].path

    result = build_bundle(
        layout, Repo(layout), manifest, related_k=1, mergeable_paths={second_best}
    )
    view = _read_json(result.bundle_dir / "related" / "c1.json")

    assert view["status"] == "ok"
    assert [h["path"] for h in view["hits"]] == [second_best]
    # Still a suffix trim of the SAME ranking, never a re-rank (ADR-0012 §0a): the surviving hit is
    # byte-identical to the oracle's own.
    assert view["hits"][0]["score"] == full[1].score
    assert view["hits"][0]["match_reason"] == full[1].match_reason
