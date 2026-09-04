"""Gate B: the ranking, pinned on both sides of the Stratum layout flip.

Two records live here and they are not symmetrical.

``golden_v1*.json`` is the PRE-flip baseline, recorded when ``core.wiki._is_moc_path`` read the MOC
out of the path (``wiki/<domain>/<domain>-moc.md``). It is **frozen**: since ADR-0041 D5 the map
tier is a DIRECTORY (``wiki/maps/…``), a v1 path names no kind at all, and this build cannot
reproduce that record — D5 says so, which is why the flip ADDS a record instead of overwriting one.
What still defends it is :func:`~tests.rank_golden.regen.frozen_baseline_drift`: a schema-1 run
under today's ranker differs from it by exactly
:data:`~tests.rank_golden.regen.PRE_FLIP_SEED_LOSS_DIFF_LINES` lines — the loss of every
``d_moc = 0`` seed, which is precisely the mutation README.md's coverage table measured as the
simulated flip.

``golden_v2*.json`` is the LIVE record over the kind-first layout, and it is defended the ordinary
way: a fresh snapshot must equal it field for field, in both ADR-0012 §8 frontmatter modes.

WHAT IS ASSERTED, AND WHAT IS NOT. Per ADR-0012 §0a nothing outside ``core`` computes ``lex`` /
``struct`` / ``fm`` / ``score`` / ``match_reason``; these tests never recompute a ranking quantity,
they compare a fresh transcription of ``Wiki.query`` against a committed one. They also do not
assert that the ranking is GOOD — only that it is what it was. The judgement calls live in
``queries.yaml`` (``expect`` + the expected top ``note``), which is asserted separately so a golden
that silently drifted into answering the wrong note still fails.

``header["agora_version"]`` is deliberately excluded from every comparison: a version bump must not
turn this gate red, because a red gate people learn to fix by regenerating is not a gate.

FLIP-DIFF.md is the D5 deliverable — the full ``diff_snapshots`` listing from ``golden_v1`` to
``golden_v2``, explained per category — and it is asserted here rather than trusted, so the
document cannot drift away from the records it describes.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from agora_kb.core import frontmatter
from agora_kb.core import wiki as wiki_mod
from agora_kb.core.layout import RepoLayout
from agora_kb.core.rank_snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    QuerySpec,
    diff_snapshots,
    dumps,
    load_queries,
    snapshot,
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

FLIP_DIFF_PATH = Path(__file__).resolve().parent / "FLIP-DIFF.md"

#: One embedded listing per ADR-0012 §8 column, keyed by the marker FLIP-DIFF.md carries.
_LISTING_RE = re.compile(
    r"<!-- listing:(?P<mode>fm-on|fm-off) -->\n```text\n(?P<body>.*?)\n```\n", re.S
)

#: The category table rows FLIP-DIFF.md must account the listing with: `| label | fm-on | fm-off |`.
_COUNT_ROW_RE = re.compile(r"^\|(?P<label>[^|]+)\|(?P<on>[^|]+)\|(?P<off>[^|]+)\|")


@pytest.fixture(scope="module")
def layout(tmp_path_factory: pytest.TempPathFactory) -> RepoLayout:
    """The LIVE (schema-2) golden corpus, built exactly the way :mod:`regen` builds it.

    The parent is a temp dir but the repo directory NAME is ``regen.repo_name(2)``: ``Wiki.repo``
    is the layout root's directory name and lands in the record header, so building under a raw
    ``tmp_path`` name would make the record depend on the test runner's temp path.
    """
    return regen.build_corpus(tmp_path_factory.mktemp("golden-v2"), schema_version=2)


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


def _flip_listing(mode: str) -> list[str]:
    """The listing FLIP-DIFF.md publishes for one ``fm`` column."""
    text = FLIP_DIFF_PATH.read_text(encoding="utf-8")
    found = {m.group("mode"): m.group("body") for m in _LISTING_RE.finditer(text)}
    assert set(found) == {"fm-on", "fm-off"}, (
        f"FLIP-DIFF.md must embed one `<!-- listing:fm-on -->` and one `<!-- listing:fm-off -->` "
        f"fenced block; found {sorted(found)}"
    )
    return found[mode].split("\n")


# --- the live record (schema 2) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fm", "golden_path"),
    [
        pytest.param(True, regen.GOLDEN_V2_FM_ON, id="fm-on"),
        pytest.param(False, regen.GOLDEN_V2_FM_OFF, id="fm-off"),
    ],
)
def test_snapshot_matches_the_committed_golden(
    layout: RepoLayout, queries: list[QuerySpec], fm: bool, golden_path: Path
) -> None:
    """A fresh schema-2 snapshot equals the committed record, field for field, in BOTH §8 modes.

    Both columns of the ADR-0012 §8 table are pinned because the frontmatter boost and the
    structural term are the two things a layout change can plausibly disturb, and pinning only the
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


