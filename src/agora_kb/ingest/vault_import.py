"""Obsidian/markdown vault NORMALIZER — the opt-in ``agora import`` op (ADR-0014 D5).

This is the increment-4 piece of ADR-0014: a NON-DESTRUCTIVE importer that reads an external
Obsidian (or plain-markdown) vault and writes a normalized, *closer-to-ADR-0010-conformant* Agora
repo to a fresh destination, accompanied by a clear :class:`ImportReport` of what was auto-fixed and
what still needs human/curator hands. It is the bridge demanded by ADR-0014 D1's
strict-producer/tolerant-consumer split: the SOURCE is read with the tolerant-consumer posture
(D4 — never crash on real-world input), and the DESTINATION is moved toward the strict producer
schema (ADR-0010) so a real vault like ``~/knowledge`` becomes curate-able *by explicit choice*,
never by silent auto-mutation of the original (ADR-0014 D5: "on a backup branch / a new repo").

Design contract (why this module is the way it is):

* **SRC is read-only.** ``import_vault`` only ever READS ``src``; every write lands under ``dest``.
* **Pure of wall clock.** ``import_date`` is INJECTED (the CLI passes today's UTC date) so the
  function is a deterministic, testable function of its inputs — the same purity discipline the
  curator follows for ``run_date`` (ADR-0010 D1).
* **Tolerant consumer (ADR-0014 D4).** A note with malformed YAML frontmatter, a missing field, a
  broken wikilink, or a layout that does not fit ADR-0010 is REPAIRED or MOVED with a recorded
  warning — never a crash and never dropped content.
* **Best-effort, honest report.** v1 does NOT promise a fully lint-clean ``dest``; it emits the
  schema + taxonomy + a git repo, runs :func:`agora_kb.schema.lint.lint`, and ATTACHES that result
  to the report so the operator sees exactly what remains (e.g. an L1-7 theme that still needs
  sources, an unresolved link). Auto-fixes that are deterministic and safe are applied; everything
  judgemental is reported-only.

The auto-fixes (deterministic) and the report-only findings are enumerated on :func:`import_vault`.
"""

from __future__ import annotations

import datetime as _dt
import os.path
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from agora_kb.core import frontmatter
from agora_kb.core.frontmatter import FrontmatterError
from agora_kb.core.layout import RepoLayout
from agora_kb.core.repo import GitError, Repo
from agora_kb.schema import Taxonomy, emit_schema, lint
from agora_kb.schema.lint import LintResult
from agora_kb.schema.notes import note_basename, wikilinks

__all__ = ["ImportReport", "NoteRecord", "import_vault"]

# OKF v0.1 bundle-root version (ADR-0014 D2) — mirrors the curator's ``apply._OKF_VERSION`` and the
# seed index emitted by ``Repo.init``. Emitted on the bundle-root ``index.md`` ONLY.
_OKF_VERSION = "0.1"

# The ADR-0010 §2.6 status vocabulary. An existing frontmatter ``status:`` is preserved only if it
# is one of these; anything else (or absent) is replaced with the ``active`` default.
_STATUS_VALUES = frozenset({"active", "stub", "contested", "deprecated"})

# A bare ``YYYY-MM-DD`` calendar date (ADR-0010 §2). An existing ``created`` / ``updated`` is
# preserved only when it canonicalizes to this shape; otherwise ``import_date`` is substituted.
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

# The Obsidian inline-list frontmatter shape this normalizer repairs: a key whose VALUE on the same
# line is one or more bare ``[[wikilink]]`` tokens (optionally comma-separated), e.g.
# ``links: [[a]], [[b]], [[c]]`` or ``related: [[x]]``. That is invalid YAML (an unquoted ``[``
# starts a flow sequence that never closes), so ``yaml.safe_load`` raises — we rescue it by
# rewriting the value to a valid YAML list of quoted strings BEFORE re-parsing (see
# :func:`_repair_obsidian_frontmatter`).
_INLINE_WIKILINK_KEY_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_-]+)[ \t]*:[ \t]*(?P<value>\[\[.*\]\][ \t]*)$"
)
_WIKILINK_TOKEN_RE = re.compile(r"\[\[[^\[\]\r\n]*\]\]")

# A body wikilink ``[[basename]]`` or ``[[basename|display]]`` — the form increment-2 converts to a
# standard markdown body link when the basename resolves to a known note (ADR-0014 D3). An IMAGE
# transclusion ``![[..]]`` is excluded (a leading ``!`` is asserted absent) since it is an Obsidian
# embed, not a graph edge — it is left verbatim (tolerated, not emitted; ADR-0014 D5).
_BODY_WIKILINK_RE = re.compile(r"(?<!\!)\[\[(?P<inner>[^\[\]\r\n]*)\]\]")

