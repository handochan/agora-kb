"""Note parsing foundation + the FROZEN content grammars (ADR-0010/ADR-0041, the schema authority).

A KB wiki repo holds notes: markdown files opening with a YAML frontmatter block (parsed by
:mod:`agora_kb.core.frontmatter`). **Two on-disk schemas are read here.** Schema 1 (ADR-0010) puts
the SUBJECT in the path (``wiki/<domain>/themes/<slug>.md``) and the KIND in a closed four-value
``type:`` enum (``index | moc | theme | daily``). Schema 2 (ADR-0041) flips that axis: the first
segment under ``wiki/`` IS the kind (``concepts | summaries | notes | maps | entities | people``,
plus the root ``index.md``) and the subject moves into the ``subjects:`` frontmatter list.

This module provides the deterministic, model-free reading layer the linter (ADR-0010 §6) and
``core.read`` (ADR-0009) both build on:

* :class:`Note` — a parsed note (its relative path, basename, declared ``type``, frontmatter, body)
  plus the schema-2 accessors :attr:`Note.kind`, :attr:`Note.subjects`, :attr:`Note.kb`,
  :attr:`Note.provenance` and :attr:`Note.derived`. ``kind`` is derived for BOTH schemas — a
  schema-1 ``type:`` is mapped through the frozen ADR-0041 D2.5 table — so a read-side caller has
  ONE kind vocabulary regardless of which schema the repo is on.
* :func:`parse_all_notes` — scans ``index.md`` + ``wiki/**/*.md`` in deterministic path order,
  SKIPPING the parse-exempt schema doc (``AGENTS.md`` / ``SCHEMA.md``) by exact basename and its
  symlinks (``CLAUDE.md`` / ``QWEN.md`` / ``GEMINI.md``) by symlink identity (ADR-0010 §1).
* the FROZEN grammars that two independent implementations MUST agree on byte-for-byte (ADR-0010
  D5, as amended by ADR-0014 D3): :func:`wikilinks` (the ``[[basename]]`` resolver normalization,
  §3.1 — still used for the frontmatter ``related:`` / ``children:`` arrays), :func:`child_bullets`
  (the MOC child-bullet regex at indent 0, §3.2 — now a STANDARD MARKDOWN LINK
  ``[Title](relative.md)`` body bullet per ADR-0014 D3, with the basename parsed from the link
  path), and :func:`heading_slug` (the heading-anchor slugger with ``-1`` / ``-2`` duplicate
  disambiguation, §4.1).

These grammars are intentionally rigid: rigidity is the price of a reproducible hard-reject gate and
a deterministic, testable retrieval path (ADR-0010 "Consequences").
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from agora_kb.core import frontmatter
from agora_kb.core.frontmatter import FrontmatterError
from agora_kb.core.layout import RepoLayout

__all__ = [
    "Note",
    "Provenance",
    "parse_all_notes",
    "resolve_schema_version",
    "wikilinks",
    "child_bullets",
    "body_link_basenames",
    "heading_slug",
    "note_basename",
    "PARSE_EXEMPT_BASENAMES",
    "V1_TYPE_TO_KIND",
    "SCHEMA2_DECLARABLE_KINDS",
    "SCHEMA2_KINDS",
    "KIND_BY_DIRECTORY",
    "DIRECTORY_BY_KIND",
    "PEOPLE_DIR_PREFIX",
    "is_people_path",
    "is_ungraded_people_note",
    "kind_directory_segment",
    "path_kind",
    "kind_from_type",
    "v1_path_domain",
]

# --- the schema-2 kind vocabulary (ADR-0041 D1/D2.5/D3.1, frozen) -------------------------------

#: The v1 ``type:`` → schema-2 ``kind:`` table (ADR-0041 D2.5), frozen. It is what lets a schema-1
#: repo be READ through the schema-2 vocabulary without touching a byte on disk: nothing derives a
#: kind from a v1 path, so the mapping is total over the four v1 types and empty elsewhere.
V1_TYPE_TO_KIND: Mapping[str, str] = MappingProxyType(
    {"theme": "concept", "daily": "note", "moc": "map", "index": "index"}
)

#: The CLOSED set of kinds a curator may DECLARE in ``kind:`` (ADR-0041 D2 common base). ``person``
#: is NOT here: it is DERIVED from ``wiki/people/`` and never authored (D2.5/D3.3).
SCHEMA2_DECLARABLE_KINDS = frozenset({"concept", "summary", "note", "map", "entity", "index"})

#: Every schema-2 kind, including the derived ``person``.
SCHEMA2_KINDS = SCHEMA2_DECLARABLE_KINDS | {"person"}

#: ``wiki/<segment-1>`` → kind (ADR-0041 D1/D3.1). The directory is AUTHORITATIVE and ``kind:`` is a
#: mirror (D2.1). ``index`` is absent on purpose: the root map lives at ``index.md``, not under a
#: kind directory, so the directory rule cannot name it (D1.2).
KIND_BY_DIRECTORY: Mapping[str, str] = MappingProxyType(
    {
        "concepts": "concept",
        "summaries": "summary",
        "notes": "note",
        "maps": "map",
        "entities": "entity",
        "people": "person",
    }
)

#: The inverse of :data:`KIND_BY_DIRECTORY` — the directory a kind is written to.
DIRECTORY_BY_KIND: Mapping[str, str] = MappingProxyType(
    {kind: directory for directory, kind in KIND_BY_DIRECTORY.items()}
)

#: The human-owned namespace the curator may never write and lint never grades (ADR-0041 D3.3).
PEOPLE_DIR_PREFIX = "wiki/people/"


def is_people_path(rel_path: str) -> bool:
    """Return True iff ``rel_path`` is inside the human-owned ``wiki/people/`` tree (D3.3).

    Schema 2 only. The tree is outside invariant 2's subject: the curator never writes it, lint
    never grades it, and its basenames are outside the global ``[[basename]]`` identity space.
    """
    return rel_path.startswith(PEOPLE_DIR_PREFIX)


def is_ungraded_people_note(note: Note) -> bool:
    """True iff ``note`` is a schema-2 ``wiki/people/**`` note — human-owned and ungraded (D3.3).

    The SINGLE answer to "is this note outside the curated wiki?", shared by every caller that
    has to agree with :func:`agora_kb.schema.lint.lint`. The version test is not decoration:
    ``lint()`` computes its own exclusion as ``skip_people = version >= 2``, so an unconditional
    path test would make a caller disagree with the gate on a schema-1 repo that merely happens to
    own a ``people`` DOMAIN (where ``wiki/people/x.md`` is an ordinary v1 note, graded like any
    other). One question, one answer — which is why the D3.3 read-side exclusions in
    :mod:`agora_kb.core.gold` and :mod:`agora_kb.faces.mcp_server` call this rather than
    :func:`is_people_path` directly.
    """
    return note.schema_version >= 2 and is_people_path(note.rel_path)


def kind_directory_segment(rel_path: str) -> str | None:
    """Return segment 1 under ``wiki/`` when it is a DIRECTORY component, else ``None``.

    ``wiki/concepts/foo.md`` → ``"concepts"``; ``wiki/concepts/a/b.md`` → ``"concepts"``. A note
    sitting DIRECTLY under ``wiki/`` (``wiki/foo.md``) has no kind directory at all and yields
    ``None`` — lint L1-22 rejects it rather than treating its filename as a kind.
    """
    parts = rel_path.split("/")
    if len(parts) >= 3 and parts[0] == "wiki":
        return parts[1]
    return None


def path_kind(rel_path: str) -> str | None:
    """Return the schema-2 kind a PATH declares, or ``None`` when the path declares none.

    The directory is authoritative (ADR-0041 D2.1): the root ``index.md`` is ``index`` (D1.2) and
    ``wiki/<dir>/…`` maps through :data:`KIND_BY_DIRECTORY`. An unknown segment-1 directory, or a
    note directly under ``wiki/``, returns ``None`` (lint L1-22).
    """
    if rel_path == "index.md":
        return "index"
    segment = kind_directory_segment(rel_path)
    if segment is None:
        return None
    return KIND_BY_DIRECTORY.get(segment)


def kind_from_type(type_value: object) -> str | None:
    """Map a schema-1 ``type:`` to its schema-2 kind (the frozen D2.5 table), else ``None``."""
    if not isinstance(type_value, str):
        return None
    return V1_TYPE_TO_KIND.get(type_value)


def v1_path_domain(rel_path: str) -> str | None:
    """Return a SCHEMA-1 note's domain — the first path component under ``wiki/`` — or ``None``.

    ``wiki/<domain>/...`` ⇒ ``<domain>``. The root ``index.md`` (and any other non-``wiki/`` note)
    has no domain. This is the v1 subject carrier; schema 2 records the subject in ``subjects:``
    and NO code derives one from a path (ADR-0041 D3.2).

    The ``>= 2`` guard is deliberate and is what makes this the LINT reading rather than the read
    one: ``wiki/stray.md`` yields ``"stray.md"``, so a note tolerated directly under ``wiki/`` still
    fails L1-5's domain-membership check. The read facet (:func:`_derive_subjects`) wants the
    opposite and guards on a real directory component, because reporting that filename as a SUBJECT
    would invent a value no note declares.
    """
    parts = rel_path.split("/")
    if len(parts) >= 2 and parts[0] == "wiki":
        return parts[1]
    return None


# The schema doc and its symlinks are NOT notes: excluded from parse_all_notes and every L1 note
# rule (ADR-0010 §1). The doc itself is skipped by exact basename; the symlinks are *additionally*
# skipped by symlink identity (they may be plain copies on Windows, so basename-skip is the fallback
# that still keeps them parse-exempt). The basename set is the union of both so the skip is total
# regardless of whether the OS materialized real symlinks.
PARSE_EXEMPT_BASENAMES = frozenset({"AGENTS", "SCHEMA", "CLAUDE", "QWEN", "GEMINI"})

# --- FROZEN grammars (ADR-0010 D5) ------------------------------------------------------------

# §3.2 MOC child-bullet grammar — EXACT, at indent level 0 (no leading whitespace). ADR-0014 D3
# moves the BODY graph link from a ``[[basename]]`` wikilink to a STANDARD MARKDOWN LINK
# ``[Title](relative-path.md)`` — the single form native to git + Obsidian + OKF (no export step).
# Marker is ``- `` (hyphen-space) only; exactly one markdown link and it is the first token; the
# child BASENAME is the link-target filename minus its directory and ``.md`` suffix (extracted by
# :func:`_basename_from_link_path`). A markdown link in prose, in a nested (indented) bullet, or as
# a second link on the line is NOT a child bullet. Internal IDENTITY remains the globally-unique
# basename (ADR-0010 D5); the path is derived from it and the resolver maps basename↔path.
#
# ``text`` is the link TEXT (the child's title; ``[^\]\r\n]*`` keeps it on one line, no nested
# brackets). ``path`` is the relative target — ``themes/<base>.md`` from a MOC, or
# ``wiki/<domain>/<domain>-moc.md`` from the root index — captured up to the closing ``)`` with no
# newline. The optional trailing group ``(?:\s.*)?$`` permits whitespace-led prose after the link.
_CHILD_BULLET_RE = re.compile(r"^- \[(?P<text>[^\]\r\n]*)\]\((?P<path>[^)\r\n]+)\)(?:\s.*)?$")

# §3.1 Wikilink token — ``[[basename]]`` or ``[[basename|display]]``. The link key is the substring
# left of ``|`` (or the whole), with leading/trailing ASCII whitespace stripped; NO case folding, NO
# unicode normalization, NO slugging. ``[^\[\]\r\n]`` keeps a token on one line and free of nested
# brackets so ``[[a]] [[b]]`` yields two links, not one. STILL used for the frontmatter ``related:``
# / ``children:`` ``[[ ]]`` arrays (Obsidian-Properties-native; ADR-0014 D3 keeps them as wikis).
_WIKILINK_RE = re.compile(r"\[\[(?P<inner>[^\[\]\r\n]*)\]\]")

# A STANDARD MARKDOWN LINK in a note BODY — ``[text](target)`` — used as a graph edge by ADR-0014 D3
# (the MOC/index child bullets). The visible ``text`` carries no nested brackets/newline; ``target``
# is captured up to the closing ``)`` with no newline. This is the body-graph counterpart to
# :data:`_WIKILINK_RE`: :func:`body_link_basenames` resolves each target path to its basename for
# the L1-2 broken-link check and the read-path graph seed (ADR-0012). An IMAGE link ``![alt](...)``
# is excluded (preceding ``!`` asserted absent) so an ``assets/`` image is never a graph edge
# (ADR-0010 §3.5 — assets live outside the link graph).
_BODY_MDLINK_RE = re.compile(r"(?<!\!)\[(?P<text>[^\]\r\n]*)\]\((?P<target>[^)\r\n]+)\)")

# Inline-markdown strippers for the heading-slug algorithm (§4.1 step 2), applied in order:
#   1. wikilink with display [[x|y]] -> y;  bare wikilink [[x]] -> x;
#   2. inline code `code` -> code;
#   3. markdown link [text](url) -> text;
#   4. bold/italic emphasis unwrapped (**b** -> b, _i_ -> i) — only PAIRED delimiter SPANS are
#      unwrapped (keeping the inner text), never raw delimiter chars, and an INTRAWORD `_` is left
#      intact (CommonMark: ``_`` emphasis must not be intraword). So the lone `_` in a snake_case
#      heading survives step 2 and step 4 collapses it to a single `-`: the ADR algorithm yields
#      'the-run-id-field' for 'The run_id field' and 'snake-case-name' for 'snake_case_name', NOT
#      'the-runid-field' / 'snakecasename' (D5 / §4.1). A standalone/unmatched `*` (e.g. 'a * b')
#      likewise has no closing delimiter so it survives and collapses to '-'.
_SLUG_WIKILINK_DISPLAY_RE = re.compile(r"\[\[[^\]\r\n]*\|([^\]\r\n]+)\]\]")
_SLUG_WIKILINK_BARE_RE = re.compile(r"\[\[([^\]\r\n|]+)\]\]")
_SLUG_CODE_RE = re.compile(r"`([^`]*)`")
_SLUG_MDLINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# Paired emphasis spans (longest delimiters first so ``***`` / ``___`` unwrap before ``**`` and
# ``*``). ``*`` spans match anywhere; ``_`` spans require a non-alphanumeric boundary on each outer
# side so an intraword underscore (snake_case, run_id) is NOT treated as emphasis.
_SLUG_EMPHASIS_RES = (
    re.compile(r"\*\*\*([^*]+)\*\*\*"),
    re.compile(r"(?<![A-Za-z0-9])___([^_]+)___(?![A-Za-z0-9])"),
    re.compile(r"\*\*([^*]+)\*\*"),
    re.compile(r"(?<![A-Za-z0-9])__([^_]+)__(?![A-Za-z0-9])"),
    re.compile(r"\*([^*]+)\*"),
    re.compile(r"(?<![A-Za-z0-9])_([^_]+)_(?![A-Za-z0-9])"),
)
# Step 4: any run of non ``[a-z0-9]`` collapses to a single ``-`` (post-lowercase, ASCII fold).
_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Provenance:
    """A note's ADR-0041 D2.3 ``provenance:`` block, split into its two deliberately-unequal lists.

    ``writers`` holds AUTHENTICATED principals and is TRUSTED. ``agents`` holds agent
    SELF-DECLARATIONS and is RECORDED, NEVER TRUSTED. The split is the whole point: without it an
    unauthenticated self-declared agent name would be indistinguishable from an authenticated one,
    and the custody claim Agora makes would be false. Both are empty on a schema-1 note (the block
    does not exist there) and on any note that omits the key.
    """

    writers: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()


@dataclass(frozen=True)
class Note:
    """One parsed wiki note (ADR-0010 §1 / ADR-0041 D2).

    ``rel_path`` is POSIX-style and relative to the repo root (so it is stable across platforms and
    sorts deterministically). ``basename`` is the filename without its ``.md`` suffix — the key the
    ``[[basename]]`` resolver matches against (§3.1). ``type`` is the declared ``type:`` frontmatter
    value, or ``None`` when the key is absent (the linter, not this parser, decides that is an
    error). ``frontmatter`` is the raw parsed mapping and ``body`` the markdown beneath it.

    The remaining attributes are the ADR-0041 reading layer, and every one of them is DERIVED at
    parse time under the repo's ``schema_version`` so a read-side caller never has to branch:

    * ``kind`` — the schema-2 kind. On schema 2 the DIRECTORY is authoritative (D2.1) and the
      frontmatter ``kind:`` is only consulted when the path declares none (an off-layout note lint
      L1-22 rejects). On schema 1 it is the ``type:`` value mapped through the frozen D2.5 table
      (``theme→concept``, ``daily→note``, ``moc→map``, ``index→index``), so both schemas are read
      through ONE vocabulary. ``None`` when neither source yields a kind.
    * ``subjects`` — the note's subjects. On schema 2 the ``subjects:`` frontmatter list (``()`` is
      a legal, honest value, D2.2). On schema 1 the single path DOMAIN DIRECTORY
      (``wiki/<domain>/…``) — the v1 path is the subject carrier there — or ``()`` for the root
      index and for a stray note sitting directly under ``wiki/``, which has no domain directory
      and therefore no subject (see :func:`_derive_subjects`).
    * ``kb`` — the ``_meta/kb.yaml`` ``kb_id`` stamped into the note (D1.5), or ``None``.
    * ``provenance`` — the D2.3 trusted/untrusted split (see :class:`Provenance`).
    * ``derived`` — D2.4's marker for output of a proposal/derivation plane; ``False`` by default.
    * ``schema_version`` — the schema the note was READ under, recorded so a caller that keeps a
      ``Note`` past the read can tell which derivation produced ``kind``/``subjects``.

    Every pre-ADR-0041 attribute keeps its exact meaning and position, and every new one has a
    default, so existing callers and constructions are unaffected.
    """

    rel_path: str
    basename: str
    type: str | None
    frontmatter: dict[str, object] = field(default_factory=dict)
    body: str = ""
    kind: str | None = None
    subjects: tuple[str, ...] = ()
    kb: str | None = None
    provenance: Provenance = field(default_factory=Provenance)
    derived: bool = False
    schema_version: int = 1


def note_basename(path: Path) -> str:
    """Return a note's basename: its filename with the ``.md`` suffix removed.

    Globally-unique basenames are what make ``[[basename]]`` a total function (ADR-0010 §3.1).
    """
    return path.stem


def _iter_note_paths(layout: RepoLayout) -> list[Path]:
    """Return the candidate note paths — ``index.md`` + ``wiki/**/*.md`` — in deterministic order.

    Parse-exempt files (the schema doc + its symlinks) are filtered here: by exact basename in all
    cases, and additionally by symlink identity so a real symlink is never followed/parsed even if
    it somehow lives under the scanned tree (ADR-0010 §1). Order is a stable sort on the POSIX
    relative path so the note list is reproducible across runs and platforms.
    """
    paths: list[Path] = []
    if layout.index_file.is_file():
        paths.append(layout.index_file)
    if layout.wiki_dir.is_dir():
        paths.extend(p for p in layout.wiki_dir.rglob("*.md") if p.is_file())

    kept: list[Path] = []
    for p in paths:
        if note_basename(p) in PARSE_EXEMPT_BASENAMES:
            continue  # skip the schema doc / symlinks by exact basename (Windows-safe fallback)
        if p.is_symlink():
            continue  # skip any symlink by identity (the only allowed symlinks are the schema ones)
        kept.append(p)

    root = layout.root
    return sorted(kept, key=lambda p: p.relative_to(root).as_posix())


def _str_tuple(value: object) -> tuple[str, ...]:
    """Return the string elements of a frontmatter list value, else ``()`` (tolerant accessor)."""
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def _derive_kind(
    rel_path: str, fm: dict[str, object], type_str: str | None, version: int
) -> str | None:
    """Derive a note's kind under ``version`` (ADR-0041 D2.1 / D2.5).

    Schema 2: the DIRECTORY wins (D2.1 — it cannot be falsified by a brain writing prose), and the
    frontmatter ``kind:`` mirror is consulted only when the path declares no kind at all. Schema 1:
    the ``type:`` value through the frozen D2.5 table. Neither derivation reads the other's source,
    so a v1 repo is never re-interpreted by directory and a v2 repo never by ``type:``.
    """
    if version >= 2:
        from_path = path_kind(rel_path)
        if from_path is not None:
            return from_path
        declared = fm.get("kind")
        return declared if isinstance(declared, str) else None
    return kind_from_type(type_str)


def _derive_subjects(rel_path: str, fm: dict[str, object], version: int) -> tuple[str, ...]:
    """Derive a note's subjects under ``version`` (ADR-0041 D2.2 / D3.2).

    Schema 2 reads ``subjects:`` and NOTHING else — no code derives a subject from a path (D3.2),
    and an empty list is a legal, honest value. Schema 1 has exactly one subject and it lives in
    the path segment, so the single path DIRECTORY is lifted into the same tuple shape.

    The v1 leg guards on :func:`kind_directory_segment` (segment 1 only when it is a real DIRECTORY
    component) rather than on :func:`v1_path_domain`, and the difference is the whole point: a
    stray note tolerated directly under ``wiki/`` (``wiki/README.md`` — ADR-0014 D1's tolerant read
    exists for exactly that file) has NO domain directory, and ``v1_path_domain``'s ``>= 2`` guard
    would hand back its FILENAME as a subject. That fabricated value would then surface as a
    heading on the web home page, a chip on ``/graph``, and a member of ``GET /api/notes``'
    ``subjects`` union. ``v1_path_domain`` itself is deliberately left alone: lint's
    ``_note_domain`` wants the ``>= 2`` reading so ``wiki/stray.md`` still raises L1-5 instead of
    silently passing.
    """
    if version >= 2:
        return _str_tuple(fm.get("subjects"))
    domain = kind_directory_segment(rel_path)
    return (domain,) if domain is not None else ()


def _derive_provenance(fm: dict[str, object]) -> Provenance:
    """Read the D2.3 ``provenance:`` block tolerantly (a malformed block degrades to empty)."""
    block = fm.get("provenance")
    if not isinstance(block, dict):
        return Provenance()
    return Provenance(
        writers=_str_tuple(block.get("writers")), agents=_str_tuple(block.get("agents"))
    )


def resolve_schema_version(layout: RepoLayout) -> int:
    """Resolve the repo's KB wiki schema version from disk, defaulting to ``1`` (ADR-0010 §5.1).

    One question, one answer, canonical source: ``_meta/taxonomy.yaml`` wins and ``_kb/repo.yaml``
    is consulted only when it is absent — the precedence :func:`agora_kb.config.load_repo_config`
    and the #98 entry-point guard already use. An INDETERMINATE version (unparseable YAML, a
    non-integer value, a directory that is no Agora repo) resolves to ``1``: the conservative
    answer, because the v1 derivation is what every pre-ADR-0041 release produced.

    The import is function-local ON PURPOSE and not an oversight: ``agora_kb.config`` imports
    ``agora_kb.schema`` (for :class:`~agora_kb.schema.emit.Taxonomy`), so a module-level import
    here would close that cycle. This is the only direction the dependency may run.
    """
    from agora_kb.config import read_kb_schema_version

    return read_kb_schema_version(layout) or 1


def parse_all_notes(
    layout: RepoLayout, *, strict: bool = False, schema_version: int | None = None
) -> list[Note]:
    """Parse every wiki note in ``layout`` into :class:`Note` objects, in deterministic path order.

    Scans ``index.md`` and ``wiki/**/*.md`` (the schema doc ``AGENTS.md`` / ``SCHEMA.md`` and its
    ``CLAUDE.md`` / ``QWEN.md`` / ``GEMINI.md`` symlinks are EXCLUDED — they are parse-exempt,
    ADR-0010 §1). The returned list is sorted by POSIX relative path.

    By default this is a **tolerant** read (ADR-0014 D1 tolerant-consumer / strict-producer): a note
    that does not open with a well-formed frontmatter fence is parsed with empty frontmatter and its
    full text as ``body`` — mirroring :func:`agora_kb.core.wiki._parse_note`, so one fenceless /
    foreign note can never crash a read path (the browse face, ADR-0019). Pass ``strict=True`` to
    re-raise :class:`agora_kb.core.frontmatter.FrontmatterError` (with the offending relative path)
    for callers that must surface a malformed note rather than degrade it (e.g. the producer lint).

    ``schema_version`` selects the KB wiki schema the repo is on (``_meta/taxonomy.yaml``
    ``schema_version``, ADR-0010 §5.1). It changes NOTHING about which files are scanned, how they
    are decoded, or what ``rel_path`` / ``basename`` / ``type`` / ``frontmatter`` / ``body`` hold —
    only how :attr:`Note.kind` and :attr:`Note.subjects` are DERIVED (ADR-0041 D2.1/D2.2).

    ``None`` (the default) RESOLVES it from the repo, exactly as :func:`agora_kb.schema.lint.lint`
    does, so a caller that does not pass one is correct on both schemas without a call-site change.
    That default is what makes the promise in :class:`Note`'s docstring true — "derived under the
    repo's ``schema_version`` so a read-side caller never has to branch" — and it closes the trap a
    hardcoded ``1`` would otherwise set for the read side: on a schema-2 repo a defaulted caller
    would silently get ``subjects`` derived FROM THE PATH (the kind directory) and ``kind = None``,
    the exact path-derived subject ADR-0041 D3.2 forbids. Schema-1 repos resolve to ``1`` and are
    byte-identical. An explicit value still overrides the repo — the escape hatch for a converter
    writing a destination repo, and for a test.
    """
    version = resolve_schema_version(layout) if schema_version is None else schema_version
    notes: list[Note] = []
    for path in _iter_note_paths(layout):
        # Decode LOSSILY (errors="replace") so a non-UTF8 note in a foreign/not-yet-normalized
        # vault never crashes the consumer lint/read path with UnicodeDecodeError (ADR-0014 D4
        # tolerant consumer). The authoritative UTF-8/LF/no-BOM gate is the producer lint's
        # byte-level L1-16 scan, which still flags it; a curated worktree is UTF-8, so this is a
        # no-op there.
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            fm, body = frontmatter.parse(text)
        except FrontmatterError as exc:
            if strict:  # surface which file is malformed for strict callers (the producer lint)
                rel = path.relative_to(layout.root).as_posix()
                raise FrontmatterError(f"{rel}: {exc}") from exc
            # Tolerant read: degrade a fenceless/malformed note to empty frontmatter + full body,
            # exactly like the query path (wiki._parse_note), so the browse face stays up.
            fm, body = {}, text
        type_value = fm.get("type")
        type_str = type_value if isinstance(type_value, str) else None
        rel_path = path.relative_to(layout.root).as_posix()
        kb_value = fm.get("kb")
        notes.append(
            Note(
                rel_path=rel_path,
                basename=note_basename(path),
                type=type_str,
                frontmatter=fm,
                body=body,
                kind=_derive_kind(rel_path, fm, type_str, version),
                subjects=_derive_subjects(rel_path, fm, version),
                kb=kb_value if isinstance(kb_value, str) else None,
                provenance=_derive_provenance(fm),
                derived=fm.get("derived") is True,
                schema_version=version,
            )
        )
    return notes


def wikilinks(text: str) -> list[str]:
    """Extract the resolved link keys of every ``[[ ]]`` occurrence in ``text`` (frozen, §3.1).

    For each ``[[basename]]`` or ``[[basename|display]]`` the key is the substring left of ``|``
    (or the whole, if no ``|``), with leading/trailing ASCII whitespace stripped — and NOTHING
    else: no case folding, no unicode normalization, no slugging. Order is document order
    (left-to-right); duplicates are preserved (callers wanting a set apply ``set()``). An empty key
    (e.g. ``[[]]`` or ``[[ |x]]``) is dropped, since it can never match a basename.
    """
    keys: list[str] = []
    for m in _WIKILINK_RE.finditer(text):
        inner = m.group("inner")
        left = inner.split("|", 1)[0]
        key = left.strip(" \t\r\n\f\v")
        if key:
            keys.append(key)
    return keys


def _basename_from_link_path(path: str) -> str:
    """Return the child BASENAME from a markdown-link target path (ADR-0014 D3 / ADR-0010 D5).

    The basename is the link-target filename minus its directory and ``.md`` suffix — the internal
    globally-unique identity (ADR-0010 D5) the resolver keys off, independent of the relative path
    the curator emitted. Robust to either body-link shape:

    * a MOC's theme child  ``themes/curator-concurrency.md`` → ``curator-concurrency``;
    * the root index's MOC child ``wiki/ai-tech/ai-tech-moc.md`` → ``ai-tech-moc``.

    The path is split on ``/`` (POSIX-style, the only form APPLY emits — no leading ``/`` or
    ``./``); the last segment's trailing ``.md`` (if any) is stripped. ASCII whitespace around the
    captured path is trimmed so a stray space inside ``](  path.md )`` still resolves. A path empty
    after trimming yields the empty string (the caller drops it — it can never match a basename).
    """
    last = path.strip(" \t\r\n\f\v").rsplit("/", 1)[-1]
    if last.endswith(".md"):
        last = last[: -len(".md")]
    return last


def child_bullets(body: str) -> set[str]:
    """Return the set of MOC child basenames in ``body`` (frozen grammar, §3.2 / ADR-0014 D3).

    A child bullet is a line matching EXACTLY the §3.2 regex at indent level 0: marker ``- ``
    (hyphen-space) only, exactly one STANDARD MARKDOWN LINK ``[Title](relative-path.md)`` as the
    first token, optional whitespace-led trailing text. The child basename is parsed from the link
    PATH (filename minus directory minus ``.md``, via :func:`_basename_from_link_path`) — the
    internal globally-unique identity regardless of the relative path emitted (ADR-0010 D5). A
    markdown link in prose, in a nested (indented) bullet, or as a second link on the line is
    IGNORED. Duplicates collapse (set semantics) — L1-6 compares this set against the ``children:``
    basename set (still ``[[basename]]`` wikilink tokens; see :func:`wikilinks`).
    """
    children: set[str] = set()
    for line in body.splitlines():
        m = _CHILD_BULLET_RE.match(line)
        if m is not None:
            base = _basename_from_link_path(m.group("path"))
            if base:
                children.add(base)
    return children


def body_link_basenames(body: str) -> list[str]:
    """Return the resolved child basenames of every BODY markdown graph link (ADR-0014 D3).

    Scans ``body`` for standard markdown links ``[text](target.md)`` (the ADR-0014 D3 body-graph
    edge form), resolving each ``.md`` target PATH to its basename via
    :func:`_basename_from_link_path` (filename minus directory minus ``.md``, the internal
    globally-unique identity, ADR-0010 D5). Order is document order; duplicates are preserved (a
    caller wanting a set applies ``set()``).

    ONLY ``.md`` targets are graph edges: an external URL ``[GCP](https://…)`` or an asset image
    ``![alt](assets/foo.png)`` is NOT a note link, so a non-``.md`` target (and any image link, via
    the regex's ``(?<!\\!)`` guard) is skipped — it is a citation/asset, not a basename edge
    (ADR-0010 §3.5). This is the body counterpart of :func:`wikilinks`, which now resolves ONLY the
    frontmatter ``related:`` / ``children:`` ``[[ ]]`` arrays (ADR-0014 D3 keeps those as wikis);
    the two together are the complete link surface L1-2 resolves and the read path seeds on.
    """
    keys: list[str] = []
    for m in _BODY_MDLINK_RE.finditer(body):
        target = m.group("target").strip(" \t\r\n\f\v")
        if not target.endswith(".md"):
            continue  # external URL or non-note target — not a basename graph edge
        base = _basename_from_link_path(target)
        if base:
            keys.append(base)
    return keys


def _strip_inline_markdown(text: str) -> str:
    """Strip inline markdown for the heading-slug algorithm (§4.1 step 2)."""
    text = _SLUG_WIKILINK_DISPLAY_RE.sub(r"\1", text)  # [[x|y]] -> y
    text = _SLUG_WIKILINK_BARE_RE.sub(r"\1", text)  # [[x]]   -> x
    text = _SLUG_CODE_RE.sub(r"\1", text)  # `code`  -> code
    text = _SLUG_MDLINK_RE.sub(r"\1", text)  # [t](u)  -> t
    for emphasis_re in _SLUG_EMPHASIS_RES:  # **b** -> b; lone `_`/`*` survive (snake_case, a * b)
        text = emphasis_re.sub(r"\1", text)
    return text


def heading_slug(text: str, *, seen: dict[str, int] | None = None) -> str:
    """Compute a heading's anchor slug from its text (frozen, §4.1; matches GitHub/Quartz).

    Steps: (1) take the heading text after the ``#``s — pass that text in as ``text``; (2) strip
    inline markdown (wikilink ``[[x|y]]``->``y``, inline code->its text, ``[t](u)``->``t``,
    ``**b**``->``b``); (3) lowercase (ASCII fold only); (4) replace any run of non ``[a-z0-9]`` with
    a single ``-``;
    (5) trim leading/trailing ``-``; (6) on a duplicate anchor within a file, append ``-1``, ``-2``,
    … in document order.

    Pass a shared ``seen`` mapping (base-slug -> times-emitted) across the headings of ONE file, in
    document order, to get the step-6 disambiguation; omit it for a standalone single-slug compute.
    The returned slug is exactly what ``core.read`` emits as ``SearchHit.anchor`` (ADR-0009).
    """
    stripped = _strip_inline_markdown(text)
    lowered = stripped.lower()
    base = _SLUG_NON_ALNUM_RE.sub("-", lowered).strip("-")
    if seen is None:
        return base
    count = seen.get(base, 0)
    seen[base] = count + 1
    if count == 0:
        return base
    return f"{base}-{count}"
