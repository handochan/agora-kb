"""Regenerate the ranking golden — the ONLY sanctioned way ``golden_v2*.json`` is produced.

    python -m tests.rank_golden.regen

Run from the repo root. It builds :data:`tests.rank_golden.corpus.CORPUS` into a throwaway
directory with :func:`tests.support.kb_builder.build_kb`, runs
:func:`agora_kb.core.rank_snapshot.snapshot` over :mod:`queries.yaml` once per ADR-0012 §8
frontmatter mode, and writes the two records with the canonical serializer.

TWO RECORDS, ONE OF THEM FROZEN. This directory now holds a record per KB wiki schema:

* ``golden_v1.json`` / ``golden_v1_fm_off.json`` — the PRE-flip baseline, taken over the ADR-0010
  v1 layout by the build that shipped ``_is_moc_path`` matching ``wiki/<d>/<d>-moc.md``. It is
  **frozen and no longer reproducible**: since ADR-0041 D5 the map tier is read out of the
  DIRECTORY, so a v1 path declares no kind at all and a v1 corpus seeds no ``d_moc = 0`` level.
  :func:`write_records` REFUSES to write it, and :func:`frozen_baseline_drift` measures exactly how
  far a schema-1 run under today's build has moved away from it.
* ``golden_v2.json`` / ``golden_v2_fm_off.json`` — the LIVE record, over the ADR-0041 kind-first
  layout this build's curator writes. This is what ``main`` regenerates.

D5 says it plainly — *"``regen`` can only produce the current layout and a discarded baseline
cannot be re-derived"* — which is why the flip ADDS a record rather than overwriting one, and why
the two files above are treated here as read-only history.

WHY A SCRIPT AND NOT A ``--regen`` FLAG ON THE TEST. A test that can rewrite its own expectation is
not a gate: the first red run gets "fixed" by regenerating, and the baseline the Stratum layout flip
is supposed to be measured against quietly becomes whatever the flip produced. Regeneration is a
deliberate, separately-reviewed act — the PR that changes a golden owes a
:func:`~agora_kb.core.rank_snapshot.diff_snapshots` listing and a reason (see README.md, and
FLIP-DIFF.md for the listing the flip itself owed).

DETERMINISM. Each corpus is built under a FIXED directory name (:func:`repo_name`) because
``header["repo"]`` is ``layout.root.name``; building under a raw ``mktemp`` name would bake the
temp directory into the record. Nothing else here reads a clock, the network, the environment, or
a model — ``build_kb`` freezes its dates at ``BUILDER_DATE`` and the ranker is the pure-Python
oracle.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from agora_kb.core.layout import RepoLayout
from agora_kb.core.rank_snapshot import QuerySpec, diff_snapshots, dumps, load_queries, snapshot
from agora_kb.core.wiki import Wiki
from tests.rank_golden.corpus import CORPUS, DOMAINS
from tests.support.kb_builder import build_kb, v2_basename

__all__ = [
    "FROZEN_SCHEMA_VERSIONS",
    "GOLDEN_V1_FM_OFF",
    "GOLDEN_V1_FM_ON",
    "GOLDEN_V2_FM_OFF",
    "GOLDEN_V2_FM_ON",
    "LIVE_SCHEMA_VERSION",
    "PRE_FLIP_SEED_LOSS_DIFF_LINES",
    "QUERIES_PATH",
    "REPO_NAMES",
    "basename_renames",
    "build_corpus",
    "expected_note",
    "frozen_baseline_drift",
    "golden_paths",
    "main",
    "record",
    "regenerate",
    "repo_name",
    "write_records",
]

HERE = Path(__file__).resolve().parent

#: The KB wiki schema this build's curator writes (ADR-0041 D6) — the record ``main`` maintains.
LIVE_SCHEMA_VERSION = 2

#: Records that are HISTORY: readable, comparable, never rewritten. See the module docstring.
FROZEN_SCHEMA_VERSIONS: tuple[int, ...] = (1,)

#: Fixed corpus directory name per schema. ``Wiki.repo`` is the layout root's directory name and
#: lands in the record header, so this — not ``tmp_path`` — is what a golden records. The two names
#: differ because the two records really are two different trees on disk; ``diff_snapshots`` reports
#: that as ``header: repo …``, which is the reporter doing its job, not noise to be suppressed.
REPO_NAMES: dict[int, str] = {1: "rank-golden-v1", 2: "rank-golden-v2"}

QUERIES_PATH = HERE / "queries.yaml"
GOLDEN_V1_FM_ON = HERE / "golden_v1.json"
GOLDEN_V1_FM_OFF = HERE / "golden_v1_fm_off.json"
GOLDEN_V2_FM_ON = HERE / "golden_v2.json"
GOLDEN_V2_FM_OFF = HERE / "golden_v2_fm_off.json"

#: How far a schema-1 corpus built by TODAY's ranker has drifted from the frozen ``golden_v1*``
#: record, in :func:`~agora_kb.core.rank_snapshot.diff_snapshots` lines, per ``fm`` column.
#:
#: This is the ADR-0041 D5 / README "simulated flip" number, and it is the same number for a
#: reason: simulating the flip meant forcing ``_is_moc_path`` to ``False``, and the schema-2
#: predicate returns exactly that for every v1 path (a v1 directory names no kind, ADR-0041 D5
#: "no shim"). It is asserted by the golden test, so a ranker change that moves it turns the gate
#: red instead of silently re-defining the baseline.
PRE_FLIP_SEED_LOSS_DIFF_LINES = 347


def _display(path: Path) -> str:
    """``path`` relative to the repo root when it is under it, else its absolute form.

    ``regen`` prints what it wrote, and a redirected output path (a test pointing the live record at
    a temp directory) is outside the tree — a printer that raised there would make the command's
    failure mode "cannot report success".
    """
    try:
        return str(path.relative_to(HERE.parents[1]))
    except ValueError:
        return str(path)


def repo_name(schema_version: int) -> str:
    """The fixed corpus directory name recorded in ``header["repo"]`` for ``schema_version``."""
    try:
        return REPO_NAMES[schema_version]
    except KeyError:
        raise ValueError(f"unknown schema_version {schema_version!r} (expected 1 or 2)") from None


def golden_paths(schema_version: int) -> tuple[Path, Path]:
    """The ``(fm-on, fm-off)`` record paths for ``schema_version``.

    The module constants are read at CALL time rather than captured in a table at import time, so a
    test can redirect the live record to a temp directory (and prove that a regeneration touches
    nothing else) without reaching into a frozen mapping.
    """
    if schema_version == 1:
        return (GOLDEN_V1_FM_ON, GOLDEN_V1_FM_OFF)
    if schema_version == 2:
        return (GOLDEN_V2_FM_ON, GOLDEN_V2_FM_OFF)
    raise ValueError(f"unknown schema_version {schema_version!r} (expected 1 or 2)")


def basename_renames() -> dict[str, str]:
    """v1 basename → schema-2 basename, for exactly the notes the flip renames.

    The record is BASENAME-keyed (see :mod:`agora_kb.core.rank_snapshot`), so a rename is the one
    thing it cannot see through: the note leaves the listing as ``dropped`` and re-enters it as
    ``appeared``. Stating the rename as a mapping over ``corpus.py`` — through
    :func:`tests.support.kb_builder.v2_basename`, which the builder exports for this purpose — is
    what lets ``queries.yaml`` keep naming the v1 note it has always named while the expectation
    test still checks the right row of the schema-2 record.

    Two families, both from ADR-0041: a map loses its ``-moc`` suffix because the kind marker moved
    into the directory (D5), and same-dated dailies merge into one ``YYYY-MM-DD`` journal (D2.6).
    """
    renames: dict[str, str] = {}
    for spec in CORPUS:
        v1, v2 = spec.basename(), v2_basename(spec)
        if v1 != v2:
            renames[v1] = v2
    return renames


def expected_note(spec: QuerySpec, *, schema_version: int) -> str | None:
    """``spec.note`` as it is basenamed under ``schema_version`` (``None`` stays ``None``).

    ``queries.yaml`` names the v1 basename throughout — deliberately, because the query file states
    a JUDGEMENT about which note should answer, and that judgement did not change when the file
    moved. The translation happens here, once.
    """
    if spec.note is None or schema_version == 1:
        return spec.note
    return basename_renames().get(spec.note, spec.note)


def build_corpus(parent: Path, *, schema_version: int = LIVE_SCHEMA_VERSION) -> RepoLayout:
    """Materialize the golden corpus under ``parent/repo_name(schema_version)``; return its layout.

    ``schema_version`` is the LAYOUT axis and nothing else: ``corpus.py`` is content-only and is
    shared verbatim by both records, which is the property that makes the flip listing a
    measurement of the layout rather than of two different corpora.
    """
    root = Path(parent) / repo_name(schema_version)
    build_kb(root, CORPUS, schema_version=schema_version, domains=DOMAINS)
    return RepoLayout(root)


def record(layout: RepoLayout, queries: list[QuerySpec], *, fm: bool) -> dict[str, Any]:
    """Snapshot ``queries`` against the corpus at ``layout`` in one explicit frontmatter mode.

    ``fm`` is passed explicitly (never left at ``None``) so the record states which ADR-0012 §8
    column it is, rather than inheriting whatever ``wiki.FM_ENABLED`` the build happens to ship.
    """
    return snapshot(Wiki(layout), queries, fm=fm)


def regenerate(*, schema_version: int = LIVE_SCHEMA_VERSION) -> dict[str, dict[str, Any]]:
    """Rebuild both ``fm`` records for ``schema_version`` from ``corpus.py`` + ``queries.yaml``."""
    queries = load_queries(QUERIES_PATH)
    with tempfile.TemporaryDirectory(prefix="agora-rank-golden-") as tmp:
        layout = build_corpus(Path(tmp), schema_version=schema_version)
        return {
            "fm_on": record(layout, queries, fm=True),
            "fm_off": record(layout, queries, fm=False),
        }


def write_records(
    records: dict[str, dict[str, Any]], *, schema_version: int = LIVE_SCHEMA_VERSION
) -> list[Path]:
    """Write the two records for ``schema_version`` canonically; return the paths written.

    Raises :class:`ValueError` for a schema in :data:`FROZEN_SCHEMA_VERSIONS`. That refusal is the
    mechanism, not a convention: ``golden_v1*.json`` records a ranking this build can no longer
    produce, so a regeneration that "helpfully" refreshed it would destroy the only artefact the
    flip is measured against — and D5 requires the pre-flip records to be PRESERVED, not
    overwritten in place before the listing is taken.
    """
    if schema_version in FROZEN_SCHEMA_VERSIONS:
        raise ValueError(
            f"the schema-{schema_version} record is FROZEN history (ADR-0041 D5): `regen` writes "
            f"only the live schema-{LIVE_SCHEMA_VERSION} record. The pre-flip baseline cannot be "
            f"re-derived by this build — see tests/rank_golden/FLIP-DIFF.md."
        )
    fm_on_path, fm_off_path = golden_paths(schema_version)
    written = []
    for path, rec in ((fm_on_path, records["fm_on"]), (fm_off_path, records["fm_off"])):
        path.write_text(dumps(rec), encoding="utf-8", newline="\n")
        written.append(path)
    return written


def frozen_baseline_drift() -> dict[str, list[str]]:
    """How far a schema-1 run under THIS build has moved from the committed ``golden_v1*`` record.

    Returns ``{"fm_on": [...], "fm_off": [...]}`` — the ``diff_snapshots`` listing per ADR-0012 §8
    column — or ``{}`` when a baseline file is missing.

    This is the frozen record's only remaining defence. It cannot be reproduced (that is what
    "frozen" means here), but it CAN be reproduced *modulo one named change*: the schema-2 map
    predicate reads no kind out of a v1 path, so a v1 corpus loses every ``d_moc = 0`` seed and
    every note's structural term flattens. That is exactly the mutation README.md's coverage table
    measured as the simulated flip, at :data:`PRE_FLIP_SEED_LOSS_DIFF_LINES` lines per column — so
    the number stays checkable, and a ranker change that moves it is visible rather than absorbed.
    """
    fm_on_path, fm_off_path = golden_paths(1)
    if not (fm_on_path.is_file() and fm_off_path.is_file()):
        return {}
    queries = load_queries(QUERIES_PATH)
    with tempfile.TemporaryDirectory(prefix="agora-rank-golden-v1-") as tmp:
        layout = build_corpus(Path(tmp), schema_version=1)
        return {
            "fm_on": diff_snapshots(
                json.loads(fm_on_path.read_text(encoding="utf-8")),
                record(layout, queries, fm=True),
            ),
            "fm_off": diff_snapshots(
                json.loads(fm_off_path.read_text(encoding="utf-8")),
                record(layout, queries, fm=False),
            ),
        }


def main(argv: list[str] | None = None) -> int:
    """Write the live records, report the frozen baseline, then gate on ``queries.yaml``.

    The files are written even on a violation, ON PURPOSE: the documented way to record a ranking
    gap is to run this, READ the rank it prints, and add ``observed_rank: <N>`` to ``queries.yaml``
    (see README.md) — refusing to write would make that workflow impossible. What must not happen
    is that a scripted ``regen && git add`` treats a contradicted baseline as a success, so the
    EXIT CODE gates instead: ``1`` when any query's status differs from its ``expect``, or when a
    positive's expected note is not at the rank the query file declares (``observed_rank``, default
    1). A declared gap that still holds is reported and does NOT fail.

    The frozen-baseline line is a MEASUREMENT, not a gate: ``golden_v1*.json`` is history and this
    command cannot rewrite it. The golden test is what asserts the number.
    """
    if argv:
        print("usage: python -m tests.rank_golden.regen", file=sys.stderr)
        return 2

    queries = {q.id: q for q in load_queries(QUERIES_PATH)}
    records = regenerate(schema_version=LIVE_SCHEMA_VERSION)
    for path in write_records(records, schema_version=LIVE_SCHEMA_VERSION):
        print(f"wrote {_display(path)}")

    live = records["fm_on"]
    header = live["header"]
    print(
        f"  schema={header['kb_schema_version']} notes={header['corpus_note_count']} "
        f"queries={header['query_count']} limit={header['limit']} fm=on "
        f"cache={'on' if header['index_cache_enabled'] else 'off'} "
        f"cache_used={'yes' if header['index_cache_used'] else 'no'}"
    )

    drift = frozen_baseline_drift()
    for mode in ("fm_on", "fm_off"):
        if mode in drift:
            print(
                f"  frozen golden_v1 [{mode}]: {len(drift[mode])} lines from a schema-1 run under "
                f"this build (expected {PRE_FLIP_SEED_LOSS_DIFF_LINES} — the lost d_moc seeds; "
                f"see FLIP-DIFF.md)"
            )

    violations = 0
    for row in live["queries"]:
        spec = queries[row["id"]]
        if row["status"] != spec.expect:
            print(f"  VIOLATION {row['id']}: expect {spec.expect}, got {row['status']}")
            violations += 1
            continue
        want = expected_note(spec, schema_version=LIVE_SCHEMA_VERSION)
        if want is None:
            continue
        rank = next((h["rank"] for h in row["hits"] if h["note"] == want), None)
        expected = spec.observed_rank or 1
        if rank == expected:
            if spec.observed_rank is not None:
                print(f"  KNOWN GAP {row['id']}: {want!r} at declared rank {expected}")
            continue
        where = "absent from the hits" if rank is None else f"rank {rank}"
        print(f"  VIOLATION {row['id']}: expected note {want!r} is {where}, not {expected}")
        violations += 1

    if violations:
        print(
            f"  {violations} violation(s): the files above CONTRADICT queries.yaml. Either fix the"
            " ranking or record the truth (`observed_rank:`) — do not commit them as they stand.",
            file=sys.stderr,
        )
    return 1 if violations else 0


if __name__ == "__main__":  # pragma: no cover — exercised through `python -m`.
    raise SystemExit(main(sys.argv[1:]))