# First ATX H1 in a body (``# Title`` at line start). Used to infer a missing ``title`` (ADR-0010
# §2.1 has no filename fallback for the curator, but the importer SUPPLIES one from the H1, else a
# kebab→Title-Case of the filename, recording it in ``inferred_fields``).
_H1_RE = re.compile(r"^#[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)

# A summary is the first non-empty body paragraph collapsed to ONE line, truncated to this many
# characters (ADR-0010 §2.1 ``summary`` is a one-line precis).
_SUMMARY_MAX_CHARS = 200


@dataclass(frozen=True)
class NoteRecord:
    """Per-note outcome of the import (one record per source ``.md`` note; ADR-0014 D5).

    ``rel_path`` is the POSIX destination-relative path the normalized note was written to (which
    may differ from its source path when the note was MOVED to fit the layout). ``type_inferred`` is
    the ADR-0010 note ``type`` deduced from that destination path. ``repaired_frontmatter`` is True
    iff the source frontmatter was invalid YAML and was rescued (Obsidian inline-wikilink repair, or
    treated-as-empty). ``inferred_fields`` lists the required/OKF frontmatter keys this importer
    SUPPLIED because the source omitted them (e.g. ``title``, ``created``, ``summary``).
    ``stripped_tags`` are source tags dropped because they are not in the destination taxonomy
    (ADR-0010 L1-5; the importer never silently widens the taxonomy). ``converted_links`` counts the
    body ``[[wikilink]]`` tokens rewritten to standard markdown links (ADR-0014 D3 form).
    ``unresolved_links`` are body wikilinks left verbatim because their basename matched no note in
    the vault (tolerated, not dropped; ADR-0014 D4). ``warnings`` are the report-only items the
    operator/curator must resolve (a moved note, a theme with no sources, an unparseable block).
    """

    rel_path: str
    type_inferred: str
    repaired_frontmatter: bool = False
    inferred_fields: tuple[str, ...] = ()
    stripped_tags: tuple[str, ...] = ()
    converted_links: int = 0
    unresolved_links: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportReport:
    """The full outcome of an ``agora import`` run (ADR-0014 D5).

    ``notes`` is one :class:`NoteRecord` per imported source note, in destination-path order.
    ``summary`` is a counts mapping (e.g. ``notes``, ``repaired_frontmatter``, ``moved``,
    ``converted_links``, ``unresolved_links``, ``stripped_tags``, ``themes_without_sources``) so a
    caller can print a one-line digest without re-walking ``notes``. ``warnings`` are run-level
    findings not tied to a single note (e.g. a non-``.md`` file that was ignored is NOT a warning —
    it is silently skipped per D4 — but a structural surprise would land here). ``lint`` is the
    post-import :class:`~agora_kb.schema.lint.LintResult` over ``dest``: v1 does NOT promise it is
    clean, so the report carries it verbatim for the operator to act on (this is the honest
    "what still needs hands" surface, ADR-0014 D5).

    The dataclass is frozen and uses tuples throughout so a report is an immutable, deterministic
    value object (the same discipline as :class:`~agora_kb.schema.lint.LintResult`).
    """

    notes: tuple[NoteRecord, ...]
    summary: dict[str, int]
    warnings: tuple[str, ...]
    lint: LintResult


# --- internal: a mutable per-note accumulator (frozen NoteRecord is built at the end) ----------


@dataclass
class _NoteBuild:
    """Mutable scratch for building one :class:`NoteRecord` as the note is normalized."""

    src_path: Path
    rel_path: str = ""  # destination-relative POSIX path (filled once the layout is decided)
    type_inferred: str = "theme"
    repaired_frontmatter: bool = False
    inferred_fields: list[str] = field(default_factory=list)
    stripped_tags: list[str] = field(default_factory=list)
    converted_links: int = 0
    unresolved_links: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fm: dict[str, object] = field(default_factory=dict)
    body: str = ""

    def finalize(self) -> NoteRecord:
        return NoteRecord(
            rel_path=self.rel_path,
            type_inferred=self.type_inferred,
            repaired_frontmatter=self.repaired_frontmatter,
            inferred_fields=tuple(self.inferred_fields),
            stripped_tags=tuple(self.stripped_tags),
            converted_links=self.converted_links,
            unresolved_links=tuple(self.unresolved_links),
            warnings=tuple(self.warnings),
        )


# --- frontmatter repair (ADR-0014 D4 tolerant read) --------------------------------------------


def _repair_obsidian_frontmatter(text: str) -> tuple[dict[str, object], str, bool]:
    """Tolerantly parse a note, REPAIRING Obsidian inline-wikilink frontmatter (ADR-0014 D4/D5).

    Returns ``(frontmatter, body, repaired)``. First tries :func:`agora_kb.core.frontmatter.parse`
    as-is. If that raises :class:`FrontmatterError` because the YAML is malformed, we attempt the
    one repair this normalizer knows: an Obsidian inline-list line whose value is one or more bare
    ``[[wikilink]]`` tokens (``links: [[a]], [[b]]``), which is invalid YAML. Each such line is
    rewritten to a VALID YAML list of single-quoted strings
    (``links: ['[[a]]', '[[b]]']``) and the document is re-parsed. If the repair makes the block
    parse, ``repaired`` is True. If it still does not parse (some other YAML defect), we fall back
    to EMPTY frontmatter keeping the body after the closing fence — never crashing (D4). When the
    note has no frontmatter fence at all, frontmatter is ``{}`` and the whole text is the body.

    The single-quote escaping is YAML-safe: a ``[[basename]]`` token contains no single quote, so
    wrapping it in ``'...'`` is always valid; we still escape any embedded ``'`` (doubling it) for
    total safety.
    """
    try:
        fm, body = frontmatter.parse(text)
        return fm, body, False
    except FrontmatterError:
        pass

    # Only a fenced document can be repaired here; if it never opened with a ``---`` fence there is
    # no frontmatter to repair — treat the whole text as body (tolerant; D4).
    nl = text.find("\n")
    first = text if nl == -1 else text[:nl]
    if first.strip() != "---":
        return {}, text.strip("\n"), False

    rest = text[nl + 1 :] if nl != -1 else ""
    closing = re.search(r"^---[ \t]*$", rest, re.MULTILINE)
    if closing is None:
        # An unterminated fence: nothing safe to repair — keep everything as body.
        return {}, text.strip("\n"), False
    yaml_text = rest[: closing.start()]
    body = rest[closing.end() :].lstrip("\n").rstrip("\n")

    repaired_lines: list[str] = []
    did_repair = False
    for line in yaml_text.splitlines():
        m = _INLINE_WIKILINK_KEY_RE.match(line)
        if m is None:
            repaired_lines.append(line)
            continue
        tokens = _WIKILINK_TOKEN_RE.findall(m.group("value"))
        if not tokens:
            repaired_lines.append(line)
            continue
        quoted = ", ".join("'" + t.replace("'", "''") + "'" for t in tokens)
        repaired_lines.append(f"{m.group('indent')}{m.group('key')}: [{quoted}]")
        did_repair = True

    if did_repair:
        candidate = "\n".join(repaired_lines)
        try:
            loaded = yaml.safe_load(candidate) if candidate.strip() else {}
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict):
            return loaded, body, True

    # Repair did not yield a parseable mapping: empty frontmatter, body preserved (D4).
    return {}, body, False


