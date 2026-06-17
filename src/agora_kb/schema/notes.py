"""Note parsing foundation + the FROZEN content grammars (ADR-0010, the schema authority).

A KB wiki repo holds exactly four note types (``index | moc | theme | daily``), each a markdown
file opening with a YAML frontmatter block (parsed by :mod:`agora_kb.core.frontmatter`). This module
provides the deterministic, model-free reading layer the linter (ADR-0010 §6) and ``core.read``
(ADR-0009) both build on:

* :class:`Note` — a parsed note (its relative path, basename, declared ``type``, frontmatter, body).
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
from dataclasses import dataclass, field
from pathlib import Path

from agora_kb.core import frontmatter
from agora_kb.core.frontmatter import FrontmatterError
from agora_kb.core.layout import RepoLayout

__all__ = [
    "Note",
    "parse_all_notes",
    "wikilinks",
    "child_bullets",
    "body_link_basenames",
    "heading_slug",
    "note_basename",
    "PARSE_EXEMPT_BASENAMES",
]

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
class Note:
    """One parsed wiki note (ADR-0010 §1).

    ``rel_path`` is POSIX-style and relative to the repo root (so it is stable across platforms and
    sorts deterministically). ``basename`` is the filename without its ``.md`` suffix — the key the
    ``[[basename]]`` resolver matches against (§3.1). ``type`` is the declared ``type:`` frontmatter
    value, or ``None`` when the key is absent (the linter, not this parser, decides that is an
    error). ``frontmatter`` is the raw parsed mapping and ``body`` the markdown beneath it.
    """

    rel_path: str
    basename: str
    type: str | None
    frontmatter: dict[str, object] = field(default_factory=dict)
    body: str = ""


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


def parse_all_notes(layout: RepoLayout) -> list[Note]:
    """Parse every wiki note in ``layout`` into :class:`Note` objects, in deterministic path order.

    Scans ``index.md`` and ``wiki/**/*.md`` (the schema doc ``AGENTS.md`` / ``SCHEMA.md`` and its
    ``CLAUDE.md`` / ``QWEN.md`` / ``GEMINI.md`` symlinks are EXCLUDED — they are parse-exempt,
    ADR-0010 §1). Files that do not open with a well-formed frontmatter fence raise
    :class:`agora_kb.core.frontmatter.FrontmatterError`, surfacing a malformed note rather than
    silently dropping it. The returned list is sorted by POSIX relative path.
    """
    notes: list[Note] = []
    for path in _iter_note_paths(layout):
        text = path.read_text(encoding="utf-8")
        try:
            fm, body = frontmatter.parse(text)
        except FrontmatterError as exc:  # surface which file is malformed
            rel = path.relative_to(layout.root).as_posix()
            raise FrontmatterError(f"{rel}: {exc}") from exc
        type_value = fm.get("type")
        type_str = type_value if isinstance(type_value, str) else None
        notes.append(
            Note(
                rel_path=path.relative_to(layout.root).as_posix(),
                basename=note_basename(path),
                type=type_str,
                frontmatter=fm,
                body=body,
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
