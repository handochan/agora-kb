"""Regenerate the ranking golden — the ONLY sanctioned way ``golden_v1*.json`` is produced.

    python -m tests.rank_golden.regen

Run from the repo root. It builds :data:`tests.rank_golden.corpus.CORPUS` into a throwaway
directory with :func:`tests.support.kb_builder.build_kb`, runs
:func:`agora_kb.core.rank_snapshot.snapshot` over :mod:`queries.yaml` once per ADR-0012 §8
frontmatter mode, and writes the two records with the canonical serializer.

WHY A SCRIPT AND NOT A ``--regen`` FLAG ON THE TEST. A test that can rewrite its own expectation is
not a gate: the first red run gets "fixed" by regenerating, and the baseline the Stratum layout flip
is supposed to be measured against quietly becomes whatever the flip produced. Regeneration is a
deliberate, separately-reviewed act — the PR that changes a golden owes a
:func:`~agora_kb.core.rank_snapshot.diff_snapshots` listing and a reason (see README.md).

DETERMINISM. The corpus is built under a FIXED directory name (:data:`REPO_NAME`) because
``header["repo"]`` is ``layout.root.name``; building under a raw ``mktemp`` name would bake the
temp directory into the record. Nothing else here reads a clock, the network, the environment, or
a model — ``build_kb`` freezes its dates at ``BUILDER_DATE`` and the ranker is the pure-Python
oracle.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

from agora_kb.core.layout import RepoLayout
from agora_kb.core.rank_snapshot import QuerySpec, dumps, load_queries, snapshot
from agora_kb.core.wiki import Wiki
from tests.rank_golden.corpus import CORPUS, DOMAINS
from tests.support.kb_builder import build_kb

__all__ = [
    "REPO_NAME",
    "QUERIES_PATH",
    "GOLDEN_FM_ON",
    "GOLDEN_FM_OFF",
    "build_corpus",
    "record",
    "regenerate",
    "main",
]

HERE = Path(__file__).resolve().parent

#: Fixed corpus directory name. ``Wiki.repo`` is the layout root's directory name and lands in the
#: record header, so this — not ``tmp_path`` — is what the golden records.
REPO_NAME = "rank-golden-v1"

QUERIES_PATH = HERE / "queries.yaml"
GOLDEN_FM_ON = HERE / "golden_v1.json"
GOLDEN_FM_OFF = HERE / "golden_v1_fm_off.json"


def build_corpus(parent: Path) -> RepoLayout:
    """Materialize the golden corpus under ``parent/REPO_NAME`` and return its layout."""
    root = Path(parent) / REPO_NAME
    build_kb(root, CORPUS, domains=DOMAINS)
    return RepoLayout(root)


def record(layout: RepoLayout, queries: list[QuerySpec], *, fm: bool) -> dict[str, Any]:
    """Snapshot ``queries`` against the corpus at ``layout`` in one explicit frontmatter mode.

    ``fm`` is passed explicitly (never left at ``None``) so the record states which ADR-0012 §8
    column it is, rather than inheriting whatever ``wiki.FM_ENABLED`` the build happens to ship.
    """
    return snapshot(Wiki(layout), queries, fm=fm)


def regenerate() -> dict[str, dict[str, Any]]:
    """Rebuild both records from ``corpus.py`` + ``queries.yaml``; return them keyed by mode."""
    queries = load_queries(QUERIES_PATH)
    with tempfile.TemporaryDirectory(prefix="agora-rank-golden-") as tmp:
        layout = build_corpus(Path(tmp))
        return {
            "fm_on": record(layout, queries, fm=True),
            "fm_off": record(layout, queries, fm=False),
        }


def main(argv: list[str] | None = None) -> int:
    """Write both golden files, then report violations; exit non-zero if any survived.

    The files are written even on a violation, ON PURPOSE: the documented way to record a ranking
    gap is to run this, READ the rank it prints, and add ``observed_rank: <N>`` to ``queries.yaml``
    (see README.md) — refusing to write would make that workflow impossible. What must not happen
    is that a scripted ``regen && git add`` treats a contradicted baseline as a success, so the
    EXIT CODE gates instead: ``1`` when any query's status differs from its ``expect``, or when a
    positive's expected note is not at the rank the query file declares (``observed_rank``, default
    1). A declared gap that still holds is reported and does NOT fail.
    """
    if argv:
        print("usage: python -m tests.rank_golden.regen", file=sys.stderr)
        return 2

    queries = {q.id: q for q in load_queries(QUERIES_PATH)}
    records = regenerate()
    for path, rec in ((GOLDEN_FM_ON, records["fm_on"]), (GOLDEN_FM_OFF, records["fm_off"])):
        path.write_text(dumps(rec), encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(HERE.parents[1])}")

    live = records["fm_on"]
    header = live["header"]
    print(
        f"  notes={header['corpus_note_count']} queries={header['query_count']} "
        f"limit={header['limit']} fm=on cache={'on' if header['index_cache_enabled'] else 'off'} "
        f"cache_used={'yes' if header['index_cache_used'] else 'no'}"
    )
    violations = 0
    for row in live["queries"]:
        spec = queries[row["id"]]
        if row["status"] != spec.expect:
            print(f"  VIOLATION {row['id']}: expect {spec.expect}, got {row['status']}")
            violations += 1
            continue
        if spec.note is None:
            continue
        rank = next((h["rank"] for h in row["hits"] if h["note"] == spec.note), None)
        expected = spec.observed_rank or 1
        if rank == expected:
            if spec.observed_rank is not None:
                print(f"  KNOWN GAP {row['id']}: {spec.note!r} at declared rank {expected}")
            continue
        where = "absent from the hits" if rank is None else f"rank {rank}"
        print(f"  VIOLATION {row['id']}: expected note {spec.note!r} is {where}, not {expected}")
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