# --- type / layout inference (ADR-0010 §1 folder rules) ----------------------------------------


def _kebab_to_title(stem: str) -> str:
    """Turn a kebab/space/underscore filename stem into Title Case (the ADR-0010 title fallback)."""
    words = re.split(r"[-_ ]+", stem.strip())
    return " ".join(w[:1].upper() + w[1:] for w in words if w) or stem


def _slugify(text: str) -> str:
    """Lowercase + non-alnum→'-' kebab slug (collapsed, trimmed). Mirrors the adapters' slugger."""
    lowered = str(text).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return re.sub(r"-+", "-", slug).strip("-")


def _infer_layout(dest_rel: str, first_domain: str) -> tuple[str, str, str | None]:
    """Infer ``(type, normalized-dest-rel, moved_from_or_None)`` from a DEST-relative path.

    The ADR-0010 §1 folder rules drive the inference:

    * ``index.md`` (root)                         → ``index``;
    * ``wiki/<d>/<d>-moc.md``                      → ``moc``;
    * ``wiki/<d>/themes/<slug>.md``                → ``theme``;
    * ``wiki/<d>/daily/<...>.md``                  → ``daily``.

    A note whose path does NOT fit any of these is MOVED to ``wiki/<first-domain>/themes/<slug>.md``
    (a theme is the catch-all durable concept page, ADR-0010 §1) and the original path is returned
    as the third tuple element so the caller can record a ``moved`` warning. A note already in the
    right shape keeps its path and returns ``None`` for the move.
    """
    parts = dest_rel.split("/")

    if dest_rel == "index.md":
        return "index", dest_rel, None

    if len(parts) >= 2 and parts[0] == "wiki":
        domain = parts[1]
        # wiki/<d>/<d>-moc.md
        if len(parts) == 3 and parts[2] == f"{domain}-moc.md":
            return "moc", dest_rel, None
        # wiki/<d>/themes/<slug>.md
        if len(parts) == 4 and parts[2] == "themes" and parts[3].endswith(".md"):
            return "theme", dest_rel, None
        # wiki/<d>/daily/<...>.md
        if len(parts) >= 4 and parts[2] == "daily" and parts[-1].endswith(".md"):
            return "daily", dest_rel, None

    # Anything else: move to a theme under the first domain (catch-all), record the original path.
    stem = parts[-1][: -len(".md")] if parts[-1].endswith(".md") else parts[-1]
    slug = _slugify(stem) or "note"
    moved_dest = f"wiki/{first_domain}/themes/{slug}.md"
    return "theme", moved_dest, dest_rel