# --- the frozen baseline (schema 1) --------------------------------------------------------------


@pytest.fixture(scope="module")
def frozen_drift() -> dict[str, list[str]]:
    """The frozen baseline's drift, measured once — it builds a corpus and runs 88 queries."""
    return regen.frozen_baseline_drift()


@pytest.mark.parametrize("mode", ["fm_on", "fm_off"])
def test_the_frozen_baseline_moved_by_exactly_the_lost_seeds(
    mode: str, frozen_drift: dict[str, list[str]]
) -> None:
    """``golden_v1`` is unreproducible in exactly ONE named way, and that way is measured.

    A schema-1 corpus read by today's ranker has no maps at all: ADR-0041 D5 moved the map tier
    into ``wiki/maps/``, so a v1 path declares no kind and the corpus gets no ``d_moc = 0`` seeds
    ("no shim", deliberately). That is the same mutation README.md's coverage table simulated by
    forcing ``_is_moc_path`` to ``False``, and it lands on the same number — which is why this test
    can pin it: :data:`regen.PRE_FLIP_SEED_LOSS_DIFF_LINES` lines per column.

    If this goes red, something OTHER than the layout predicate changed the ranking. The frozen
    record has no other defence — it cannot be regenerated — so this number is it.
    """
    assert frozen_drift, (
        "the frozen golden_v1 records are missing; they are history and must be restored"
    )
    lines = frozen_drift[mode]
    assert len(lines) == regen.PRE_FLIP_SEED_LOSS_DIFF_LINES, (
        f"a schema-1 run now differs from the frozen golden_v1 baseline by {len(lines)} lines, "
        f"not {regen.PRE_FLIP_SEED_LOSS_DIFF_LINES}. The baseline CANNOT be regenerated — see "
        f"FLIP-DIFF.md — so this is a ranking change to explain, not a number to update casually."
        + "\n"
        + "\n".join(lines[:20])
    )
    assert not [ln for ln in lines if ln.startswith("header:")], (
        "the two records must be taken under identical conditions; a header difference means they "
        "are not comparable"
    )
    assert not [ln for ln in lines if " status " in ln], (
        "losing the structural seeds must not flip any query's ADR-0012 §5 status"
    )


def test_regen_refuses_to_rewrite_the_frozen_baseline() -> None:
    """``write_records`` will not touch ``golden_v1*.json`` — the refusal IS the preservation.

    D5 requires the pre-flip records to be kept alongside rather than overwritten before the
    listing is taken. A convention would have been enough right up until the first person ran the
    obvious command; a raise is enough afterwards too.
    """
    records = {"fm_on": {"header": {}, "queries": []}, "fm_off": {"header": {}, "queries": []}}
    with pytest.raises(ValueError, match="FROZEN"):
        regen.write_records(records, schema_version=1)


