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
import posixpath
import re
import string
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import yaml

from agora_kb.core import frontmatter
from agora_kb.core.frontmatter import FrontmatterError
from agora_kb.core.hashing import content_sha256
from agora_kb.core.layout import KIND_DIRECTORIES, RepoLayout
from agora_kb.core.pathsafe import safe_slug_component
from agora_kb.core.repo import GitError, Repo
from agora_kb.schema import Taxonomy, emit_schema, lint
from agora_kb.schema.emit import materialize_kind_directories
from agora_kb.schema.lint import LintResult
from agora_kb.schema.notes import (
    PARSE_EXEMPT_BASENAMES,
    child_bullets,
    note_basename,
    wikilinks,
)

__all__ = ["ImportReport", "NoteRecord", "import_vault"]

# OKF v0.1 bundle-root version (ADR-0014 D2) — mirrors the curator's ``apply._OKF_VERSION`` and the
# seed index emitted by ``Repo.init``. Emitted on the bundle-root ``index.md`` ONLY.
_OKF_VERSION = "0.1"

# The KB WIKI SCHEMA this importer emits. Named rather than repeated as a literal, because it is ONE
# fact with several consumers that must never disagree: the `Taxonomy` the emitted repo declares,
# the `Repo.init` seed, the lint ruleset the report attaches, and the destination guard
# (`_assert_importable_destination`) that refuses to write this layout into a repo declaring a
# different one.
#
# It is 2 (ADR-0041 D1, the kind-first layout). It was 1 through wave W2.2, and the consequence was
# a genuine defect rather than a cosmetic lag: this build's curator WRITES schema 2 and D6 makes a
# schema-1 repo READ-ONLY, so an importer emitting schema 1 minted a repo that refused every write
# the moment it was created — `agora curate`, `kb_remember` and the web upload all bounced off a
# repo whose own import had just reported `lint: clean`.
IMPORTER_SCHEMA_VERSION = 2

# STRUCTURAL / non-knowledge files that the DEST emits its OWN copy of, so importing them AS notes
# only produces junk themes (ADR-0014 D5 v2 change A). They are NOT knowledge content:
#
# * the schema doc + its agent-tool symlinks — skipped by exact STEM (the union ``schema.notes``
#   uses to keep them parse-exempt, ADR-0010 §1): ``AGENTS`` / ``SCHEMA`` / ``CLAUDE`` / ``QWEN`` /
#   ``GEMINI``;
# * ``log.md`` — the append-only curator run log (ADR-0010 §1; the dest writes its own);
# * anything under a ``_templates/`` / ``_meta/`` / ``_kb/`` directory at ANY depth (the per-type
#   note templates, the taxonomy, and the git-ignored engine spool — ADR-0010 §1 layout);
# * any SYMLINK (the only allowed symlinks are the schema-doc ones, already covered by stem — a
#   symlink is never imported as a note, mirroring ``schema.notes._iter_note_paths``).
#
# ``log`` is added to the exempt-STEM set (``log.md`` -> stem ``log``) alongside the parse-exempt
# schema basenames so a single stem check covers the schema doc, its symlinks, AND the run log.
_EXEMPT_STEMS = PARSE_EXEMPT_BASENAMES | frozenset({"log"})

# Directory names whose subtree (at any depth) is structural, never knowledge (ADR-0010 §1).
_STRUCTURAL_DIRS = frozenset({"_templates", "_meta", "_kb"})

# An indent-0 MOC child bullet, used by change C to DROP a child-bullet line whose target does not
# resolve to a written note. Mirrors :data:`agora_kb.schema.notes._CHILD_BULLET_RE` (the FROZEN §3.2
# grammar) so the line this importer strips is EXACTLY the one ``child_bullets`` would have counted
# guaranteeing L1-6 (``children:``-set == child-bullet-set) holds by construction after the strip.
_CHILD_BULLET_LINE_RE = re.compile(r"^- \[(?P<text>[^\]\r\n]*)\]\((?P<path>[^)\r\n]+)\)(?:\s.*)?$")

# The ADR-0010 §2.6 status vocabulary, carried into schema 2 unchanged. An existing frontmatter
# ``status:`` is preserved only if it is one of these; anything else (or absent) becomes ``active``.
_STATUS_VALUES = frozenset({"active", "stub", "contested", "deprecated"})

# The MAP TIER (ADR-0041 D1.2/D1.3): the two kinds that carry ``children:`` and whose child bullets
# lint L1-6 grades. ``index`` is the ROOT of the tier and lives at the repo root, not under
# ``wiki/maps/`` — which is why it is named here rather than derived from a directory.
_MAP_KINDS = frozenset({"map", "index"})

# The child kinds a map may list (ADR-0041 D1.3 / lint L1-24). ``note`` is NEVER admitted (a dated
# journal would churn the map every run) and ``entity`` is excluded on day 1 (OD-2), so an imported
# child bullet resolving to anything outside this set is DROPPED + reported rather than written into
# a repo the destination's own lint would reject.
_ADMITTED_CHILD_KINDS = frozenset({"concept", "summary", "map"})

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

# A body markdown link ``[label](target)`` — the ADR-0014 D3 graph edge in its EMITTED form, which
# a vault may already carry. An IMAGE embed ``![label](target)`` is excluded (an attachment, not an
# edge). Mirrors ``kb_convert._MDLINK_RE`` so the two import lanes read one grammar.
_BODY_MDLINK_RE = re.compile(r"(?<!\!)\[(?P<label>[^\]\r\n]*)\]\((?P<target>[^)\r\n]+)\)")

