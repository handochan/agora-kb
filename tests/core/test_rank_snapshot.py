"""The model-free deterministic ranking snapshot (issue #44, Stratum unit 3 gate B).

WHY A SNAPSHOT AND NOT A SEARCH HARNESS. Gate B was originally an n=24 five-arm search comparison;
that harness never existed as code. It is re-scoped here to what actually gates the Stratum layout
flip: a golden fixture that pins TODAY's ranking behaviour before ``wiki/<domain>/themes|daily`` +
``<domain>-moc.md`` becomes a kind-first tree. ``core.wiki._is_moc_path`` matches
``wiki/<domain>/<domain>-moc.md`` and seeds ``d_moc`` — hence the structural term — for the whole
corpus, so the flip moves scores. Without a baseline the movement cannot be attributed.

WHAT THESE TESTS PIN.

1. **The snapshot computes nothing.** Its numbers come from :meth:`Wiki.query` (ADR-0012 §0a: the
   pure-Python oracle is the ONLY thing that computes a ``SearchHit`` field).
2. **Determinism.** Two calls agree; two repos with the same notes written to disk in OPPOSITE
   order agree; the JSON serialization is byte-identical. No clock, no locale, no network, no
   model.
3. **Identity is the BASENAME, never the path.** This is the property the whole fixture rests on:
   move a note between directories and the record must not report it as one note vanishing and a
   different one appearing. Paths never enter the record at all — their entire content is layout,
   the axis under test.
4. **``diff_snapshots`` names the two failure shapes that matter at the flip**: a status flip
   (``ok`` ⇄ ``not_found``) and a rank change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agora_kb.core import wiki as wiki_mod
from agora_kb.core.layout import RepoLayout
from agora_kb.core.rank_snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    QueryFileError,
    QuerySpec,
    diff_snapshots,
    dumps,
    load_queries,
    snapshot,
)
from agora_kb.core.wiki import Wiki

# --- fixture corpus ------------------------------------------------------------------------------
# The v1 layout the Stratum flip is about to move: `wiki/<domain>/<domain>-moc.md` plus a
# `themes/` subtree. Deliberately the same SHAPE as tests/core/test_wiki_lexical_evidence_146.py so
# the two files agree on what a realistic corpus looks like.

INDEX_MD = """\
# personal

- [Eng MOC](wiki/eng/eng-moc.md)
"""

MOC_TEMPLATE = """\
---
status: active
type: moc
title: eng MOC
summary: Map of content for the eng domain.
---
# eng MOC

{bullets}"""

DEADLOCK = """\
---
status: active
type: theme
tags: [locking]
---
# Deadlock recovery

A deadlock is recovered by dropping the younger advisory lock and letting the older writer
proceed; recovery is deterministic and leaves no partial write.

## Recovery order
The recovery order is fixed so two hosts never both back off.
"""

LOCKING = """\
---
status: active
type: theme
tags: [locking]
---
# Advisory locking

