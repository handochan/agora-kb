"""Deterministic ranking SNAPSHOT — a model-free golden fixture over :meth:`Wiki.query` (#44).

WHY THIS EXISTS. Stratum flipped the wiki layout axis (v1 ``wiki/<domain>/themes|daily`` +
``<domain>-moc.md`` → the kind-first tree of ADR-0041 D1). :func:`agora_kb.core.wiki._is_map_path`
— still exported under its pre-Stratum name ``_is_moc_path`` — reads the map tier out of the PATH
(``wiki/maps/…`` now, ``wiki/<domain>/<domain>-moc.md`` before) and seeds the ``d_moc`` structural
score for the WHOLE corpus, so moving a file changes ranking and, downstream, gold-pack selection.
Without a baseline recorded BEFORE the flip, that change could not have been attributed to the flip
rather than to anything else in the diff. This module recorded that baseline and is what keeps the
frozen pre-flip record (``tests/rank_golden/golden_v1*.json``) comparable against the live
post-flip one (``golden_v2*.json``) — see :func:`diff_snapshots`, and
``tests/rank_golden/README.md`` for the two-record arrangement.

WHAT IT IS NOT. It is **not a scorer**. ADR-0012 §0a is explicit: the pure-Python oracle in
:mod:`agora_kb.core.wiki` is the ONLY component that computes ``lex`` / ``struct`` / ``fm`` /
``score`` / ``match_reason`` / ``anchor`` / ``line`` / ``excerpt``. This module calls
:meth:`Wiki.query` and TRANSCRIBES what comes back. It contains no arithmetic on any ranking
quantity beyond a repr-stabilizing ``round(x, 6)`` (see :func:`_round6`) and a 1-based enumeration
of the order the oracle already fixed. If a future edit here starts deriving a ranking value, the
golden fixture has stopped being evidence about the ranker and has become evidence about itself.

WHY IDENTITY IS THE BASENAME. A record keyed on ``SearchHit.path`` would report every note as
"gone, and a new one appeared" the moment the layout axis flips — the exact change the fixture is
meant to measure through. Basenames are globally unique per repo (DATA-MODEL §10, ADR-0010 §3.1)
and survive a directory move, so ``note`` here is ``Path(hit.path).stem``. The paths themselves are
deliberately absent from the record: their whole content is layout, which is the axis under test.

DETERMINISM. No clock, no locale, no network, no model, no randomness, and no dependence on
filesystem iteration order (:meth:`Wiki.query` fixes scan order internally, and everything this
module iterates is either that output or a sorted derivative). Two calls on the same corpus produce
equal dicts; ``json.dumps`` of them is byte-identical.

The companion :func:`diff_snapshots` turns two such records into human-readable lines — status
flips, rank moves, score deltas — which was the actual deliverable at the flip (ADR-0041 D5's
normative PR requirement) and remains the deliverable any PR that moves a golden owes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from .. import __version__
from . import wiki as wiki_mod
from .wiki import Wiki

__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "QuerySpec",
    "QueryFileError",
    "load_queries",
    "snapshot",
    "diff_snapshots",
    "dumps",
]

# Bumped whenever the RECORD SHAPE below changes, so an old golden file cannot be silently compared
# against a new one field-by-field. It is unrelated to (and never confused with) the repo's KB
# ``schema_version`` or ``index_cache.CACHE_SCHEMA_VERSION``.
SNAPSHOT_SCHEMA_VERSION = 2

# Every recorded float is quantized here. ADR-0012 §6.1 already rounds each component and the
# combined score to 6 decimals inside the oracle, so this is idempotent on today's values; it is
# applied anyway because a golden file must be repr-stable — an unrounded float that ever reached
# the record would serialize with 17 significant digits and turn a sub-ULP difference into a
# spurious diff line.
_ROUND_DECIMALS = 6


class QueryFileError(ValueError):
    """A query file is missing, unparseable, or does not describe a list of valid queries.

    Deliberately loud (ADR-0014 D1 strict-producer posture): an eval harness that silently skips a
    malformed query would report a green baseline it never actually measured.
    """


class QuerySpec(BaseModel):
    """One evaluation probe: a question plus the status its author asserts is correct.

    ``expect`` is the CI gate — ``ok`` means "this corpus must answer", ``not_found`` means "this
    corpus must honestly decline" (ADR-0012 §5). ``note``, ``tags``, ``rationale`` and
    ``observed_rank`` are human metadata for the query file only; they are NOT transcribed into
    the snapshot, so editing a comment can never churn the golden record.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    question: str
    expect: Literal["ok", "not_found"] = "ok"
    note: str | None = None
    rationale: str | None = None
    observed_rank: int | None = None
    tags: tuple[str, ...] = ()