def test_regen_writes_the_live_record_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full ``regen`` run reproduces ``golden_v2*`` byte-for-byte and leaves ``golden_v1*`` alone.

    The live output is redirected into ``tmp_path`` so the run cannot half-write the working tree
    on a red day; the committed v1 files are digested before and after, which is the claim that
    matters (``write_records``' refusal covers the deliberate call, this covers the whole command).
    """
    frozen = regen.golden_paths(1)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in frozen}

    redirected = (tmp_path / "golden_v2.json", tmp_path / "golden_v2_fm_off.json")
    monkeypatch.setattr(regen, "GOLDEN_V2_FM_ON", redirected[0])
    monkeypatch.setattr(regen, "GOLDEN_V2_FM_OFF", redirected[1])

    assert regen.main([]) == 0, "regen reported a queries.yaml violation"

    for written, committed in zip(redirected, regen.golden_paths(2), strict=True):
        assert written.read_bytes() == committed.read_bytes(), (
            f"a fresh regeneration does not reproduce {committed.name} byte-for-byte"
        )
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in frozen} == before


# --- both records --------------------------------------------------------------------------------


def test_golden_files_are_canonically_serialized() -> None:
    """Each golden file is byte-identical to ``dumps`` of its own content.

    What this catches is a RE-SERIALIZED file — one an editor reformatted, a different dumper
    wrote, or a merge resolved by hand into non-canonical whitespace/escaping — so that the four
    committed records and anything ``agora eval --out`` writes stay byte-comparable.

    What it does NOT catch is a changed VALUE: an edit that leaves the file canonically formatted
    (nudging a score, a rank, a note name) round-trips through ``dumps(json.loads(text))``
    unchanged and passes here. ``test_snapshot_matches_the_committed_golden`` is what catches that
    for the live record, and ``test_the_frozen_baseline_moved_by_exactly_the_lost_seeds`` for the
    frozen one — a golden is defended by reproduction, never by its own formatting.
    """
    for path in (*regen.golden_paths(1), *regen.golden_paths(2)):
        text = path.read_text(encoding="utf-8")
        assert text == dumps(json.loads(text)), (
            f"{path.name} is not canonically serialized (hand-edited?)"
        )


@pytest.mark.parametrize("schema_version", [1, 2])
def test_golden_header_records_the_conditions_the_numbers_mean(
    schema_version: int, queries: list[QuerySpec]
) -> None:
    """Each header states its layout, its mode, the corpus size, and the record schema."""
    fm_on_path, fm_off_path = regen.golden_paths(schema_version)
    on = _load_golden(fm_on_path)["header"]
    off = _load_golden(fm_off_path)["header"]
    for header, fm_enabled in ((on, True), (off, False)):
        assert header["snapshot_schema_version"] == SNAPSHOT_SCHEMA_VERSION
        assert header["repo"] == regen.repo_name(schema_version)
        assert header["kb_schema_version"] == schema_version, "each record names its own layout"
        assert header["fm_enabled"] is fm_enabled
        assert header["query_count"] == len(queries)
        # The same 46 notes on both sides: `corpus.py` is content-only and the flip renames files
        # rather than adding or merging any (the three dailies carry three different dates, so
        # ADR-0041 D2.6's journal merge is an identity here — see `_merge_journal`).
        assert header["corpus_note_count"] == 46
        # The policy flag is on (it is the default) but the cached read path was NOT engaged:
        # `kb_builder` deliberately runs no `git init`, so there is no curated commit, no usable
        # cache, and every number here came from the full scan — which IS the ADR-0012 oracle.
        # Gate B pins the full-scan ranking only; the §2 promise that a cached read is
        # byte-identical to a scan is a separate, git-backed fixture and is NOT covered here.
        assert header["index_cache_enabled"] is True
        assert header["index_cache_used"] is False


# --- what the query file asserts ------------------------------------------------------------------


def test_every_query_expectation_holds(queries: list[QuerySpec]) -> None:
    """``queries.yaml``'s declared intent holds against the LIVE record.

    Two claims per query: the ``status`` matches ``expect`` (the ADR-0012 §5 honesty gate — a
    negative that starts answering means the floor or the evidence gate moved), and for a positive
    the declared ``note`` is at the rank the query file declares. A query whose expected note is
    NOT first is honest about it via ``observed_rank`` rather than by a corpus tweak that forces the
    ranking; those are listed in README.md as known gaps. As of the flip there is still exactly one
    (p27), and the flip changed no probe's status and no probe's declared rank.

    The declared ``note`` is translated through :func:`regen.expected_note` because the flip renamed
    six files. ``queries.yaml`` keeps naming the v1 basename: the file records a JUDGEMENT about
    which note should answer, and that judgement did not change when the note moved.
    """
    record = _load_golden(regen.GOLDEN_V2_FM_ON)
    rows = {row["id"]: row for row in record["queries"]}
    assert list(rows) == [q.id for q in queries], "golden and query file disagree on the query set"

    failures: list[str] = []
    for spec in queries:
        row = rows[spec.id]
        if row["status"] != spec.expect:
            failures.append(f"{spec.id}: expect {spec.expect}, got {row['status']}")
            continue
        want = regen.expected_note(spec, schema_version=2)
        if want is None:
            assert not row["hits"], f"{spec.id}: a not_found query must carry no hits"
            continue
        rank = next((h["rank"] for h in row["hits"] if h["note"] == want), None)
        expected_rank = spec.observed_rank or 1
        if rank != expected_rank:
            where = "absent from the hits" if rank is None else f"rank {rank}"
            failures.append(f"{spec.id}: expected note {want!r} is {where}, not {expected_rank}")
    assert not failures, "\n".join(failures)


def test_the_flip_renamed_exactly_the_maps_and_the_journals() -> None:
    """The rename map is closed and named — six files, two families, both from ADR-0041.

    Pinned because the basename is the record's identity: a rename the fixture did not expect would
    show up in the listing as an unexplained drop/appear pair, and the D5 obligation is to explain
    every line. A seventh entry here means a file moved that nobody accounted for.
    """
    assert regen.basename_renames() == {
        # D5: the kind marker left the filename for the directory, so `-moc` is gone.
        "finance-moc": "finance",
        "cooking-moc": "cooking",
        "engineering-moc": "engineering",
        # D2.6: one journal per `run_date`, repo-wide — the domain no longer namespaces the date.
        "finance-2026-01-12": "2026-01-12",
        "cooking-2026-01-13": "2026-01-13",
        "engineering-2026-01-14": "2026-01-14",
    }


# --- the D5 deliverable ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "v1_path", "v2_path"),
    [
        pytest.param("fm-on", regen.GOLDEN_V1_FM_ON, regen.GOLDEN_V2_FM_ON, id="fm-on"),
        pytest.param("fm-off", regen.GOLDEN_V1_FM_OFF, regen.GOLDEN_V2_FM_OFF, id="fm-off"),
    ],
)
def test_flip_diff_publishes_the_exact_listing(mode: str, v1_path: Path, v2_path: Path) -> None:
    """FLIP-DIFF.md's embedded listing IS ``diff_snapshots(golden_v1, golden_v2)``, line for line.

    ADR-0041 D5 makes the listing a normative deliverable of the flip PR. A document that merely
    quoted a listing would start drifting the first time either record moved; asserting it means the
    explanation and the evidence fail together.
    """
    live = diff_snapshots(_load_golden(v1_path), _load_golden(v2_path))
    published = _flip_listing(mode)
    assert published == live, (
        f"FLIP-DIFF.md's {mode} listing is stale ({len(published)} lines published, "
        f"{len(live)} live). Re-derive it with "
        f"`diff_snapshots(golden_v1, golden_v2)` and re-explain what changed."
    )


def test_flip_diff_category_counts_sum_to_the_listing() -> None:
    """Every line of the listing is accounted for by exactly one category count.

    D5's obligation is *"a count per category that sums to the listing"*. This asserts the sum, in
    both columns, against the listing FLIP-DIFF.md itself publishes — so the explanation cannot
    quietly cover 400 of 423 lines.
    """
    text = FLIP_DIFF_PATH.read_text(encoding="utf-8")
    rows: list[tuple[str, int, int]] = []
    totals: dict[str, int] = {}
    for line in text.splitlines():
        m = _COUNT_ROW_RE.match(line.strip())
        if m is None:
            continue
        label = m.group("label").strip()
        cells = [m.group("on").strip().strip("*"), m.group("off").strip().strip("*")]
        if not all(c.isdigit() for c in cells):
            continue  # the table's own header/separator rows
        if label.strip("*").lower() == "total":
            totals = {"fm-on": int(cells[0]), "fm-off": int(cells[1])}
            continue
        rows.append((label, int(cells[0]), int(cells[1])))

    assert rows, "FLIP-DIFF.md publishes no per-category count table"
    assert totals, "FLIP-DIFF.md's category table has no **total** row"
    summed = {"fm-on": sum(r[1] for r in rows), "fm-off": sum(r[2] for r in rows)}
    assert summed == totals, f"the category counts {summed} do not sum to the stated total {totals}"
    for mode in ("fm-on", "fm-off"):
        assert totals[mode] == len(_flip_listing(mode)), (
            f"the {mode} total {totals[mode]} does not match the {mode} listing "
            f"({len(_flip_listing(mode))} lines)"
        )


def test_the_flip_listing_is_the_journal_merge_and_the_renames_not_the_seed_rule(
    tmp_path: Path, queries: list[QuerySpec]
) -> None:
    """The attribution FLIP-DIFF.md rests on, measured rather than argued.

    Rebuild the corpus in the schema-2 layout, then restore ONLY the three journals' v1 H1 title
    and v1 body — undoing ADR-0041 D2.6's merge as CONTENT while leaving every path, every
    frontmatter key and the whole D5 seed rule exactly as the flip left them. The record that comes
    back reproduces ``golden_v1``'s every ``score``, ``rank``, ``match_reason``, ``anchor``,
    ``line`` and ``excerpt``: what survives is the ``type`` mirror, the six renames and the header.

    That is the whole attribution. If the D5 seed rule had moved a single number, reverting a
    journal's prose could not have restored it — so the maps-replace-MOCs change is, on this
    corpus, structurally neutral, and every score in the listing belongs to the merge.
    """
    root = tmp_path / "attribution"
    build_kb(root, CORPUS, schema_version=2, domains=DOMAINS)
    for spec in (s for s in CORPUS if s.kind == "daily"):
        date = str(spec.extra_frontmatter["date"])
        matches = sorted(root.glob(f"wiki/notes/*/*/{date}.md"))
        assert len(matches) == 1, f"expected exactly one journal for {date}, got {matches}"
        fm, _merged_body = frontmatter.parse(matches[0].read_text(encoding="utf-8"))
        body = f"# {spec.title}\n\n{spec.body.strip(chr(10))}"
        matches[0].write_text(frontmatter.render(fm, body), encoding="utf-8", newline="\n")

    wiki = Wiki(RepoLayout(root))
    for fm_mode, golden_path in ((True, regen.GOLDEN_V1_FM_ON), (False, regen.GOLDEN_V1_FM_OFF)):
        fresh = snapshot(wiki, queries, fm=fm_mode)
        residual = [
            line
            for line in diff_snapshots(_load_golden(golden_path), fresh)
            if not line.startswith("header:")
            and " type " not in line
            and "dropped (was rank" not in line
            and "appeared at rank" not in line
        ]
        assert not residual, (
            "un-merging the journals did NOT restore the pre-flip ranking; the flip moved "
            "something other than the journal content:\n" + "\n".join(residual[:20])
        )


# --- pins that are layout-independent ------------------------------------------------------------


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


@pytest.mark.parametrize("schema_version", [1, 2])
def test_build_kb_ignores_spec_order(tmp_path: Path, schema_version: int) -> None:
    """Reversing the spec list produces a BYTE-IDENTICAL tree, not merely an equal record.

    Stronger than the ranking comparison it replaces, and honest about what it shows: because every
    map in ``corpus.py`` carries an explicit ``children:`` list, spec order does not even reorder a
    child bullet, so the two trees are the same bytes. Asserting the FILES (not a snapshot of them)
    is what makes that a real claim — comparing two snapshots of two identical trees would have
    asserted nothing at all, which is what this test used to do under the name
    ``test_snapshot_is_invariant_to_note_creation_order``.

    Both layouts are checked: schema 2 because it is what the live record is built from, schema 1
    because the frozen baseline's drift measurement builds one on every run.
    """
    forward = build_kb(tmp_path / "forward", CORPUS, schema_version=schema_version, domains=DOMAINS)
    reverse = build_kb(
        tmp_path / "reverse", list(reversed(CORPUS)), schema_version=schema_version, domains=DOMAINS
    )

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