# First ATX H1 in a body (``# Title`` at line start). Used to infer a missing ``title`` (ADR-0010
# §2.1 has no filename fallback for the curator, but the importer SUPPLIES one from the H1, else a
# kebab→Title-Case of the filename, recording it in ``inferred_fields``).
_H1_RE = re.compile(r"^#[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)

# A summary is the first non-empty body paragraph collapsed to ONE line, truncated to this many
# characters (ADR-0010 §2.1 ``summary`` is a one-line precis).
_SUMMARY_MAX_CHARS = 200

# Slug cap (UTF-8 BYTES) and ASCII-only case folding — the exact pair
# :mod:`agora_kb.adapters.ollama_brain` uses, mirrored here so the import lane and the curator lane
# derive the same basename from the same text (ADR-0041 D4.4). See :func:`_slugify`.
_SLUG_MAX_BYTES = 60
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


@dataclass(frozen=True)
class NoteRecord:
    """Per-note outcome of the import (one record per source ``.md`` note; ADR-0014 D5).

    ``rel_path`` is the POSIX destination-relative path the normalized note was written to (which
    differs from its source path for every note now — schema 2 files by KIND, not by subject).
    ``type_inferred`` is the note's schema-2 ``kind`` (ADR-0041 D2.5: ``concept`` / ``map`` /
    ``index``), deduced from the SOURCE path and materialized as the destination directory; the
    field keeps its name because it keeps its meaning — "the structural class this importer decided"
    — and renaming it would break every caller that prints it. ``subjects`` is the D2.2 subject list
    the note was filed under: the origin folder when it maps to a declared taxonomy domain, and the
    honest empty tuple otherwise (nothing is dropped for lack of a domain, ADR-0022 leg 1/2).
    ``repaired_frontmatter`` is True
    iff the source frontmatter was invalid YAML and was rescued (Obsidian inline-wikilink repair, or
    treated-as-empty). ``inferred_fields`` lists the required/OKF frontmatter keys this importer
    SUPPLIED because the source omitted them (e.g. ``title``, ``created``, ``summary``).
    ``stripped_tags`` are source tags dropped because they are not in the destination taxonomy
    (ADR-0010 L1-5; the importer never silently widens the taxonomy). ``converted_links`` counts the
    body ``[[wikilink]]`` tokens rewritten to standard markdown links (ADR-0014 D3 form) and
    ``retargeted_links`` the PRE-EXISTING markdown links re-pointed at where their target actually
    landed under the kind-first layout (:func:`_retarget_body_md_links`).
    ``unresolved_links`` are body links left verbatim because their basename matched no note in
    the vault (tolerated, not dropped; ADR-0014 D4). ``warnings`` are the report-only items the
    operator/curator must resolve (a moved note, a theme with no sources, an unparseable block).

    The v2 grounding fields (ADR-0014 D5 v2 — what makes the imported repo LINT-CLEAN + curatable):
    ``synth_raw_source`` is the POSIX ``raw/<domain>/<slug>.md`` path the importer SYNTHESIZED as
    this theme's immutable import-snapshot source (decision (a): every claim traces to ``raw/``,
    ADR-0010 D3) — set only when the theme had a non-empty body and no pre-existing valid ``raw/``
    source; ``None`` otherwise. ``stubbed_empty_theme`` is True when an EMPTY-body theme was given
    ``status: stub`` (L1-7-exempt) instead of a synth source. ``stripped_sources`` are pre-existing
    ``sources:`` entries that were NOT ``raw/...`` paths and so were removed + recorded (change D —
    the synth ``raw/`` becomes the authoritative source, never a foreign ``~/dev/...`` locator).
    """

    rel_path: str
    type_inferred: str
    subjects: tuple[str, ...] = ()
    repaired_frontmatter: bool = False
    inferred_fields: tuple[str, ...] = ()
    stripped_tags: tuple[str, ...] = ()
    converted_links: int = 0
    retargeted_links: int = 0
    unresolved_links: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    synth_raw_source: str | None = None
    stubbed_empty_theme: bool = False
    stripped_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportReport:
    """The full outcome of an ``agora import`` run (ADR-0014 D5).

    ``notes`` is one :class:`NoteRecord` per imported source note, in destination-path order.
    ``summary`` is a counts mapping (e.g. ``notes``, ``repaired_frontmatter``, ``moved``,
    ``converted_links``, ``unresolved_links``, ``stripped_tags``, ``themes_without_sources`` plus
    the v2 grounding counts ``synth_raw_sources`` / ``stubbed_empty_themes`` / ``stripped_sources``
    / ``excluded_structural_files``) so a caller can print a one-line digest without re-walking
    ``notes``. ``warnings`` are run-level findings not tied to a single note (e.g. a non-``.md``
    file that was ignored is NOT a warning — it is silently skipped per D4 — but a structural
    surprise would land here). ``lint`` is the post-import
    :class:`~agora_kb.schema.lint.LintResult` over
    ``dest``: with the v2 grounding a real vault imports lint-clean, and the report carries the lint
    verbatim for the operator to act on (the honest "what still needs hands" surface, ADR-0014 D5).

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
    type_inferred: str = "concept"  # the schema-2 KIND (ADR-0041 D2.5)
    subject: str | None = None  # the single D2.2 subject, or None for `subjects: []`
    repaired_frontmatter: bool = False
    inferred_fields: list[str] = field(default_factory=list)
    stripped_tags: list[str] = field(default_factory=list)
    converted_links: int = 0
    retargeted_links: int = 0
    unresolved_links: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fm: dict[str, object] = field(default_factory=dict)
    body: str = ""
    synth_raw_source: str | None = None
    stubbed_empty_theme: bool = False
    stripped_sources: list[str] = field(default_factory=list)

    def finalize(self) -> NoteRecord:
        return NoteRecord(
            rel_path=self.rel_path,
            type_inferred=self.type_inferred,
            subjects=tuple([self.subject] if self.subject else []),
            repaired_frontmatter=self.repaired_frontmatter,
            inferred_fields=tuple(self.inferred_fields),
            stripped_tags=tuple(self.stripped_tags),
            converted_links=self.converted_links,
            retargeted_links=self.retargeted_links,
            # De-duplicated, order-preserving: the same missing basename is legitimately seen by
            # several passes (an fm array entry, a body link, the child bullet change C drops), and
            # a report that lists `ghost` three times says nothing the first one did not.
            unresolved_links=tuple(dict.fromkeys(self.unresolved_links)),
            warnings=tuple(self.warnings),
            synth_raw_source=self.synth_raw_source,
            stubbed_empty_theme=self.stubbed_empty_theme,
            stripped_sources=tuple(self.stripped_sources),
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
    """Unicode-preserving path-component slug, or ``""`` when nothing safe survives (ADR-0041 D4.4).

    An EXACT mirror of :func:`agora_kb.adapters.ollama_brain._slugify` — same
    :func:`agora_kb.core.pathsafe.safe_slug_component` call, same ASCII-only lowercasing, same
    60-byte cap, same leading-underscore strip and re-verify — so the import lane and the curator
    lane name the same note the same way. It is a mirror rather than an import because the two
    layers do not depend on each other (``ingest`` must not import ``adapters``), which is exactly
    why the shared rule lives in ``core.pathsafe`` and only the wrapper is duplicated.

    This closes a PRE-EXISTING divergence, not just a charset one: the v1 import slugger had **no
    length cap** (a long vault filename could exceed the filesystem's 255-byte NAME_MAX) and **no
    re-verification** of its own output, while the curator slugger had both. Both lanes now emit a
    component that is canonical by construction — and a purely Korean filename yields a Korean
    slug instead of falling through to :func:`_hash_basename`.
    """
    slug = safe_slug_component(str(text).translate(_ASCII_LOWER), max_bytes=_SLUG_MAX_BYTES)
    if slug.startswith("_"):
        slug = safe_slug_component(slug.lstrip("_"), max_bytes=_SLUG_MAX_BYTES)
    return slug


def _hash_basename(text: str) -> str:
    """Deterministic ``note-<sha8>`` basename for an un-slugifiable stem (#57, mirrored here).

    A purely non-ASCII filename (e.g. a Korean note title) slugifies to ``""``. The curator path
    already solves this — :func:`agora_kb.adapters.ollama_brain._hash_fallback_basename` names such
    a note ``note-`` + the first 8 hex chars of the canonical ``content_sha256`` — but the importer
    never got that fix and used a literal ``"note"``, so EVERY un-slugifiable note in a vault landed
    on the same destination and silently overwrote the previous one while the run still reported
    ``lint: clean`` (the losers no longer exist, so no duplicate basename remains for L1-1 to find).
    Keying on content keeps distinct notes distinct and identical notes genuinely deduplicated.
    """
    return f"note-{content_sha256(text)[:8]}"


def _path_subject(src_rel: str, domains: set[str]) -> str | None:
    """The D2.2 subject a SOURCE path implies: its first component that is a declared domain.

    ADR-0041 D2.2 leg 1 states the rule for this lane verbatim — the importer records "the origin
    folder as a ``subjects:`` entry when it maps to a declared domain and ``subjects: []``
    otherwise", which is "the same rule D6 step 2 gives the ``--from-kb`` lane, so the two importers
    agree by construction". Scanning every DIRECTORY component (never the filename) makes one rule
    cover both shapes at once: ``wiki/<domain>/themes/x.md`` and a plain Obsidian ``<domain>/x.md``
    both yield ``<domain>``, and a folder the taxonomy never declared yields ``None``.

    Returning ``None`` rather than ``domains[0]`` is the ADR-0022 retirement, not a regression: in
    schema 1 the catch-all existed because a note needed a PATH and the path needed a domain, so an
    unclassifiable note had to be given a possibly-false one. A schema-2 concept lands at
    ``wiki/concepts/<slug>.md`` whatever its subject, so nothing is dropped for lack of a domain and
    nothing has to assert one. The catch-all survives in exactly one place — the ``raw/<domain>/``
    shard key (leg 3, :func:`_synth_raw_source`).
    """
    for part in src_rel.split("/")[:-1]:
        if part in domains:
            return part
    return None


def _infer_layout(
    src_rel: str, *, domains: set[str], text: str
) -> tuple[str, str, str | None, str | None]:
    """Infer ``(kind, dest-rel, moved_from_or_None, subject_or_None)`` from a SOURCE-relative path.

    This is the axis flip (ADR-0041 D1): the destination's first path segment under ``wiki/`` is the
    note's KIND and the subject has left the path entirely for ``subjects:``. The ADR-0010 §1 source
    shapes are still READ — a vault exported from an Agora v1 repo, or organised like one, is the
    common case — and mapped through the frozen D2.5 table:

    * ``index.md`` (root)              → ``index``   at ``index.md`` (unchanged; D1.2);
    * ``wiki/<d>/<d>-moc.md``          → ``map``     at ``wiki/maps/<d>.md`` (the ``-moc`` suffix
      was the kind marker in the NAME and the kind is now the DIRECTORY — D6 rule 3);
    * ``wiki/<d>/themes/<slug>.md``    → ``concept`` at ``wiki/concepts/<slug>.md``.

    **Everything else — including a ``wiki/<d>/daily/`` note — becomes a ``concept``**, the
    catch-all durable page, with the original path returned as ``moved_from`` so the caller records
    a warning. A daily is deliberately NOT converted to a schema-2 ``note``: D2.6 makes a journal a
    CURATOR RUN artifact (one per ``run_date``, carrying that run's ``run_id``, sharded by its
    date), and a vault note has no run behind it — minting a ``run_id`` for it would put a
    fabricated back-link to a run that never happened into the permanent record. A real v1 journal
    reaches schema 2 through
    ``agora import --from-kb`` (:mod:`agora_kb.ingest.kb_convert`), which HAS the run to name.
    """
    parts = src_rel.split("/")
    subject = _path_subject(src_rel, domains)

    if src_rel == "index.md":
        return "index", "index.md", None, None

    if len(parts) >= 2 and parts[0] == "wiki":
        domain = parts[1]
        # wiki/<d>/<d>-moc.md -> wiki/maps/<d>.md  (D6 rule 3 / D5)
        if len(parts) == 3 and parts[2] == f"{domain}-moc.md":
            slug = _slugify(domain) or _hash_basename(text)
            return "map", f"wiki/{KIND_DIRECTORIES['map']}/{slug}.md", None, subject
        # wiki/<d>/themes/<slug>.md -> wiki/concepts/<slug>.md
        if len(parts) == 4 and parts[2] == "themes" and parts[3].endswith(".md"):
            slug = parts[3][: -len(".md")]
            return "concept", f"wiki/{KIND_DIRECTORIES['concept']}/{slug}.md", None, subject

    # Anything else: a concept, the catch-all durable page; record the original path.
    stem = parts[-1][: -len(".md")] if parts[-1].endswith(".md") else parts[-1]
    slug = _slugify(stem) or _hash_basename(text)
    return "concept", f"wiki/{KIND_DIRECTORIES['concept']}/{slug}.md", src_rel, subject


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


def _infer_required_frontmatter(build: _NoteBuild, import_date: str, *, kb_id: str) -> None:
    """Add MISSING ADR-0041 D2 required frontmatter to ``build.fm`` (recording each inferred key).

    ADR-0014 D5 auto-fix #2 + #3 + #4, re-expressed against the schema-2 common base over the
    (already kind-decided) note:

    * ``kind`` — the schema-2 kind inferred from the SOURCE layout and MIRRORED by the destination
      directory, which is authoritative where the two disagree (D2.1). ``type`` is emitted as the
      derived OKF mirror of it (OD-3), the way ``description`` already mirrors ``summary`` —
      nothing in Agora reads it under schema 2, so a free-form source ``type:`` is overwritten;
    * ``kb`` — the ``_meta/kb.yaml`` ULID minted for THIS destination (D1.5), stamped into every
      note so a note copied out of the repo still names its origin;
    * ``subjects`` — ``[<origin domain>]`` when the origin folder maps to a declared domain, else
      the honest ``[]`` (D2.2 / :func:`_path_subject`);
    * ``title`` — first H1 in the body, else the filename kebab→Title-Case;
    * ``created`` / ``updated`` — the existing valid ``YYYY-MM-DD`` value if present, else
      ``import_date``;
    * ``status`` — the existing value if it is a valid ADR-0010 status, else ``active``;
    * ``summary`` — the existing value, else the first body paragraph (one line, ≤~200 chars),
      else the title;
    * ``derived: false`` — D2.4 reserves ``true`` for a proposal/derivation plane; an imported note
      is a curated claim, not a computed one;
    * ``provenance`` — the D2.3 split, with BOTH lists empty. An import authenticates nobody and no
      agent declared anything, so two empty lists is the only honest value: ``writers`` is the
      TRUSTED list and inventing an entry there would forge the custody claim the split exists for;
    * the OKF fields (ADR-0014 D2): ``description`` mirrors ``summary``; ``timestamp`` ==
      ``<updated>T00:00:00Z`` (deterministic, no wall clock); ``okf_version: '0.1'`` on the
      bundle-root ``index.md`` ONLY.

    Every key this function SUPPLIES (because the source omitted it or carried an invalid value) is
    appended to ``build.inferred_fields`` so the report shows exactly what the importer filled in.
    """
    fm = build.fm

    # kind — materialized from the inferred layout (the D2.5 kind set is CLOSED and the directory is
    # authoritative). Recorded as inferred when the source declared no matching value.
    if fm.get("kind") != build.type_inferred:
        build.inferred_fields.append("kind")
    fm["kind"] = build.type_inferred
    fm["type"] = build.type_inferred  # the DERIVED OKF mirror (OD-3), never the kind authority
    fm["kb"] = kb_id
    build.inferred_fields.append("kb")
    fm["subjects"] = [build.subject] if build.subject else []
    build.inferred_fields.append("subjects")
    # `aliases:` completes the D2 common base. It is normalized rather than merely preserved: a
    # non-list (or a list carrying non-strings) would fail lint L1-4's "must be a list of strings",
    # and an ABSENT one would leave the one common-base key APPLY always emits missing — so an
    # imported note and a curated one would not read the same in a diff.
    if "aliases" not in fm:
        build.inferred_fields.append("aliases")
    fm["aliases"] = _str_items(fm.get("aliases"))

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

    # D2.4 / D2.3: the last two keys of the common base, materialized rather than inherited.
    fm["derived"] = fm.get("derived") is True
    fm["provenance"] = {"writers": [], "agents": []}

    # Kind-specific required keys (ADR-0041 D2). A map/index needs ``children:`` (L1-4); an absent
    # value defaults to ``[]``, consistent with the empty child-bullet set (L1-6). A non-list
    # existing value is normalized to ``[]`` by :func:`_normalize_fm_link_arrays`.
    if build.type_inferred in _MAP_KINDS and "children" not in fm:
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


def _retarget_body_md_links(
    build: _NoteBuild,
    *,
    src_root: Path,
    src_rel: str,
    src_to_dest: dict[str, str],
    basename_to_relpath: dict[str, str],
    renames: dict[str, str],
) -> None:
    """Re-point a PRE-EXISTING body markdown link at where its target actually landed (ADR-0014 D3).

    :func:`_convert_body_links` emits destination-correct paths for the ``[[wikilink]]`` tokens it
    converts. The links a vault already carries in the emitted form — ``[Alpha](themes/alpha.md)``,
    the shape every Agora-produced note and many hand-written vaults use — got no such treatment,
    and under schema 2 that is a dead link in every case: the note moves to ``wiki/concepts/`` and
    its map to ``wiki/maps/``, so a relative path written against the source tree resolves to
    nothing. The import then reported ``lint: clean`` and ``unresolved_links: 0`` over prose whose
    navigation no longer works, because L1-2/L1-6 key on BASENAMES and cannot see a rotten path.

    Resolution mirrors :func:`agora_kb.ingest.kb_convert._retarget` — the two lanes have to agree,
    since a vault and a v1 repo can hold the identical link:

    1. the LITERAL path, normalized against the linking note's own SOURCE directory (exact);
    2. failing that — and only when that path exists NOWHERE under ``src_root`` — the BASENAME
       through the D6 rule-3 rename map, since the basename is the identity (ADR-0010 D5).

    A target that DOES resolve in the source but is not an imported note (``raw/`` evidence, an
    excluded structural file such as ``AGENTS.md``, an attachment) is a deliberate reference to a
    NON-note and is left BYTE-IDENTICAL: re-pointing it at whatever note shares its filename stem
    would silently rewrite the operator's own text. A target that resolves nowhere at all is left
    verbatim too, and recorded in ``unresolved_links`` on the same no-loss terms as an unresolvable
    wikilink. Runs BEFORE the wikilink conversion, so the links that pass emits are never re-read.
    """
    from_dir = str(Path(build.rel_path).parent)
    src_dir = posixpath.dirname(src_rel)

    def repl(m: re.Match[str]) -> str:
        raw_target = m.group("target")
        cleaned = raw_target.strip()
        fragment = ""
        if "#" in cleaned:
            cleaned, _, rest = cleaned.partition("#")
            fragment = f"#{rest}"
        if not cleaned.endswith(".md"):
            return m.group(0)
        if cleaned.startswith("/") or "://" in cleaned or ":" in cleaned or "\\" in cleaned:
            return m.group(0)

        resolved = posixpath.normpath(posixpath.join(src_dir, cleaned))
        target = src_to_dest.get(resolved)
        if target is None:
            if resolved == ".." or resolved.startswith("../") or (src_root / resolved).exists():
                return m.group(0)  # a real NON-note file, or an escape: never a note edge
            base = _basename_from_child_path(cleaned)
            target = basename_to_relpath.get(renames.get(base, base))
            if target is None:
                build.unresolved_links.append(base)
                return m.group(0)
        rel_posix = Path(os.path.relpath(target, from_dir)).as_posix()
        if rel_posix == cleaned:
            return m.group(0)  # already correct (a same-directory link that did not move)
        build.retargeted_links += 1
        return f"[{m.group('label')}]({rel_posix}{fragment})"

    build.body = _BODY_MDLINK_RE.sub(repl, build.body)


def _convert_body_links(
    build: _NoteBuild, basename_to_relpath: dict[str, str], renames: dict[str, str]
) -> None:
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

    ``renames`` is the ADR-0041 D6 rule-3 map — the basenames the FLIP itself changed, today just
    ``<domain>-moc`` → ``<domain>`` (the ``-moc`` suffix was the kind marker in the name and the
    kind is now the directory). A vault written against schema 1 links maps as ``[[<domain>-moc]]``,
    so without this the flip would turn every one of those into an "unresolved link" report over
    notes that are plainly present. The display text keeps the token the AUTHOR wrote; only the
    target moves.
    """
    from_dir = str(Path(build.rel_path).parent)

    def repl(m: re.Match[str]) -> str:
        inner = m.group("inner")
        left, sep, right = inner.partition("|")
        key = left.strip(" \t\r\n\f\v")
        display = right.strip() if sep else key
        if not key:
            return m.group(0)  # an empty [[]] — leave verbatim, never a graph edge
        target = basename_to_relpath.get(renames.get(key, key))
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


def _normalize_fm_link_arrays(
    build: _NoteBuild, known_basenames: set[str], renames: dict[str, str]
) -> None:
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
                # ADR-0041 D6 rule 3: a `[[<domain>-moc]]` written against the v1 layout still names
                # a real note — the map — and is REWRITTEN to its flipped basename rather than
                # reported as unresolved.
                renamed = renames.get(base, base)
                if renamed in known_basenames:
                    kept.append(f"[[{renamed}]]")
                else:
                    build.unresolved_links.append(base)
        build.fm[key] = kept


# --- v2 grounding: raw/ provenance · MOC child sync · non-raw source strip (ADR-0014 D5) -------


_RAW_ROOT = "raw"


def _within(path: Path, root: Path) -> bool:
    """True iff ``path`` is ``root`` itself or lives beneath it (both already resolved).

    Same containment predicate the harvester uses for its connector globs
    (``harvester.connectors._within``) — kept local so ``ingest`` does not import ``harvester``.
    """
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _contained_raw_ref(
    entry: object, root: Path, *, raw_may_be_relocated: bool = False
) -> str | None:
    """Return ``entry`` iff it is a safe relative ``raw/...`` reference CONTAINED under ``root``.

    The single validator both ``sources:`` consumers share (issue #108): ``_strip_non_raw_sources``
    decides what may stay in frontmatter and ``_synth_raw_source`` decides what may be READ from the
    source vault and WRITTEN into the destination. ``entry.startswith("raw/")`` alone is NOT a
    containment check — ``raw/../../etc/x`` passes it, and ``dest / entry`` then lands outside
    ``dest``. An imported vault is UNTRUSTED input (that is the point of ``agora import``), so the
    check is two layers, on every platform (no ``os.name`` branch — a KB must behave identically
    wherever it is imported):

    1. **grammar** — a non-empty ``str`` with no NUL, no ``\\`` (a Windows separator that POSIX
       semantics would treat as a filename character), no ``:`` (drive letter / NTFS stream), not
       absolute, at least two components, first component exactly ``raw``, and no ``..`` part;
    2. **resolution** — ``(root / entry)`` resolved must live strictly beneath ``(root / "raw")``
       resolved, which catches an escape through a SYMLINK planted INSIDE ``raw/`` that the grammar
       cannot see.

    ``raw_may_be_relocated`` decides whether ``raw/`` ITSELF may be a symlink pointing out of
    ``root``, and the asymmetry is about WHO controls the tree:

    * the SOURCE vault is untrusted, so the strict default ALSO requires the resolved target to sit
      under ``root`` itself — otherwise a vault shipping ``raw/ -> /`` turns the provenance copy
      into an exfiltration read of an arbitrary host file;
    * the DESTINATION repo belongs to the operator, who may legitimately park ``raw/`` on another
      volume via a symlink. The caller passes ``raw_may_be_relocated=True`` there, because the
      contract a dest entry must satisfy is exactly "does not leave ``raw/``" — and the
      body-snapshot fallback in :func:`_synth_raw_source` writes ``dest / "raw/..."`` through that
      same link unconditionally. Demanding more of a CITED artifact than of the snapshot written
      beside it would drop valid provenance without preventing any write (issue #108 review).

    Returns the entry in its NORMALIZED POSIX spelling when safe (``raw/./x.md`` and ``./raw/x.md``
    → ``raw/x.md``; an already-canonical entry is returned byte-identical, so existing imports do
    not change), else ``None`` (callers warn + drop; never silently pass).
    """
    if not isinstance(entry, str) or not entry:
        return None
    if "\x00" in entry or "\\" in entry or ":" in entry:
        return None
    candidate = PurePosixPath(entry)
    if candidate.is_absolute():
        return None
    parts = candidate.parts
    if len(parts) < 2 or parts[0] != _RAW_ROOT or any(part == ".." for part in parts):
        return None
    try:
        base = root.resolve()
        raw_root = (root / _RAW_ROOT).resolve()
        target = (root / entry).resolve()
    except (OSError, ValueError):  # pragma: no cover - hostile byte sequences the OS rejects
        return None
    if target == raw_root or not _within(target, raw_root):
        return None
    if not raw_may_be_relocated and not _within(target, base):
        return None
    return candidate.as_posix()


def _strip_non_raw_sources(build: _NoteBuild, *, dest: Path) -> None:
    """Remove every pre-existing ``sources:`` entry that is not a ``raw/...`` path (change D).

    ADR-0014 D5 v2 change D: a real vault theme may cite a FOREIGN locator (e.g.
    ``'~/dev/analytics/psa @ 705f4a4 (2026-06-12)'``) that is not an immutable ``raw/`` artifact —
    L1-8 hard-rejects it ("sources path does not exist"). The Karpathy invariant is that every claim
    traces to ``raw/`` (ADR-0010 D3), so such an entry is STRIPPED + recorded (a warning); the synth
    ``raw/`` snapshot from change B then becomes the authoritative source. A non-list ``sources:``
    is normalized to ``[]`` (tolerant). A valid ``raw/<...>`` entry is KEPT (change B then skips the
    synth, deferring to the pre-existing artifact if it exists in the vault).

    "Valid" is :func:`_contained_raw_ref`, not a ``raw/`` prefix test: a traversal entry such as
    ``raw/../../etc/x`` LOOKS like a raw source but escapes ``dest/raw/`` (issue #108). It is
    dropped on the same no-loss terms as a foreign locator — recorded in ``stripped_sources`` with a
    warning, never silently kept. Conversely a merely UNNORMALIZED spelling of a real raw artifact
    (``./raw/x.md``) is no longer dropped as "not under raw/": it is kept in canonical POSIX form,
    which is what every downstream consumer (lint L1-8, the graph) expects to read.
    """
    raw = build.fm.get("sources")
    if not isinstance(raw, list):
        build.fm["sources"] = []
        return
    kept: list[str] = []
    for entry in raw:
        ref = _contained_raw_ref(entry, dest, raw_may_be_relocated=True)
        if ref is not None:
            kept.append(ref)
            continue
        build.stripped_sources.append(str(entry))
        if isinstance(entry, str) and entry.startswith(f"{_RAW_ROOT}/"):
            build.warnings.append(f"stripped unsafe source {entry!r} (escapes raw/)")
        else:
            build.warnings.append(f"stripped non-raw source {entry!r} (not under raw/)")
    build.fm["sources"] = kept


def _synth_raw_source(build: _NoteBuild, *, src: Path, dest: Path, shard: str) -> None:
    """Ground a CONCEPT in ``raw/`` provenance, or STUB an empty-body one (change B / decision (a)).

    ADR-0014 D5 v2 change B + ADR-0010 D3 (the Karpathy "every claim traces to ``raw/``" invariant):

    * a theme that ALREADY carries a valid ``raw/<...>`` source whose file EXISTS in the SOURCE
      vault is left untouched — the pre-existing immutable artifact is authoritative (change D kept
      the ref); its bytes are COPIED into ``dest`` so L1-8 ("sources path exists") passes;
    * a theme with a NON-EMPTY body gets its body written VERBATIM to
      ``raw/<domain>/<basename>.md`` (an immutable import snapshot; dirs created) and its
      ``sources:`` set to ``['raw/<domain>/<basename>.md']`` (POSIX). The basename (globally unique,
      ADR-0010 D5) is the slug. This satisfies L1-7 (non-empty sources) AND L1-8 (the path exists);
    * a theme with an EMPTY body instead gets ``status: stub`` (L1-7-exempt, ADR-0010 §3.4.1) + a
      recorded note — there is no content to snapshot, so forcing a source would be dishonest.

    Mirrors the curator's ``apply._materialize_raw_source``: the DETERMINISTIC engine (never a
    model) writes ``raw/`` from an immutable body, the path is POSIX, and the write is idempotent
    (a re-import over an existing snapshot rewrites the same bytes). Only ``concept`` notes are
    grounded (the claim-bearing kind L1-7 grades); a map/index has no ``sources:`` requirement.

    ``shard`` is the ``raw/<shard>/`` DIRECTORY the snapshot lands in, and it is the one place
    ADR-0022's ``domains[0]`` catch-all SURVIVES the flip (D2.2 leg 3): ``raw/`` never moves (D1.4),
    so a snapshot still needs a directory even when the note it grounds has no subject at all. The
    caller passes the note's own subject when it has one and the first declared domain when it does
    not — so the evidence tier is byte-for-byte the shape it was in schema 1, while the note's
    ``subjects:`` stays honestly empty.
    """
    if build.type_inferred != "concept":
        return

    slug = note_basename(Path(build.rel_path))

    # A valid pre-existing raw/ source whose file exists in the SOURCE vault wins — copy it into the
    # dest (non-destructively: src stays read-only) and defer to that immutable artifact.
    current = build.fm.get("sources")
    if isinstance(current, list):
        for entry in current:
            # BOTH ends of the copy are validated with the SAME helper (issue #108): the WRITE end
            # must stay under the dest's raw/ (wherever the operator relocated it — the fallback
            # snapshot below writes through that same link) and the READ end must stay under the
            # SOURCE vault's raw/ AND inside the vault, since an attacker-planted symlink would
            # otherwise turn the copy into an exfiltration read of an arbitrary host file. Failing
            # either, the entry is skipped and the theme falls back to the body snapshot below —
            # the normal-note result is unchanged.
            cited = _contained_raw_ref(entry, dest, raw_may_be_relocated=True)
            if cited is None:
                continue
            if _contained_raw_ref(cited, src) is None:
                build.warnings.append(
                    f"skipped raw/ source {cited!r} (escapes the source vault raw/)"
                )
                continue
            origin = src / cited
            if not origin.is_file():
                continue
            out = dest / cited
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(origin.read_bytes())
            return

    if build.body.strip():
        ref = f"raw/{shard}/{slug}.md" if shard else f"raw/{slug}.md"
        out = dest / ref
        out.parent.mkdir(parents=True, exist_ok=True)
        # Immutable import snapshot: the theme's (normalized) body verbatim. Deterministic — a pure
        # function of the source bytes + the injected import_date, no wall clock.
        out.write_text(build.body if build.body.endswith("\n") else build.body + "\n", "utf-8")
        build.fm["sources"] = [ref]
        build.synth_raw_source = ref
    else:
        # No body to snapshot: an honest stub (exempt from L1-7) rather than a fabricated source.
        build.fm["status"] = "stub"
        build.fm["sources"] = []
        build.stubbed_empty_theme = True
        build.warnings.append("imported empty concept -> stub")


def _sync_moc_children(build: _NoteBuild, admissible_basenames: set[str]) -> None:
    """Make a map/index's ``children:`` EXACTLY its ADMISSIBLE child-bullet set (change C / L1-6).

    ADR-0014 D5 v2 change C: L1-6 requires ``children:`` (resolved via ``wikilinks``) to equal the
    body child-bullet basename set (the FROZEN §3.2 grammar, :func:`child_bullets`, which ADR-0041
    D1.3 keeps byte-for-byte). The importer makes this hold BY CONSTRUCTION on the NORMALIZED body
    (after :func:`_convert_body_links` has rewritten resolvable ``[[wikilink]]`` bullets to the
    standard markdown-link form the grammar matches):

    1. compute ``child_bullets`` over the normalized body;
    2. DROP any indent-0 child-bullet line whose basename is not in ``admissible_basenames``
       (recording it in ``unresolved_links``) — so the body never carries a child bullet L1-2 or
       L1-24 would reject;
    3. set ``children:`` to the canonical ``[[basename]]`` strings of the ADMISSIBLE set, sorted.

    ``admissible_basenames`` is narrower than "every written note" under schema 2, and the narrowing
    is a RULE rather than tidiness: D1.3 admits only ``concept`` / ``summary`` / ``map`` as a map's
    children, and lint L1-24 enforces it. A bullet pointing at anything else is dropped on the same
    no-loss terms as an unresolvable one — recorded, never silently kept.

    Runs on ``map`` / ``index`` notes only. A non-child-bullet body link (inline prose, an indented
    sub-bullet) is untouched — only the L1-6 child set is synchronized.
    """
    if build.type_inferred not in _MAP_KINDS:
        return

    # The FROZEN §3.2 grammar (the SAME function L1-6 evaluates) yields the child-bullet basename
    # set on the normalized body — never reimplemented here, so importer and linter can't drift.
    all_children = child_bullets(build.body)
    resolvable = {b for b in all_children if b in admissible_basenames}
    unresolvable = all_children - resolvable

    if unresolvable:
        # Drop each unresolvable indent-0 child-bullet LINE (it would fail L1-2 + break L1-6) and
        # record its basename; a resolvable bullet and any non-child-bullet line are kept verbatim.
        kept_lines: list[str] = []
        for line in build.body.split("\n"):
            m = _CHILD_BULLET_LINE_RE.match(line)
            if m is not None:
                base = _basename_from_child_path(m.group("path"))
                if base in unresolvable:
                    build.unresolved_links.append(base)
                    continue
            kept_lines.append(line)
        build.body = "\n".join(kept_lines)

    build.fm["children"] = [f"[[{b}]]" for b in sorted(resolvable)]


def _child_bullet(*, basename: str, target: str, label: str, from_dir: str) -> str:
    """Render ONE §3.2 child bullet that is GUARANTEED to parse back to ``basename``.

    The label a navigation bullet wants is the child's human ``title:`` — an operator-authored
    string that has been through no slugger and may hold any character at all. The §3.2 child-bullet
    grammar is FROZEN (``_CHILD_BULLET_LINE_RE``, byte-identical to the one L1-6 evaluates) and its
    link text is ``[^\\]\\r\\n]*``, so a title containing ``]`` produces a line the grammar either
    cannot match, or — worse — matches with the WRONG path: for ``title: 'a](x.md) b'`` the regex
    stops at the inner ``]``, reads ``x.md`` as the target and swallows the real link into the
    optional trailing-prose group. Either way the emitted bullet set stops equalling ``children:``,
    and the importer reports L1-6 findings in the repo it just minted — against its own contract
    that set equality holds BY CONSTRUCTION.

    So the grammar is ASSERTED rather than assumed: each candidate label is rendered, matched, and
    the parsed path resolved back to a basename; the first candidate that round-trips to
    ``basename`` wins. The title is tried first (it is what a human wants to read); the child's own
    BASENAME is the fallback, and it is a total one — a basename is a ``core.pathsafe`` component,
    whose closed category allowlist (Unicode ``L``/``N``/``M`` plus ``-_.``) contains neither ``]``
    nor ``)`` nor a newline, and the target path is composed of those same components. A degraded
    label costs a bullet its prose title; a broken one costs the repo its lint.
    """
    rel = Path(os.path.relpath(target, from_dir)).as_posix()
    for candidate in (label, basename):
        line = f"- [{candidate}]({rel})"
        m = _CHILD_BULLET_LINE_RE.match(line)
        if m is not None and _basename_from_child_path(m.group("path")) == basename:
            return line
    # Unreachable while the target path is pathsafe (see above); emitting the basename form is
    # still the closest thing to a correct bullet, and the destination lint grades the result.
    return f"- [{basename}]({rel})"


def _navigation_note(
    *,
    rel_path: str,
    kind: str,
    title: str,
    summary: str,
    subject: str | None,
    children: list[tuple[str, str, str]],
    import_date: str,
    kb_id: str,
) -> tuple[str, str]:
    """Render one SYNTHESIZED navigation note — a domain map, or the root map (ADR-0041 D1.2/D1.3).

    Returns ``(rel_path, rendered_text)``.

    ``children`` is ``(basename, dest_rel, label)`` per child, in the order the bullets are emitted.
    Both sides of L1-6 are built from that ONE list — the ``children:`` frontmatter array and the
    body child bullets in the FROZEN §3.2 grammar — so set equality holds by construction rather
    than by a later sync, and every bullet's target is a real relative path a human can click. The
    bullets go through :func:`_child_bullet`, which is what makes "by construction" true for a child
    whose ``title:`` holds a ``]``: the rendered line is matched against the frozen grammar and must
    parse back to the child's basename, or the basename is used as the label instead.

    The frontmatter is the D2 common base in the ADR's own key order (the same order
    ``curator.apply._common_frontmatter`` emits), so a synthesized map and a map APPLY later
    re-renders read identically in a diff.
    """
    from_dir = str(Path(rel_path).parent)
    bullets = [
        _child_bullet(basename=basename, target=target, label=label, from_dir=from_dir)
        for basename, target, label in children
    ]
    body = f"# {title}\n"
    if bullets:
        body += "\n" + "\n".join(bullets) + "\n"
    fm: dict[str, object] = {
        "title": title,
        "kind": kind,
        "type": kind,
        "kb": kb_id,
    }
    if kind == "index":
        fm["okf_version"] = _OKF_VERSION
    fm["subjects"] = [subject] if subject else []
    fm["aliases"] = []
    fm["tags"] = []
    fm["created"] = import_date
    fm["updated"] = import_date
    fm["timestamp"] = f"{import_date}T00:00:00Z"
    fm["status"] = "active"
    fm["summary"] = summary
    fm["description"] = summary
    fm["derived"] = False
    fm["provenance"] = {"writers": [], "agents": []}
    fm["children"] = [f"[[{basename}]]" for basename, _target, _label in children]
    return rel_path, frontmatter.render(fm, body)


def _build_navigation(
    builds: list[_NoteBuild],
    *,
    domains: list[str],
    import_date: str,
    kb_id: str,
) -> tuple[list[tuple[str, str]], int, int]:
    """Complete the schema-2 NAVIGATION tier (ADR-0041 D1.2/D1.3/D5).

    Returns ``(files, synthesized_maps, index_synthesized)``.

    Two obligations schema 2 puts on an importer that schema 1 did not, both structural rather than
    decorative:

    * **a map per subject.** ADR-0041 D5 seeds the ranker's whole structural term from
      ``wiki/maps/**`` (``core.wiki._is_map_path``), so a repo with no maps ranks on lexical
      evidence alone and every concept in it is an orphan for the curator's own ``max_orphans``
      gate (ADR-0022 §E). A ``wiki/maps/<domain>.md`` is therefore synthesized for every DECLARED
      domain that has concepts, with ``children:`` == those concepts. An IMPORTED map for that
      domain WINS and is left exactly as the operator wrote it (its children were synced from its
      own bullets by change C) — the importer completes the tier, it never overrules it.
    * **the root map.** ``index.md`` is the ROOT of the map tier (D1.2), so its ``children:`` is
      every map. An imported index keeps its own prose and its own bullets and is EXTENDED with the
      maps it does not already list; absent one, it is synthesized whole.

    A domain whose map basename is already taken by an imported note is SKIPPED with a warning on
    that note rather than renamed: a renamed map is a map nothing links to, and D6 rule 7's reason
    for refusing silent renames applies just as much here.

    Both sides of L1-6 come from ONE list per note, so ``children:`` and the body child bullets are
    equal by construction; every child is a ``concept`` or a ``map``, which is exactly the D1.3
    admitted set (L1-24).
    """
    by_dest = {build.rel_path: build for build in builds}
    taken = {note_basename(Path(build.rel_path)): build for build in builds}

    concepts_by_domain: dict[str, list[_NoteBuild]] = {}
    for build in builds:
        if build.type_inferred == "concept" and build.subject:
            concepts_by_domain.setdefault(build.subject, []).append(build)

    files: list[tuple[str, str]] = []
    synthesized_maps = 0
    # (basename, dest_rel, label) per map the destination ends up with — imported or synthesized
    maps: list[tuple[str, str, str]] = [
        (note_basename(Path(build.rel_path)), build.rel_path, _title_of(build))
        for build in builds
        if build.type_inferred == "map"
    ]

    for domain in domains:
        concepts = concepts_by_domain.get(domain)
        if not concepts:
            continue
        map_rel = f"wiki/{KIND_DIRECTORIES['map']}/{domain}.md"
        if map_rel in by_dest:
            continue  # the operator's own map for this domain — never overruled
        clash = taken.get(domain)
        if clash is not None:
            clash.warnings.append(
                f"basename {domain!r} is this note's, so no wiki/maps/{domain}.md was synthesized "
                f"for the {domain!r} subject; its concepts have no map (ADR-0041 D1.3)"
            )
            continue
        children = sorted(
            ((note_basename(Path(c.rel_path)), c.rel_path, _title_of(c)) for c in concepts),
            key=lambda entry: entry[0],
        )
        files.append(
            _navigation_note(
                rel_path=map_rel,
                kind="map",
                title=f"{domain} map",
                summary=f"Map of content for the {domain} subject.",
                subject=domain,
                children=children,
                import_date=import_date,
                kb_id=kb_id,
            )
        )
        maps.append((domain, map_rel, f"{domain} map"))
        synthesized_maps += 1

    maps.sort(key=lambda entry: entry[0])

    index_build = by_dest.get("index.md")
    if index_build is None:
        files.append(
            _navigation_note(
                rel_path="index.md",
                kind="index",
                title="Knowledge base",
                summary="Root map of every subject in this knowledge base.",
                subject=None,
                children=maps,
                import_date=import_date,
                kb_id=kb_id,
            )
        )
        return files, synthesized_maps, 1

    # An IMPORTED index: keep its frontmatter, prose and existing bullets; append only the maps it
    # does not already list, so the operator's own ordering and labels survive the import.
    #
    # ``children:`` is EXTENDED, never replaced. Change C has already synced it to the admissible
    # child-bullet set of this note's own body (a CONCEPT is an admitted D1.3 child of the index,
    # not only a map), so overwriting it with the map list alone would delete a child whose bullet
    # is still in the body — and L1-6, which grades ``children:`` against exactly that bullet set,
    # would reject the repo the importer just announced as clean. Both sides stay derived from ONE
    # list: the bullets already there, plus the ones appended just below, in that order.
    declared_order = list(
        dict.fromkeys(wikilinks(" ".join(_str_items(index_build.fm.get("children")))))
    )
    declared = set(declared_order)
    missing = [entry for entry in maps if entry[0] not in declared]
    if missing:
        from_dir = str(Path(index_build.rel_path).parent)
        bullets = [
            _child_bullet(basename=basename, target=target, label=label, from_dir=from_dir)
            for basename, target, label in missing
        ]
        body = index_build.body.rstrip("\n")
        index_build.body = f"{body}\n\n" + "\n".join(bullets) if body else "\n".join(bullets)
        index_build.fm["children"] = [
            f"[[{basename}]]" for basename in declared_order + [entry[0] for entry in missing]
        ]
    return files, synthesized_maps, 0


def _str_items(value: object) -> list[str]:
    """The string members of a frontmatter list value, else ``[]`` (tolerant accessor)."""
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _title_of(build: _NoteBuild) -> str:
    """The human label a navigation bullet uses for ``build`` (its resolved ``title:``)."""
    title = build.fm.get("title")
    return title.strip() if isinstance(title, str) and title.strip() else build.rel_path


def _basename_from_child_path(path: str) -> str:
    """Return the child basename from a markdown-link target path (mirrors notes._basename..).

    The basename is the link-target filename minus directory + ``.md`` (the internal globally-unique
    identity, ADR-0010 D5) — kept LOCAL (a one-liner) to avoid importing a private name from
    ``schema.notes``; it must agree with that module's ``_basename_from_link_path`` byte-for-byte so
    the line this importer strips matches exactly what ``child_bullets`` counts.
    """
    last = path.strip(" \t\r\n\f\v").rsplit("/", 1)[-1]
    return last[: -len(".md")] if last.endswith(".md") else last


# --- the public entry point --------------------------------------------------------------------


def _assert_importable_destination(dest: Path) -> None:
    """Refuse an import into ANY destination that is already a knowledge base.

    The vault normalizer is a DIRECT ``wiki/`` writer — it composes paths itself and never goes
    through ``Inbox.write`` — so it inherits none of the ADR-0041 D6 write refusal and needs its own
    boundary guard. It writes a **new** repo, and the guard is what makes that word true: only a
    FRESH destination (nothing declared) passes.

    **A schema-1 destination** would get ``wiki/concepts/…`` committed beside
    ``wiki/<domain>/themes/…`` — two layouts in one repo, verbatim the state D6's refusal exists to
    prevent — and the importer would not even SEE it, because it lints its own output with its own
    ``Taxonomy``. (That was reproduced live before the guard existed: exit 0, a committed
    mixed-layout tree, and two misleading ``L1-17`` header findings.) A schema-1 repo crosses to
    schema 2 exactly once, through ``agora import --from-kb``
    (:func:`agora_kb.ingest.kb_convert.convert_kb`), which is the only crossing D6 authorises.

    **A schema-2 destination is refused too, and that half is not symmetry for its own sake.** The
    importer is not an incremental writer: it mints ``_meta/kb.yaml`` and it composes the root map
    ``index.md`` from THIS vault's maps alone. Run against a populated schema-2 repo it therefore
    re-stamps the KB with a NEW ``kb_id`` while the notes already there keep the old one (ADR-0041
    D1.5 says a ``kb_id`` is minted once and never rewritten), drops the advisory ``declared_kind``,
    and rewrites ``children:`` and the map bullets of a root map it did not build — reported, in
    the live repro of this, as ``lint: clean``. Adding to an existing KB is what the INBOX is for
    (``kb_remember`` / the web upload / ``agora curate``); the importer creates one.
    """
    from agora_kb.config import ConfigError, load_kb_identity, read_canonical_kb_schema_version

    layout = RepoLayout(Path(dest))
    try:
        declared = read_canonical_kb_schema_version(layout)
    except ConfigError:
        return  # an unreadable/absent taxonomy is not this guard's failure to report
    if declared is not None and declared != IMPORTER_SCHEMA_VERSION:
        raise ValueError(
            f"{layout.root} is a schema-{declared} KB and 'agora import' writes the "
            f"schema-{IMPORTER_SCHEMA_VERSION} layout — importing would leave TWO layouts in one "
            f"repo. Import into a NEW directory instead, and convert the existing repo once with "
            f"'agora import --from-kb {layout.root} <new-repo>' (ADR-0041 D6)"
        )
    if declared is not None:
        raise ValueError(
            f"{layout.root} is already a schema-{declared} KB and 'agora import' writes a NEW one "
            f"— it would re-mint _meta/kb.yaml with a fresh kb_id the notes already there do not "
            f"carry (ADR-0041 D1.5: a kb_id is stamped once and never rewritten) and rebuild the "
            f"root map index.md from this vault alone. Import into a NEW directory; to ADD to an "
            f"existing KB, capture through the inbox ('kb_remember' / the web upload) and run "
            f"'agora curate'"
        )
    # Defence in depth for a HALF-initialized destination: `_meta/kb.yaml` present with no
    # `_meta/taxonomy.yaml` declares no schema, so the check above cannot see it, and minting over
    # it would still rewrite an identity D1.5 says is written once.
    try:
        identity = load_kb_identity(layout)
    except ConfigError:
        return
    if identity is not None:
        raise ValueError(
            f"{layout.root} already declares a KB identity (_meta/kb.yaml, kb_id "
            f"{identity.kb_id}) and 'agora import' writes a NEW repo — a kb_id is stamped once and "
            f"never rewritten (ADR-0041 D1.5). Import into a NEW directory"
        )


def import_vault(
    src: Path,
    dest: Path,
    *,
    domains: list[str],
    import_date: str,
    tags: list[str] | None = None,
    kb_id: str | None = None,
    name: str | None = None,
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
    2. note ``kind`` + layout inference from the SOURCE-relative path (:func:`_infer_layout`); a
       non-conforming path becomes a ``concept`` (the catch-all durable page) and is reported;
    3. missing required ADR-0041 D2 frontmatter SUPPLIED (``kind``/``kb``/``subjects``/``title``/
       ``created``/``updated``/``status``/``summary``/``derived``/``provenance``) + 4. the OKF
       fields (``description``/``timestamp``/``okf_version`` on index)
       (:func:`_infer_required_frontmatter`);
    5. pre-existing body markdown links RE-TARGETED at where their note actually landed, since
       schema 2 moves every note (:func:`_retarget_body_md_links`), then body ``[[wikilink]]`` →
       standard markdown link when the basename resolves (:func:`_convert_body_links`); an
       unresolvable link is left verbatim and reported;
    6. tags outside the destination taxonomy are STRIPPED + recorded (:func:`_filter_tags`).

    v2 GROUNDING auto-fixes — what makes a real vault like ``~/knowledge`` import LINT-CLEAN and
    curate-able (ADR-0014 D5 v2), applied per note after the link conversion:

    A. STRUCTURAL / non-knowledge files are EXCLUDED from import-as-notes in pass 0 — the schema doc
       + its symlinks + ``log.md`` (:data:`_EXEMPT_STEMS`), the ``_templates/`` / ``_meta/`` /
       ``_kb/`` subtrees (:data:`_STRUCTURAL_DIRS`), and any symlink — so they never become junk
       themes (the dest emits its OWN schema);
    D. pre-existing non-``raw/`` ``sources:`` entries are STRIPPED + recorded
       (:func:`_strip_non_raw_sources`) so an L1-8 foreign locator (``~/dev/...``) is removed;
    B. each THEME is grounded in ``raw/`` provenance (:func:`_synth_raw_source`, decision (a)): a
       non-empty body is snapshotted VERBATIM to ``raw/<domain>/<slug>.md`` and cited as the theme's
       ``sources:`` (satisfies L1-7 + L1-8 + the Karpathy "every claim traces to ``raw/``"
       invariant, ADR-0010 D3); an EMPTY-body theme becomes ``status: stub`` (L1-7-exempt) instead;
    C. each ``map`` / ``index`` has its ``children:`` SYNCED to exactly the ADMISSIBLE child-bullet
       basename set of its normalized body (:func:`_sync_moc_children`), dropping unresolvable and
       inadmissible child bullets — so L1-6 (``children:``-set == child-bullet-set) and L1-24 (the
       D1.3 admitted child kinds) both hold by construction;
    E. the NAVIGATION TIER is completed (:func:`_navigation_note`): a ``wiki/maps/<domain>.md`` is
       SYNTHESIZED for every declared domain that has concepts and does not already have an imported
       map, and the root ``index.md`` is made the ROOT MAP over every map. This is new in schema 2
       and it is not cosmetic: the ranker seeds its whole structural term from ``wiki/maps/**``
       (ADR-0041 D5), so an imported repo with no maps would rank on lexical evidence alone, and
       every concept in it would be an orphan for the curator's own ``max_orphans`` gate.

    REPORT-ONLY (recorded as warnings for the operator/curator, not blocking): a note that was MOVED
    to fit the layout; stripped unknown tags; stripped non-raw sources; unresolved links; an empty
    theme stubbed; a note whose frontmatter could not be parsed at all. After writing ``dest`` the
    schema + taxonomy are emitted, the repo is git-initialized, and
    :func:`agora_kb.schema.lint.lint` is run — its result is ATTACHED to the report. With the v2
    grounding, a real vault imports to a LINT-CLEAN (``lint(dest).ok``) repo; the attached lint
    remains the honest surface for any residue.

    Raises ``FileNotFoundError`` / ``NotADirectoryError`` if ``src`` is missing or not a directory
    (a hard error). ``domains`` must be non-empty (the first is the ``raw/`` shard-key floor, D2.2
    leg 3). ``kb_id`` overrides the minted ``_meta/kb.yaml`` ULID (a test pins it; production mints
    one) and ``name`` its display name, defaulting to the destination directory name.
    """
    src = Path(src)
    dest = Path(dest)
    if not src.exists():
        raise FileNotFoundError(f"source vault does not exist: {src}")
    if not src.is_dir():
        raise NotADirectoryError(f"source vault is not a directory: {src}")
    if not domains:
        raise ValueError("import_vault requires at least one domain (the raw/ shard-key floor)")
    _assert_importable_destination(dest)

    first_domain = domains[0]
    declared_domains = set(domains)
    src = src.resolve()

    # --- pass 0: discover every source .md note (tolerant: ignore non-.md and hidden dirs) -------
    # ADR-0014 D4: non-``.md`` files (``.canvas``, images) and the ``.obsidian/`` config dir are
    # IGNORED, not errors. Hidden directories (leading ``.``) and the dest (if nested) are skipped.
    #
    # v2 change A (ADR-0014 D5): STRUCTURAL / non-knowledge files are also skipped — the DEST emits
    # its OWN schema doc + symlinks + log + templates, so importing them as notes only yields junk
    # themes (the v1 ``agents``/``claude``/``qwen``/``gemini``/``schema``/``log``/``theme`` themes).
    # Skipped: an exempt STEM (the schema doc + its symlinks + ``log.md``, :data:`_EXEMPT_STEMS`),
    # anything under a ``_templates/`` / ``_meta/`` / ``_kb/`` dir at any depth
    # (:data:`_STRUCTURAL_DIRS`), and any SYMLINK (never import a symlink as a note). These are
    # counted (``excluded_structural_files``) but otherwise simply not imported.
    src_md: list[Path] = []
    excluded_structural = 0
    for p in sorted(src.rglob("*.md")):
        rel_parts = p.relative_to(src).parts
        if any(part.startswith(".") for part in rel_parts):
            continue  # skip .obsidian/, .git/, dotfiles
        if p.is_symlink():
            excluded_structural += 1  # a symlink is never a note (e.g. CLAUDE.md -> AGENTS.md)
            continue
        if not p.is_file():
            continue
        if note_basename(p) in _EXEMPT_STEMS:
            excluded_structural += 1  # the schema doc / its symlinks / the run log
            continue
        if any(part in _STRUCTURAL_DIRS for part in rel_parts[:-1]):
            excluded_structural += 1  # _templates/ · _meta/ · _kb/ subtree (at any depth)
            continue
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

        # Infer the KIND + destination path + subject from the SOURCE-relative path (ADR-0041 D1).
        kind, dest_rel, moved_from, subject = _infer_layout(
            src_rel, domains=declared_domains, text=body
        )
        build.type_inferred = kind
        build.subject = subject
        build.rel_path = dest_rel
        if moved_from is not None:
            build.warnings.append(
                f"moved to fit the ADR-0041 kind-first layout (was {moved_from!r}; "
                f"no {kind} path matched)"
            )
        builds.append(build)

    # NO-LOSS: two sources may still infer the SAME destination — distinct stems that slugify alike
    # (``projects/Setup.md`` + ``archive/setup.md`` → ``setup``), or two notes whose paths were both
    # rewritten by the catch-all above. The writer would overwrite, and because the loser then does
    # not exist there is no duplicate basename left for lint L1-1 to report, so the import announced
    # ``lint: clean`` over notes it had just destroyed. Disambiguate deterministically with the same
    # content-keyed suffix, in a stable order, and say so in the per-note warnings.
    #
    # The claim is keyed on the destination BASENAME rather than on the destination PATH, and under
    # schema 2 that is the only key that works: the kind is now the directory, so two notes can land
    # on DIFFERENT paths (``wiki/concepts/general.md`` and ``wiki/maps/general.md``) while sharing
    # one basename — which is a hard L1-1 duplicate, because the basename is the global identity
    # every ``[[basename]]`` resolves on (ADR-0010 D5). The path-keyed check would have passed that
    # pair straight through to a lint failure at the end of a completed import.
    #
    # ``index`` is RESERVED, and it is the one basename the loop cannot defend by itself: the root
    # map may not be an imported note at all — change E synthesizes one when the vault has none, and
    # ``Repo.init`` seeds one after that — so a source note that lands elsewhere basenamed ``index``
    # (an Obsidian ``general/index.md`` folder note becomes ``wiki/concepts/index.md``) collides
    # with a note that does not exist yet. That is exactly the path-key-vs-basename-key blind spot
    # this claim pass was rewritten to close, one branch further along. Reserving it up front keeps
    # the rule identical for both: the ROOT index keeps the name, every other claimant is renamed by
    # the same content-keyed suffix, and lint L1-13 ("only the root note is basenamed index") holds.
    reserved: set[str] = set()
    if not any(build.rel_path == "index.md" for build in builds):
        reserved.add("index")
    claimed: dict[str, _NoteBuild] = {}
    for build in sorted(builds, key=lambda b: b.rel_path):
        basename = note_basename(Path(build.rel_path))
        first = claimed.get(basename)
        if first is None and basename not in reserved:
            claimed[basename] = build
            continue
        collided = build.rel_path
        parent = PurePosixPath(collided).parent
        build.rel_path = f"{parent}/{basename}-{content_sha256(build.body)[:8]}.md"
        renamed_to = PurePosixPath(build.rel_path).name
        claim = (
            f"was already claimed by {first.src_path.name!r}"
            if first is not None
            else "is RESERVED for the ROOT map at index.md (ADR-0041 D1.2, lint L1-13)"
        )
        build.warnings.append(
            f"basename {basename!r} {claim}; renamed to {renamed_to!r} so neither note is lost"
        )
        claimed[note_basename(Path(build.rel_path))] = build

    # ADR-0041 D6 rule 3, applied to THIS lane: a vault written against schema 1 links its maps as
    # ``[[<domain>-moc]]``, and the flip renames that note to ``<domain>``. The rename map is built
    # AFTER the disambiguation above so it names where the map ACTUALLY landed, and it is confined
    # to the map tier: a concept whose slug merely differs from its source stem is NOT aliased,
    # because that is normalization, not a rename the flip performed, and aliasing it would start
    # resolving links the schema-1 importer honestly reported as unresolved.
    renames: dict[str, str] = {}
    for build in builds:
        if build.type_inferred != "map":
            continue
        source_stem = note_basename(build.src_path)
        dest_stem = note_basename(Path(build.rel_path))
        if source_stem != dest_stem:
            renames[source_stem] = dest_stem

    # Build the basename→dest-relpath map over ALL notes (basenames are now unique by construction),
    # and beside it the SOURCE-relpath→dest-relpath map the body markdown-link retarget resolves on
    # first (the exact answer; the basename map is its fallback).
    src_to_dest: dict[str, str] = {}
    for build in builds:
        basename_to_relpath[note_basename(Path(build.rel_path))] = build.rel_path
        src_to_dest[build.src_path.relative_to(src).as_posix()] = build.rel_path
    known_basenames = set(basename_to_relpath)
    # L1-24: only these kinds may appear in a map's ``children:`` (ADR-0041 D1.3).
    admissible_children = {
        note_basename(Path(b.rel_path)) for b in builds if b.type_inferred in _ADMITTED_CHILD_KINDS
    }

    # --- the closed destination taxonomy (operator-declared; never widened) ----------------------
    # ADR-0010 D6 / ADR-0014 D5: ``allowed_tags`` is exactly what the operator passed via ``--tag``;
    # the importer keeps a source tag only when it is already in this set and strips the rest.
    taxonomy = Taxonomy(
        schema_version=IMPORTER_SCHEMA_VERSION,
        taxonomy_policy="open",
        allowed_tags=tuple(tags or ()),
        domains=tuple(domains),
    )
    allowed_tags = set(taxonomy.allowed_tags)
    layout = RepoLayout(dest)
    dest.mkdir(parents=True, exist_ok=True)

    # ADR-0041 D1.5: the KB identity is minted ONCE, here, and every note mirrors it into ``kb:``.
    # It is written BEFORE the notes so a run that fails part-way leaves an identity-bearing repo
    # rather than notes stamped with an id no file declares. The mint is UNCONDITIONAL because
    # `_assert_importable_destination` above has already proved there is no identity here to
    # overwrite — that guard, not a second absence check, is what keeps D1.5's "stamped once and
    # never rewritten" true for this lane.
    from agora_kb.config import KbIdentity, write_kb_identity
    from agora_kb.core.ids import new_ulid

    kb_identity = KbIdentity(kb_id=kb_id or new_ulid(), name=name or dest.name or "knowledge")
    write_kb_identity(layout, kb_identity)

    # --- pass 2: normalize frontmatter + links for every note ------------------------------------
    # Order matters for the v2 grounding (ADR-0014 D5): D (strip foreign sources) THEN B (synth the
    # authoritative raw/ snapshot or stub) THEN C (sync MOC children on the NORMALIZED body, after
    # link conversion has rewritten resolvable [[wikilink]] bullets to the markdown-link form the
    # frozen child-bullet grammar matches). ``known_basenames`` is exactly the WRITTEN-note set
    # (structural files were excluded in pass 0), so it is the resolvable target set for change C.
    for build in builds:
        _infer_required_frontmatter(build, import_date, kb_id=kb_identity.kb_id)
        _filter_tags(build, allowed_tags)
        _normalize_fm_link_arrays(build, known_basenames, renames)
        # Retarget BEFORE the wikilink conversion: the links that pass emits are already
        # destination-relative, and re-reading them against the SOURCE tree would break them.
        _retarget_body_md_links(
            build,
            src_root=src,
            src_rel=build.src_path.relative_to(src).as_posix(),
            src_to_dest=src_to_dest,
            basename_to_relpath=basename_to_relpath,
            renames=renames,
        )
        _convert_body_links(build, basename_to_relpath, renames)
        _strip_non_raw_sources(build, dest=dest)  # change D — drop non-raw//escaping sources
        # change B — synth raw/ provenance or stub. The shard key is the note's own subject and
        # falls back to `domains[0]`: ADR-0022's catch-all survives in the raw/ tier and ONLY there
        # (D2.2 leg 3), because raw/ never moves and a snapshot still needs a directory.
        _synth_raw_source(build, src=src, dest=dest, shard=build.subject or first_domain)
        _sync_moc_children(build, admissible_children)  # change C — children == admissible set
        if build.type_inferred == "concept" and build.fm.get("status") != "stub":
            # After change B every non-stub concept has a synth (or kept) raw/ source; a residual
            # empty-sources concept (none — defensive) is still reported for the operator (L1-7).
            if not _has_sources(build.fm):
                build.warnings.append(
                    "concept has no raw/ sources (L1-7): needs sources or status: stub"
                )

    # --- change E: complete the NAVIGATION tier (ADR-0041 D1.2/D1.3) -----------------------------
    navigation, synthesized_maps, synthesized_index = _build_navigation(
        builds,
        domains=domains,
        import_date=import_date,
        kb_id=kb_identity.kb_id,
    )

    # --- write the normalized notes under dest ---------------------------------------------------
    # Change B already wrote the immutable ``raw/<shard>/<slug>.md`` snapshots; here the normalized
    # wiki notes themselves are emitted (raw/ is outside the wiki tree and never overwritten here).
    for build in builds:
        out_path = dest / build.rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(frontmatter.render(_ordered_fm(build.fm), build.body), encoding="utf-8")
    for rel_path, text in navigation:
        out_path = dest / rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")

    # The kind CONTAINERS are the schema; their population is not. ``wiki/summaries/`` and
    # ``wiki/entities/`` ship EMPTY (ADR-0041 OD-7/OD-8) and ``wiki/people/`` is human-owned, so the
    # importer creates the directories and writes into none of them. This is ``agora repo init``'s
    # OWN helper, deliberately — not a local ``mkdir``: each container gets a ``.gitkeep``, because
    # git cannot track an empty directory and the admin commit below would otherwise drop every
    # unpopulated container, leaving an imported repo a DIFFERENT tree from an init'd one at the
    # same schema — and the containers would vanish on ``agora sync`` + clone.
    materialize_kind_directories(layout)

    # --- emit schema + taxonomy, git-init, then lint ---------------------------------------------
    # The injected ``import_date`` pins the init/admin commit date so the repo is byte-reproducible
    # (no wall clock; ADR-0010 D1 discipline). ``Repo.init`` seeds its own ``index.md`` only when
    # one is absent — and change E always writes one, so the seed never fires and the ROOT MAP the
    # importer built (children == every map) is what the repo keeps. The import's notes are
    # committed as one admin commit; an empty diff is a no-op, not an error.
    emit_schema(layout, taxonomy=taxonomy)
    repo = Repo(layout)
    when = datetime.fromisoformat(f"{import_date}T00:00:00+00:00").astimezone(UTC)
    # EXPLICIT, never the default: `Repo.init`'s `schema_version` default is a library choice
    # that may move, and the importer's output layout is fixed by `_infer_layout`. Passing the
    # constant keeps the DECLARED version and the WRITTEN tree one fact — a repo declaring 2
    # over a schema-1 tree is the exact half-migration ADR-0041 D6 refuses.
    repo.init(when=when, schema_version=IMPORTER_SCHEMA_VERSION, kb_id=kb_identity.kb_id)
    try:
        repo.commit_all("chore: import external vault (agora import)", when=when)
    except GitError:
        pass  # nothing to commit (no diff over the seed)

    lint_result = lint(layout, taxonomy=taxonomy, schema_version=IMPORTER_SCHEMA_VERSION)

    # --- assemble the report ---------------------------------------------------------------------
    records = tuple(sorted((b.finalize() for b in builds), key=lambda r: r.rel_path))
    summary = {
        "notes": len(records),
        "repaired_frontmatter": sum(1 for r in records if r.repaired_frontmatter),
        "moved": sum(1 for r in records if any("moved to fit" in w for w in r.warnings)),
        "converted_links": sum(r.converted_links for r in records),
        "retargeted_links": sum(r.retargeted_links for r in records),
        "unresolved_links": sum(len(r.unresolved_links) for r in records),
        "stripped_tags": sum(len(r.stripped_tags) for r in records),
        "themes_without_sources": sum(
            1 for r in records if any("needs sources" in w for w in r.warnings)
        ),
        # v2 grounding counts (ADR-0014 D5): how many themes got a synth raw/ source, how many empty
        # themes became stubs, how many non-raw source entries were stripped, and how many
        # structural / non-knowledge files (schema doc, symlinks, log, _templates/_meta/_kb) were
        # excluded from import-as-notes (change A).
        "synth_raw_sources": sum(1 for r in records if r.synth_raw_source is not None),
        "stubbed_empty_themes": sum(1 for r in records if r.stubbed_empty_theme),
        "stripped_sources": sum(len(r.stripped_sources) for r in records),
        "excluded_structural_files": excluded_structural,
        # change E (ADR-0041): the navigation tier the importer COMPLETED rather than imported.
        # Counted separately and kept OUT of ``notes`` on purpose: ``notes`` is "one record per
        # imported SOURCE note", and folding a synthesized map into it would make the count of what
        # the operator's vault contained a function of what the importer decided to add.
        "synthesized_maps": synthesized_maps,
        "synthesized_index": synthesized_index,
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

    The curator's APPLY emits the ADR-0041 D2 common base in a fixed order
    (``curator.apply._common_frontmatter``): ``title``, ``kind``, the ``type`` OKF mirror, ``kb``,
    ``okf_version`` on the bundle-root index only, ``subjects``, ``aliases``, ``tags``, ``created``,
    ``updated``, ``timestamp``, ``status``, ``summary``, ``description``, ``derived``,
    ``provenance``, then the kind-specific keys. Mirroring that order keeps an imported note's YAML
    byte-comparable to a curated one and the diff readable. Unknown keys (preserved from the source
    per the tolerant round-trip, ADR-0014 D4) are appended in their original order after the known
    ones.
    """
    order = [
        "title",
        "kind",
        "type",
        "kb",
        "okf_version",
        "subjects",
        "aliases",
        "tags",
        "created",
        "updated",
        "timestamp",
        "status",
        "summary",
        "description",
        "derived",
        "provenance",
        "sources",
        "related",
        "children",
        "date",
        "run_id",
        "origin",
        "confidence",
        "body_status",
    ]
    ordered: dict[str, object] = {}
    for key in order:
        if key in fm:
            ordered[key] = fm[key]
    for key, value in fm.items():
        if key not in ordered:
            ordered[key] = value
    return ordered