# --- required + OKF frontmatter inference (ADR-0010 §2 / ADR-0014 D2) ---------------------------


def _first_paragraph_summary(body: str) -> str:
    """Return the first non-empty body paragraph collapsed to one line, truncated (ADR-0010 §2.1).

    Skips an opening ATX heading line and blank lines; takes the run of non-blank lines that follows
    as the first paragraph, joins them with single spaces, collapses internal whitespace, and
    truncates to :data:`_SUMMARY_MAX_CHARS` (on a word boundary where possible). Returns ``""`` when
    the body has no prose paragraph (the caller then falls back to the title).
    """
    lines = body.splitlines()
    i = 0
    # Skip leading blanks and heading lines (we want PROSE, not the H1 already used for the title).
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    para: list[str] = []
    while i < len(lines) and lines[i].strip():
        para.append(lines[i].strip())
        i += 1
    text = re.sub(r"\s+", " ", " ".join(para)).strip()
    if len(text) <= _SUMMARY_MAX_CHARS:
        return text
    clipped = text[:_SUMMARY_MAX_CHARS]
    # Back off to the last word boundary so we do not truncate mid-word, then strip trailing space.
    cut = clipped.rsplit(" ", 1)[0] if " " in clipped else clipped
    return cut.rstrip()


def _canonical_date(value: object) -> str | None:
    """Return ``value`` as a ``YYYY-MM-DD`` string if it is a valid date scalar, else ``None``.

    Accepts a ``str`` matching ``YYYY-MM-DD`` or a ``datetime.date`` (the shape ``yaml.safe_load``
    produces for an unquoted date scalar); anything else is rejected so the caller substitutes the
    injected ``import_date`` (and records the inference).
    """
    if isinstance(value, _dt.datetime):
        return None
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, str) and _DATE_RE.match(value):
        return value
    return None