def load_queries(path: str | Path) -> list[QuerySpec]:
    """Load and validate a query file (YAML or JSON) into :class:`QuerySpec` objects.

    The document must be a LIST of mappings, each with ``id`` and ``question`` (plus optional
    ``expect`` / ``note`` / ``tags``). YAML is a superset of JSON, so one ``safe_load`` reads both
    ``.yaml`` and ``.json`` files; ``safe_load`` is used (never ``load``) so a query file can never
    construct a Python object.

    Raises :class:`QueryFileError` — never returns a partial list — when the file is unreadable, is
    not a list, holds an invalid entry, is empty, or repeats an ``id``. Duplicate ids are rejected
    because the record and every diff line are keyed on the id: two entries sharing one would make
    a snapshot ambiguous rather than merely redundant.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise QueryFileError(f"cannot read query file {p}: {exc}") from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise QueryFileError(f"{p}: not valid YAML/JSON: {exc}") from exc

    if not isinstance(doc, list):
        raise QueryFileError(f"{p}: expected a LIST of queries, got {type(doc).__name__}")
    if not doc:
        raise QueryFileError(f"{p}: contains no queries (an empty eval set would gate nothing)")

    specs: list[QuerySpec] = []
    seen: set[str] = set()
    for i, entry in enumerate(doc):
        if not isinstance(entry, dict):
            raise QueryFileError(f"{p}: query #{i} is a {type(entry).__name__}, expected a mapping")
        try:
            spec = QuerySpec.model_validate(entry)
        except ValidationError as exc:
            raise QueryFileError(f"{p}: query #{i} is invalid: {exc}") from exc
        if spec.id in seen:
            raise QueryFileError(f"{p}: duplicate query id {spec.id!r} (ids must be unique)")
        seen.add(spec.id)
        specs.append(spec)
    return specs


def snapshot(
    wiki: Wiki,
    queries: Sequence[QuerySpec],
    *,
    fm: bool | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run every query against ``wiki`` and return a JSON-serialisable, LAYOUT-INDEPENDENT record.

    ``fm`` selects the ADR-0012 §8 frontmatter-boost mode: ``None`` (default) records whatever the
    build has live, ``True``/``False`` force it for the duration of this call only (restored in a
    ``finally``; see :func:`_fm_mode` for why the toggle is a module global and what that costs).
    ``limit`` is passed straight to :meth:`Wiki.query`; ``None`` means the ADR's ``max_hits``. A
    limit below ``1`` raises :class:`ValueError` rather than recording an all-``ok``, all-empty
    baseline (see the guard below).

    The returned dict is ``{"header": {...}, "queries": [...]}``. Each query record is
    ``{id, question, expect, status, hits}`` and each hit is
    ``{note, title, type, score, lex, struct, fm, match_reason, anchor, line, excerpt, rank}``,
    where ``note`` is the basename (never the path — see the module docstring) and ``rank`` is
    1-based in the order the oracle returned.

    ``anchor`` / ``line`` / ``excerpt`` ARE recorded: they are on the frozen ``SearchHit`` contract
    (ADR-0012 §0) and they derive from note CONTENT — a heading slug, a 1-based line inside the
    body, a body window — never from the path, so recording them costs nothing in flip robustness
    and gives the §7 extraction contract a baseline it would otherwise not have.

    ``lex`` / ``struct`` / ``fm`` are recorded as ``None`` on today's build. They are real
    quantities inside the oracle but they are NOT part of the frozen ``SearchHit`` contract
    (ADR-0012 §0, which exposes ``score`` only), and recomputing them out here is precisely what
    §0a forbids. The keys are present so that if core ever grows an explain seam that carries them
    on the hit, they populate with no change to :data:`SNAPSHOT_SCHEMA_VERSION` consumers.

    The header records the cache POLICY (``index_cache_enabled``) and, separately, whether the
    ADR-0012 §2 cached read path was actually engaged for this snapshot (``index_cache_used``) —
    the policy flag alone would read as if the cache had been exercised when a git-free fixture
    makes it structurally impossible.
    """
    policy = wiki._index_policy()  # noqa: SLF001 — same package; core's own read of its own config.
    # The ranker's OWN parse of the corpus, reused rather than re-derived: this is the note list
    # `query` scores, so `title` here is exactly the title the oracle saw (first H1, else
    # frontmatter `title:`, else the de-kebabbed basename) and `corpus_note_count` is exactly its
    # N. Deriving a title out here would be a second, drift-prone parser.
    internal = {n.path: n for n in wiki._load_notes(policy)}  # noqa: SLF001
    # `type:` is a frontmatter declaration the ranker does not model, so it comes from the public
    # browse reader. It is recorded because "the directory IS the kind" is the Stratum axis: a hit's
    # declared type is what the flip is supposed to preserve while its path changes.
    types = {n.rel_path: n.type for n in wiki.list_notes()}

    effective_limit = wiki_mod.MAX_HITS if limit is None else int(limit)
    # A non-positive limit is rejected rather than honoured: `Wiki.query` decides `status` BEFORE
    # slicing `eligible[: max(0, limit)]`, so `limit=0` would record `status: ok` with an empty hit
    # list for every query — a snapshot that pins no ranking while looking green.
    if effective_limit < 1:
        raise ValueError(f"limit must be >= 1, got {effective_limit}")

    records: list[dict[str, Any]] = []
    with _fm_mode(fm) as fm_enabled:
        for spec in queries:
            result = wiki.query(spec.question, limit=effective_limit)
            records.append(
                {
                    "id": spec.id,
                    "question": spec.question,
                    "expect": spec.expect,
                    "status": result.status,
                    "hits": [
                        _hit_record(hit, rank, internal, types)
                        for rank, hit in enumerate(result.hits, start=1)
                    ],
                }
            )

    header = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "repo": wiki.repo,
        "kb_schema_version": _kb_schema_version(wiki),
        "fm_enabled": fm_enabled,
        "index_cache_enabled": bool(policy.enabled),
        "index_cache_used": _index_cache_used(wiki, policy),
        "limit": effective_limit,
        "query_count": len(records),
        "corpus_note_count": len(internal),
        "agora_version": __version__,
    }
    return {"header": header, "queries": records}


