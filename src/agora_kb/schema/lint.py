"""Deterministic L1 lint ruleset (ADR-0010 §6 / the L1-1..L1-19 table).

This is the model-free, wall-clock-free integrity gate that ADR-0011 §4.4 runs after APPLY +
AUTHOR (before commit) and that the dashboard / ``kb_status`` reuse verbatim — the SAME code path,
so the curator and the dashboard can never disagree. It builds on the FROZEN reading layer in
:mod:`agora_kb.schema.notes` (:func:`~agora_kb.schema.notes.parse_all_notes`,
:func:`~agora_kb.schema.notes.wikilinks` for the frontmatter ``related:`` / ``children:`` ``[[ ]]``
arrays, :func:`~agora_kb.schema.notes.body_link_basenames` for the ADR-0014 D3 standard-markdown
BODY graph links, and :func:`~agora_kb.schema.notes.child_bullets` for the MOC child set) and the
:class:`~agora_kb.schema.emit.Taxonomy` model.

Purity contract (ADR-0010 D1, "reads no wall clock"):

* :func:`lint` is a pure function of ``(layout, taxonomy, run_date, run_id)`` plus what is on disk
  in the worktree. It reads NO system clock.
* The STRUCTURAL rules (basenames, links, required frontmatter, enums, MOC children, sources,
  contested shape, encoding, schema_version, origin, body-sentinel integrity) apply ALWAYS. The
  date-FORMAT half of L1-12 (must be ``YYYY-MM-DD``) is likewise structural and runs either way.
* The run-relative date checks — L1-12's NO-FUTURE half (``created``/``updated``/``date`` ``>
  run_date`` fails; this is a ``<= run_date`` bound, NOT an equality) and the daily ``date`` /
  ``run_id`` equality (L1-14) — fire ONLY when ``run_date`` is provided. The curator passes the
  injected ``run_date`` (and ``run_id``) at INGEST; the dashboard calls :func:`lint` WITHOUT them,
  because outside a run there is no canonical "today" to compare against (a dashboard must not flag
  every historical note as a future date).

Determinism (ADR-0010 D5): :class:`LintResult.findings` is sorted by ``(path, code)`` so two
independent implementations emit byte-identical results. Frontmatter dates may be unquoted YAML
scalars that ``yaml.safe_load`` coerces to :class:`datetime.date`; the date rules canonicalize
both ``date`` and ``str`` to ``YYYY-MM-DD`` (via ``_date_str``) so the gate fires on the spec's
own on-disk shape (ADR-0010 §2) rather than fail-open on a non-string.

Scope: this module implements the L1 (hard-reject) ruleset EVALUATED on a single worktree read.
TWO L1 rules are explicitly OUT of that surface because they are diff / before-after scoped:
**L1-9** (path escape / disallowed symlink / off-allowlist add-or-modify) needs the run's git diff
(ADR-0011 §4.0/§4.5) and is enforced by the worker's final-diff allowlist assertion, not here;
**L1-18** (``taxonomy_policy`` on the SEPARATE admin/human evolution path) needs a (before, after)
taxonomy pair, which a single-worktree read does not have. L1-3 (ambiguous wikilink) emits no
finding — it is belt-and-suspenders behind L1-1/L1-15 and subsumed by them. The L2 HEALTH signals
(orphan/stale/…) are DERIVED at read/dashboard time and are not part of this hard gate.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Literal

import yaml

from agora_kb.core.frontmatter import FrontmatterError
from agora_kb.core.layout import RepoLayout

# The L1-20 body-sentinel grammar (ADR-0011 §4.4 check 6 / ADR-0010 §2.6) + the L2-6 unauthored-
# region grader. These MUST match the curator's apply.py sentinel grammar BYTE-FOR-BYTE so the gate
# the curator runs at §4.4 and the dashboard's reuse agree. They used to be re-spelled LOCALLY here
# to keep ``schema/`` free of a curator import; #119 SATISFIES that constraint properly instead —
# the strict producer grammar now lives in ``core.sentinel``, BELOW both packages, so importing it
# introduces no cycle and deletes the fork (apply.py + worker.py + here were three copies of the
# same two patterns). The persisted region id is run-scoped (``{run_id}--{candidate_id}``,
# apply.region_sentinel_id) but L1-20 is id-opaque — it only pairs starts/ends and forbids
# unmatched/nested/duplicated markers, so it works for ANY id string.
from agora_kb.core.sentinel import BODY_END_LINE_RE as _BODY_SENTINEL_END_RE
from agora_kb.core.sentinel import BODY_START_LINE_RE as _BODY_SENTINEL_START_RE
from agora_kb.core.sentinel import has_unauthored_region
from agora_kb.schema.emit import Taxonomy
from agora_kb.schema.notes import (
    PARSE_EXEMPT_BASENAMES,
    Note,
    body_link_basenames,
    child_bullets,
    note_basename,
    parse_all_notes,
    wikilinks,
)

__all__ = ["LintFinding", "LintResult", "lint"]

Severity = Literal["error", "warning"]

# --- frozen enums (ADR-0010 §1, §2.6, D4) -----------------------------------------------------

# The four note types (ADR-0010 §1) and the frozen status vocabulary (ADR-0010 §2.6 / D2). There is
# NO `canonical`/`verified`/`draft`/`orphan`/`stale` status: orphan/stale are DERIVED, never stored.
_NOTE_TYPES = frozenset({"index", "moc", "theme", "daily"})
_STATUS_VALUES = frozenset({"active", "stub", "contested", "deprecated"})

# The unparameterized members of the inbox `source` enum (DATA-MODEL §1), copied byte-for-byte as
# the `origin` enum (ADR-0010 D4 — the prior `upload` value is removed). `web:<user>` and
# `harvest:<agent>` are the two PARAMETERIZED forms, validated by prefix below.
_ORIGIN_PLAIN = frozenset(
    {"claude-code", "codex", "qwen", "gemini", "opencode", "hermes", "manual"}
)
_ORIGIN_PREFIXES = ("web:", "harvest:")

# A YYYY-MM-DD calendar date (L1-12 date-format half). Frozen so two linters agree byte-for-byte.
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

# The contested-shape body callout (ADR-0010 §3.8 / L1-10): a line STARTING with `> [!contested]`.
_CONTESTED_CALLOUT_RE = re.compile(r"^> \[!contested\]", re.MULTILINE)

# A daily basename is `<domain>-YYYY-MM-DD`; the trailing 10 chars are the date (ADR-0010, L1-14).
_DAILY_DATE_RE = re.compile(r"-(?P<date>\d{4}-\d{2}-\d{2})\Z")


def _date_str(value: object) -> str | None:
    """Canonicalize a frontmatter date scalar to a ``YYYY-MM-DD`` string, else ``None``.

    ADR-0010 §2 shows dates UNQUOTED (``created: 2026-06-13``); ``yaml.safe_load`` coerces an
    unquoted ``YYYY-MM-DD`` scalar to a :class:`datetime.date`, so a spec-conformant note carries a
    ``date`` object, not a ``str``. The date rules (L1-12 format / no-future, L1-14 equality, L1-10
    ``contested_at``) MUST still fire on that shape — comparing a ``str`` ``run_date`` against a raw
    ``date`` would always mis-fire and silently bypass the hard gate. We therefore normalize BOTH
    str and ``datetime.date`` to a canonical ``YYYY-MM-DD`` string here. A ``datetime.datetime`` is
    NOT a date scalar (its ``isoformat`` carries a time), so it is rejected (returns ``None``) and
    surfaces as an L1-12 format error. Any other type returns ``None`` (absence/type is L1-4's job).
    """
    if isinstance(value, datetime.datetime):
        return None  # a datetime is not a bare YYYY-MM-DD date scalar; let L1-12 flag the format
    if isinstance(value, datetime.date):
        return value.isoformat()  # canonical YYYY-MM-DD (date.isoformat has no time component)
    if isinstance(value, str):
        return value
    return None


def _origin_ok(origin: str) -> bool:
    """Return True iff ``origin`` is a member of the inbox `source` enum (D4 / L1-19)."""
    if origin in _ORIGIN_PLAIN:
        return True
    # `web:<user>` / `harvest:<agent>` — a non-empty parameter after the prefix.
    return any(origin.startswith(p) and len(origin) > len(p) for p in _ORIGIN_PREFIXES)


@dataclass(frozen=True)
class LintFinding:
    """One L1 lint finding (ADR-0010 §6).

    ``code`` is the rule id (e.g. ``"L1-7"``). ``severity`` is ``"error"`` for a hard-reject L1 rule
    or ``"warning"`` for a soft L2 signal. Every L1 rule is ``"error"`` — ``kb_schema.md`` freezes
    L1 as "STRUCTURAL (hard; reject the commit)" — while the L2 rules emitted from inside
    :func:`lint` are warnings: L2-1 (orphan count, ADR-0022) and L2-6 (the #119 ``body_status``
    invariant). The distinction is load-bearing, not cosmetic: ``LintResult.ok`` is False iff an
    ERROR exists, and that is what the curator's §4.4 gate discards a whole run on. ``path`` is the
    POSIX repo-relative path of the offending note (or the relevant metadata file for whole-repo
    rules like schema_version drift). ``message`` is a human-readable one-liner.
    """

    code: str
    severity: Severity
    path: str
    message: str


@dataclass(frozen=True)
class LintResult:
    """The outcome of an L1 lint pass over a worktree (ADR-0010 §6 / ADR-0011 §4.4).

    ``ok`` is True iff there are NO error-severity findings (the curator commits only when ``ok``;
    ADR-0010's "commit only if not lint_l1(...)"). ``findings`` is sorted deterministically by
    ``(path, code)`` so the result is byte-identical across implementations (D5).
    """

    ok: bool
    findings: tuple[LintFinding, ...] = field(default_factory=tuple)


def _load_taxonomy(layout: RepoLayout) -> Taxonomy:
    """Load ``_meta/taxonomy.yaml`` into a :class:`Taxonomy`, or default when absent/empty.

    The on-disk shape (ADR-0010 §5, written by :func:`agora_kb.schema.emit.emit_schema`) is
    ``{schema_version, taxonomy_policy, domains: [...], allowed_tags: {tag: {...}}}`` — ``domains``
    is a list and ``allowed_tags`` is a mapping whose KEYS are the closed tag set. We read the keys
    only (descriptors are advisory). A missing file degenerates to an empty taxonomy, so a repo
    without ``_meta/`` lints with no allowed tags/domains (every tag/domain then fails L1-5 — the
    correct conservative behavior).
    """
    path = layout.root / "_meta" / "taxonomy.yaml"
    if not path.is_file():
        return Taxonomy()
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return Taxonomy()
    allowed = loaded.get("allowed_tags")
    if isinstance(allowed, dict):
        tags = tuple(str(t) for t in allowed)
    elif isinstance(allowed, list):
        tags = tuple(str(t) for t in allowed)
    else:
        tags = ()
    domains_value = loaded.get("domains")
    domains = tuple(str(d) for d in domains_value) if isinstance(domains_value, list) else ()
    version = loaded.get("schema_version", 1)
    policy = loaded.get("taxonomy_policy", "open")
    return Taxonomy(
        schema_version=int(version) if isinstance(version, int) else 1,
        taxonomy_policy=str(policy),
        allowed_tags=tags,
        domains=domains,
    )


def _note_domain(rel_path: str) -> str | None:
    """Return a note's domain — the first path component under ``wiki/`` — or ``None``.

    ``wiki/<domain>/...`` ⇒ ``<domain>``. The root ``index.md`` (and any other non-``wiki/`` note)
    has NO domain and is therefore exempt from the L1-5 domain-membership check (the index lists
    domain MOCs; it does not itself belong to a domain).
    """
    parts = rel_path.split("/")
    if len(parts) >= 2 and parts[0] == "wiki":
        return parts[1]
    return None


def _as_str_list(value: object) -> list[str] | None:
    """Return ``value`` as a list[str] if it is a list of strings, else ``None`` (type error).

    An ABSENT key (``None``) is the caller's concern (a missing-required check); this tells a
    present-but-wrong-typed value apart from absent by returning ``None`` only for a non-list.
    """
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return list(value)


def _str_items(value: object) -> list[str]:
    """Return the string elements of ``value`` if it is a list, else ``[]``.

    A typed accessor over a raw frontmatter value (``dict[str, object]``): non-list values and
    non-string elements are silently dropped (their type errors are reported by the dedicated
    required-frontmatter check, L1-4). Keeps the rule loops total without per-site isinstance noise.
    """
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def _is_utf8_lf_no_bom(raw: bytes) -> bool:
    """Return True iff ``raw`` is valid UTF-8, BOM-free, and uses LF (no CR) endings (L1-16)."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return False  # UTF-8 BOM
    if b"\r" in raw:
        return False  # CR (CRLF or bare CR) — only LF is allowed
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _candidate_note_paths(layout: RepoLayout) -> list[str]:
    """Return POSIX rel-paths of candidate note files (``index.md`` + ``wiki/**/*.md``), sorted.

    Mirrors :func:`agora_kb.schema.notes.parse_all_notes`'s file selection (same exempt-basename and
    symlink-identity skips, ADR-0010 §1) WITHOUT parsing, so the L1-16 encoding scan can flag a
    BOM/CRLF file even when that file then fails frontmatter parsing (a BOM precedes the ``---``
    fence, so the parser would otherwise abort before the encoding rule ran).
    """
    root = layout.root
    paths: list[str] = []
    if layout.index_file.is_file():
        paths.append("index.md")
    if layout.wiki_dir.is_dir():
        for p in layout.wiki_dir.rglob("*.md"):
            if not p.is_file():
                continue
            if note_basename(p) in PARSE_EXEMPT_BASENAMES or p.is_symlink():
                continue
            paths.append(p.relative_to(root).as_posix())
    return sorted(paths)


def _scan_encoding(layout: RepoLayout) -> list[LintFinding]:
    """L1-16 pre-pass over every candidate note file (runs even if frontmatter then fails parse)."""
    findings: list[LintFinding] = []
    for rel in _candidate_note_paths(layout):
        if not _is_utf8_lf_no_bom((layout.root / rel).read_bytes()):
            findings.append(
                LintFinding("L1-16", "error", rel, "not UTF-8-no-BOM with LF line endings")
            )
    return findings


# --- per-note required-frontmatter tables (ADR-0010 §2) ---------------------------------------

# Common base keys required on EVERY note type (§2.1). `tags`/`aliases` are required-as-typed (the
# §2 table lists them with `[]` defaults; their type is checked, an empty list is valid).
_COMMON_STR_KEYS = ("title", "summary")
_COMMON_LIST_KEYS = ("tags", "aliases")
_DATE_KEYS = ("created", "updated")


def _check_required_frontmatter(note: Note) -> list[LintFinding]:
    """L1-4 (missing required key for type) + L1-11 (unknown type/status) for one note.

    Implements the §2 required-field tables: the common base for all types, plus the type-specific
    additions (theme: sources/related/confidence; daily: date/run_id; moc & index: children). Type
    errors (a key present but wrong-typed) are reported under L1-4 alongside absences, since both
    mean "the required frontmatter for this type is not satisfied".
    """
    findings: list[LintFinding] = []
    fm = note.frontmatter
    path = note.rel_path

    def miss(code: str, msg: str) -> None:
        findings.append(LintFinding(code, "error", path, msg))

    # L1-11: type must be one of the four. (None == key absent.)
    ntype = note.type
    if ntype is None or ntype not in _NOTE_TYPES:
        miss("L1-11", f"unknown or missing 'type' {ntype!r}; expected one of {sorted(_NOTE_TYPES)}")

    # L1-11: status must be one of the four (required on every type, §2.1).
    status = fm.get("status")
    if status is None:
        miss("L1-4", "missing required 'status'")
    elif not (isinstance(status, str) and status in _STATUS_VALUES):
        miss("L1-11", f"unknown 'status' {status!r}; expected one of {sorted(_STATUS_VALUES)}")

    # Common base: title/summary (str), tags/aliases (list[str]), created/updated (date string).
    for key in _COMMON_STR_KEYS:
        v = fm.get(key)
        if v is None:
            miss("L1-4", f"missing required '{key}'")
        elif not isinstance(v, str):
            miss("L1-4", f"'{key}' must be a string, got {type(v).__name__}")
    for key in _COMMON_LIST_KEYS:
        if key in fm and _as_str_list(fm.get(key)) is None:
            miss("L1-4", f"'{key}' must be a list of strings")
    for key in _DATE_KEYS:
        v = fm.get(key)
        if v is None:
            miss("L1-4", f"missing required '{key}'")

    # Type-specific required additions.
    if ntype == "theme":
        # sources is required-as-typed here (the non-empty rule is L1-7); related/confidence typed.
        if "sources" in fm and _as_str_list(fm.get("sources")) is None:
            miss("L1-4", "'sources' must be a list of strings")
        if "related" in fm and _as_str_list(fm.get("related")) is None:
            miss("L1-4", "'related' must be a list of strings")
        conf = fm.get("confidence")
        if conf is not None and conf not in ("high", "medium", "low"):
            miss("L1-4", f"'confidence' must be high|medium|low, got {conf!r}")
    elif ntype == "daily":
        if fm.get("date") is None:
            miss("L1-4", "missing required 'date' on daily")
        if fm.get("run_id") is None:
            miss("L1-4", "missing required 'run_id' on daily")
    elif ntype in ("moc", "index"):
        if "children" not in fm:
            miss("L1-4", f"missing required 'children' on {ntype}")
        elif _as_str_list(fm.get("children")) is None:
            miss("L1-4", "'children' must be a list of strings")

    return findings


def _check_dates(note: Note, run_date: str | None) -> list[LintFinding]:
    """L1-12: created/updated/date must be ``YYYY-MM-DD`` (always), and ``<= run_date`` when given.

    The FORMAT check (must be ``YYYY-MM-DD``) is structural and runs unconditionally. The
    no-future-date check (``> run_date`` fails) fires ONLY when ``run_date`` is provided (the
    curator injects it; the dashboard omits it). String comparison is valid because ``YYYY-MM-DD``
    sorts lexicographically as it sorts chronologically. Note dates may arrive as ``datetime.date``
    (unquoted YAML scalars per ADR-0010 §2) or as strings; :func:`_date_str` canonicalizes both so
    the gate fires on the spec's own on-disk shape instead of silently skipping it.
    """
    findings: list[LintFinding] = []
    fm = note.frontmatter
    keys = ("created", "updated") + (("date",) if note.type == "daily" else ())
    for key in keys:
        raw = fm.get(key)
        if raw is None:
            continue  # absence handled by L1-4
        value = _date_str(raw)
        if value is None:
            # present but not a date scalar (e.g. a datetime, or a non-string non-date) — L1-12.
            findings.append(
                LintFinding("L1-12", "error", note.rel_path, f"'{key}' is not YYYY-MM-DD: {raw!r}")
            )
            continue
        if not _DATE_RE.match(value):
            findings.append(
                LintFinding(
                    "L1-12", "error", note.rel_path, f"'{key}' is not YYYY-MM-DD: {value!r}"
                )
            )
            continue
        if run_date is not None and value > run_date:
            findings.append(
                LintFinding(
                    "L1-12",
                    "error",
                    note.rel_path,
                    f"'{key}' {value} is in the future (> run_date {run_date})",
                )
            )
    return findings


def _basename_date(basename: str) -> str | None:
    """Return the trailing ``YYYY-MM-DD`` of a daily basename ``<domain>-YYYY-MM-DD``, or None."""
    m = _DAILY_DATE_RE.search(basename)
    return m.group("date") if m is not None else None


def _resolve_targets(notes: list[Note]) -> set[str]:
    """Return the set of all resolvable link targets: every basename ∪ every alias (ADR-0010 §3.1).

    ``[[X]]`` resolves iff ``X`` matches a note basename or an entry in some note's ``aliases:``
    (byte-for-byte, no folding). Used by L1-2 (broken link) after L1-1/L1-15 have validated that the
    union is globally unique.
    """
    known: set[str] = set()
    for n in notes:
        known.add(n.basename)
        known.update(_str_items(n.frontmatter.get("aliases")))
    return known


def _check_body_sentinels(note: Note) -> list[LintFinding]:
    """L1-20: body-sentinel integrity (ADR-0011 §4.4 check 6 / ADR-0010 L1 sentinel integrity).

    Scan the note BODY for ``agora:body:start/end`` markers and FAIL on any of: an unmatched start
    (no closing end), an unmatched end (no open start), a mismatched id on an end, a nested/
    overlapping pair (a start before the prior end), or a DUPLICATED id within the note. This is the
    check that should have failed the corrupted dogfood run, where a cross-run MERGE / daily-append
    produced two identical ``agora:body:start id=…`` markers in one note (the bare per-run
    candidate_id collided); the persisted id is now run-scoped (apply.region_sentinel_id), and this
    gate hard-rejects any note that still carries colliding/unbalanced markers.

    Uses the SAME line grammar as :func:`agora_kb.curator.apply._extract_sentinel_regions` —
    literally the same two compiled patterns, imported from :mod:`agora_kb.core.sentinel`, which
    sits BELOW both packages so ``schema/`` still never imports the curator (#119 replaced three
    private copies with that one home). A malformed marker line that does not match the exact
    grammar is treated as ordinary content (not a sentinel) — like apply.
    """
    path = note.rel_path

    def fail(msg: str) -> list[LintFinding]:
        return [LintFinding("L1-20", "error", path, msg)]

    open_cid: str | None = None
    seen_ids: set[str] = set()
    for line in note.body.split("\n"):
        start = _BODY_SENTINEL_START_RE.match(line)
        end = _BODY_SENTINEL_END_RE.match(line)
        if start is not None:
            if open_cid is not None:
                return fail(
                    f"nested/overlapping agora:body sentinel: start "
                    f"id={start.group('cid')!r} before close of id={open_cid!r}"
                )
            open_cid = start.group("cid")
            continue
        if end is not None:
            cid = end.group("cid")
            if open_cid is None:
                return fail(f"unmatched agora:body:end id={cid!r} (no open start)")
            if cid != open_cid:
                return fail(
                    f"mismatched agora:body sentinel: end id={cid!r} closes start id={open_cid!r}"
                )
            if cid in seen_ids:
                return fail(f"duplicated agora:body sentinel id={cid!r} in note")
            seen_ids.add(cid)
            open_cid = None
    if open_cid is not None:
        return fail(f"unmatched agora:body:start id={open_cid!r} (no closing end)")
    return []


def _check_body_status(note: Note) -> list[LintFinding]:
    """L2-6: a stale ``body_status: pending`` over a fully-authored note (#119, ADR-0010 §2.6).

    ADR-0010 §2.6 / §7.3: the key is present ONLY while a note's prose is not yet authored, and
    the curator's worker DROPS it after the §4.2 AUTHOR gate. Until #119 nothing ever removed it,
    so every published note carried it and the signal was worthless. This is the at-rest assertion
    of that contract, and the EXACT INVERSE of
    :func:`agora_kb.curator.worker._clear_body_status` — both call
    :func:`agora_kb.core.sentinel.has_unauthored_region`, so a curator-produced note can never
    trip it.

    The predicate is "NO region is still a placeholder", NOT "prose exists": a note whose PASS-2
    filled 2 of 3 regions legitimately has prose AND a legitimate pending. This is the single
    thing most likely to be got wrong.

    Severity is WARNING, deliberately and load-bearingly. :func:`lint` grades the WHOLE worktree,
    not the run's diff, and every note published by a pre-#119 build carries a stale flag. At error
    severity ``LintResult.ok`` would be False on any pre-existing repo, the worker's §4.4 gate would
    ``_fail`` the run over notes it never touched, and — because a lint failure discards the whole
    diff — the run could never repair what made it fail, burning the §5.1 retry budget until the
    events go terminal to ``failed/``. Promote to a hard ``L1-21`` error ONLY AFTER
    ``agora repo upgrade`` (#63) can perform the one-shot repair on an existing repo; at that point
    mark L2-6 superseded in ADR-0010's table using the append-only banner pattern (ADR-0011 §7.1).

    ONLY the present-direction is checked. The converse ("an unauthored region but no flag") is NOT
    a violation. The curator no longer PRODUCES that shape — #131 made region placement and the flag
    one decision in ``apply._apply_append_daily``, which until then placed an empty region for a
    ``needs_prose=False`` APPEND_DAILY that ``_needs_prose_map`` would never author — but the shape
    still exists AT REST, which is what matters to a check that grades the whole worktree: every
    daily published by a pre-#131 build carries it, as does any hand-authored or imported note.
    Asserting the biconditional would false-positive on all of them, on a repo that has no way to
    repair itself until ``agora repo upgrade`` (#63).
    """
    if note.frontmatter.get("body_status") is None:
        return []
    if has_unauthored_region(note.body):
        return []
    return [
        LintFinding(
            "L2-6",
            "warning",
            note.rel_path,
            "body_status: pending but every agora:body region is authored (stale flag; the "
            "curator drops it after PASS-2 — ADR-0010 §2.6)",
        )
    ]


def lint(
    layout: RepoLayout,
    *,
    taxonomy: Taxonomy | None = None,
    run_date: str | None = None,
    run_id: str | None = None,
    max_orphans: int | None = None,
) -> LintResult:
    """Run the deterministic L1 lint over the worktree at ``layout`` (ADR-0010 §6 / ADR-0011 §4.4).

    Pure, model-free, and wall-clock-free. Parses every ``.md`` note EXCEPT the schema doc + its
    symlinks (via :func:`agora_kb.schema.notes.parse_all_notes`, ADR-0010 §1) and applies the L1
    ruleset EVALUATED on a single worktree read:

    * structural rules (basenames, links, required frontmatter, enums, MOC children, sources,
      contested shape, encoding, schema_version, origin) ALWAYS;
    * the no-future date check (L1-12 ``> run_date`` fails) and the daily date/``run_id`` equality
      (L1-14) ONLY when ``run_date`` (and, for the ``run_id`` half, ``run_id``) is provided.

    ``taxonomy`` defaults to the one loaded from ``_meta/taxonomy.yaml`` (its ``allowed_tags`` keys
    and ``domains`` are the FIXED read-only INGEST input, ADR-0010 D6). ``run_date`` is the injected
    ``run_id[:10]`` calendar date (``YYYY-MM-DD``) the curator passes at INGEST; the dashboard omits
    it. ``run_id`` is the FULL injected run manifest id (ADR-0011 §4.4 item 1); when supplied (the
    curator passes it at INGEST) L1-14 asserts a daily's ``run_id`` equals it BYTE-FOR-BYTE — not
    merely its date prefix — so a replayed/hand-edited run_id with a valid date but wrong
    time/suffix is rejected (ADR-0008 replay idempotency, D1). The dashboard omits ``run_id`` and
    then only the date-prefix half is checked.

    Two L1 rules are intentionally OUT of this single-worktree surface: **L1-9** (path escape /
    disallowed symlink / off-allowlist add-or-modify) and **L1-18** (``taxonomy_policy`` on the
    admin taxonomy-evolution path). Both are diff/before-after scoped — L1-9 needs the run's git
    diff (ADR-0011 §4.0/§4.5; enforced by the worker's final-diff allowlist assertion) and L1-18
    needs a (before, after) taxonomy pair — which a single-worktree read does not have. L1-3
    (ambiguous wikilink) emits no finding here: it is belt-and-suspenders behind L1-1/L1-15 and is
    subsumed by them.

    Returns a :class:`LintResult` whose ``findings`` are sorted by ``(path, code)`` (D5) and whose
    ``ok`` is True iff no error-severity finding was raised.
    """
    tax = taxonomy if taxonomy is not None else _load_taxonomy(layout)
    allowed_tags = set(tax.allowed_tags)
    allowed_domains = set(tax.domains)

    findings: list[LintFinding] = []

    # L1-16 encoding pre-pass — runs over raw bytes BEFORE parsing, so a BOM/CRLF file is flagged
    # even when its (BOM-prefixed) frontmatter then fails to parse.
    findings.extend(_scan_encoding(layout))

    # Parse all notes; a malformed frontmatter block is itself an L1 reject (not a valid note).
    # parse_all_notes is fail-fast: it raises on the FIRST malformed file (in sorted scan order), so
    # a worktree with several malformed notes surfaces one L1-4 per lint pass — deterministic given
    # the bytes, and successive passes report the next file as each is fixed.
    try:
        notes = parse_all_notes(layout, strict=True)
    except FrontmatterError as exc:
        # parse_all_notes prefixes the message with "<rel_path>: ..."; recover the path for the
        # finding so the result still sorts deterministically and points at the broken file. Fall
        # back to a sentinel path if the message ever lacks the ":" separator, so the finding never
        # carries an empty path (which would otherwise sort first and be ambiguous).
        msg = str(exc)
        rel = msg.split(":", 1)[0] if ":" in msg else "<unknown>"
        findings.append(LintFinding("L1-4", "error", rel, f"malformed frontmatter: {msg}"))
        findings.sort(key=lambda f: (f.path, f.code))
        return LintResult(ok=False, findings=tuple(findings))

    # --- L1-1 duplicate basename / L1-13 second index ----------------------------------------
    by_basename: dict[str, list[str]] = {}
    for n in notes:
        by_basename.setdefault(n.basename, []).append(n.rel_path)
    for base, paths in by_basename.items():
        if len(paths) > 1:
            for p in paths:
                findings.append(
                    LintFinding(
                        "L1-1",
                        "error",
                        p,
                        f"duplicate basename {base!r} (also at {[q for q in paths if q != p]})",
                    )
                )
        if base == "index":
            # Only the root index.md may be named "index" (L1-13). Any "index" not at the root is a
            # second index. (Duplicates are also caught by L1-1, but L1-13 names the specific rule.)
            for p in paths:
                if p != "index.md":
                    findings.append(
                        LintFinding("L1-13", "error", p, "second note basenamed 'index' (not root)")
                    )

    # --- L1-15 alias/basename collision (global uniqueness of basenames ∪ aliases) ------------
    # Seed with all basenames; then add aliases one at a time, flagging any clash with the union so
    # far. This catches alias==basename, alias==alias, and (implicitly) basename duplicates.
    seen_names: set[str] = set(by_basename)
    # Sort by path so alias-collision findings are reported deterministically against the union.
    for n in sorted(notes, key=lambda nn: nn.rel_path):
        for a in _str_items(n.frontmatter.get("aliases")):
            if a in seen_names:
                findings.append(
                    LintFinding(
                        "L1-15",
                        "error",
                        n.rel_path,
                        f"alias {a!r} collides with an existing basename/alias (not unique)",
                    )
                )
            seen_names.add(a)

    known = _resolve_targets(notes)

    # --- per-note rules ----------------------------------------------------------------------
    for n in notes:
        path = n.rel_path
        fm = n.frontmatter

        # L1-2 broken/dangling link — resolved against the known basename/alias set. Two link
        # surfaces, per ADR-0014 D3: (1) the note BODY now carries STANDARD MARKDOWN graph links
        # ``[Title](relative.md)`` (the MOC/index child bullets), resolved path→basename via
        # body_link_basenames; (2) the frontmatter related:/children: arrays STAY ``[[basename]]``
        # wikilinks, resolved via wikilinks(). A body markdown link whose resolved basename is
        # unknown is a broken link — the SAME hard reject as before (the strict producer, ADR-0014
        # D1). The two key kinds are reported with their native syntax for a precise message.
        for key in body_link_basenames(n.body):
            if key not in known:
                findings.append(
                    LintFinding("L1-2", "error", path, f"broken link [{key}](…) (no such note)")
                )
        fm_link_keys: list[str] = []
        for key in ("related", "children"):
            for entry in _str_items(fm.get(key)):
                fm_link_keys.extend(wikilinks(entry))
        for key in fm_link_keys:
            if key not in known:
                findings.append(
                    LintFinding("L1-2", "error", path, f"broken wikilink [[{key}]] (no such note)")
                )

        # L1-4 / L1-11 required frontmatter + enums.
        findings.extend(_check_required_frontmatter(n))

        # L1-20 body-sentinel integrity (no unmatched/nested/duplicated agora:body markers,
        # ADR-0011 §4.4 check 6). Runs on every note; only theme/daily carry markers, so a
        # moc/index with no markers always passes.
        findings.extend(_check_body_sentinels(n))

        # L2-6 stale `body_status: pending` over a fully-authored note (#119). WARNING severity —
        # it never flips LintResult.ok, so the §4.4 curator gate and the dashboard verdict are
        # byte-unchanged; see _check_body_status for why an error here would self-lock a run.
        findings.extend(_check_body_status(n))

        # L1-12 dates (format always; no-future only when run_date given).
        findings.extend(_check_dates(n, run_date))

        # L1-5 tag/domain membership in the FIXED taxonomy.
        for tag in _str_items(fm.get("tags")):
            if tag not in allowed_tags:
                findings.append(
                    LintFinding("L1-5", "error", path, f"tag {tag!r} not in taxonomy allowed_tags")
                )
        domain = _note_domain(path)
        if domain is not None and domain not in allowed_domains:
            findings.append(
                LintFinding("L1-5", "error", path, f"domain {domain!r} not in taxonomy domains")
            )

        # L1-6 MOC/index children == child-bullet set. The two sides use different normalizers by
        # design: `children:` entries go through wikilinks() (strips interior `[[ ]]` padding) while
        # the body side uses the frozen child-bullet grammar (which forbids interior padding after
        # `[[`). `children:` entries are therefore EXPECTED to be canonical `[[basename]]` tokens
        # with no interior whitespace — exactly what APPLY materializes — matching the body grammar;
        # a padded body bullet (`- [[ a ]]`) is malformed and correctly diverges from the set.
        if n.type in ("moc", "index"):
            declared = {k for entry in _str_items(fm.get("children")) for k in wikilinks(entry)}
            if declared != child_bullets(n.body):
                findings.append(
                    LintFinding(
                        "L1-6",
                        "error",
                        path,
                        f"children: {sorted(declared)} != child-bullet set "
                        f"{sorted(child_bullets(n.body))}",
                    )
                )

        # Theme-specific rules: L1-7/L1-8/L1-8b sources, L1-10 contested shape, L1-19 origin.
        # L1-8/L1-8b (sources existence + no sidecar) are intentionally THEME-SCOPED: a daily's
        # `sources:` are advisory and MAY be empty (ADR-0010 §2.3 — "a daily is not durable
        # provenance"; §3.4 frames source citation around themes), so they are not checked here.
        if n.type == "theme":
            status = fm.get("status")
            sources_raw = fm.get("sources")
            sources = sources_raw if isinstance(sources_raw, list) else []

            # L1-19 origin ∈ inbox source enum (origin is OPTIONAL; present iff harvested).
            origin = fm.get("origin")
            if origin is not None and not (isinstance(origin, str) and _origin_ok(origin)):
                findings.append(
                    LintFinding(
                        "L1-19", "error", path, f"origin {origin!r} not in inbox source enum"
                    )
                )

            # L1-7 non-stub theme must have non-empty sources (stub exempt, D3/§3.4.1).
            if status != "stub" and not sources:
                findings.append(
                    LintFinding("L1-7", "error", path, "non-stub theme with empty 'sources:'")
                )

            # L1-8 / L1-8b: each source must be a real raw/ artifact, never a .meta.yaml sidecar.
            for s in sources:
                if not isinstance(s, str):
                    continue
                if s.endswith(".meta.yaml"):
                    findings.append(
                        LintFinding("L1-8b", "error", path, f"sources cites a sidecar: {s!r}")
                    )
                elif not (layout.root / s).exists():
                    findings.append(
                        LintFinding("L1-8", "error", path, f"sources path does not exist: {s!r}")
                    )

            # L1-10 contested shape (full conjunction, ADR-0010 §3.8).
            if status == "contested":
                contested_by = fm.get("contested_by")
                contested_at = fm.get("contested_at")
                has_callout = _CONTESTED_CALLOUT_RE.search(n.body) is not None
                cb_ok = (
                    isinstance(contested_by, list)
                    and len(contested_by) > 0
                    and all(isinstance(x, str) for x in contested_by)
                )
                # contested_at must equal run_date — a date-equality rule, gated on run_date being
                # supplied. When the dashboard calls lint() without run_date, we require only that
                # contested_at is present and a valid date (the equality half is the curator gate).
                # _date_str canonicalizes an unquoted YAML date scalar (datetime.date) to a string
                # so the comparison/format check fires on the spec's on-disk shape (ADR-0010 §2).
                contested_at_str = _date_str(contested_at)
                if run_date is not None:
                    at_ok = contested_at_str == run_date
                else:
                    at_ok = contested_at_str is not None and bool(_DATE_RE.match(contested_at_str))
                src_ok = len([s for s in sources if isinstance(s, str)]) >= 2
                if not (cb_ok and at_ok and src_ok and has_callout):
                    findings.append(
                        LintFinding(
                            "L1-10",
                            "error",
                            path,
                            "malformed contested shape (need >=2 sources, non-empty contested_by, "
                            "contested_at == run_date, and a '> [!contested]' callout)",
                        )
                    )

        # L1-14 daily date/run_id equality. The basename-date / date-vs-basename comparison is
        # structural (always). The date-vs-run_date and run_id checks are run-relative (gated on
        # run_date). When the FULL injected run_id is supplied, run_id is asserted BYTE-FOR-BYTE
        # (ADR-0010 L1-14 / ADR-0011 §4.4 item 1 — the daily's run_id is the back-link to the run,
        # D1); without it, only the date-portion (run_id[:10] == run_date) can be checked.
        if n.type == "daily":
            bdate = _basename_date(n.basename)
            date_value = _date_str(fm.get("date"))
            run_id_value = fm.get("run_id")
            problems: list[str] = []
            if bdate is None:
                problems.append("basename has no trailing YYYY-MM-DD date")
            elif date_value is not None and date_value != bdate:
                problems.append(f"date {date_value!r} != basename date {bdate!r}")
            if run_date is not None:
                if date_value is not None and date_value != run_date:
                    problems.append(f"date {date_value!r} != run_date {run_date!r}")
                if isinstance(run_id_value, str):
                    if run_id is not None:
                        # Full equality against the injected run_id (the curator gate, ADR-0011).
                        if run_id_value != run_id:
                            problems.append(
                                f"run_id {run_id_value!r} != injected run_id {run_id!r}"
                            )
                    elif run_id_value[:10] != run_date:
                        # No injected run_id (dashboard): only the date-portion can be validated.
                        problems.append(
                            f"run_id {run_id_value!r} date-portion != run_date {run_date!r}"
                        )
            if problems:
                findings.append(
                    LintFinding(
                        "L1-14", "error", path, "daily date mismatch: " + "; ".join(problems)
                    )
                )

    # --- L1-17 schema_version drift across locations present in the worktree -----------------
    findings.extend(_check_schema_version(layout, tax))

    # --- L2-1 orphan-theme count (DERIVED, warning-only; ADR-0022 step 2) ---------------------
    # OFF by default (max_orphans is None ⇒ byte-identical). When set, replicate health()'s exact
    # whole-tree derivation: a THEME whose basename is referenced by NO other note's body markdown
    # link nor any frontmatter related:/children: [[ ]] is an orphan (dailies + MOC/index roots are
    # exempt, type != "theme"). severity="warning" so LintResult.ok is UNCHANGED — this never breaks
    # the §4.4 curator gate or the dashboard; it is a signal, not a per-note error (INGEST §4.4).
    if max_orphans is not None:
        referenced: set[str] = set()
        for n in notes:
            referenced.update(body_link_basenames(n.body))
            for fkey in ("related", "children"):
                fval = n.frontmatter.get(fkey)
                for item in fval if isinstance(fval, list) else [fval]:
                    if isinstance(item, str):
                        referenced.update(wikilinks(item))
        orphans = sum(1 for n in notes if n.type == "theme" and n.basename not in referenced)
        if orphans > max_orphans:
            findings.append(
                LintFinding(
                    "L2-1",
                    "warning",
                    "index.md",
                    f"{orphans} orphan theme(s) exceed curator.lint.max_orphans={max_orphans}",
                )
            )

    findings.sort(key=lambda f: (f.path, f.code))
    ok = not any(f.severity == "error" for f in findings)
    return LintResult(ok=ok, findings=tuple(findings))


def _check_schema_version(layout: RepoLayout, taxonomy: Taxonomy) -> list[LintFinding]:
    """L1-17: ``schema_version`` must agree across every location present in the worktree.

    Canonical source is ``_meta/taxonomy.yaml: schema_version`` (ADR-0010 §5.1). ``_kb/repo.yaml``
    (DATA-MODEL §3) and the schema doc's header MUST equal it. We assert equality only across the
    locations that EXIST in the worktree (a solo MVP repo may have none of the mirrors yet). The
    canonical value is taken from the passed/loaded :class:`Taxonomy`.
    """
    findings: list[LintFinding] = []
    canonical = taxonomy.schema_version

    # _kb/repo.yaml mirror (DATA-MODEL §3). _kb/ is git-ignored but may exist in the worktree.
    repo_yaml = layout.kb_dir / "repo.yaml"
    if repo_yaml.is_file():
        try:
            loaded = yaml.safe_load(repo_yaml.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict) and "schema_version" in loaded:
            value = loaded["schema_version"]
            if value != canonical:
                findings.append(
                    LintFinding(
                        "L1-17",
                        "error",
                        "_kb/repo.yaml",
                        f"schema_version {value!r} != canonical {canonical!r} (taxonomy.yaml)",
                    )
                )

    # The schema doc header (ADR-0010 §5.1: "this schema doc's header MUST equal it"). The emitted
    # doc states "The canonical `schema_version` is **`1`**"; we parse the bolded integer.
    if layout.schema_file.is_file():
        header = layout.schema_file.read_text(encoding="utf-8")
        m = re.search(r"schema_version`?\s+is\s+\*\*`?(\d+)`?\*\*", header)
        if m is not None:
            value = int(m.group(1))
            if value != canonical:
                findings.append(
                    LintFinding(
                        "L1-17",
                        "error",
                        layout.schema_file.relative_to(layout.root).as_posix(),
                        f"schema-doc header schema_version {value} != canonical {canonical}",
                    )
                )
    return findings