def _infer_required_frontmatter(build: _NoteBuild, import_date: str) -> None:
    """Add MISSING ADR-0010 required frontmatter to ``build.fm`` (recording each inferred key).

    Implements ADR-0014 D5 auto-fix #2 (``type``) + #3 + #4 over the (already type-decided) note:

    * ``type`` — the structural type inferred from the destination path (:func:`_infer_layout`); the
      ADR-0010 §1 ``type`` enum is CLOSED, so this is materialized from the layout, never preserved
      from a free-form source ``type:`` (recorded as inferred when the source had no valid value);
    * ``title`` — first H1 in the body, else the filename kebab→Title-Case;
    * ``created`` / ``updated`` — the existing valid ``YYYY-MM-DD`` value if present, else
      ``import_date``;
    * ``status`` — the existing value if it is a valid ADR-0010 status, else ``active``;
    * ``summary`` — the existing value, else the first body paragraph (one line, ≤~200 chars),
      else the title;
    * the OKF fields (ADR-0014 D2): ``description`` mirrors ``summary``; ``timestamp`` ==
      ``<updated>T00:00:00Z`` (deterministic, no wall clock); ``okf_version: '0.1'`` on the
      bundle-root ``index.md`` ONLY.

    Every key this function SUPPLIES (because the source omitted it or carried an invalid value) is
    appended to ``build.inferred_fields`` so the report shows exactly what the importer filled in.
    """
    fm = build.fm

    # type — materialized from the inferred layout (the ADR-0010 §1 enum is closed). Recorded as
    # inferred when the source carried no matching valid type value.
    if fm.get("type") != build.type_inferred:
        build.inferred_fields.append("type")
    fm["type"] = build.type_inferred

    # title — H1 → filename Title Case.
    title = fm.get("title")
    if not isinstance(title, str) or not title.strip():
        h1 = _H1_RE.search(build.body)
        title = h1.group("title").strip() if h1 else _kebab_to_title(build.src_path.stem)
        fm["title"] = title
        build.inferred_fields.append("title")
    else:
        title = title.strip()
        fm["title"] = title

    # created / updated — preserve a valid existing date, else import_date.
    for key in ("created", "updated"):
        existing = _canonical_date(fm.get(key))
        if existing is None:
            fm[key] = import_date
            build.inferred_fields.append(key)
        else:
            fm[key] = existing

    # status — preserve a valid existing status, else 'active'.
    status = fm.get("status")
    if not (isinstance(status, str) and status in _STATUS_VALUES):
        fm["status"] = "active"
        build.inferred_fields.append("status")

    # summary — existing one-line string, else first paragraph, else the title.
    summary = fm.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = _first_paragraph_summary(build.body) or str(fm["title"])
        fm["summary"] = summary
        build.inferred_fields.append("summary")
    else:
        summary = summary.strip()
        fm["summary"] = summary

    # OKF additive fields (ADR-0014 D2). description mirrors summary; timestamp from updated.
    fm["description"] = summary
    build.inferred_fields.append("description")
    updated = str(fm["updated"])
    fm["timestamp"] = f"{updated}T00:00:00Z"
    build.inferred_fields.append("timestamp")
    if build.type_inferred == "index":
        fm["okf_version"] = _OKF_VERSION
        build.inferred_fields.append("okf_version")

    # Type-specific required keys (ADR-0010 §2). An index/moc needs ``children:`` (L1-4); the
    # importer does not synthesize child bullets, so an absent value defaults to ``[]`` — consistent
    # with the empty child-bullet set (L1-6). A non-list existing value is normalized to ``[]`` by
    # :func:`_normalize_fm_link_arrays`. ``daily`` ``date``/``run_id`` are NOT synthesized (no run
    # context exists at import); a daily that lacks them is reported via the attached lint, not
    # auto-fixed (ADR-0014 D5 — v1 does not promise a lint-clean daily).
    if build.type_inferred in ("index", "moc") and "children" not in fm:
        fm["children"] = []
        build.inferred_fields.append("children")


# --- tag taxonomy gate (ADR-0010 L1-5) ---------------------------------------------------------


def _filter_tags(build: _NoteBuild, allowed_tags: set[str]) -> None:
    """Keep only ``tags`` already in the destination taxonomy; STRIP + record the rest (ADR-0014).

    ADR-0014 D5 auto-fix #6 / ADR-0010 L1-5: the importer NEVER silently widens the taxonomy. A
    source tag absent from ``allowed_tags`` (the ``--tag`` set provided at import) is removed from
    the note and recorded in ``build.stripped_tags`` (so the operator can choose to extend the
    taxonomy on the separate admin path). A non-list / non-string ``tags`` value is replaced with an
    empty list (tolerant — a malformed tags field never crashes the import).
    """
    raw = build.fm.get("tags")
    kept: list[str] = []
    if isinstance(raw, list):
        for t in raw:
            if not isinstance(t, str):
                continue
            if t in allowed_tags:
                kept.append(t)
            else:
                build.stripped_tags.append(t)
    build.fm["tags"] = kept


# --- body link conversion (ADR-0014 D3 / increment-2 form) -------------------------------------


def _convert_body_links(build: _NoteBuild, basename_to_relpath: dict[str, str]) -> None:
    """Convert body ``[[wikilink]]`` tokens to standard markdown links when resolvable (ADR-0014).

    For each body ``[[basename]]`` / ``[[basename|display]]`` (image transclusions ``![[..]]`` are
    excluded), resolve the basename against ``basename_to_relpath`` (the vault-wide basename→
    dest-relpath map built over ALL notes). When it resolves, rewrite the token to a standard
    markdown link ``[display-or-title](<relative-path>.md)`` whose path is RELATIVE from the LINKING
    note's directory (matching increment-2's emitted form), and increment ``converted_links``. When
    it does NOT resolve, leave the ``[[token]]`` verbatim and record the basename in
    ``unresolved_links`` (tolerant — content is never dropped; ADR-0014 D4).

    The display text is the part after ``|`` when present, else the basename itself (the importer
    has no resolved title here, and the basename is a stable, human-readable label). The relative
    path is computed from the destination directory of THIS note to the target's destination path
    via :func:`os.path.relpath`, then POSIX-normalized.
    """
    from_dir = str(Path(build.rel_path).parent)

    def repl(m: re.Match[str]) -> str:
        inner = m.group("inner")
        left, sep, right = inner.partition("|")
        key = left.strip(" \t\r\n\f\v")
        display = right.strip() if sep else key
        if not key:
            return m.group(0)  # an empty [[]] — leave verbatim, never a graph edge
        target = basename_to_relpath.get(key)
        if target is None:
            build.unresolved_links.append(key)
            return m.group(0)
        rel = os.path.relpath(target, from_dir)
        rel_posix = Path(rel).as_posix()
        build.converted_links += 1
        return f"[{display}]({rel_posix})"

    build.body = _BODY_WIKILINK_RE.sub(repl, build.body)


