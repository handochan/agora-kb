"""Gate B: today's ranking, pinned — the baseline the Stratum layout flip is measured against.

``core.wiki._is_moc_path`` reads the MOC out of the PATH (``wiki/<domain>/<domain>-moc.md``) and
seeds ``d_moc`` — the structural term — for the WHOLE corpus. Flipping the wiki layout axis to the
Stratum kind-first tree therefore moves ranking, and downstream gold-pack selection, whether or not
anyone intended it to. These tests record what the ranker returns BEFORE the flip so that after it
the delta is attributable rather than merely noticed.

WHAT IS ASSERTED, AND WHAT IS NOT. Per ADR-0012 §0a nothing outside ``core`` computes ``lex`` /
``struct`` / ``fm`` / ``score`` / ``match_reason``; these tests never recompute a ranking quantity,
they compare a fresh transcription of ``Wiki.query`` against a committed one. They also do not
assert that the ranking is GOOD — only that it is what it was. The judgement calls live in
``queries.yaml`` (``expect`` + the expected top ``note``), which is asserted separately so a golden
that silently drifted into answering the wrong note still fails.

``header["agora_version"]`` is deliberately excluded from every comparison: a version bump must not
turn this gate red, because a red gate people learn to fix by regenerating is not a gate.
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
    QuerySpec,
    diff_snapshots,
    dumps,
    load_queries,
)
from agora_kb.core.wiki import Wiki
from tests.rank_golden import regen
from tests.rank_golden.corpus import CORPUS, DOMAINS
from tests.support.kb_builder import build_kb

_VERSION_SENTINEL = "<any>"

_REGEN_HINT = (
    "If this change is INTENDED, regenerate with `python -m tests.rank_golden.regen` and paste the "
    "diff below into the PR description with a reason (see tests/rank_golden/README.md)."
)


@pytest.fixture(scope="module")
def layout(tmp_path_factory: pytest.TempPathFactory) -> RepoLayout:
    """The golden corpus, built exactly the way :mod:`regen` builds it.

    The parent is a temp dir but the repo directory NAME is ``regen.REPO_NAME``: ``Wiki.repo`` is
    the layout root's directory name and lands in the record header, so building under a raw
    ``tmp_path`` name would make the record depend on the test runner's temp path.
    """
    return regen.build_corpus(tmp_path_factory.mktemp("golden"))


@pytest.fixture(scope="module")
def queries() -> list[QuerySpec]:
    return load_queries(regen.QUERIES_PATH)


def _normalized(record: dict[str, Any]) -> dict[str, Any]:
    """A copy with the volatile ``agora_version`` masked; everything else is compared verbatim."""
    header = {**record["header"], "agora_version": _VERSION_SENTINEL}
    return {**record, "header": header}


def _load_golden(path: Path) -> dict[str, Any]:
    assert path.is_file(), (
        f"missing golden {path}; regenerate with `python -m tests.rank_golden.regen`"
    )
    return json.loads(path.read_text(encoding="utf-8"))


# --- the pins -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fm", "golden_path"),
    [
        pytest.param(True, regen.GOLDEN_FM_ON, id="fm-on"),
        pytest.param(False, regen.GOLDEN_FM_OFF, id="fm-off"),
    ],
)
def test_snapshot_matches_the_committed_golden(
    layout: RepoLayout, queries: list[QuerySpec], fm: bool, golden_path: Path
) -> None:
    """A fresh snapshot equals the committed record, field for field, in BOTH §8 modes.

    Both columns of the ADR-0012 §8 table are pinned because the frontmatter boost and the
    structural term are the two things the layout flip can plausibly disturb, and pinning only the
    live mode would hide a change that the boost happens to mask (in ``fm=off`` the contested and
    stub notes rise several ranks — see README.md).
    """
    fresh = _normalized(regen.record(layout, queries, fm=fm))
    golden = _normalized(_load_golden(golden_path))

    differences = diff_snapshots(golden, fresh)
    assert not differences, (
        f"ranking moved against {golden_path.name} ({len(differences)} differences).\n"
        + _REGEN_HINT
        + "\n"
        + "\n".join(differences)
    )
    # `diff_snapshots` reports every recorded field except `question` (which is the query file's
    # own text). The equality below is still the contract — it needs no list of fields to stay
    # current — and the diff above exists so a failure is readable rather than a dict dump.
    assert fresh == golden


def test_golden_files_are_canonically_serialized() -> None:
    """Each golden file is byte-identical to ``dumps`` of its own content.

    What this catches is a RE-SERIALIZED file — one an editor reformatted, a different dumper
    wrote, or a merge resolved by hand into non-canonical whitespace/escaping — so that the two
    committed records and anything ``agora eval --out`` writes stay byte-comparable.

    What it does NOT catch is a changed VALUE: an edit that leaves the file canonically formatted
    (nudging a score, a rank, a note name) round-trips through ``dumps(json.loads(text))``
    unchanged and passes here. ``test_snapshot_matches_the_committed_golden`` is what catches that,
    by re-running the ranker and comparing field for field — a golden is defended by reproduction,
    never by its own formatting.
    """
    for path in (regen.GOLDEN_FM_ON, regen.GOLDEN_FM_OFF):
        text = path.read_text(encoding="utf-8")
        assert text == dumps(json.loads(text)), (
            f"{path.name} is not canonically serialized (hand-edited?); "
            "regenerate with `python -m tests.rank_golden.regen`"
        )


def test_golden_header_records_the_conditions_the_numbers_mean(queries: list[QuerySpec]) -> None:
    """The header states the mode, the corpus size, and the record schema it was taken under."""
    on = _load_golden(regen.GOLDEN_FM_ON)["header"]
    off = _load_golden(regen.GOLDEN_FM_OFF)["header"]
    for header, fm_enabled in ((on, True), (off, False)):
        assert header["snapshot_schema_version"] == SNAPSHOT_SCHEMA_VERSION
        assert header["repo"] == regen.REPO_NAME
        assert header["kb_schema_version"] == 1, "the v1 layout is what gate B pins"
        assert header["fm_enabled"] is fm_enabled
        assert header["query_count"] == len(queries)
        assert header["corpus_note_count"] == 46
        # The policy flag is on (it is the default) but the cached read path was NOT engaged:
        # `kb_builder` deliberately runs no `git init`, so there is no curated commit, no usable
        # cache, and every number here came from the full scan — which IS the ADR-0012 oracle.
        # Gate B pins the full-scan ranking only; the §2 promise that a cached read is
        # byte-identical to a scan is a separate, git-backed fixture and is NOT covered here.
        assert header["index_cache_enabled"] is True
        assert header["index_cache_used"] is False


def test_every_query_expectation_holds(queries: list[QuerySpec]) -> None:
    """``queries.yaml``'s declared intent holds against the committed record.

    Two claims per query: the ``status`` matches ``expect`` (the ADR-0012 §5 honesty gate — a
    negative that starts answering means the floor or the evidence gate moved), and for a positive
    the declared ``note`` is at the rank the query file declares. A query whose expected note is
    NOT first is honest about it via ``observed_rank`` rather than by a corpus tweak that forces the
    ranking; those are listed in README.md as known gaps. As of this recording there is one (p27).
    """
    record = _load_golden(regen.GOLDEN_FM_ON)
    rows = {row["id"]: row for row in record["queries"]}
    assert list(rows) == [q.id for q in queries], "golden and query file disagree on the query set"

    failures: list[str] = []
    for spec in queries:
        row = rows[spec.id]
        if row["status"] != spec.expect:
            failures.append(f"{spec.id}: expect {spec.expect}, got {row['status']}")
            continue
        if spec.note is None:
            assert not row["hits"], f"{spec.id}: a not_found query must carry no hits"
            continue
        rank = next((h["rank"] for h in row["hits"] if h["note"] == spec.note), None)
        expected_rank = spec.observed_rank or 1
        if rank != expected_rank:
            where = "absent from the hits" if rank is None else f"rank {rank}"
            failures.append(
                f"{spec.id}: expected note {spec.note!r} is {where}, not {expected_rank}"
            )
    assert not failures, "\n".join(failures)


def test_a_lexless_candidate_can_never_clear_the_floor() -> None:
    """Why `p25` pins the #146 GUARD and not ``_passes_gate``'s second branch — and why it cannot.

    Since the #146 fix, ``_combined`` gives a ``lex == 0`` candidate no structural term at all, so
    its whole score is its frontmatter boost: at most ``+0.10``, which is under ``FLOOR = 0.18``.
    Such a candidate is therefore ALWAYS filtered before it can become a hit, and deleting the
    ``d_moc == 0`` admission branch entirely changes no snapshot anywhere — measured: the golden is
    byte-identical with that branch removed.

    That is a real limit of any output-level fixture, stated here rather than left for the next
    reader to rediscover: gate B pins the GUARD (revert it and `p25`'s husk reappears at 0.28), and
    the branch itself is covered by the unit regression in
    ``tests/core/test_wiki_lexical_evidence_146.py``. The arithmetic below is what makes the claim
    checkable instead of a comment that could quietly stop being true.
    """
    statuses = ("active", "stub", "contested", "deprecated", "unknown", "")
    assert max(wiki_mod._fm(s) for s in statuses) < wiki_mod.FLOOR  # noqa: SLF001
    # Even a perfect structural score (d_moc == 0 AND max in-degree) cannot lift lex == 0.
    assert wiki_mod._combined(0.0, 1.0, wiki_mod._fm("active")) < wiki_mod.FLOOR  # noqa: SLF001


def test_build_kb_ignores_spec_order(tmp_path: Path) -> None:
    """Reversing the spec list produces a BYTE-IDENTICAL tree, not merely an equal record.

    Stronger than the ranking comparison it replaces, and honest about what it shows: because every
    MOC in ``corpus.py`` carries an explicit ``children:`` list, spec order does not even reorder a
    child bullet, so the two trees are the same bytes. Asserting the FILES (not a snapshot of them)
    is what makes that a real claim — comparing two snapshots of two identical trees would have
    asserted nothing at all, which is what this test used to do under the name
    ``test_snapshot_is_invariant_to_note_creation_order``.
    """
    forward = build_kb(tmp_path / "forward", CORPUS, domains=DOMAINS)
    reverse = build_kb(tmp_path / "reverse", list(reversed(CORPUS)), domains=DOMAINS)

    rel_f = sorted(p.relative_to(forward).as_posix() for p in forward.rglob("*") if p.is_file())
    rel_r = sorted(p.relative_to(reverse).as_posix() for p in reverse.rglob("*") if p.is_file())
    assert rel_f == rel_r
    for rel in rel_f:
        assert (forward / rel).read_bytes() == (reverse / rel).read_bytes(), rel


def test_the_read_path_scans_in_sorted_order_whatever_the_filesystem_says(
    layout: RepoLayout,
) -> None:
    """The record's independence from filesystem iteration order rests on ONE line in core.

    ``Wiki._iter_note_files`` sorts, so ``_CorpusStats``, the BFS frontier and every float
    accumulation see the same sequence on APFS, ext4 and NTFS alike. That is the property the
    golden actually depends on; a test that rebuilt the corpus in a different order would only
    exercise the builder (see :func:`test_build_kb_ignores_spec_order`), because directory order on
    a modern filesystem is not creation order in the first place.
    """
    rels = [rel for rel, _basename, _is_index, _path in Wiki(layout)._iter_note_files()]  # noqa: SLF001
    assert rels == sorted(rels)
    assert rels[0] == "index.md", "index.md sorts first and seeds the BFS"
