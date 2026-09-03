"""Deterministic L1 lint ruleset (ADR-0010 §6 / the L1-1..L1-24 table).

**Two rulesets, one entry point (ADR-0041 D6).** :func:`lint` reads the repo's KB wiki
``schema_version`` (canonically ``_meta/taxonomy.yaml``, ADR-0010 §5.1; overridable with the
``schema_version=`` kwarg) and dispatches. Schema 1 is ADR-0010's ruleset, UNCHANGED and
byte-identical — the subject is the path segment and the kind is the ``type:`` enum. Schema 2 is
the ADR-0041 ruleset recorded rule-by-rule in ADR-0010's supersession banner: the predicates that
read ``type`` read ``kind`` instead (L1-6/7/8/8b/10/11/14/19, L2-1), the domain check reads
``subjects:`` (L1-5), ``wiki/people/**`` is permanently ungraded (L1-9/D3.3), and three rules are
added — **L1-22** (a ``wiki/`` segment-1 directory outside the closed kind set), **L1-23** (a
taxonomy domain beginning with ``_``, the ``raw/`` reserved-prefix namespace) and **L1-24** (a map
``children:`` bullet whose child kind is not admitted). **L1-21 stays pre-reserved** for the L2-6
promotion after ``agora repo upgrade`` (#63) and is NOT reused. Every L1 severity stays ``error``.

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

The optional ``scope`` argument narrows WHICH notes are graded as producer artifacts (issue #152 /
the ADR-0014 D1 addendum) — never which rules run, and never a severity. Omitted (every read-only
surface, and every call before #152) it grades the whole worktree, byte-identically. The curator
passes the notes IT produced, so a human's hand-written ``wiki/`` note is READ but not GRADED: it is
not a producer artifact, and grading it as one stopped curation of the whole repo forever.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal

import yaml

from agora_kb.core import frontmatter
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
    KIND_BY_DIRECTORY,
    PARSE_EXEMPT_BASENAMES,
    SCHEMA2_DECLARABLE_KINDS,
    Note,
    body_link_basenames,
    child_bullets,
    is_people_path,
    kind_directory_segment,
    note_basename,
    parse_all_notes,
    path_kind,
    v1_path_domain,
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
# the `origin` enum (ADR-0010 D4 — the prior `upload` value is removed). `agent:<name>`,
# `web:<user>` and `harvest:<agent>` are the three PARAMETERIZED forms, validated by prefix below.
#
# EQUALITY WITH THE SOURCE ENUM IS THE CONTRACT, not a coincidence: `kb_schema.md` §2.2/§9 and
# ADR-0010 D4 both call `origin` an EXACT copy, and L1-19's message says so out loud. So when
# `core.models` grows a source form, this grows with it — issue #147 added `agent:<name>` there and
# `tests/schema/test_lint.py` now pins the two together, because a divergence is not a lint bug an
# operator can act on: it is a note (an imported vault, an OKF bundle from another Agora) that is
# hard-rejected forever for carrying a value the writer half calls legal.
_ORIGIN_PLAIN = frozenset(
    {"claude-code", "codex", "qwen", "gemini", "opencode", "hermes", "manual"}
)
_ORIGIN_PREFIXES = ("agent:", "web:", "harvest:")

# A YYYY-MM-DD calendar date (L1-12 date-format half). Frozen so two linters agree byte-for-byte.
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

# The contested-shape body callout (ADR-0010 §3.8 / L1-10): a line STARTING with `> [!contested]`.
_CONTESTED_CALLOUT_RE = re.compile(r"^> \[!contested\]", re.MULTILINE)

# A daily basename is `<domain>-YYYY-MM-DD`; the trailing 10 chars are the date (ADR-0010, L1-14).
_DAILY_DATE_RE = re.compile(r"-(?P<date>\d{4}-\d{2}-\d{2})\Z")

# --- schema-2 rule predicates (ADR-0041, via ADR-0010's supersession banner) -------------------
#
# Each set below replaces ONE v1 `type`-keyed predicate. They are named after the rule group they
# gate so a future reader can check them against the banner line by line rather than by guessing.

# L1-7 / L1-8 / L1-8b / L1-10 / L1-19 and the L2-1 orphan population. `entity` is DELIBERATELY
# absent: ADR-0041 D2 lets an entity carry empty `sources:` while `status: stub`, the banner
# EXCLUDES it from L1-7's non-stub-needs-sources rule by name, and `wiki/entities/` has no day-1
# producer at all (OD-8), so there is nothing for the rule to grade.
_V2_SOURCED_KINDS = frozenset({"concept", "summary"})

# L1-6 (children == child-bullet set) and L1-24 (admitted child kinds). The v1 predicate was
# `type in (moc, index)`; the set-equality check itself is unchanged.
_V2_MAP_KINDS = frozenset({"map", "index"})

# L1-24: the child kinds a map may list (ADR-0041 D1.3). `note` is the v1 "dailies MUST NOT appear
# in children:" prose finally given an enforcement point; `entity` is excluded on day 1 for the
# #146 thin-page seeding reason (OD-2); `person` can never be a child (the curator may not author a
# bullet into a tree it may not write, D3.3).
_V2_ADMITTED_CHILD_KINDS = frozenset({"concept", "summary", "map"})

# The schema-2 `note` journal path: wiki/notes/<yyyy>/<mm>/<yyyy>-<mm>-<dd>.md (D1.1 / D2.6).
_V2_NOTES_DIR = "wiki/notes"

# `body_status:` is present ONLY as `pending`, and only while PASS-2 prose is unauthored; it is
# DROPPED (key absent) once authored (ADR-0010 §2.6, the L2-6 contract).
_BODY_STATUS_PENDING = "pending"


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
    # `agent:<name>` / `web:<user>` / `harvest:<agent>` — a non-empty parameter after the prefix.
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
    """Return a SCHEMA-1 note's domain — the first path component under ``wiki/`` — or ``None``.

    ``wiki/<domain>/...`` ⇒ ``<domain>``. The root ``index.md`` (and any other non-``wiki/`` note)
    has NO domain and is therefore exempt from the L1-5 domain-membership check (the index lists
    domain MOCs; it does not itself belong to a domain).

    SCHEMA 2 NEVER CALLS THIS. ADR-0041 D3.2 leaves exactly one place a subject is recorded — the
    ``subjects:`` frontmatter list — and no code derives one from a path. Delegates to
    :func:`agora_kb.schema.notes.v1_path_domain` so the v1 derivation has one home, shared with the
    ``Note.subjects`` accessor; the behaviour is unchanged, character for character.
    """
    return v1_path_domain(rel_path)


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


def _graded_candidate_paths(
    layout: RepoLayout, graded: frozenset[str] | None, *, skip_people: bool
) -> list[str]:
    """Return the candidate note paths that are GRADED as producer artifacts, in scan order.

    Two independent narrowings, both of which must be applied by every pass that reads BYTES rather
    than parsed notes (the L1-16 encoding pre-pass and the strict frontmatter re-check):

    * ``graded`` — the caller's ``scope`` (issue #152). ``None`` means "every candidate".
    * ``skip_people`` — the schema-2 ``wiki/people/**`` exclusion (ADR-0041 D3.3). It lives INSIDE
      :func:`lint` rather than in a caller-supplied argument on purpose: every caller (curator,
      dashboard, ``kb_status``, ``/metrics``, ``health()``) must behave identically, or a read-only
      surface would report a red KB over a file the curator is FORBIDDEN to fix.

    With ``graded=None`` and ``skip_people=False`` this is :func:`_candidate_note_paths` verbatim,
    which is what keeps schema 1 byte-identical.
    """
    kept: list[str] = []
    for rel in _candidate_note_paths(layout):
        if graded is not None and rel not in graded:
            continue
        if skip_people and is_people_path(rel):
            continue
        kept.append(rel)
    return kept


def _scan_encoding(
    layout: RepoLayout, graded: frozenset[str] | None = None, *, skip_people: bool = False
) -> list[LintFinding]:
    """L1-16 pre-pass over every candidate note file (runs even if frontmatter then fails parse).

    ``graded`` is :func:`lint`'s producer scope (``None`` ⇒ every candidate path, today's
    behaviour). An out-of-scope path is not even READ: a human/Obsidian note is allowed to be CRLF
    or BOM-prefixed, and the point of the scope is that its bytes are none of the producer gate's
    business (ADR-0014 D1/D4, issue #152). ``skip_people`` applies the same reasoning permanently to
    ``wiki/people/**`` under schema 2 (ADR-0041 D3.3): a human writing in Obsidian on Windows may
    well produce CRLF, and that is not a producer finding.
    """
    findings: list[LintFinding] = []
    for rel in _graded_candidate_paths(layout, graded, skip_people=skip_people):
        if not _is_utf8_lf_no_bom((layout.root / rel).read_bytes()):
            findings.append(
                LintFinding("L1-16", "error", rel, "not UTF-8-no-BOM with LF line endings")
            )
    return findings


def _first_malformed(
    layout: RepoLayout, graded: frozenset[str] | None, *, skip_people: bool = False
) -> tuple[str, str] | None:
    """Return ``(rel_path, message)`` of the FIRST GRADED note whose frontmatter does not parse.

    Reached whenever :func:`parse_all_notes` was called tolerantly so that an ungraded note can
    never abort the pass — SCOPED mode (issue #152) and every schema-2 pass (a malformed
    ``wiki/people/**`` note must not hard-reject a curator run it has nothing to do with, ADR-0041
    D3.3). The graded half of the strict contract is preserved here: a producer artifact whose
    frontmatter is malformed is still the ``L1-4`` hard reject it has always been, reported
    fail-fast (first file in sorted scan order) exactly like the unscoped v1 path. Reads only
    graded files.
    """
    for rel in _graded_candidate_paths(layout, graded, skip_people=skip_people):
        text = (layout.root / rel).read_text(encoding="utf-8", errors="replace")
        try:
            frontmatter.parse(text)
        except FrontmatterError as exc:
            return rel, f"{rel}: {exc}"
    return None


# --- per-note required-frontmatter tables (ADR-0010 §2) ---------------------------------------

# Common base keys required on EVERY note type (§2.1). `tags`/`aliases` are required-as-typed (the
# §2 table lists them with `[]` defaults; their type is checked, an empty list is valid).
_COMMON_STR_KEYS = ("title", "summary")
_COMMON_LIST_KEYS = ("tags", "aliases")
_DATE_KEYS = ("created", "updated")


# Schema-2 common base (ADR-0041 D2). `kb:` is REQUIRED and new: the `_meta/kb.yaml` `kb_id`
# stamped by APPLY (D1.5), so a note copied out of the repo still names its origin. `subjects:`
# joins tags/aliases as required-as-typed — an EMPTY list is legal and honest (D2.2).
_V2_COMMON_STR_KEYS = ("title", "summary", "kb")
_V2_COMMON_LIST_KEYS = ("tags", "aliases", "subjects")


def _check_provenance(note: Note) -> list[LintFinding]:
    """L1-4 shape check for the ADR-0041 D2.3 ``provenance:`` block (OPTIONAL, typed when present).

    Two lists, deliberately not one: ``writers`` (authenticated principals, trusted) and ``agents``
    (agent self-declarations, recorded but never trusted). Lint checks only the SHAPE — whether a
    principal is genuinely authenticated is an auth-plane question (Phase 4), not something a
    single-worktree read can answer, and pretending otherwise would be worse than saying nothing.
    """
    block = note.frontmatter.get("provenance")
    if block is None:
        return []
    if not isinstance(block, dict):
        return [
            LintFinding(
                "L1-4",
                "error",
                note.rel_path,
                "'provenance' must be a mapping with 'writers'/'agents' lists",
            )
        ]
    findings: list[LintFinding] = []
    for key in ("writers", "agents"):
        if key in block and _as_str_list(block.get(key)) is None:
            findings.append(
                LintFinding(
                    "L1-4",
                    "error",
                    note.rel_path,
                    f"'provenance.{key}' must be a list of strings",
                )
            )
    return findings


def _check_body_status_value(note: Note) -> list[LintFinding]:
    """L1-4: ``body_status:`` is present ONLY as ``pending`` (ADR-0010 §2.6, carried into schema 2).

    The key is a two-state flag whose ``absent`` state is the KEY BEING ABSENT — there is no
    ``body_status: absent`` value. Any other value is a malformed producer artifact. Whether a
    *present* ``pending`` is STALE is L2-6's (warning-severity) question, not this one's.
    """
    value = note.frontmatter.get("body_status")
    if value is None or value == _BODY_STATUS_PENDING:
        return []
    return [
        LintFinding(
            "L1-4",
            "error",
            note.rel_path,
            f"'body_status' must be {_BODY_STATUS_PENDING!r} when present (the key is DROPPED "
            f"once authored), got {value!r}",
        )
    ]


def _check_required_frontmatter_v2(note: Note) -> list[LintFinding]:
    """L1-4 (missing required key for kind) + L1-11 (unknown kind/status, kind≠directory), schema 2.

    The ADR-0010 banner's amendment of L1-4 and L1-11, implemented literally:

    * **L1-11** grades ``kind:`` against the CLOSED declarable set (``person`` is derived from
      ``wiki/people/`` and never authored, D2.5/D3.3) and then cross-checks it against the
      DIRECTORY, which is authoritative where the two disagree (D2.1). ``status:`` is unchanged.
    * **L1-4** applies the D2 common base — now including a REQUIRED ``kb:`` — plus the per-kind
      additions, whose SHAPE carries over from v1 unchanged: ``concept``/``summary`` add
      ``sources:``/``related:``/``confidence:``/``body_status:``, ``note`` adds
      ``date:``/``run_id:``/``sources:``/``body_status:``, ``map``/``index`` add ``children:``, and
      ``entity`` adds ``sources:``/``related:`` (empty ``sources:`` is legal for an entity — the
      non-empty rule is L1-7 and entity is excluded from it).

    The per-kind branch keys off ``note.kind`` — the DIRECTORY-derived value — so a note in
    ``wiki/concepts/`` claiming ``kind: map`` is graded as the concept it structurally is, and the
    lie is reported separately by L1-11 rather than silently choosing the liar's rule set.
    ``origin:`` (L1-19) and the contested triple (L1-10) keep their own rules; they are not
    duplicated here, exactly as in v1.
    """
    findings: list[LintFinding] = []
    fm = note.frontmatter
    path = note.rel_path

    def miss(code: str, msg: str) -> None:
        findings.append(LintFinding(code, "error", path, msg))

    declared_raw = fm.get("kind")
    declared = declared_raw if isinstance(declared_raw, str) else None
    if declared is None or declared not in SCHEMA2_DECLARABLE_KINDS:
        miss(
            "L1-11",
            f"unknown or missing 'kind' {declared_raw!r}; expected one of "
            f"{sorted(SCHEMA2_DECLARABLE_KINDS)}",
        )
    from_path = path_kind(path)
    if declared is not None and from_path is not None and declared != from_path:
        miss(
            "L1-11",
            f"'kind' {declared!r} contradicts its directory (kind {from_path!r}); the DIRECTORY is "
            f"authoritative and 'kind:' only mirrors it (ADR-0041 D2.1)",
        )

    status = fm.get("status")
    if status is None:
        miss("L1-4", "missing required 'status'")
    elif not (isinstance(status, str) and status in _STATUS_VALUES):
        miss("L1-11", f"unknown 'status' {status!r}; expected one of {sorted(_STATUS_VALUES)}")

    for key in _V2_COMMON_STR_KEYS:
        v = fm.get(key)
        if v is None:
            miss("L1-4", f"missing required '{key}'")
        elif not isinstance(v, str):
            miss("L1-4", f"'{key}' must be a string, got {type(v).__name__}")
    for key in _V2_COMMON_LIST_KEYS:
        if key in fm and _as_str_list(fm.get(key)) is None:
            miss("L1-4", f"'{key}' must be a list of strings")
    for key in _DATE_KEYS:
        if fm.get(key) is None:
            miss("L1-4", f"missing required '{key}'")
    if "derived" in fm and not isinstance(fm.get("derived"), bool):
        miss("L1-4", f"'derived' must be a bool, got {type(fm.get('derived')).__name__}")
    findings.extend(_check_provenance(note))

    kind = note.kind
    if kind in _V2_SOURCED_KINDS:
        for key in ("sources", "related"):
            if key in fm and _as_str_list(fm.get(key)) is None:
                miss("L1-4", f"'{key}' must be a list of strings")
        conf = fm.get("confidence")
        if conf is not None and conf not in ("high", "medium", "low"):
            miss("L1-4", f"'confidence' must be high|medium|low, got {conf!r}")
        findings.extend(_check_body_status_value(note))
    elif kind == "note":
        if fm.get("date") is None:
            miss("L1-4", "missing required 'date' on note")
        if fm.get("run_id") is None:
            miss("L1-4", "missing required 'run_id' on note")
        if "sources" in fm and _as_str_list(fm.get("sources")) is None:
            miss("L1-4", "'sources' must be a list of strings")
        findings.extend(_check_body_status_value(note))
    elif kind in _V2_MAP_KINDS:
        if "children" not in fm:
            miss("L1-4", f"missing required 'children' on {kind}")
        elif _as_str_list(fm.get("children")) is None:
            miss("L1-4", "'children' must be a list of strings")
    elif kind == "entity":
        # `sources:` MAY be empty while `status: stub` (D2), and entity is excluded from L1-7, so
        # only the TYPE is graded here.
        for key in ("sources", "related"):
            if key in fm and _as_str_list(fm.get(key)) is None:
                miss("L1-4", f"'{key}' must be a list of strings")

    return findings


def _check_required_frontmatter(note: Note, schema_version: int = 1) -> list[LintFinding]:
    """L1-4 (missing required key for type) + L1-11 (unknown type/status) for one note.

    Implements the §2 required-field tables: the common base for all types, plus the type-specific
    additions (theme: sources/related/confidence; daily: date/run_id; moc & index: children). Type
    errors (a key present but wrong-typed) are reported under L1-4 alongside absences, since both
    mean "the required frontmatter for this type is not satisfied".

    ``schema_version`` 2 dispatches to :func:`_check_required_frontmatter_v2` (the ADR-0041 D2
    tables keyed on ``kind``); the body below is the schema-1 ruleset, untouched.
    """
    if schema_version >= 2:
        return _check_required_frontmatter_v2(note)
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


def _check_dates(note: Note, run_date: str | None, schema_version: int = 1) -> list[LintFinding]:
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
    keys = ("created", "updated") + (("date",) if _is_journal_kind(note, schema_version) else ())
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


# --- the version-keyed rule predicates (one per v1 `type` test the banner amends) --------------
#
# Each returns the SCHEMA-1 expression verbatim for version 1, so the v1 verdict is byte-identical
# by construction rather than by inspection, and the ADR-0041 kind predicate for version 2.


def _is_sourced_kind(note: Note, schema_version: int) -> bool:
    """L1-7/8/8b/10/19 gate: v1 ``type == theme`` → v2 ``kind in {concept, summary}``."""
    if schema_version >= 2:
        return note.kind in _V2_SOURCED_KINDS
    return note.type == "theme"


def _is_map_kind(note: Note, schema_version: int) -> bool:
    """L1-6 / L1-24 gate: v1 ``type in (moc, index)`` → v2 ``kind in (map, index)``."""
    if schema_version >= 2:
        return note.kind in _V2_MAP_KINDS
    return note.type in ("moc", "index")


def _is_journal_kind(note: Note, schema_version: int) -> bool:
    """L1-12 ``date`` key / L1-14 gate: v1 ``type == daily`` → v2 ``kind == note``."""
    if schema_version >= 2:
        return note.kind == "note"
    return note.type == "daily"


def _check_wiki_kind_directory(note: Note) -> list[LintFinding]:
    """L1-22 (schema 2): a note under ``wiki/`` whose segment-1 directory is not a known kind.

    This is what makes the kind vocabulary CLOSED AT THE DIRECTORY LEVEL (ADR-0041 D3.1) — a
    stronger guarantee than a frontmatter enum a brain writes prose into, because adding a kind
    becomes an explicit reviewable act rather than the side effect of a model inventing a folder.
    A note sitting DIRECTLY under ``wiki/`` has no kind directory at all and is rejected too: its
    filename must not be mistaken for a kind.
    """
    path = note.rel_path
    if not path.startswith("wiki/") or path_kind(path) is not None:
        return []
    segment = kind_directory_segment(path)
    if segment is None:
        return [
            LintFinding(
                "L1-22",
                "error",
                path,
                "note sits directly under wiki/ with no kind directory; expected "
                f"wiki/<{'|'.join(sorted(KIND_BY_DIRECTORY))}>/… (ADR-0041 D3.1)",
            )
        ]
    return [
        LintFinding(
            "L1-22",
            "error",
            path,
            f"unknown wiki/ kind directory {segment!r}; the kind set is closed at the directory "
            f"level: {sorted(KIND_BY_DIRECTORY)} (ADR-0041 D3.1)",
        )
    ]


def _check_reserved_domains(taxonomy: Taxonomy) -> list[LintFinding]:
    """L1-23 (schema 2): a ``_meta/taxonomy.yaml`` ``domains`` entry beginning with ``_``.

    ``raw/<domain>/`` and ``raw/_blob/`` share ONE namespace (ADR-0041 D1.4), so a domain literally
    named ``_blob`` would make APPLY write an event into the content-addressed tree. This is
    **layer 2** of a two-layer reservation and NOT a restatement of layer 1: ``_meta/taxonomy.yaml``
    is human-written and never passes through the plan validator, while a plan token never passes
    through taxonomy load — neither layer covers the other's input. Reported once per offending
    domain, in sorted order, against the taxonomy file itself.
    """
    return [
        LintFinding(
            "L1-23",
            "error",
            "_meta/taxonomy.yaml",
            f"domain {d!r} begins with '_', which is RESERVED for the raw/ prefix namespace "
            f"(raw/_blob, raw/_pages) — ADR-0041 D1.4",
        )
        for d in sorted(set(taxonomy.domains))
        if d.startswith("_")
    ]


def _check_note_shard(note: Note, run_date: str | None, run_id: str | None) -> list[LintFinding]:
    """L1-14 (schema 2): the ``wiki/notes/<yyyy>/<mm>/<yyyy>-<mm>-<dd>.md`` date/shard identity.

    The successor to v1's daily rule. One journal per ``run_date``, repo-wide (ADR-0041 D2.6), so
    the basename is the bare date rather than ``<domain>-YYYY-MM-DD`` and the note↔``run_id``
    relation is 1:1 — which turns a per-domain fan-out into a clean identity check:

    * the basename IS the date (``YYYY-MM-DD``);
    * ``date:`` equals it;
    * the ``<yyyy>/<mm>`` path shard equals the year and month of ``date:`` (ADR-0041 D1.1 — the
      shard is DERIVED from ``run_date`` by the path composer, never parsed out of a model-supplied
      basename, and this is the at-rest assertion of that);
    * and, when the run injects them, ``date == run_date`` and ``run_id`` matches byte-for-byte —
      both carried over from v1 verbatim.
    """
    fm = note.frontmatter
    base = note.basename
    bdate = base if _DATE_RE.match(base) else None
    date_value = _date_str(fm.get("date"))
    run_id_value = fm.get("run_id")
    problems: list[str] = []

    if bdate is None:
        problems.append(f"basename {base!r} is not YYYY-MM-DD")
    elif date_value is not None and date_value != bdate:
        problems.append(f"date {date_value!r} != basename {bdate!r}")

    # The shard is graded against `date:` (D1.1's own wording) and falls back to the basename only
    # when `date:` is absent/unparseable — its absence is already L1-4's finding, and a missing
    # required key must not make the shard rule fail open.
    shard_key = date_value if date_value is not None and _DATE_RE.match(date_value) else bdate
    if shard_key is not None:
        expected = f"{_V2_NOTES_DIR}/{shard_key[:4]}/{shard_key[5:7]}/{base}.md"
        if note.rel_path != expected:
            problems.append(f"path {note.rel_path!r} != {expected!r}")

    if run_date is not None:
        if date_value is not None and date_value != run_date:
            problems.append(f"date {date_value!r} != run_date {run_date!r}")
        if isinstance(run_id_value, str):
            if run_id is not None:
                if run_id_value != run_id:
                    problems.append(f"run_id {run_id_value!r} != injected run_id {run_id!r}")
            elif run_id_value[:10] != run_date:
                problems.append(f"run_id {run_id_value!r} date-portion != run_date {run_date!r}")

    if not problems:
        return []
    return [
        LintFinding(
            "L1-14", "error", note.rel_path, "note date/shard mismatch: " + "; ".join(problems)
        )
    ]


def _check_admitted_children(
    note: Note, children: set[str], kind_by_basename: dict[str, str | None]
) -> list[LintFinding]:
    """L1-24 (schema 2): a map ``children:`` bullet whose child's kind is not admitted (D1.3).

    ``concept``/``summary``/``map`` YES; ``note`` NEVER (a dated journal churns the map every run —
    this is v1's UNENFORCED prose rule finally given a rule number); ``entity`` NO on day 1 (every
    map child is an ADR-0012 ``d_moc = 0`` seed and a population of thin entity pages is exactly
    the #146 husk shape, OD-2); ``person`` never resolves at all (people basenames are outside the
    identity space, D3.3 — an attempt is L1-2's broken link).

    Graded over the UNION of the declared ``children:`` set and the body child bullets, so the rule
    fires on whichever side carries the inadmissible child even when L1-6 is also failing. A child
    that resolves to no note is skipped: that is L1-2's finding, not this one's.
    """
    findings: list[LintFinding] = []
    for child in sorted(children):
        child_kind = kind_by_basename.get(child)
        if child_kind is not None and child_kind not in _V2_ADMITTED_CHILD_KINDS:
            findings.append(
                LintFinding(
                    "L1-24",
                    "error",
                    note.rel_path,
                    f"child {child!r} has kind {child_kind!r}, which a map may not list "
                    f"(admitted: {sorted(_V2_ADMITTED_CHILD_KINDS)} — ADR-0041 D1.3)",
                )
            )
    return findings


def _resolve_targets(notes: list[Note]) -> set[str]:
    """Return the set of all resolvable link targets: every basename ∪ every alias (ADR-0010 §3.1).

    ``[[X]]`` resolves iff ``X`` matches a note basename or an entry in some note's ``aliases:``
    (byte-for-byte, no folding). Used by L1-2 (broken link) after L1-1/L1-15 have validated that the
    union is globally unique. In SCOPED mode (#152) that validation narrows: only GRADED notes are
    checked, so two notes may share a basename when one of them is out of scope (deliberate — the
    curator cannot fix a collision whose other half it may not touch). What IS still enforced is
    the half the curator owns: a graded ALIAS may not take an out-of-scope note's basename.
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
    still exists AT REST, which is what matters to a check that grades the whole worktree: any daily
    published by a pre-#131 build whose APPEND_DAILY dispositions never flagged ``needs_prose``
    carries it, as does any hand-authored or imported note. Asserting the biconditional would
    false-positive on all of them, on a repo that has no way to repair itself until
    ``agora repo upgrade`` (#63).
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
    scope: Collection[str] | None = None,
    schema_version: int | None = None,
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

    ``scope`` (issue #152 / ADR-0014 D1 addendum) restricts WHICH notes are graded as PRODUCER
    artifacts, without changing a single rule or severity. ``None`` (the default, and what every
    read-only surface passes) grades the whole worktree exactly as before — byte-identical. When a
    collection of POSIX repo-relative note paths IS given, only those notes carry findings; every
    other note is still READ (it populates the link-resolution target set and the L2-1 orphan
    derivation, so a curator note may legitimately link to one) but can never raise one. The
    curator passes the set of notes IT PRODUCED — this run's diff plus the notes carrying its own
    stamp — so that a human's hand-written ``wiki/`` note, which the curator neither wrote nor may
    touch, cannot hard-reject the run forever. Parsing turns TOLERANT in scoped mode for the same
    reason (a malformed OUT-OF-SCOPE note no longer aborts the pass); an in-scope malformed note is
    still the fail-fast ``L1-4`` hard reject.

    ``schema_version`` selects the RULESET (ADR-0041 D6). ``None`` (the default) reads it from the
    resolved :class:`~agora_kb.schema.emit.Taxonomy` — i.e. from ``_meta/taxonomy.yaml``, the
    canonical home (ADR-0010 §5.1) — so no caller has to learn a new argument and a repo carries its
    own ruleset. ``1`` is ADR-0010's ruleset, byte-identical to every release before ADR-0041.
    ``2`` is the ADR-0041 ruleset: predicates key off ``kind`` instead of ``type``, L1-5 reads
    ``subjects:`` instead of the path, ``wiki/people/**`` is never graded (D3.3), and L1-22 / L1-23
    / L1-24 are added. An explicit value overrides the taxonomy — the escape hatch for a caller that
    knows the version out of band (a converter writing a destination repo, a test).

    Returns a :class:`LintResult` whose ``findings`` are sorted by ``(path, code)`` (D5) and whose
    ``ok`` is True iff no error-severity finding was raised.
    """
    tax = taxonomy if taxonomy is not None else _load_taxonomy(layout)
    allowed_tags = set(tax.allowed_tags)
    allowed_domains = set(tax.domains)
    graded: frozenset[str] | None = None if scope is None else frozenset(scope)
    version = tax.schema_version if schema_version is None else schema_version
    # ADR-0041 D3.3: `wiki/people/**` is PERMANENTLY excluded from the graded population, inside
    # lint() itself rather than behind a caller-supplied lever, so the curator, the dashboard,
    # `kb_status`, `/metrics` and `health()` can never disagree about a file the curator is
    # forbidden to fix. It is an exclusion from GRADING, not a severity downgrade: this module's
    # whole value is that it has one severity axis, and a subtree-keyed second one would end that.
    skip_people = version >= 2

    findings: list[LintFinding] = []

    # L1-16 encoding pre-pass — runs over raw bytes BEFORE parsing, so a BOM/CRLF file is flagged
    # even when its (BOM-prefixed) frontmatter then fails to parse.
    findings.extend(_scan_encoding(layout, graded, skip_people=skip_people))

    # Parse all notes; a malformed frontmatter block is itself an L1 reject (not a valid note).
    # parse_all_notes is fail-fast: it raises on the FIRST malformed file (in sorted scan order), so
    # a worktree with several malformed notes surfaces one L1-4 per lint pass — deterministic given
    # the bytes, and successive passes report the next file as each is fixed.
    if graded is None and not skip_people:
        try:
            notes = parse_all_notes(layout, strict=True, schema_version=version)
        except FrontmatterError as exc:
            # parse_all_notes prefixes the message with "<rel_path>: ..."; recover the path for the
            # finding so the result still sorts deterministically and points at the broken file.
            # Fall back to a sentinel path if the message ever lacks the ":" separator, so the
            # finding never carries an empty path (which would otherwise sort first and be
            # ambiguous).
            msg = str(exc)
            rel = msg.split(":", 1)[0] if ":" in msg else "<unknown>"
            findings.append(LintFinding("L1-4", "error", rel, f"malformed frontmatter: {msg}"))
            findings.sort(key=lambda f: (f.path, f.code))
            return LintResult(ok=False, findings=tuple(findings))
    else:
        # SCOPED (and every schema-2 pass): read tolerantly so an UNGRADED note that does not parse
        # degrades (empty frontmatter + full text as body) instead of aborting the whole pass — the
        # ADR-0014 D4 tolerant-consumer read the browse face already uses, and, under schema 2, the
        # only way a malformed `wiki/people/**` note can fail to hard-reject a curator run it has
        # nothing to do with. The strict half is re-applied to the GRADED files only, with the same
        # fail-fast shape and the same message.
        notes = parse_all_notes(layout, schema_version=version)
        malformed = _first_malformed(layout, graded, skip_people=skip_people)
        if malformed is not None:
            rel, msg = malformed
            findings.append(LintFinding("L1-4", "error", rel, f"malformed frontmatter: {msg}"))
            findings.sort(key=lambda f: (f.path, f.code))
            return LintResult(ok=False, findings=tuple(findings))

    # Schema 2: the human-owned tree, excluded from grading AND from the basename identity space
    # (ADR-0041 D3.3). Empty on schema 1, which keeps every expression below unchanged there.
    people_paths = frozenset(
        n.rel_path for n in notes if skip_people and is_people_path(n.rel_path)
    )

    # The GRADED population for the cross-note uniqueness rules (L1-1 / L1-13 / L1-15) and the
    # per-note loop. Identical to `notes` when no scope was given and no people tree exists.
    # Deliberately NOT used for `_resolve_targets` below: link RESOLUTION must see every note on
    # disk, or a curator note that links to an out-of-scope note (its basename is still reserved in
    # the plan-gate registry) would be flagged L1-2 broken for a target that plainly exists.
    if graded is None and not people_paths:
        graded_notes = notes
    else:
        graded_notes = [
            n
            for n in notes
            if (graded is None or n.rel_path in graded) and n.rel_path not in people_paths
        ]

    # --- L1-1 duplicate basename / L1-13 second index ----------------------------------------
    by_basename: dict[str, list[str]] = {}
    for n in graded_notes:
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
    # SCOPED mode: an out-of-scope note is not GRADED, but its basename is still RESERVED against
    # the curator's ALIASES — the same asymmetry the plan gate's `live_basenames` already uses
    # (ADR-0014 addendum). `_resolve_targets` below spans every note on disk, so without this a
    # curator alias could silently take a human note's basename and `[[X]]` would then resolve to
    # two different notes with the linter blessing it. An alias is the curator's OWN field, so it
    # is the half the curator can fix — unlike an L1-1 basename collision with a scoped-out note,
    # which stays unreported on purpose (the curator may not touch the other half). Their ALIASES
    # are likewise not reserved: an ungraded note's frontmatter was never validated. Empty (hence
    # byte-identical) whenever no scope was given.
    if graded is not None:
        seen_names |= {
            n.basename for n in notes if n.rel_path not in graded and n.rel_path not in people_paths
        }
    # Sort by path so alias-collision findings are reported deterministically against the union.
    for n in sorted(graded_notes, key=lambda nn: nn.rel_path):
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

    # `known` spans every note that IS in the `[[basename]]` identity space. Under schema 2 that
    # deliberately EXCLUDES `wiki/people/**`: a people note is addressed by PATH, never by
    # `[[basename]]` (ADR-0041 D3.3), so a link into the tree is an L1-2 broken link — stated by the
    # ADR rather than discovered. Without the exclusion a human file at `wiki/people/x/agora.md`
    # would silently reserve the basename `agora` against the curator, which is the exact inverse of
    # "human-owned, curator never touches it".
    linkable = notes if not people_paths else [n for n in notes if n.rel_path not in people_paths]
    known = _resolve_targets(linkable)

    # L1-24 needs each child's KIND, keyed by the identity a child bullet resolves to. Built once,
    # over the linkable population, and only for schema 2 (the rule does not exist in v1).
    kind_by_basename: dict[str, str | None] = (
        {n.basename: n.kind for n in linkable} if version >= 2 else {}
    )

    # --- per-note rules ----------------------------------------------------------------------
    for n in graded_notes:
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

        # L1-4 / L1-11 required frontmatter + enums (keyed on `type` in v1, on `kind` in v2).
        findings.extend(_check_required_frontmatter(n, version))

        # L1-22 (schema 2 only): the kind vocabulary is CLOSED at the directory level (D3.1).
        if version >= 2:
            findings.extend(_check_wiki_kind_directory(n))

        # L1-20 body-sentinel integrity (no unmatched/nested/duplicated agora:body markers,
        # ADR-0011 §4.4 check 6). Runs on every note; only theme/daily carry markers, so a
        # moc/index with no markers always passes.
        findings.extend(_check_body_sentinels(n))

        # L2-6 stale `body_status: pending` over a fully-authored note (#119). WARNING severity —
        # it never flips LintResult.ok, so the §4.4 curator gate and the dashboard verdict are
        # byte-unchanged; see _check_body_status for why an error here would self-lock a run.
        findings.extend(_check_body_status(n))

        # L1-12 dates (format always; no-future only when run_date given).
        findings.extend(_check_dates(n, run_date, version))

        # L1-5 tag/domain membership in the FIXED taxonomy.
        for tag in _str_items(fm.get("tags")):
            if tag not in allowed_tags:
                findings.append(
                    LintFinding("L1-5", "error", path, f"tag {tag!r} not in taxonomy allowed_tags")
                )
        if version >= 2:
            # ADR-0041 D2.2/D3.2: the subject lives in `subjects:` and NOWHERE else. Each entry must
            # already exist in the taxonomy (ADR-0010 D6 preserved exactly — the model still cannot
            # widen the controlled vocabulary), and an EMPTY list is legal and honest: a capture
            # whose subject could not be resolved is filed with no subject rather than a possibly
            # false one, which is what retires ADR-0022 §A's `domains[0]` floor on the wiki side.
            for subject in _str_items(fm.get("subjects")):
                if subject not in allowed_domains:
                    findings.append(
                        LintFinding(
                            "L1-5", "error", path, f"subject {subject!r} not in taxonomy domains"
                        )
                    )
        else:
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
        if _is_map_kind(n, version):
            declared = {k for entry in _str_items(fm.get("children")) for k in wikilinks(entry)}
            bullets = child_bullets(n.body)
            if declared != bullets:
                findings.append(
                    LintFinding(
                        "L1-6",
                        "error",
                        path,
                        f"children: {sorted(declared)} != child-bullet set {sorted(bullets)}",
                    )
                )
            # L1-24 (schema 2 only): the ADMITTED child set, which v1 stated as prose and never
            # enforced — L1-6 is pure set equality and never inspects a child's kind.
            if version >= 2:
                findings.extend(_check_admitted_children(n, declared | bullets, kind_by_basename))

        # Theme-specific rules: L1-7/L1-8/L1-8b sources, L1-10 contested shape, L1-19 origin.
        # L1-8/L1-8b (sources existence + no sidecar) are intentionally THEME-SCOPED: a daily's
        # `sources:` are advisory and MAY be empty (ADR-0010 §2.3 — "a daily is not durable
        # provenance"; §3.4 frames source citation around themes), so they are not checked here.
        if _is_sourced_kind(n, version):
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
        if version >= 2:
            # Schema 2: the wiki/notes/<yyyy>/<mm>/<yyyy>-<mm>-<dd>.md identity (D1.1/D2.6). The v1
            # branch below is NOT reachable here even for a note carrying a stale `type: daily` —
            # `type:` is RETIRED as an authority in schema 2 (D2.5) and must not select a rule.
            if _is_journal_kind(n, version):
                findings.extend(_check_note_shard(n, run_date, run_id))
        elif n.type == "daily":
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

    # --- L1-23 reserved `_`-prefixed taxonomy domain (schema 2 only; ADR-0041 D1.4 layer 2) ---
    if version >= 2:
        findings.extend(_check_reserved_domains(tax))

    # --- L2-1 orphan-theme count (DERIVED, warning-only; ADR-0022 step 2) ---------------------
    # OFF by default (max_orphans is None ⇒ byte-identical). When set, replicate health()'s exact
    # whole-tree derivation: a THEME whose basename is referenced by NO other note's body markdown
    # link nor any frontmatter related:/children: [[ ]] is an orphan (dailies + MOC/index roots are
    # exempt, type != "theme"). severity="warning" so LintResult.ok is UNCHANGED — this never breaks
    # the §4.4 curator gate or the dashboard; it is a signal, not a per-note error (INGEST §4.4).
    if max_orphans is not None:
        referenced: set[str] = set()
        # Schema 2: links OUT of a people note are ungraded (ADR-0041 D3.3), so they do not feed
        # the reference universe either. Otherwise one human file linking a concept would silently
        # suppress that concept's orphan count — a human-owned tree acquiring a vote on the signal
        # that gates a curator run (curator.lint.max_orphans, ADR-0022). Empty on schema 1, so the
        # v1 derivation is byte-identical. W2.3 must land the SAME answer in the two duplicate
        # derivations in `faces/mcp_server.py` (health()), or the dashboard and lint will disagree.
        for n in notes:
            if n.rel_path in people_paths:
                continue
            referenced.update(body_link_basenames(n.body))
            for fkey in ("related", "children"):
                fval = n.frontmatter.get(fkey)
                for item in fval if isinstance(fval, list) else [fval]:
                    if isinstance(item, str):
                        referenced.update(wikilinks(item))
        # The orphan POPULATION follows the same predicate as the sourced-kind rules: v1 themes,
        # v2 `kind in {concept, summary}`. `entity` and `person` are EXEMPT by construction —
        # entities may not be map children (D1.3) and the curator may not link into `people/` at
        # all (D3.3), so both are orphans by design and counting them would make the signal noise.
        orphans = sum(
            1 for n in notes if _is_sourced_kind(n, version) and n.basename not in referenced
        )
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