# --- frontmatter related/children normalization ------------------------------------------------


def _flow_seq_basenames(entry: object) -> list[str]:
    """Recover the basename(s) from a YAML-parsed inline-wikilink ``related:``/``children:`` entry.

    An Obsidian inline value like ``related: [[only]]`` stands alone as VALID YAML — ``[[only]]`` is
    a nested flow sequence, so ``yaml.safe_load`` yields the list ``['only']`` (and ``[[]]`` yields
    ``[]``) instead of the string ``'[[only]]'``. The frontmatter-line repair only fires on a YAML
    PARSE FAILURE, which a single standalone token never triggers, so such an entry reaches this
    normalizer as a LIST, not a string. Recover the inner basename(s) so the resolvable graph edge
    is never silently dropped (ADR-0014 D4 — content is never lost). A string entry runs through
    :func:`wikilinks`; a one-level-nested list (the flow-sequence shape) yields its string members'
    basenames; anything else yields nothing (tolerant).
    """
    if isinstance(entry, str):
        return wikilinks(entry)
    if isinstance(entry, list):
        bases: list[str] = []
        for inner in entry:
            if isinstance(inner, str) and inner.strip():
                bases.append(inner.strip())
        return bases
    return []


def _normalize_fm_link_arrays(build: _NoteBuild, known_basenames: set[str]) -> None:
    """Keep only ``related:``/``children:`` wikilinks that resolve; report the rest (ADR-0014 D3).

    Frontmatter ``related:`` and ``children:`` STAY ``[[basename]]`` strings (ADR-0014 D3 — the
    Obsidian-Properties-native form, unchanged by increment 2). The strict producer schema rejects a
    ``[[basename]]`` that resolves to no note (L1-2), so to push ``dest`` toward conformance we DROP
    any entry whose basename is unknown in the vault and record it in ``unresolved_links`` (the same
    tolerant-but-honest treatment as a body link). Entries are normalized to canonical
    ``[[basename]]`` tokens (stripping any padding / display suffix) so they match the body grammar.

    Each entry may arrive as a ``'[[basename]]'`` STRING (the form the line-repair produced from a
    multi-token / invalid-YAML block) OR as a LIST (the form ``yaml.safe_load`` produced from a
    standalone, already-valid ``[[basename]]`` line, e.g. ``['basename']``); both shapes are
    flattened to their inner basename(s) via :func:`_flow_seq_basenames` so a single-token inline
    link is resolved-or-reported, never silently dropped. A non-list value is replaced with an empty
    list (tolerant).
    """
    for key in ("related", "children"):
        raw = build.fm.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list):
            build.fm[key] = []
            continue
        kept: list[str] = []
        for entry in raw:
            for base in _flow_seq_basenames(entry):
                if base in known_basenames:
                    kept.append(f"[[{base}]]")
                else:
                    build.unresolved_links.append(base)
        build.fm[key] = kept


# --- the public entry point --------------------------------------------------------------------