The curator takes one advisory lock per repo so exactly one writer advances the curated branch.
"""

QUERIES = [
    QuerySpec(id="q-deadlock", question="deadlock recovery", expect="ok"),
    QuerySpec(id="q-locking", question="advisory locking curator", expect="ok"),
    QuerySpec(id="q-unrelated", question="quantum biology photosynthesis", expect="not_found"),
]


def _build_repo(
    root: Path,
    *,
    with_deadlock: bool = True,
    themes_dir: str = "themes",
    reverse_write_order: bool = True,
) -> RepoLayout:
    """Write the fixture corpus under ``root``.

    ``themes_dir`` relocates the theme notes (``""`` puts them straight under the domain) — that is
    the miniature of the layout flip. ``reverse_write_order`` controls the ORDER the files hit the
    filesystem, which is what the iteration-order test varies: nothing about the record may depend
    on it.
    """
    domain = root / "wiki" / "eng"
    themes = domain / themes_dir if themes_dir else domain
    themes.mkdir(parents=True, exist_ok=True)

    prefix = f"{themes_dir}/" if themes_dir else ""
    bullets = [f"- [Advisory locking]({prefix}advisory-locking.md)\n"]
    files: list[tuple[Path, str]] = [(themes / "advisory-locking.md", LOCKING)]
    if with_deadlock:
        bullets.append(f"- [Deadlock recovery]({prefix}deadlock-recovery.md)\n")
        files.append((themes / "deadlock-recovery.md", DEADLOCK))

    files.append((root / "index.md", INDEX_MD))
    files.append((domain / "eng-moc.md", MOC_TEMPLATE.format(bullets="".join(bullets))))

    for path, text in reversed(files) if reverse_write_order else files:
        path.write_text(text, encoding="utf-8")
    return RepoLayout(root)


def _snapshot(root: Path, **kwargs: Any) -> dict[str, Any]:
    return snapshot(Wiki(_build_repo(root, **kwargs)), QUERIES)


def _notes(record: dict[str, Any], qid: str) -> list[str]:
    [query] = [q for q in record["queries"] if q["id"] == qid]
    return [h["note"] for h in query["hits"]]


# --- load_queries --------------------------------------------------------------------------------
def test_load_queries_reads_yaml(tmp_path: Path) -> None:
    path = tmp_path / "q.yaml"
    path.write_text(
        "- id: a\n"
        "  question: deadlock recovery\n"
        "  expect: ok\n"
        "  note: a human comment\n"
        "  tags: [ranking]\n"
        "- id: b\n"
        "  question: nothing here\n"
        "  expect: not_found\n",
        encoding="utf-8",
    )
    specs = load_queries(path)
    assert [s.id for s in specs] == ["a", "b"]
    assert specs[0].tags == ("ranking",)
    assert specs[1].expect == "not_found"


def test_load_queries_reads_json_through_the_same_loader(tmp_path: Path) -> None:
    """YAML is a superset of JSON, so one loader serves both file kinds — no format sniffing."""
    path = tmp_path / "q.json"
    path.write_text(json.dumps([{"id": "a", "question": "deadlock recovery"}]), encoding="utf-8")
    [spec] = load_queries(path)
    assert (spec.id, spec.question, spec.expect) == ("a", "deadlock recovery", "ok")


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("id: a\nquestion: x\n", "expected a LIST"),
        ("[]\n", "no queries"),
        ("- id: a\n", "invalid"),  # missing `question`
        ("- id: a\n  question: x\n  bogus: 1\n", "invalid"),  # extra='forbid'
        ("- id: a\n  question: x\n- id: a\n  question: y\n", "duplicate query id"),
        ("- 'just a string'\n", "expected a mapping"),
        ("- id: a\n   question: [\n", "not valid YAML/JSON"),
    ],
)
def test_load_queries_fails_loud(tmp_path: Path, body: str, needle: str) -> None:
    """A malformed query file must raise, never silently yield a shorter eval set.

    A harness that skips a query it could not parse reports a green baseline it never measured —
    the failure mode a CI gate exists to prevent.
    """
    path = tmp_path / "q.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(QueryFileError) as exc:
        load_queries(path)
    assert needle in str(exc.value)


def test_load_queries_missing_file_raises_query_file_error(tmp_path: Path) -> None:
    with pytest.raises(QueryFileError) as exc:
        load_queries(tmp_path / "nope.yaml")
    assert "cannot read query file" in str(exc.value)


# --- determinism ---------------------------------------------------------------------------------
def test_snapshot_is_deterministic_across_two_calls(tmp_path: Path) -> None:
    wiki = Wiki(_build_repo(tmp_path / "personal"))
    first = snapshot(wiki, QUERIES)
    second = snapshot(wiki, QUERIES)
    assert first == second
    assert dumps(first) == dumps(second)


def test_snapshot_is_independent_of_note_file_write_order(tmp_path: Path) -> None:
    """The same corpus written to disk in opposite orders must snapshot identically.

    ADR-0012 §12 asks for exactly this property test on the ranker ("order-dependent IDF/avgdl
    accumulation"); the fixture inherits it, because a golden file that depends on creation order
    would flag the flip for a reason that has nothing to do with the flip.
    """
    forward = _snapshot(tmp_path / "personal", reverse_write_order=False)
    backward = _snapshot(tmp_path / "other" / "personal", reverse_write_order=True)
    assert forward == backward
    assert diff_snapshots(forward, backward) == []


def test_snapshot_records_the_oracles_scores_verbatim(tmp_path: Path) -> None:
    """Every recorded score/reason/order is the one `Wiki.query` returned — nothing is derived.

    This is the ADR-0012 §0a guard in test form: if the snapshot ever starts computing a ranking
    quantity of its own, this comparison against the oracle's own output is what breaks.
    """
    wiki = Wiki(_build_repo(tmp_path / "personal"))
    record = snapshot(wiki, QUERIES)
    [query] = [q for q in record["queries"] if q["id"] == "q-deadlock"]
    result = wiki.query("deadlock recovery")

    assert query["status"] == result.status
    assert [h["score"] for h in query["hits"]] == [round(h.score, 6) for h in result.hits]
    assert [h["match_reason"] for h in query["hits"]] == [h.match_reason for h in result.hits]
    assert [h["rank"] for h in query["hits"]] == list(range(1, len(result.hits) + 1))
    # lex/struct/fm are NOT on the frozen SearchHit contract (ADR-0012 §0), and recomputing them
    # out here is what §0a forbids — so the keys exist and are null rather than invented.
    assert all(h["lex"] is None and h["struct"] is None and h["fm"] is None for h in query["hits"])


def test_snapshot_header_describes_the_run(tmp_path: Path) -> None:
    record = snapshot(Wiki(_build_repo(tmp_path / "personal")), QUERIES, limit=3)
    header = record["header"]
    assert header["snapshot_schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert header["repo"] == "personal"
    assert header["kb_schema_version"] == 1  # the documented default for a non-initialized dir
    assert header["query_count"] == len(QUERIES)
    assert header["corpus_note_count"] == 4  # index + moc + 2 themes
    assert header["limit"] == 3
    assert header["fm_enabled"] is wiki_mod.FM_ENABLED
    assert isinstance(header["agora_version"], str)


def test_snapshot_header_separates_the_cache_POLICY_from_whether_it_was_USED(
    tmp_path: Path,
) -> None:
    """``index_cache_enabled`` is config; ``index_cache_used`` is fact, and they differ here.

    A repo with no ``.git`` has no curated commit, so ``Wiki._load_notes`` full-scans however the
    policy is set. Recording only the flag would let a reader conclude the ADR-0012 §2 cached path
    had been exercised when it structurally cannot have been — the record would state a condition
    the numbers do not actually meet.
    """
    header = snapshot(Wiki(_build_repo(tmp_path / "personal")), QUERIES)["header"]
    assert header["index_cache_enabled"] is True
    assert header["index_cache_used"] is False


def test_snapshot_records_the_layout_independent_extraction_fields(tmp_path: Path) -> None:
    """``anchor`` / ``line`` / ``excerpt`` are on the frozen SearchHit contract and are recorded.

    All three derive from note CONTENT — a heading slug, a 1-based body line, a body window — never
    from the path, so they survive the layout flip and give the ADR-0012 §7 extraction contract a
    baseline. They are transcribed, never computed here (§0a).
    """
    record = snapshot(Wiki(_build_repo(tmp_path / "personal")), QUERIES)
    hits = [h for q in record["queries"] for h in q["hits"]]
    assert hits
    for hit in hits:
        assert set(hit) >= {"anchor", "line", "excerpt"}
        assert isinstance(hit["anchor"], str)
        assert isinstance(hit["line"], int) and hit["line"] >= 1
        assert isinstance(hit["excerpt"], str)
    assert any(hit["excerpt"] for hit in hits), "a real match must carry a real excerpt"


def test_snapshot_honours_limit(tmp_path: Path) -> None:
    wiki = Wiki(_build_repo(tmp_path / "personal"))
    assert len(_notes(snapshot(wiki, QUERIES, limit=1), "q-deadlock")) == 1
    assert len(_notes(snapshot(wiki, QUERIES), "q-deadlock")) >= 2


@pytest.mark.parametrize("limit", [0, -1])
def test_snapshot_refuses_a_non_positive_limit(tmp_path: Path, limit: int) -> None:
    """``limit=0`` would record ``status: ok`` with ZERO hits for every query.

    ``Wiki.query`` decides ``status`` from the eligible set and only THEN slices
    ``eligible[: max(0, limit)]``, so a zero limit produces a baseline that is green by
    construction and pins no ranking at all — the exact failure this fixture exists to prevent.
    It is refused rather than honoured.
    """
    wiki = Wiki(_build_repo(tmp_path / "personal"))
    with pytest.raises(ValueError, match="limit must be >= 1"):
        snapshot(wiki, QUERIES, limit=limit)


def test_snapshot_fm_mode_is_forced_then_restored(tmp_path: Path) -> None:
    """``fm=`` pins the ADR-0012 §8 column; the module global it rebinds is always put back.

    The oracle reads ``fm_enabled`` as a module constant (flipped live by the #56 addendum A3), so
    forcing a mode means rebinding a global. A leak would silently change every LATER test's
    ranking, so restoration is pinned, not assumed.
    """
    wiki = Wiki(_build_repo(tmp_path / "personal"))
    original = wiki_mod.FM_ENABLED

    off = snapshot(wiki, QUERIES, fm=False)
    assert off["header"]["fm_enabled"] is False
    assert wiki_mod.FM_ENABLED is original

    on = snapshot(wiki, QUERIES, fm=True)
    assert on["header"]["fm_enabled"] is True
    assert wiki_mod.FM_ENABLED is original

    # The `active` notes here take +0.10 under fm=on, so the two columns are genuinely different.
    assert diff_snapshots(off, on) != []


# --- basename identity (the property the layout flip rests on) -----------------------------------
def test_identity_is_the_basename_and_paths_never_enter_the_record(tmp_path: Path) -> None:
    """Move every theme note up a directory: the record must not notice.

    This is the miniature of the Stratum flip. Under a path-keyed record the move would read as
    "two notes disappeared and two appeared" — noise that would drown the real signal. Basenames
    are globally unique per repo (ADR-0010 §3.1), so they survive the move.
    """
    nested = _snapshot(tmp_path / "a" / "personal", themes_dir="themes")
    flat = _snapshot(tmp_path / "b" / "personal", themes_dir="")

    for qid in ("q-deadlock", "q-locking", "q-unrelated"):
        assert _notes(nested, qid) == _notes(flat, qid)

    text = dumps(nested)
    assert "themes/" not in text
    assert "wiki/" not in text
    assert ".md" not in text


# --- diff_snapshots ------------------------------------------------------------------------------
def test_diff_reports_a_status_flip(tmp_path: Path) -> None:
    """Deleting the only note that answers a query flips `ok` → `not_found`; the diff says so."""
    before = _snapshot(tmp_path / "a" / "personal", with_deadlock=True)
    after = _snapshot(tmp_path / "b" / "personal", with_deadlock=False)
    lines = diff_snapshots(before, after)
    assert "q-deadlock: status 'ok' -> 'not_found'" in lines
    # The unrelated probe is `not_found` on both sides, so it contributes nothing.
    assert not any(line.startswith("q-unrelated:") for line in lines)


def test_diff_of_a_snapshot_against_itself_is_empty(tmp_path: Path) -> None:
    record = _snapshot(tmp_path / "personal")
    assert diff_snapshots(record, record) == []
    assert diff_snapshots(record, json.loads(json.dumps(record))) == []


def _record(*hits: tuple[str, int, float]) -> dict[str, Any]:
    """A minimal snapshot dict — the diff reads plain mappings, so a fixture needs no repo."""
    return {
        "header": {
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "fm_enabled": True,
            "repo": "personal",
            "index_cache_enabled": True,
            "index_cache_used": False,
            "limit": 20,
            "corpus_note_count": 4,
            "kb_schema_version": 1,
            "agora_version": "test",
        },
        "queries": [
            {
                "id": "q1",
                "question": "deadlock recovery",
                "expect": "ok",
                "status": "ok",
                "hits": [
                    {
                        "note": note,
                        "title": note,
                        "type": "theme",
                        "score": score,
                        "lex": None,
                        "struct": None,
                        "fm": None,
                        "match_reason": "lexical",
                        "anchor": "",
                        "line": 1,
                        "excerpt": "an excerpt",
                        "rank": rank,
                    }
                    for note, rank, score in hits
                ],
            }
        ],
    }


def test_diff_reports_a_rank_change_with_its_score_delta() -> None:
    before = _record(("alpha", 1, 0.8), ("beta", 2, 0.7))
    after = _record(("beta", 1, 0.9), ("alpha", 2, 0.8))
    lines = diff_snapshots(before, after)
    assert "q1: alpha rank 1 -> 2 (score unchanged)" in lines
    assert "q1: beta rank 2 -> 1" in lines
    assert "q1: beta score 0.7 -> 0.9 (+0.200000)" in lines
    # `alpha` kept its score, so no score line for it — the diff reports differences only.
    assert not any(line.startswith("q1: alpha score") for line in lines)


def test_diff_reports_dropped_and_appeared_notes_and_reason_changes() -> None:
    before = _record(("alpha", 1, 0.8))
    after = _record(("gamma", 1, 0.6))
    lines = diff_snapshots(before, after)
    assert any("alpha dropped" in line for line in lines)
    assert any("gamma appeared at rank 1" in line for line in lines)

    changed = _record(("alpha", 1, 0.8))
    changed["queries"][0]["hits"][0]["match_reason"] = "linked-theme"
    assert "q1: alpha match_reason 'lexical' -> 'linked-theme'" in diff_snapshots(before, changed)


def test_diff_reports_header_and_membership_changes() -> None:
    """A header difference changes what the numbers MEAN, so it is reported before any of them."""
    before = _record(("alpha", 1, 0.8))
    after = _record(("alpha", 1, 0.8))
    after["header"]["fm_enabled"] = False
    after["queries"].append({"id": "q2", "question": "x", "expect": "ok", "status": "ok"})
    lines = diff_snapshots(before, after)
    assert lines[0] == "header: fm_enabled True -> False"
    assert "q2: query added (status 'ok')" in lines

    reversed_lines = diff_snapshots(after, before)
    assert "q2: query removed" in reversed_lines


def test_diff_reports_an_expect_change() -> None:
    before = _record(("alpha", 1, 0.8))
    after = _record(("alpha", 1, 0.8))
    after["queries"][0]["expect"] = "not_found"
    assert "q1: expect 'ok' -> 'not_found'" in diff_snapshots(before, after)


def test_diff_tolerates_records_missing_keys() -> None:
    """The two sides of a real comparison are a committed file and a fresh run — one may be junk.

    Includes a hit record missing ``rank`` / ``score`` outright, which is what a comparison ACROSS
    a :data:`SNAPSHOT_SCHEMA_VERSION` bump looks like: the docstring promises a missing key is
    reported as a difference rather than raised, so it must not be a ``KeyError``.
    """
    assert diff_snapshots({}, {}) == []
    lines = diff_snapshots(_record(("alpha", 1, 0.8)), {})
    assert any("q1: query removed" == line for line in lines)

    truncated = _record(("alpha", 1, 0.8))
    del truncated["queries"][0]["hits"][0]["rank"]
    del truncated["queries"][0]["hits"][0]["score"]
    lines = diff_snapshots(_record(("alpha", 1, 0.8)), truncated)
    assert "q1: alpha rank 1 -> None" in lines
    assert any(line.startswith("q1: alpha score 0.8 -> None") for line in lines)
    # And the same record on the OTHER side (a dropped/appeared arm reads the same keys).
    assert diff_snapshots(truncated, _record(("alpha", 1, 0.8)))


def test_diff_reports_title_and_type_changes() -> None:
    """``type`` is exactly what the Stratum flip must PRESERVE while every path moves.

    A reporter blind to it would print an empty listing for a change that renamed a note or
    reclassified it — and the README makes that listing the artifact a PR owes its reviewer.
    """
    before = _record(("alpha", 1, 0.8))
    after = _record(("alpha", 1, 0.8))
    after["queries"][0]["hits"][0]["title"] = "Alpha, renamed"
    after["queries"][0]["hits"][0]["type"] = "moc"
    lines = diff_snapshots(before, after)
    assert "q1: alpha title 'alpha' -> 'Alpha, renamed'" in lines
    assert "q1: alpha type 'theme' -> 'moc'" in lines


def test_diff_reports_the_extraction_fields_and_elides_a_long_excerpt() -> None:
    """The §7 anchor/line/excerpt contract is reported, with excerpts kept to one line."""
    before = _record(("alpha", 1, 0.8))
    after = _record(("alpha", 1, 0.8))
    after["queries"][0]["hits"][0]["anchor"] = "when-the-balance-clears"
    after["queries"][0]["hits"][0]["line"] = 12
    after["queries"][0]["hits"][0]["excerpt"] = "x" * 300
    lines = diff_snapshots(before, after)
    assert "q1: alpha anchor '' -> 'when-the-balance-clears'" in lines
    assert "q1: alpha line 1 -> 12" in lines
    (excerpt_line,) = [line for line in lines if " excerpt " in line]
    assert "..." in excerpt_line and len(excerpt_line) < 160


def test_diff_flags_a_rank_move_whose_score_did_not_change() -> None:
    """ADR-0012 §7's order ends in the note PATH, and the layout flip moves every path.

    The record carries no path (by design — it is the axis under test), so this annotation is the
    only thing that tells a reviewer "same score, reordered" rather than "the ranking moved".
    """
    before = _record(("alpha", 1, 0.8), ("beta", 2, 0.8))
    after = _record(("beta", 1, 0.8), ("alpha", 2, 0.8))
    lines = diff_snapshots(before, after)
    assert "q1: alpha rank 1 -> 2 (score unchanged)" in lines
    assert "q1: beta rank 2 -> 1 (score unchanged)" in lines
    # A rank move that came WITH a score change is not flagged — its cause is on the next line.
    scored = _record(("alpha", 2, 0.6), ("beta", 1, 0.9))
    assert "q1: alpha rank 1 -> 2" in diff_snapshots(before, scored)


# --- the empty corpus ----------------------------------------------------------------------------
def test_snapshot_on_an_empty_repo_records_not_found(tmp_path: Path) -> None:
    """ADR-0012 §5 gate (d): a fresh repo answers `not_found`, and the fixture records that."""
    root = tmp_path / "personal"
    root.mkdir()
    record = snapshot(Wiki(RepoLayout(root)), QUERIES)
    assert {q["status"] for q in record["queries"]} == {"not_found"}
    assert record["header"]["corpus_note_count"] == 0