def diff_snapshots(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Return human-readable differences from snapshot ``a`` (before) to ``b`` (after).

    One line per difference, in a deterministic order: header differences first, then queries in
    ``a``'s order followed by ids only ``b`` has. Within a query: an ``expect`` change, then a
    status flip, then per-note lines ordered by the note's rank in ``a`` (new notes last,
    alphabetically). An empty list means every field this reporter compares is equivalent — which
    is the listing a PR that changes a golden owes its reviewer (see ``tests/rank_golden``).

    Every recorded field is compared: rank, score, and each of :data:`_HIT_FIELDS_REPORTED`, plus
    the header keys in :data:`_HEADER_KEYS`. The one recorded field deliberately not compared is
    ``question`` — it is the query file's own text, and the records are keyed on ``id``.
    ``agora_version`` IS reported here (a caller comparing two records wants to know they came
    from different builds); it is the golden TEST that masks it, so a release bump cannot redden
    the gate. A rank move whose score is unchanged is annotated as such, because ADR-0012 §7's
    order ends in the note PATH and the layout flip moves every path — that reordering is not a
    scoring change and should not read as one.

    Tolerant of records reloaded from JSON (it reads plain dicts, not models) and of a missing key
    (reported as a difference rather than raised — every read is a ``.get``), because the two sides
    of a real comparison are typically a committed golden file and a fresh in-memory run, possibly
    across a :data:`SNAPSHOT_SCHEMA_VERSION` bump.
    """
    lines: list[str] = []
    lines.extend(_diff_header(_mapping(a.get("header")), _mapping(b.get("header"))))

    qa = {str(q.get("id")): q for q in _sequence(a.get("queries"))}
    qb = {str(q.get("id")): q for q in _sequence(b.get("queries"))}
    order = [*qa, *sorted(k for k in qb if k not in qa)]

    for qid in order:
        left, right = qa.get(qid), qb.get(qid)
        if right is None:
            lines.append(f"{qid}: query removed")
            continue
        if left is None:
            lines.append(f"{qid}: query added (status {right.get('status')!r})")
            continue
        lines.extend(_diff_query(qid, left, right))
    return lines


# --- internals ----------------------------------------------------------------------------------


@contextmanager
def _fm_mode(enabled: bool | None):  # type: ignore[no-untyped-def]
    """Temporarily force :data:`agora_kb.core.wiki.FM_ENABLED`, restoring it unconditionally.

    ADR-0012 §1 models ``fm_enabled`` as repo configuration, but the shipped oracle reads it as a
    module constant (flipped live by the #56 addendum A3), so forcing a mode means rebinding that
    global. Two honest caveats, stated rather than hidden: this is process-wide for the duration of
    the call, so it is not safe to run concurrently with another query in the same process; and
    ``fm=None`` — the default, and what CI should use — rebinds NOTHING, taking whichever mode the
    build ships. The rebind exists so a fixture can pin both columns of the ADR's §10 table.
    """
    current = wiki_mod.FM_ENABLED
    if enabled is None or bool(enabled) == current:
        yield current
        return
    wiki_mod.FM_ENABLED = bool(enabled)
    try:
        yield bool(enabled)
    finally:
        wiki_mod.FM_ENABLED = current


def _round6(value: float) -> float:
    """Quantize a recorded float to 6 decimals so the golden file is repr-stable.

    Idempotent on oracle output (ADR-0012 §6.1 already rounds to 6 decimals). It is applied on the
    way into the record anyway: the point of a golden file is that an unchanged ranker reproduces
    it byte-for-byte, and a raw float would put 17 significant digits — and every sub-ULP wobble
    the ADR's own determinism stance declines to guarantee across libm builds — into the diff.
    """
    return round(float(value), _ROUND_DECIMALS)


def _hit_record(
    hit: Any,
    rank: int,
    internal: dict[str, Any],
    types: dict[str, str | None],
) -> dict[str, Any]:
    """Transcribe one :class:`~agora_kb.core.wiki.SearchHit` into a layout-independent record."""
    note = internal.get(hit.path)
    return {
        "note": Path(hit.path).stem,  # basename identity — survives the layout flip
        "title": note.title if note is not None else None,
        "type": types.get(hit.path),
        "score": _round6(hit.score),
        # Present-but-null by design; see `snapshot`'s docstring. `getattr` (not a hard `None`) so a
        # future core explain seam populates them without a change here.
        "lex": _opt_round(getattr(hit, "lex", None)),
        "struct": _opt_round(getattr(hit, "struct", None)),
        "fm": _opt_round(getattr(hit, "fm", None)),
        "match_reason": hit.match_reason,
        # The ADR-0012 §7 extraction contract. All three are CONTENT-derived (a heading slug, a
        # 1-based line inside the body, a 240-char body window) and carry no path, so they survive
        # the layout flip unchanged — which is what makes them worth pinning here.
        "anchor": hit.anchor,
        "line": hit.line,
        "excerpt": hit.excerpt,
        "rank": rank,
    }


def _opt_round(value: float | None) -> float | None:
    return None if value is None else _round6(value)


def _index_cache_used(wiki: Wiki, policy: Any) -> bool:
    """Whether the ADR-0012 §2 cached read path was ACTUALLY engaged, not merely permitted.

    ``policy.enabled`` is configuration; this is the fact. :meth:`Wiki._load_notes` consults the
    cache only when the policy allows it AND a usable (present, schema-current, commit-fresh) cache
    exists, so a non-git repo, an unbuilt ``_kb/index/`` or a stale one all mean the numbers in
    this record came from the full scan. Recording only the flag would let a reader conclude the
    cached path had been exercised when it structurally cannot have been.

    Computes no ranking quantity — it asks core's own read path which loader it would take, which
    is exactly the kind of transcription ADR-0012 §0a permits.
    """
    if not policy.enabled:
        return False
    try:
        return wiki._usable_cache_entries() is not None  # noqa: SLF001 — same package.
    except Exception:  # noqa: BLE001 — an unreadable cache is "not used", never fatal to a read.
        return False


def _kb_schema_version(wiki: Wiki) -> int | None:
    """The repo's KB ``schema_version``, or ``None`` when it cannot be determined.

    Imported lazily and swallowed on failure for the same reason
    :meth:`Wiki._index_policy` swallows: a snapshot is a read, and an unrelated ``repo.yaml``
    problem must not abort the baseline the operator is trying to capture. ``None`` records the
    uncertainty explicitly instead of guessing the default.
    """
    from ..config import read_kb_schema_version

    try:
        return read_kb_schema_version(wiki.layout)
    except Exception:  # noqa: BLE001 — an unreadable config is "unknown", never fatal, to a read.
        return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sequence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# Header keys worth reporting when they differ: each one changes what the numbers MEAN, so a diff
# that ignored them could read as "ranking moved" when the truth is "you compared two modes".
_HEADER_KEYS = (
    "snapshot_schema_version",
    "repo",
    "fm_enabled",
    "index_cache_enabled",
    "index_cache_used",
    "limit",
    "corpus_note_count",
    "kb_schema_version",
    "agora_version",
)


def _diff_header(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in _HEADER_KEYS:
        left, right = a.get(key), b.get(key)
        if left != right:
            out.append(f"header: {key} {left!r} -> {right!r}")
    return out


def _diff_query(qid: str, a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if a.get("expect") != b.get("expect"):
        out.append(f"{qid}: expect {a.get('expect')!r} -> {b.get('expect')!r}")
    if a.get("status") != b.get("status"):
        out.append(f"{qid}: status {a.get('status')!r} -> {b.get('status')!r}")

    ha = _hits_by_note(a.get("hits"))
    hb = _hits_by_note(b.get("hits"))
    for note in _note_order(ha, hb):
        left, right = ha.get(note), hb.get(note)
        if left is None:
            if right is not None:
                out.append(
                    f"{qid}: {note} appeared at rank {right.get('rank')}, "
                    f"score {right.get('score')}"
                )
            continue
        if right is None:
            out.append(
                f"{qid}: {note} dropped (was rank {left.get('rank')}, score {left.get('score')})"
            )
            continue

        lscore, rscore = left.get("score"), right.get("score")
        if left.get("rank") != right.get("rank"):
            # A rank move with an IDENTICAL score was not caused by this note's own scoring: it is
            # either ADR-0012 §7's tie-break tail (which ends in `note.path`, and the layout flip
            # changes every path by construction) or another note moving past it. Flagging the
            # fact keeps a reviewer from reading a reordering as a scoring change — the record
            # carries no path, by design, so this annotation is the only signal available.
            same = " (score unchanged)" if lscore == rscore else ""
            out.append(f"{qid}: {note} rank {left.get('rank')} -> {right.get('rank')}{same}")
        if lscore != rscore:
            delta = _round6(_as_float(rscore) - _as_float(lscore))
            out.append(f"{qid}: {note} score {lscore} -> {rscore} ({delta:+.6f})")
        for key in _HIT_FIELDS_REPORTED:
            lv, rv = left.get(key), right.get(key)
            if lv != rv:
                out.append(f"{qid}: {note} {key} {_short(lv)} -> {_short(rv)}")
    return out


# Recorded hit fields compared verbatim by the reporter, beyond rank/score. `type` is here because
# "the directory IS the kind" is the Stratum axis — a flip that changed a note's declared type
# while preserving its score is exactly the silent regression this listing has to surface — and
# `anchor`/`line`/`excerpt` because the §7 extraction contract is recorded (see `_hit_record`) and
# a reporter that recorded a field but never mentioned it would be worse than not recording it.
_HIT_FIELDS_REPORTED = ("match_reason", "title", "type", "anchor", "line", "excerpt")

_SHORT_REPR_LEN = 60


def _short(value: Any) -> str:
    """``repr`` of a recorded field, elided in the middle so a 240-char excerpt stays one line."""
    text = repr(value)
    if len(text) <= _SHORT_REPR_LEN:
        return text
    keep = (_SHORT_REPR_LEN - 3) // 2
    return f"{text[:keep]}...{text[-keep:]}"


def _hits_by_note(hits: Any) -> dict[str, dict[str, Any]]:
    return {str(h.get("note")): h for h in _sequence(hits)}


def _note_order(a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]]) -> list[str]:
    """Notes in ``a``'s rank order, then ``b``-only notes alphabetically — stable in both cases."""
    ordered = sorted(a, key=lambda n: (_as_int(a[n].get("rank")), n))
    return [*ordered, *sorted(n for n in b if n not in a)]


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def dumps(record: dict[str, Any]) -> str:
    """Serialize a snapshot the one canonical way: 2-space indent, key order as built, UTF-8 kept.

    A single writer means a golden file committed by ``agora eval --out`` and one written by a test
    are byte-comparable. ``ensure_ascii=False`` because a Korean-heavy KB (ADR-0012 addendum, #56)
    should be readable in the diff, not escaped into ``\\uXXXX``.
    """
    return json.dumps(record, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