def import_vault(
    src: Path,
    dest: Path,
    *,
    domains: list[str],
    import_date: str,
    tags: list[str] | None = None,
) -> ImportReport:
    """Import an external Obsidian/markdown vault at ``src`` into a normalized Agora repo at dest.

    The opt-in ``agora import`` normalizer (ADR-0014 D5). ``src`` is read NON-DESTRUCTIVELY — it is
    never modified. ``dest`` receives a fresh, git-initialized Agora repo: the emitted schema +
    taxonomy (``domains`` + the operator-declared ``tags``), the normalized notes, and (returned) a
    :class:`ImportReport`. ``import_date`` is the INJECTED ``YYYY-MM-DD`` the caller derives once at
    the CLI boundary, so this function reads NO wall clock and is a deterministic function of its
    inputs (ADR-0010 D1 purity discipline). ``tags`` is the CLOSED taxonomy tag set the operator
    declared (``--tag``); a source note tag absent from it is STRIPPED, never silently added
    (ADR-0010 D6 / ADR-0014 D5 — the importer never widens the taxonomy).

    Deterministic AUTO-FIXES applied per note (ADR-0014 D5):

    1. tolerant frontmatter read with Obsidian inline-wikilink REPAIR
       (:func:`_repair_obsidian_frontmatter`) — never crashes;
    2. note ``type`` + layout inference from the DEST-relative path
       (:func:`_infer_layout`); a non-conforming path is MOVED under the first domain's ``themes/``;
    3. missing required ADR-0010 frontmatter SUPPLIED (``title``/``created``/``updated``/``status``/
       ``summary``) + 4. the OKF fields (``description``/``timestamp``/``okf_version`` on index)
       (:func:`_infer_required_frontmatter`);
    5. body ``[[wikilink]]`` → standard markdown link when the basename resolves
       (:func:`_convert_body_links`); an unresolvable link is left verbatim and reported;
    6. tags outside the destination taxonomy are STRIPPED + recorded (:func:`_filter_tags`).

    REPORT-ONLY (v1 does NOT auto-fix; recorded as warnings for the operator/curator): a theme with
    no ``sources:`` (L1-7 — "needs sources or status: stub"); a note that was MOVED to fit the
    layout; stripped unknown tags; unresolved links; a note whose frontmatter could not be parsed at
    all. After writing ``dest`` the schema + taxonomy are emitted, the repo is git-initialized, and
    :func:`agora_kb.schema.lint.lint` is run — its result is ATTACHED to the report. ``dest`` is a
    best-effort conformant repo PLUS a report of what still needs hands; a fully lint-clean ``dest``
    is NOT promised in v1.

    Raises ``FileNotFoundError`` / ``NotADirectoryError`` if ``src`` is missing or not a directory
    (a hard error). ``domains`` must be non-empty (the first domain is the move target).
    """
    src = Path(src)
    dest = Path(dest)
    if not src.exists():
        raise FileNotFoundError(f"source vault does not exist: {src}")
    if not src.is_dir():
        raise NotADirectoryError(f"source vault is not a directory: {src}")
    if not domains:
        raise ValueError("import_vault requires at least one domain (the move target)")

    first_domain = domains[0]
    src = src.resolve()

    # --- pass 0: discover every source .md note (tolerant: ignore non-.md and hidden dirs) -------
    # ADR-0014 D4: non-``.md`` files (``.canvas``, images) and the ``.obsidian/`` config dir are
    # IGNORED, not errors. Hidden directories (leading ``.``) and the dest (if nested) are skipped.
    src_md: list[Path] = []
    for p in sorted(src.rglob("*.md")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(src).parts
        if any(part.startswith(".") for part in rel_parts):
            continue  # skip .obsidian/, .git/, dotfiles
        src_md.append(p)

    # --- pass 1: parse + decide type/layout for every note, building the basename map ------------
    builds: list[_NoteBuild] = []
    basename_to_relpath: dict[str, str] = {}
    for p in src_md:
        src_rel = p.relative_to(src).as_posix()
        # SRC is read with the tolerant-consumer posture (ADR-0014 D4 — never crash on real-world
        # input). Real Obsidian vaults routinely hold notes saved as latin-1/cp1252 or with stray
        # bytes; a non-UTF8 note is decoded with replacement (content preserved, lossy bytes
        # flagged) and recorded as a per-note warning, never propagated as an UnicodeDecodeError
        # that would abort the whole import.
        decode_warning: str | None = None
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_bytes().decode("utf-8", errors="replace")
            decode_warning = "source was not valid UTF-8; decoded with replacement (needs review)"
        fm, body, repaired = _repair_obsidian_frontmatter(text)
        build = _NoteBuild(src_path=p, fm=dict(fm), body=body)
        build.repaired_frontmatter = repaired
        if decode_warning is not None:
            build.warnings.append(decode_warning)
        if not isinstance(fm, dict) or (not fm and not repaired):
            # A note whose frontmatter could not be parsed at all (empty after a failed repair on a
            # fenced doc) is reported, per ADR-0014 D5 report-only items.
            if text.lstrip().startswith("---") and not fm:
                build.warnings.append(
                    "frontmatter could not be parsed; treated as empty (needs review)"
                )

        # Infer the type + (possibly moved) destination path from the SOURCE-relative path.
        type_inferred, dest_rel, moved_from = _infer_layout(src_rel, first_domain)
        build.type_inferred = type_inferred
        build.rel_path = dest_rel
        if moved_from is not None:
            build.warnings.append(
                f"moved to fit the ADR-0010 layout (was {moved_from!r}; "
                f"no {type_inferred} path matched)"
            )
        builds.append(build)

    # Build the basename→dest-relpath map over ALL notes (last writer wins on a basename clash; a
    # genuine duplicate basename is an L1-1 the attached lint will surface).
    for build in builds:
        basename_to_relpath[note_basename(Path(build.rel_path))] = build.rel_path
    known_basenames = set(basename_to_relpath)

    # --- the closed destination taxonomy (operator-declared; never widened) ----------------------
    # ADR-0010 D6 / ADR-0014 D5: ``allowed_tags`` is exactly what the operator passed via ``--tag``;
    # the importer keeps a source tag only when it is already in this set and strips the rest.
    taxonomy = Taxonomy(
        schema_version=1,
        taxonomy_policy="open",
        allowed_tags=tuple(tags or ()),
        domains=tuple(domains),
    )
    allowed_tags = set(taxonomy.allowed_tags)
    layout = RepoLayout(dest)
    dest.mkdir(parents=True, exist_ok=True)

    # --- pass 2: normalize frontmatter + links for every note ------------------------------------
    for build in builds:
        _infer_required_frontmatter(build, import_date)
        _filter_tags(build, allowed_tags)
        _normalize_fm_link_arrays(build, known_basenames)
        _convert_body_links(build, basename_to_relpath)
        if build.type_inferred == "theme" and not _has_sources(build.fm):
            build.warnings.append("theme has no raw/ sources (L1-7): needs sources or status: stub")

    # --- write the normalized notes under dest ---------------------------------------------------
    for build in builds:
        out_path = dest / build.rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(frontmatter.render(_ordered_fm(build.fm), build.body), encoding="utf-8")

    # --- emit schema + taxonomy, git-init, then lint ---------------------------------------------
    # The injected ``import_date`` pins the init/admin commit date so the repo is byte-reproducible
    # (no wall clock; ADR-0010 D1 discipline). ``Repo.init`` seeds its own ``index.md`` only when
    # one is absent, so an imported index is preserved; the import's notes are committed as one
    # admin commit. An empty diff over the seed (a vault with no notes) is a no-op, not an error.
    emit_schema(layout, taxonomy=taxonomy)
    repo = Repo(layout)
    when = datetime.fromisoformat(f"{import_date}T00:00:00+00:00").astimezone(UTC)
    repo.init(when=when)
    try:
        repo.commit_all("chore: import external vault (agora import)", when=when)
    except GitError:
        pass  # nothing to commit (no diff over the seed)

    lint_result = lint(layout, taxonomy=taxonomy)

    # --- assemble the report ---------------------------------------------------------------------
    records = tuple(sorted((b.finalize() for b in builds), key=lambda r: r.rel_path))
    summary = {
        "notes": len(records),
        "repaired_frontmatter": sum(1 for r in records if r.repaired_frontmatter),
        "moved": sum(1 for r in records if any("moved to fit" in w for w in r.warnings)),
        "converted_links": sum(r.converted_links for r in records),
        "unresolved_links": sum(len(r.unresolved_links) for r in records),
        "stripped_tags": sum(len(r.stripped_tags) for r in records),
        "themes_without_sources": sum(
            1 for r in records if any("needs sources" in w for w in r.warnings)
        ),
        "lint_findings": len(lint_result.findings),
    }
    return ImportReport(notes=records, summary=summary, warnings=(), lint=lint_result)


# --- small frontmatter helpers -----------------------------------------------------------------


def _has_sources(fm: dict[str, object]) -> bool:
    """True iff ``fm`` carries a non-empty ``sources:`` list (ADR-0010 L1-7)."""
    raw = fm.get("sources")
    return isinstance(raw, list) and len(raw) > 0


def _ordered_fm(fm: dict[str, object]) -> dict[str, object]:
    """Return ``fm`` reordered into the ADR-0010 §2 documented key order (stable rendered YAML).

    The curator's APPLY emits frontmatter in a fixed order (``title``, ``type``, ``okf_version`` on
    index, ``aliases``, ``tags``, ``created``, ``updated``, ``timestamp``, ``status``, ``summary``,
    ``description``, then type-specific keys). Mirroring that order keeps an imported note's YAML
    byte-comparable to a curated one and the diff readable. Unknown keys (preserved from the source
    per the tolerant round-trip, ADR-0014 D4) are appended in their original order after the known
    ones.
    """
    order = [
        "title",
        "type",
        "okf_version",
        "aliases",
        "tags",
        "created",
        "updated",
        "timestamp",
        "status",
        "summary",
        "description",
        "sources",
        "related",
        "children",
        "date",
        "run_id",
        "origin",
        "confidence",
    ]
    ordered: dict[str, object] = {}
    for key in order:
        if key in fm:
            ordered[key] = fm[key]
    for key, value in fm.items():
        if key not in ordered:
            ordered[key] = value
    return ordered
