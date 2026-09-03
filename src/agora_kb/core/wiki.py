"""Deterministic, model-free read path for a knowledge repo (ADR-0012, refining ADR-0009).

``core.wiki`` is the ONLY component that computes a :class:`SearchHit` field. It is a pure-Python
stdlib scorer — the oracle and test reference — so retrieval is fully deterministic and reproducible
without any model, sqlite, or external dependency.

Issue #26 wires the ADR-0012 §2 derived READER cache (git-ignored ``_kb/index/``, NEVER canonical,
fully rebuildable from the markdown at the curated commit — invariant #1): a per-file
content-addressed parsed-note cache that skips re-parsing unchanged notes, plus an exact in-memory
inverted-index candidate prefilter (:mod:`core.index_cache`; the optional FTS5/ripgrep candidate
accelerators of ADR-0012 §9 are deferred — the exact inverted index is free while all notes are
loaded). The full pure-Python scan remains the oracle AND the crash-proof fallback: a missing,
stale (``curated_commit`` mismatch), schema-bumped, corrupt, or locked cache silently degrades to a
scan. The cache is written ONLY by deterministic worker/CLI code (never the sandboxed backend,
never in the ADR-0008 INGEST allowlist); the read path opens it strictly read-only (invariant #2).

The algorithm is the LEXICAL-UNION FRONTIER with a single pure-Python scorer (ADR-0012 §4-§7):

1. **SEED** navigation roots from ``index.md`` + every in-scope ``<domain>-moc.md``.
2. **FRONTIER** = BFS over the note link graph (``max_hops=2``) UNION lexical candidates. Edges are
   the ADR-0014 D3 BODY markdown links ``[Title](relative.md)`` (resolved path→basename), the
   frontmatter ``related:`` / ``children:`` ``"[[basename]]"`` arrays, and any tolerated stray body
   ``[[ ]]`` wikilink. The traversal stays deterministic + reproducible (ADR-0009).
3. **TOKENIZE** the question; an all-stopword question yields ``not_found`` immediately. The
   shared tokenizer (issue #56 / ADR-0012 addendum) NFC-normalizes and emits lowercased ASCII
   ``[a-z0-9]+`` tokens PLUS character bigrams over CJK runs (unigram for a length-1 run), so
   Korean/CJK questions and notes are retrievable; the CJK range table is shared with
   :mod:`core.gold` via :mod:`core.cjk` (single source).
4. **LEXICAL** BM25F over {title, aliases, tags, headings, summary, body} with repo-wide IDF
   (aliases 3.0 / summary 2.0 joined in the #56 addendum — both frontmatter-authored, previously
   unscored).
5. **STRUCTURAL** degree surrogate ``alpha/(1+d_moc) + beta*indeg_norm`` (no iterative PageRank).
6. **COMBINE** ``w_lex*lex + w_struct*struct + fm`` (``fm`` LIVE since #56: active +0.10 /
   deprecated −0.15) behind a mandatory lexical-evidence gate that drops structurally-strong but
   lexically-empty notes.
7. **ORDER** by a total order (no ties survive) and truncate to ``limit``.

Float determinism: ``lex``/``struct``/``fm`` are each rounded to 6 decimals, then combined in the
fixed order ``w_lex*lex_r + w_struct*struct_r + fm_r``, clamped to ``[0,1]`` and rounded to 6
decimals (ADR-0012 §6.1). The absolute ``(repo, path)`` tie-break absorbs residual sub-ULP equality.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from . import frontmatter, index_cache
from .cjk import CJK_RANGES
from .layout import InvalidWriterError, RepoLayout

if TYPE_CHECKING:
    from agora_kb.config import IndexPolicy
    from agora_kb.core.repo import Repo
    from agora_kb.schema.notes import Note

__all__ = ["SearchHit", "QueryResult", "Wiki", "build_cache", "IndexBuildResult"]

# --- frozen configuration (ADR-0012 §1 defaults as amended by the #56 addendum) -----------------
K1 = 1.2  # BM25 term-freq saturation
B = 0.75  # BM25 length normalization (applied per field via FIELD_B below)
# The scoring fields IN ITERATION ORDER (fixed for float determinism, ADR-0012 §6.1). aliases and
# summary joined in the #56 addendum: aliases are alternate TITLES (weight 3.0, powers QUERY as the
# schema always claimed) and summary is the note's densest sentence (2.0), both frontmatter-only
# and previously invisible to the ranker.
_FIELDS: tuple[str, ...] = ("title", "aliases", "tags", "headings", "summary", "body")
FIELD_WEIGHTS: dict[str, float] = {
    "title": 3.0,
    "aliases": 3.0,
    "tags": 2.5,
    "headings": 2.0,
    "summary": 2.0,
    "body": 1.0,
}
# Per-field BM25F length-normalization ``b`` (#56 review). ``aliases`` is EXEMPT (b=0 → denom=1):
# it is a sparse OPTIONAL field — most notes carry none — so the corpus-wide avgdl collapses
# toward 0 and the standard denominator ``1 - b + b*len_f/avgdl_f`` explodes for exactly the notes
# that DO have aliases, silently crushing the "title-equivalent 3.0" weight (e.g. a 42-note corpus
# with one 2-token alias list: avgdl≈0.05 → denom≈33 → effective per-occurrence weight ≈0.09,
# losing to a single body mention). An alias list is a tiny controlled set of alternate titles
# (L1-15-unique), so document-length normalization carries no signal there. Every other field
# keeps ``b=B`` — title/summary are present in every produced note so their avgdl is healthy, and
# changing them would perturb the frozen English ranking (ADR-0012 addendum A2).
FIELD_B: dict[str, float] = {f: (0.0 if f == "aliases" else B) for f in _FIELDS}
PIVOT = 1.5  # lexical normalization pivot: lex = raw / (raw + pivot)
W_LEX = 0.65  # combined-score weight on lexical
W_STRUCT = 0.35  # combined-score weight on structural
STRUCT_ALPHA = 0.7  # structural: weight on MOC-distance term
STRUCT_BETA = 0.3  # structural: weight on in-degree term
# Phase-1b LIVE (#56 addendum): the ADR-0012 §8 preconditions (schema emitter + LINT status enum)
# both shipped, so the status boost/demotion is on — deprecated no longer ties with active.
FM_ENABLED = True
MAX_HOPS = 2  # BFS depth from MOC/index seeds
FLOOR = 0.18  # not_found threshold on the combined [0,1] score
MAX_HITS = 20  # default limit
EXCERPT_MAX_CHARS = 240
EXCERPT_WINDOW_TOKENS = 32

STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)

# linked-theme=0 < heading=1 < lexical=2 (ADR-0012 §7 ordering)
_REASON_RANK: dict[str, int] = {"linked-theme": 0, "heading": 1, "lexical": 2}

# ATX heading line: 1-6 leading '#', a space, then text (closing '#'s optional and trimmed).
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
# A fenced code block delimiter (``` or ~~~), allowing an info string after.
_FENCE_RE = re.compile(r"^[ \t]*(```+|~~~+)")
# [[target|label]] or [[target#anchor]] or [[target]] wikilink. STILL used for the frontmatter
# related:/children: arrays (ADR-0014 D3 keeps those as "[[basename]]" strings) and for any stray
# body wikilink (a foreign/Obsidian vault may carry them; tolerated on read, ADR-0014 D4).
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
# [label](url) markdown link — used both to strip link punctuation to its label AND (since
# ADR-0014 D3) to read the BODY graph edges: a MOC/index child bullet is now ``[Title](path.md)``.
# An IMAGE link ``![alt](assets/foo.png)`` is excluded from graph edges via the ``(?<!\!)`` guard in
# _MDLINK_TARGET_RE (assets live outside the link graph, ADR-0010 §3.5).
_MDLINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# A non-image markdown link with its TARGET captured — the ADR-0014 D3 body graph-edge form. The
# target is resolved to a basename (filename minus dir minus ``.md``) by _mdlink_target_basename.
_MDLINK_TARGET_RE = re.compile(r"(?<!\!)\[(?P<text>[^\]\r\n]*)\]\((?P<target>[^)\r\n]+)\)")
# An ASCII token of the §3 tokenizer alphabet (also the excerpt-window token unit, §7).
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# The CJK run alphabet — a character class built from the SHARED range table (core.cjk, the single
# source also feeding gold's token estimator; issue #56 forbids a second range definition).
_CJK_CLASS = "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in CJK_RANGES)
# One scan alternation: an ASCII token OR a maximal CJK run (bigrammed downstream). Any other
# codepoint (spaces, ASCII punctuation, non-CJK scripts) separates tokens exactly as before.
_SCAN_RE = re.compile(rf"[a-z0-9]+|[{_CJK_CLASS}]+")


def _cjk_run_tokens(run: str) -> list[str]:
    """Character bigrams over each letter/number subrun of a CJK run (unigram when length 1).

    The shared ``CJK_RANGES`` include CJK punctuation, fullwidth symbols, and the ideographic
    space; those codepoints (Unicode categories ``P*``/``S*``/``Z*``) SPLIT the run exactly like
    any non-token character, so a bigram never bridges a sentence boundary (``메모리。그림`` →
    ``메모``, ``모리``, ``그림`` — never ``리그``). Deterministic: a pure function of the
    codepoints under the test suite's pinned CPython/unicodedata (ADR-0012 §6.1 stance).
    """
    out: list[str] = []
    sub: list[str] = []

    def _flush() -> None:
        if not sub:
            return
        if len(sub) == 1:
            out.append(sub[0])
        else:
            out.extend(sub[i] + sub[i + 1] for i in range(len(sub) - 1))
        sub.clear()

    for ch in run:
        if unicodedata.category(ch)[0] in ("L", "N"):
            sub.append(ch)
        else:
            _flush()
    _flush()
    return out


def _scan_tokens(text: str) -> list[str]:
    """NFC-normalize + lowercase, then emit ASCII tokens and CJK bigrams in document order.

    The UNFILTERED token stream (no stopword removal) — the #56-addendum widening of the raw
    ``_TOKEN_RE.findall(text.lower())`` scan. NFC first (macOS/NFD-decomposed Hangul jamo compose
    to the syllables a query carries); for pure-ASCII text NFC is the identity, so the English
    tokenization is byte-identical to the original §3 rule.
    """
    norm = unicodedata.normalize("NFC", text).lower()
    out: list[str] = []
    for m in _SCAN_RE.finditer(norm):
        tok = m.group()
        if ord(tok[0]) < 128:  # the [a-z0-9]+ branch — kept verbatim
            out.append(tok)
        else:  # a CJK run — character bigrams (unigram for a length-1 subrun)
            out.extend(_cjk_run_tokens(tok))
    return out


def _tokenize(text: str) -> list[str]:
    """The single shared tokenizer (ADR-0012 §3 + #56 addendum): ASCII tokens + CJK bigrams.

    Lowercased ``[a-z0-9]+`` runs minus stopwords, exactly as §3 froze — PLUS, per the #56
    addendum, NFC normalization and character bigrams over CJK runs (unigram when a run is a
    single codepoint), so Korean/CJK text is retrievable. The English STOPWORDS cannot collide
    with a CJK bigram, so the one filter serves both alphabets.
    """
    return [t for t in _scan_tokens(text) if t not in STOPWORDS]


def _tokenize_tags(tags: tuple[str, ...]) -> list[str]:
    """Tokenize tags with kebab expansion (ADR-0012 §3).

    A HYPHENATED kebab tag ``single-writer`` contributes the full token ``single-writer`` AND its
    split parts ``single``, ``writer``. A PLAIN single-word tag ``inbox`` contributes its single
    token ONCE (the full form and the split part coincide, so emitting both would double-count
    ``tf(t, 'tags')`` and inflate ``len_f['tags']``/``avgdl``, distorting BM25F — ADR-0012 §3).
    """
    out: list[str] = []
    for tag in tags:
        lowered = unicodedata.normalize("NFC", tag).lower()
        # Split parts under the shared token alphabet (ASCII runs + CJK bigrams, #56 addendum;
        # e.g. "single-writer" -> ["single", "writer"] exactly as before).
        parts = _scan_tokens(lowered)
        # Only a hyphenated tag gets its full kebab form injected in addition to the parts; a plain
        # tag's full form equals its single part, so emitting both would double-count.
        if "-" in lowered:
            joined = "".join(ch for ch in lowered if ch.isalnum() or ch == "-")
            if joined and joined not in STOPWORDS:
                out.append(joined)
        for p in parts:
            if p not in STOPWORDS:
                out.append(p)
    return out


def _slug(heading: str) -> str:
    """ASCII approximation of the GitHub/Obsidian slug rule (ADR-0012 §6).

    Text with no ``[a-z0-9]`` material (e.g. a pure-Korean title/heading) yields ``""`` — the
    documented no-deep-link anchor value. A real GitHub slugger preserves Unicode word characters;
    widening to that is a deferred decision (ADR-0012 addendum A4: it re-serializes into the
    reader cache and must be reconciled with the web face's heading-id rule first).
    """
    return re.sub(r"[^a-z0-9]+", "-", heading.strip().lower()).strip("-")


def _strip_link_punctuation(text: str) -> str:
    """Reduce ``[[t|label]]``/``[[t#a]]``/``[[t]]`` and ``[label](url)`` to their visible label."""

    def _wl(m: re.Match[str]) -> str:
        inner = m.group(1)
        if "|" in inner:  # [[target|label]] -> label
            return inner.split("|", 1)[1]
        target = inner.split("#", 1)[0]  # [[target#anchor]] -> target
        return target

    text = _WIKILINK_RE.sub(_wl, text)
    text = _MDLINK_RE.sub(lambda m: m.group(1), text)
    return text


def _link_target(inner: str) -> str:
    """Resolve a ``[[...]]`` inner string to its target basename (anchor & alias stripped)."""
    target = inner.split("|", 1)[0]  # drop alias
    target = target.split("#", 1)[0]  # drop anchor
    return target.strip()


def _mdlink_target_basename(target: str) -> str:
    """Resolve a markdown-link TARGET path to its note basename, or ``""`` (ADR-0014 D3).

    The body graph edge ``[Title](relative.md)`` (ADR-0014 D3) carries a relative path; its basename
    is the filename minus directory minus ``.md`` — the internal identity the read-path graph seeds
    on (ADR-0010 D5), matching :func:`agora_kb.schema.notes._basename_from_link_path`. An anchor
    fragment (``path.md#heading``) is dropped. A NON-``.md`` target (an external URL, an ``assets/``
    image) is NOT a note edge and yields ``""`` so the caller skips it. Determinism: a pure function
    of the link text, so the seeded graph is reproducible (ADR-0009).
    """
    cleaned = target.strip()
    cleaned = cleaned.split("#", 1)[0]  # drop any #anchor fragment
    if not cleaned.endswith(".md"):
        return ""
    last = cleaned.rsplit("/", 1)[-1]
    return last[: -len(".md")]


def _fm_wikilink_basenames(value: object) -> list[str]:
    """Resolve a frontmatter ``related:`` / ``children:`` array of ``"[[basename]]"`` strings.

    ADR-0014 D3 keeps the frontmatter graph arrays as Obsidian-Properties ``"[[basename]]"`` tokens
    (only the BODY child bullets became markdown links). Each list entry is scanned for ``[[ ]]``
    tokens and resolved to its target basename via :func:`_link_target`, so these structured edges
    seed the read-path graph alongside the body markdown links — keeping ``linked-theme`` BFS total.
    A non-list / non-string value contributes nothing.
    """
    out: list[str] = []
    if not isinstance(value, list):
        return out
    for entry in value:
        if not isinstance(entry, str):
            continue
        for m in _WIKILINK_RE.finditer(entry):
            target = _link_target(m.group(1))
            if target:
                out.append(target)
    return out


@dataclass
class _Heading:
    level: int
    text: str
    slug: str
    line: int  # 1-based


@dataclass
class _Note:
    """A parsed wiki note (ADR-0012 §2). All scoring inputs are derived from the markdown only."""

    path: str  # repo-relative POSIX path
    basename: str
    is_moc: bool
    is_index: bool
    title: str
    title_line: int  # 1-based line of the H1 (or 1 if none)
    headings: list[_Heading]
    tags: tuple[str, ...]
    status: str
    body_lines: list[str]  # link-punctuation-stripped body, 1-based via index+1
    raw_lines: list[str]  # ORIGINAL body lines (links intact) — for MOC link-label extraction
    outlinks: tuple[str, ...]  # ordered, de-duplicated link targets (basenames)
    field_tokens: dict[str, list[str]] = field(default_factory=dict)
    indeg: int = 0


def _note_to_dict(note: _Note) -> dict:
    """Serialize a parsed :class:`_Note` for the reader cache (ADR-0012 §2), EXCLUDING ``indeg``.

    ``indeg`` and every score are global/query-derived and are recomputed at load, so they are never
    stored (a persisted ``indeg`` would be a stale-global bug). Tuples become lists (JSON has no
    tuple) and headings become plain dicts; order is preserved so serialization is deterministic.
    """
    return {
        "path": note.path,
        "basename": note.basename,
        "is_moc": note.is_moc,
        "is_index": note.is_index,
        "title": note.title,
        "title_line": note.title_line,
        "headings": [
            {"level": h.level, "text": h.text, "slug": h.slug, "line": h.line}
            for h in note.headings
        ],
        "tags": list(note.tags),
        "status": note.status,
        "body_lines": note.body_lines,
        "raw_lines": note.raw_lines,
        "outlinks": list(note.outlinks),
        "field_tokens": note.field_tokens,
    }


def _note_from_dict(data: dict) -> _Note:
    """Rebuild a :class:`_Note` from its cached dict (inverse of :func:`_note_to_dict`).

    ``indeg`` is left at its ``0`` default (recomputed by :meth:`Wiki._compute_indegrees`). Tuple
    fields are restored from the serialized lists; headings are rebuilt as :class:`_Heading` values.

    STRICT by design: a missing/mis-typed key (including an INCOMPLETE ``field_tokens`` missing
    one of the ``_FIELDS`` scoring fields, which would otherwise ``KeyError`` later in the scorer)
    raises ``KeyError``/``TypeError``/``ValueError``. The reuse path in :meth:`Wiki._load_notes`
    catches exactly those and re-parses that one file, so a structurally-corrupt cached entry can
    never crash ``kb_query`` (ADR-0012 §2 crash-proof read path).
    """
    field_tokens = data["field_tokens"]
    if not isinstance(field_tokens, dict) or any(
        not isinstance(field_tokens.get(f), list) for f in _FIELDS
    ):
        raise ValueError("cached note has a missing/malformed field_tokens field")
    return _Note(
        path=data["path"],
        basename=data["basename"],
        is_moc=data["is_moc"],
        is_index=data["is_index"],
        title=data["title"],
        title_line=data["title_line"],
        headings=[
            _Heading(level=h["level"], text=h["text"], slug=h["slug"], line=h["line"])
            for h in data["headings"]
        ],
        tags=tuple(data["tags"]),
        status=data["status"],
        body_lines=data["body_lines"],
        raw_lines=data["raw_lines"],
        outlinks=tuple(data["outlinks"]),
        field_tokens=data["field_tokens"],
    )


def _read_tolerant(path: Path) -> str:
    """Read a wiki file for the QUERY path, tolerating non-UTF8 bytes (ADR-0014 D4).

    The read/query boundary must NEVER crash on foreign or imperfect content: a single non-UTF8
    note in a vault (e.g. a not-yet-normalized Obsidian vault) is decoded LOSSILY
    (``errors="replace"``) so it stays queryable instead of raising ``UnicodeDecodeError`` out of
    ``kb_query``. The strict UTF-8 / LF / no-BOM requirement stays the PRODUCER lint's job (L1-16),
    not the reader's — the tolerant-consumer / strict-producer split (ADR-0014 D1).
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_note(path: str, basename: str, is_index: bool, raw_text: str) -> _Note:
    """Parse one note's markdown into a :class:`_Note` (frontmatter via core.frontmatter)."""
    try:
        fm, body = frontmatter.parse(raw_text)
    except frontmatter.FrontmatterError:
        fm = {}
        body = raw_text

    # tags
    raw_tags = fm.get("tags")
    if isinstance(raw_tags, list):
        tags = tuple(str(t) for t in raw_tags)
    elif isinstance(raw_tags, str):
        tags = (raw_tags,)
    else:
        tags = ()

    # aliases (#56 addendum): frontmatter alternate titles — L1-15 enforces their global
    # uniqueness and the schema always annotated them "(powers QUERY)"; now they do, at title
    # weight. Same tolerant shape handling as tags (list of scalars, or a bare scalar).
    raw_aliases = fm.get("aliases")
    if isinstance(raw_aliases, list):
        aliases = tuple(str(a) for a in raw_aliases)
    elif isinstance(raw_aliases, str):
        aliases = (raw_aliases,)
    else:
        aliases = ()

    # summary (#56 addendum): the note's densest sentence lives in frontmatter, which is stripped
    # before body tokenization — index it as its own weighted field.
    raw_summary = fm.get("summary")
    summary = raw_summary if isinstance(raw_summary, str) else ""

    # status (normalize to the §8 enum; default 'neutral' for ranking)
    raw_status = fm.get("status")
    status = str(raw_status).strip().lower() if isinstance(raw_status, str) else "neutral"

    fm_title = fm.get("title")
    fm_title_str = str(fm_title) if isinstance(fm_title, str) else None

    is_moc = _is_moc_path(path)

    # Walk body lines: collect headings (H1 → title; H2-H6 → headings list), tracking code fences.
    body_lines_raw = body.split("\n")
    h1_text: str | None = None
    h1_line = 1
    headings: list[_Heading] = []
    slug_counts: dict[str, int] = {}
    in_fence = False
    outlinks: list[str] = []
    seen_targets: set[str] = set()

    for idx, line in enumerate(body_lines_raw, start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            hm = _HEADING_RE.match(line)
            if hm is not None:
                level = len(hm.group(1))
                text = hm.group(2).strip()
                if level == 1:
                    if h1_text is None:
                        h1_text = text
                        h1_line = idx
                else:
                    base_slug = _slug(text)
                    if base_slug:
                        n = slug_counts.get(base_slug, 0)
                        slug_counts[base_slug] = n + 1
                        slug = base_slug if n == 0 else f"{base_slug}-{n}"
                    else:
                        # #56 review: a heading with no [a-z0-9] material (e.g. pure Korean) has
                        # no ASCII-derivable anchor — keep the DOCUMENTED "" (no deep link) value
                        # rather than fabricating dedup suffixes ("-1", "-2") that resolve nowhere
                        # under any slugger. Unicode-preserving slugs are a deferred decision
                        # (ADR-0012 addendum A4).
                        slug = ""
                    headings.append(_Heading(level=level, text=text, slug=slug, line=idx))
        # Body graph edges (ADR-0012 §2 walks the note's link graph). Since ADR-0014 D3 the BODY
        # graph link is a STANDARD MARKDOWN LINK ``[Title](relative.md)`` (the MOC/index child
        # bullets), so collect non-image markdown ``.md`` targets resolved to basenames. ALSO
        # collect any stray body ``[[ ]]`` wikilink: a foreign/Obsidian vault may still carry them
        # and they are tolerated on read (ADR-0014 D4) — a produced Agora note has none in its body.
        # Image links / external URLs resolve to "" and are skipped.
        for m in _MDLINK_TARGET_RE.finditer(line):
            target = _mdlink_target_basename(m.group("target"))
            if target and target not in seen_targets:
                seen_targets.add(target)
                outlinks.append(target)
        for m in _WIKILINK_RE.finditer(line):
            target = _link_target(m.group(1))
            if target and target not in seen_targets:
                seen_targets.add(target)
                outlinks.append(target)

    # Frontmatter structured graph edges: related: / children: STAY ``"[[basename]]"`` arrays
    # (ADR-0014 D3) — seed them into the graph alongside the body markdown links so a theme's
    # ``related`` and a MOC/index's ``children`` keep contributing ``linked-theme`` BFS edges and
    # in-degree, exactly as the body ``[[ ]]`` did before the D3 grammar change.
    for key in ("related", "children"):
        for target in _fm_wikilink_basenames(fm.get(key)):
            if target not in seen_targets:
                seen_targets.add(target)
                outlinks.append(target)

    # title precedence: first H1 → frontmatter title → basename with '-'→space
    if h1_text is not None:
        title = h1_text
        title_line = h1_line
    elif fm_title_str is not None:
        title = fm_title_str
        title_line = 1
    else:
        title = basename.replace("-", " ")
        title_line = 1

    # body: prose with link punctuation stripped to the visible label, code fences dropped.
    body_lines: list[str] = []
    in_fence = False
    for line in body_lines_raw:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            body_lines.append("")  # keep line numbering stable; fence contributes no tokens
            continue
        if in_fence:
            body_lines.append("")  # fenced code excluded from body tokens, line numbering kept
            continue
        body_lines.append(_strip_link_punctuation(line))

    # field tokens (ADR-0012 §2/§3 + #56 addendum). headings appear in BOTH headings and body
    # (double-count). aliases/summary are frontmatter-only, so they appear ONLY in their fields.
    heading_text = " ".join(h.text for h in headings)
    body_text = " ".join(body_lines)
    field_tokens = {
        "title": _tokenize(title),
        "aliases": _tokenize(" ".join(aliases)),
        "tags": _tokenize_tags(tags),
        "headings": _tokenize(heading_text),
        "summary": _tokenize(summary),
        "body": _tokenize(body_text),
    }

    return _Note(
        path=path,
        basename=basename,
        is_moc=is_moc,
        is_index=is_index,
        title=title,
        title_line=title_line,
        headings=headings,
        tags=tags,
        status=status,
        body_lines=body_lines,
        raw_lines=body_lines_raw,
        outlinks=tuple(outlinks),
        field_tokens=field_tokens,
    )


def _is_moc_path(path: str) -> bool:
    """True iff ``path`` matches ``wiki/<domain>/<domain>-moc.md`` (ADR-0012 §2)."""
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "wiki":
        return False
    domain = parts[1]
    return parts[2] == f"{domain}-moc.md"


def _moc_domain(path: str) -> str | None:
    """Return the ``<domain>`` of a ``<domain>-moc.md`` path, else ``None``."""
    if not _is_moc_path(path):
        return None
    return path.split("/")[1]


class SearchHit(BaseModel):
    """One ordered citation in a :class:`QueryResult` (DATA-MODEL §9 / ADR-0012 §0)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    path: str
    anchor: str  # heading/wikilink slug; MAY be "" for a pre-heading lexical match OR a
    # title/heading with no ASCII-sluggable text (e.g. pure Korean — no deep link, addendum A4)
    line: int  # 1-based
    excerpt: str
    match_reason: Literal["linked-theme", "heading", "lexical"]
    score: float  # combined SCORE in [0,1], 6 decimals


class QueryResult(BaseModel):
    """The deterministic, model-free result of :meth:`Wiki.query` (DATA-MODEL §9 / ADR-0012 §0)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    status: Literal["ok", "not_found"]
    hits: tuple[SearchHit, ...]


@dataclass
class _Candidate:
    """Working state for a note under consideration during a single query."""

    note: _Note
    d_moc: int  # min hop distance from a MOC/index seed; max_hops+1 (=3) if unreached
    moc_label_tokens: set[str]  # union of MOC link-label tokens for d_moc==0 children
    lex: float = 0.0
    struct: float = 0.0
    fm: float = 0.0
    score: float = 0.0
    match_reason: str = "lexical"
    anchor: str = ""
    line: int = 1
    excerpt: str = ""


class Wiki:
    """Deterministic read path over the markdown wiki of one repo (ADR-0012).

    Pure-Python, stdlib-only, zero model. Construct with the repo's :class:`RepoLayout`; call
    :meth:`query`. The repo name is derived from the layout root's directory name.
    """

    def __init__(self, layout: RepoLayout) -> None:
        self.layout = layout
        self.repo = layout.root.name

    # --- public API ----------------------------------------------------------------------------
    def query(self, question: str, *, limit: int = MAX_HITS) -> QueryResult:
        """Return ordered :class:`SearchHit`s for ``question`` (or ``not_found``).

        ``limit`` caps the number of hits (default 20, the ADR's ``max_hits``). Returns
        ``status='not_found'`` with empty ``hits`` when there is no eligible evidence above the
        floor, when the question is empty/all-stopwords, or when the repo has no notes.

        This is the READ face's entry point (``kb_query`` / the web face). Today it is exactly the
        deterministic lexical oracle and nothing else; it is the method that MAY one day grow a
        further tier above that floor. Callers on the curator WRITE path must not use it — they
        call :meth:`query_lexical` (issue #144).

        Structurally a pure delegation to :meth:`query_lexical`: a ranking tier added here can only
        be written INSIDE this body, which is where both docstrings say it belongs. Editing the
        oracle instead would silently re-couple the write path.
        """
        return self.query_lexical(question, limit=limit)

    def query_lexical(self, question: str, *, limit: int = MAX_HITS) -> QueryResult:
        """Return the deterministic, model-free lexical result for ``question``.

        This is the BM25F + structural + frontmatter oracle of ADR-0012 §0a — byte-for-byte what
        :meth:`query` computes today, sharing one private implementation with it. It is the seam
        the curator's write path is pinned to (``curator/bundle.py``, the §1.1 ``related/`` view
        that feeds the planning brain's MERGE targets).

        **This method will NEVER gain a model tier.** ADR-0012 §0a puts lex / struct / fm / score /
        match_reason inside core and nowhere else; issue #144 records why the WRITE path in
        particular must stay model-free: a mis-merge is permanent (the closed ADR-0011 op
        vocabulary has no DELETE, and ``apply.py`` stamps the merged fact with real ``raw/``
        provenance), while a bad read is merely transient. If a ranking tier is ever added, it is
        added to :meth:`query` — never here. Any change to this method's ordering is a change to
        what the curator merges, and must be justified as such.

        This method IS the implementation (``query`` delegates DOWN to it, never the reverse), on
        purpose: the seam a future tier author would naturally edit has to be ``query``'s body, not
        a shared private helper whose name reads like ``query``'s own half.
        """
        policy = self._index_policy()
        notes = self._load_notes(policy)
        # not_found gate (d): EMPTY REPO — callable on a fresh repo before any notes exist.
        if not notes:
            return QueryResult(query=question, status="not_found", hits=())

        # not_found gate (a): empty q_tokens (incl. all-stopword question).
        q_tokens = _tokenize(question)
        if not q_tokens:
            return QueryResult(query=question, status="not_found", hits=())

        by_basename: dict[str, _Note] = {n.basename: n for n in notes}
        self._compute_indegrees(notes, by_basename)

        seeds = self._seed(notes, by_basename, q_tokens)
        # Candidate PREFILTER (ADR-0012 §0a/§9): restrict scoring to (BFS-reached ∪ lexical) notes.
        # Provably output-identical — a note that is neither seeded/reached NOR carries a q-token in
        # any field has lex==0 AND d_moc!=0, so it always fails the §6 gate (see _frontier).
        lexical_paths = self._lexical_candidate_paths(notes, q_tokens)
        candidates = self._frontier(notes, by_basename, seeds, lexical_paths)

        # repo-wide BM25F statistics (IDF + per-field avgdl), computed over ALL notes.
        stats = _CorpusStats.build(notes)
        q_set = sorted(set(q_tokens))

        eligible: list[_Candidate] = []
        for cand in candidates:
            cand.lex = round(_lexical(cand.note, q_set, stats), 6)
            cand.struct = round(_structural(cand.d_moc, cand.note.indeg, stats.max_indeg), 6)
            cand.fm = round(_fm(cand.note.status), 6)
            if not _passes_gate(cand, q_tokens):
                continue
            cand.score = round(_combined(cand.lex, cand.struct, cand.fm), 6)
            self._assign_reason_and_extract(cand, q_tokens, stats)
            eligible.append(cand)

        # The floor is a property of a HIT, not only of the best hit (issue #146). Applying it to
        # `best` alone let every other eligible candidate ride in behind a single strong one — so a
        # husk scoring 0.0 was returned as a search result whenever anything else cleared the floor.
        # Filtering first also subsumes not_found gate (c): an empty survivor set IS not_found.
        eligible = [c for c in eligible if c.score >= FLOOR]

        # not_found gates (b) zero eligible, (c) nothing clears the floor.
        if not eligible:
            return QueryResult(query=question, status="not_found", hits=())

        eligible.sort(key=_order_key)
        hits = tuple(
            SearchHit(
                repo=self.repo,
                path=c.note.path,
                anchor=c.anchor,
                line=c.line,
                excerpt=c.excerpt,
                match_reason=c.match_reason,  # type: ignore[arg-type]
                score=c.score,
            )
            for c in eligible[: max(0, limit)]
        )
        return QueryResult(query=question, status="ok", hits=hits)

    # --- browse read helpers (ADR-0003: read logic stays in core, not the face) ---------------
    def list_notes(self) -> list[Note]:
        """Return every tracked wiki note (``index.md`` + ``wiki/**``) for the browse face.

        Delegates to :func:`agora_kb.schema.notes.parse_all_notes`, the single deterministic note
        scanner (the schema doc and its symlinks are already excluded there, ADR-0010 §1). This is
        a pure read; it never touches :meth:`query` scoring. It uses ``parse_all_notes``' tolerant
        default (NOT ``strict``), so one fenceless / foreign note degrades to empty-frontmatter +
        full body instead of crashing the browse face — matching :meth:`query`'s tolerant-read
        posture (ADR-0014 D1). Imported lazily inside the method to avoid a ``core`` ⇄
        ``schema.notes`` import cycle (``schema.notes`` imports from ``core``).
        """
        from agora_kb.schema.notes import parse_all_notes

        return parse_all_notes(self.layout)

    def get_note(self, rel_path: str) -> Note | None:
        """Return the tracked note whose POSIX repo-relative path is ``rel_path``, else ``None``.

        Path-safe by construction: rather than reading an arbitrary path off disk, it matches
        ``rel_path`` against the real :func:`parse_all_notes` results, so only a genuinely tracked
        note (``index.md`` or ``wiki/**/*.md``, schema docs excluded) ever resolves. A traversal
        attempt (``../secret``), an absolute path, or any untracked path simply finds no match and
        returns ``None`` — the face can never read outside the repo through this seam.
        """
        for note in self.list_notes():
            if note.rel_path == rel_path:
                return note
        return None

    # --- index policy + curated commit (ADR-0012 §2 cache gates) -------------------------------
    def _index_policy(self) -> IndexPolicy:
        """Load the repo's ``index:`` cache policy; NEVER raise out of the read path.

        A malformed ``index:`` block (e.g. a non-boolean ``enabled``) is surfaced loudly by the CLI
        (``agora index`` / ``agora doctor``), but ``kb_query`` must never crash on config, so here a
        config error degrades to the default (cache on).
        """
        from ..config import IndexPolicy, load_index_policy

        try:
            return load_index_policy(self.layout)
        except Exception:  # noqa: BLE001 — the read path must not crash on malformed config.
            return IndexPolicy()

    def _curated_commit(self) -> str | None:
        """Full sha at the curated branch tip, or ``None`` if uninitialized / not a git repo.

        One ``git rev-parse`` per query when the cache is consulted (ADR-0012 §2 whole-cache gate).
        Returns ``None`` (→ full-scan fallback) on any git failure so the read path never crashes.
        """
        from .repo import GitError, Repo

        repo = Repo.resolve(self.layout.root)
        if not repo.is_initialized():
            return None
        try:
            return repo.branch_commit()
        except GitError:
            return None

    # --- note loading --------------------------------------------------------------------------
    def _iter_note_files(self) -> list[tuple[str, str, bool, Path]]:
        """The deterministic note-file enumeration shared by the cached + uncached loaders.

        Returns ``(rel, basename, is_index, abs_path)`` tuples — ``index.md`` first, then every
        ``wiki/**/*.md`` in sorted repo-relative POSIX order — the one source of scan order, so
        the cached and uncached paths can never drift (which would break byte-identical parity).
        """
        out: list[tuple[str, str, bool, Path]] = []
        root = self.layout.root
        index_path = self.layout.index_file
        if index_path.is_file():
            out.append(("index.md", "index", True, index_path))
        wiki_dir = self.layout.wiki_dir
        if wiki_dir.is_dir():
            md_files = sorted(
                (p for p in wiki_dir.rglob("*.md") if p.is_file()),
                key=lambda p: p.relative_to(root).as_posix(),
            )
            for p in md_files:
                rel = p.relative_to(root).as_posix()
                out.append((rel, Path(rel).stem, False, p))
        return out

    def _load_notes_uncached(self) -> list[_Note]:
        """Parse ``index.md`` + every ``wiki/**/*.md`` — the oracle + crash-proof fallback."""
        return [
            _parse_note(
                path=rel, basename=basename, is_index=is_index, raw_text=_read_tolerant(path)
            )
            for rel, basename, is_index, path in self._iter_note_files()
        ]

    def _load_notes(self, policy: IndexPolicy | None = None) -> list[_Note]:
        """Return parsed notes, reusing the ADR-0012 §2 cache for unchanged files.

        Whole-cache gate: use the cache only if it exists, its ``cache_schema_version`` matches,
        and its ``curated_commit`` equals the live curated tip (else full scan). Per-file gate: each
        file is ALWAYS read and digested (``source_digest`` over the exact tolerant-decoded parser
        input); a cached parse is reused ONLY on a digest match, else the file is re-parsed. So the
        result is byte-identical to a full scan regardless of working-tree/committed divergence, and
        the read path never writes the cache (invariant #2).
        """
        if policy is None:
            policy = self._index_policy()
        if not policy.enabled:
            return self._load_notes_uncached()
        entries = self._usable_cache_entries()
        if entries is None:
            return self._load_notes_uncached()
        notes: list[_Note] = []
        for rel, basename, is_index, path in self._iter_note_files():
            text = _read_tolerant(path)
            entry = entries.get(rel)
            reuse: _Note | None = None
            if entry is not None and entry["sha"] == index_cache.source_digest(text):
                try:
                    reuse = _note_from_dict(entry["note"])
                except (KeyError, TypeError, ValueError):
                    # A structurally-corrupt cached inner note (valid envelope, bad body) must not
                    # crash the query: re-parse just this file (self-healing fallback).
                    reuse = None
            notes.append(
                reuse
                if reuse is not None
                else _parse_note(path=rel, basename=basename, is_index=is_index, raw_text=text)
            )
        return notes

    def _usable_cache_entries(self) -> dict[str, dict] | None:
        """Return the cache's ``notes`` map iff it is present, schema-current, and commit-fresh.

        ``None`` (→ full scan) on: uninitialized/non-git repo, a git failure, an unsafe repo name
        (``InvalidWriterError`` from the path guard), an absent/corrupt/locked/schema-mismatched
        cache, or a ``curated_commit`` that differs from the live curated tip (stale).
        """
        commit = self._curated_commit()
        if commit is None:
            return None
        try:
            cache_path = self.layout.index_notes_path()
        except InvalidWriterError:
            return None
        payload = index_cache.read_payload(cache_path)
        if payload is None or payload.curated_commit != commit:
            return None
        return payload.notes

    def _lexical_candidate_paths(self, notes: list[_Note], q_tokens: list[str]) -> set[str]:
        """Note paths that could match lexically — the EXACT candidate prefilter (ADR-0012 §9).

        Built as an in-memory inverted index over the already-loaded ``field_tokens`` (free — the
        notes are parsed for scoring anyway). EXACT: a path is returned iff a q-token appears in
        some field, i.e. the notes that can have ``lex > 0``, so ``_frontier`` can drop every
        other unreached note with byte-identical output (ADR-0012 §0a). Because it is fed the
        tokenizer's OUTPUT, it also catches tokens SYNTHESIZED from abutting link labels / kebab-tag
        splits that a raw-bytes accelerator would miss. The FTS5/ripgrep candidate accelerators are
        deferred (see :mod:`core.index_cache`): they add parity risk for no candidate-loading saving
        while every note is loaded.
        """
        idx = index_cache.build_inverted_index({n.path: n.field_tokens for n in notes})
        return index_cache.inverted_candidates(idx, q_tokens)

    @staticmethod
    def _compute_indegrees(notes: list[_Note], by_basename: dict[str, _Note]) -> None:
        """Set ``indeg`` per note: in-degree over RESOLVED outlinks (ADR-0012 §2)."""
        for n in notes:
            n.indeg = 0
        for n in notes:
            for target in n.outlinks:
                tgt = by_basename.get(target)
                if tgt is not None:
                    tgt.indeg += 1

    # --- pipeline stages 1-2 -------------------------------------------------------------------
    def _seed(
        self,
        notes: list[_Note],
        by_basename: dict[str, _Note],
        q_tokens: list[str],
    ) -> dict[str, tuple[int, set[str]]]:
        """Stage 1 SEED: navigation roots → ``{basename: (d_moc, moc_label_tokens)}``.

        Targets of a ``<domain>-moc.md`` → ``d_moc=0`` (and the MOC itself → 0). Targets of root
        ``index.md`` → ``d_moc=1`` (and ``index.md`` itself → 1). If the question contains a token
        exactly matching an in-scope ``<domain>`` name, only that domain's MOC is seeded; else all.
        """
        q_token_set = set(q_tokens)
        moc_notes = [n for n in notes if n.is_moc]
        # Domain in-scope filter (ADR-0012 §4): if the question carries a token exactly matching a
        # <domain> kebab name, seed only that domain's MOC. A kebab domain like "ai-tech" tokenizes
        # to {ai, tech} under the §3 [a-z0-9]+ alphabet, so we require ALL of the domain's tokens to
        # be present (a literal hyphenated token can never appear, so a substring match is the only
        # workable reading and keeps multi-word domains from silently disabling the optimization).
        domain_focus: str | None = None
        for n in moc_notes:
            dom = _moc_domain(n.path)
            if dom is None:
                continue
            dom_tokens = set(_tokenize(dom))
            if dom_tokens and dom_tokens <= q_token_set:
                domain_focus = dom
                break
        if domain_focus is not None:
            moc_notes = [n for n in moc_notes if _moc_domain(n.path) == domain_focus]

        seeds: dict[str, tuple[int, set[str]]] = {}

        def _add(basename: str, d_moc: int, label_tokens: set[str]) -> None:
            if basename not in by_basename:
                return
            existing = seeds.get(basename)
            if existing is None:
                seeds[basename] = (d_moc, set(label_tokens))
            else:
                ed, el = existing
                # min d_moc; union label tokens for d_moc==0 children (multi-seed attribution).
                new_d = min(ed, d_moc)
                new_labels = set(el)
                if d_moc == 0:
                    new_labels |= label_tokens
                seeds[basename] = (new_d, new_labels)

        # MOC notes themselves are d_moc=0; their linked targets are d_moc=0 children.
        for moc in moc_notes:
            _add(moc.basename, 0, set())
            for target, label_tokens in self._moc_link_labels(moc):
                _add(target, 0, label_tokens)

        # index.md itself is d_moc=1; its linked targets are d_moc=1 (only when not domain-focused,
        # since domain focus restricts seeding to that MOC — but index is a navigation root too;
        # the ADR seeds EVERY in-scope MOC plus root index. Domain focus restricts MOCs, not index).
        index_note = by_basename.get("index")
        if index_note is not None and index_note.is_index:
            _add("index", 1, set())
            for target, _label in self._moc_link_labels(index_note):
                _add(target, 1, set())

        return seeds

    @staticmethod
    def _moc_link_labels(note: _Note) -> list[tuple[str, set[str]]]:
        """Extract a MOC/index's child link targets with their link-label tokens (ADR-0014 D3).

        Since ADR-0014 D3 a MOC/index BODY child bullet is a STANDARD MARKDOWN LINK
        ``[Title](relative.md)``, so the target basename is parsed from the link PATH
        (:func:`_mdlink_target_basename`) and the label is the visible ``Title`` text — this is what
        powers ``match_reason: linked-theme`` (the child's title tokens seed the d_moc==0
        theme-token set). A stray ``[[basename]]`` wikilink in a MOC body is ALSO honored (tolerant
        read, ADR-0014 D4): its label is the ``|display`` text or the surrounding line. Returns
        target-basename → token-set pairs in document order; image / external links are skipped.
        """
        out: list[tuple[str, set[str]]] = []
        for raw_line in note.raw_lines:
            for m in _MDLINK_TARGET_RE.finditer(raw_line):
                target = _mdlink_target_basename(m.group("target"))
                if not target:
                    continue
                out.append((target, set(_tokenize(m.group("text")))))
            for m in _WIKILINK_RE.finditer(raw_line):
                inner = m.group(1)
                target = _link_target(inner)
                if not target:
                    continue
                if "|" in inner:
                    label_text = inner.split("|", 1)[1]
                else:
                    # surrounding line text, link punctuation stripped to visible labels
                    label_text = _strip_link_punctuation(raw_line)
                out.append((target, set(_tokenize(label_text))))
        return out

    def _frontier(
        self,
        notes: list[_Note],
        by_basename: dict[str, _Note],
        seeds: dict[str, tuple[int, set[str]]],
        lexical_paths: set[str],
    ) -> list[_Candidate]:
        """Stage 2 FRONTIER: BFS over the wikilink graph ∪ lexical candidates (de-duped).

        ``lexical_paths`` is the ADR-0012 §9 prefilter set (a superset of every note that can have
        ``lex > 0``). A note that is neither BFS-reached (``basename not in d_moc``) NOR lexically
        matched (``path not in lexical_paths``) is skipped: it has ``d_moc == max_hops+1`` and
        ``lex == 0``, so it can never pass the §6 gate — dropping it here is output-identical to
        building it and letting the gate drop it, only cheaper.
        """
        # BFS recording MIN hop distance as d_moc (independent of expansion order).
        d_moc: dict[str, int] = {}
        labels: dict[str, set[str]] = {}
        # Initialize from seeds.
        frontier: list[str] = sorted(seeds.keys())  # deterministic BFS order
        for b in frontier:
            d, lab = seeds[b]
            d_moc[b] = d
            labels[b] = set(lab)

        # Expand outlinks up to MAX_HOPS.
        current = sorted(seeds.keys())
        for _hop in range(MAX_HOPS):
            nxt: list[str] = []
            for b in current:
                src = by_basename.get(b)
                if src is None:
                    continue
                base_d = d_moc[b]
                if base_d >= MAX_HOPS:
                    continue
                for target in src.outlinks:
                    if target not in by_basename:
                        continue
                    nd = base_d + 1
                    if target not in d_moc or nd < d_moc[target]:
                        d_moc[target] = nd
                        labels.setdefault(target, set())
                        nxt.append(target)
                    elif target not in labels:
                        labels[target] = set()
            current = sorted(set(nxt))

        # Build candidate set: frontier ∪ notes with ≥1 q-token (q-token membership handled later;
        # here we include ALL notes that are either in the frontier OR could lexically match — we
        # include every note and let the gate drop non-matches, which is equivalent and simpler:
        # a non-frontier note unreachable within max_hops gets d_moc = MAX_HOPS+1).
        candidates: list[_Candidate] = []
        for n in notes:
            if n.basename not in d_moc and n.path not in lexical_paths:
                continue  # cannot pass the §6 gate (lex==0 AND d_moc!=0); prefilter drop.
            dm = d_moc.get(n.basename, MAX_HOPS + 1)
            lab = labels.get(n.basename, set())
            candidates.append(_Candidate(note=n, d_moc=dm, moc_label_tokens=lab))
        return candidates

    # --- match_reason / anchor / line / excerpt -------------------------------------------------
    def _assign_reason_and_extract(
        self, cand: _Candidate, q_tokens: list[str], stats: _CorpusStats
    ) -> None:
        """Assign match_reason (§6 precedence) and extract anchor/line/excerpt (§7)."""
        note = cand.note
        q_set = set(q_tokens)

        # Reason 1: linked-theme — d_moc==0 AND q intersects title/aliases/tags/MOC-label union
        # (aliases joined the theme set in the #56 addendum, mirroring the §6 gate).
        if cand.d_moc == 0:
            theme_tokens = (
                set(note.field_tokens["title"])
                | set(note.field_tokens["aliases"])
                | set(note.field_tokens["tags"])
                | cand.moc_label_tokens
            )
            if q_set & theme_tokens:
                cand.match_reason = "linked-theme"
                cand.anchor = _slug(note.title)
                cand.line = note.title_line
                cand.excerpt = self._title_excerpt(note)
                return

        # Reason 2 (heading) vs reason 3 (lexical) hinges on WHERE THE SINGLE HIGHEST-IDF matched
        # term lands (ADR-0012 §6): reason 2 holds iff that one term is in the title or a headings
        # line. A lower-idf term hitting a heading does NOT win when the top-idf term is body-only.
        top_field = self._top_matched_field(note, q_set, stats)
        if top_field in ("title", "headings"):
            cand.match_reason = "heading"
            if top_field == "headings":
                h = self._first_heading_match(note, q_set, stats)
                # _first_heading_match cannot be None here: top_field=="headings" implies a match.
                assert h is not None
                cand.anchor = h.slug
                cand.line = h.line
                cand.excerpt = self._heading_excerpt(note, h)
            else:
                # highest-idf matched term lands in the title (H1)
                cand.anchor = _slug(note.title)
                cand.line = note.title_line
                cand.excerpt = self._title_excerpt(note)
            return

        # Reason 3: lexical — match in body/tags only.
        cand.match_reason = "lexical"
        line, anchor, excerpt = self._body_match(note, q_set)
        if line is None:
            # Frontier-only candidate with no lexical body line: default to H1 slug / line 1.
            cand.anchor = _slug(note.title)
            cand.line = note.title_line
            cand.excerpt = self._title_excerpt(note)
        else:
            cand.anchor = anchor
            cand.line = line
            cand.excerpt = excerpt

    @staticmethod
    def _top_matched_field(note: _Note, q_set: set[str], stats: _CorpusStats) -> str | None:
        """Owning field of the SINGLE highest-idf matched q-token (ADR-0012 §6), or ``None``.

        Among q-tokens present in any field, pick the one with greatest IDF; ties broken by the
        field-weight order (title>aliases>tags>headings>summary>body — the #56-addendum widening;
        equal-weight pairs order title before aliases and headings before summary) of the
        highest-weight field that carries the token, then by first-occurrence line. Returns that
        winning token's owning field — the highest-weight field it occupies — which decides
        reason 2 (title/headings) vs reason 3 (aliases/summary are frontmatter, so a match landing
        there stays reason 3 unless the note is a d_moc==0 linked theme).
        """
        # Field rank for the "where the term lands" / tie-break order (lower rank = higher weight).
        field_rank = {f: i for i, f in enumerate(_FIELDS)}
        per_field = {f: set(note.field_tokens[f]) for f in field_rank}

        best_token: str | None = None
        best_idf = -1.0
        best_rank = len(field_rank)
        best_line = -1
        for t in q_set:
            owning = [f for f in field_rank if t in per_field[f]]
            if not owning:
                continue
            owner_field = min(owning, key=lambda f: field_rank[f])
            rank = field_rank[owner_field]
            idf = stats.idf.get(t, 0.0)
            line = _first_field_line(note, owner_field, t)
            cur = (-idf, rank, line)
            best = (-best_idf, best_rank, best_line)
            if best_token is None or cur < best:
                best_token = t
                best_idf = idf
                best_rank = rank
                best_line = line
        if best_token is None:
            return None
        # Map the winning rank back to its field name.
        for f, r in field_rank.items():
            if r == best_rank:
                return f
        return None  # pragma: no cover - field_rank is total

    @staticmethod
    def _first_heading_match(note: _Note, q_set: set[str], stats: _CorpusStats) -> _Heading | None:
        """Return the heading carrying the highest-idf matched q-token, else ``None``.

        Tie-break: field-weight order is already satisfied (headings only here), then
        first-occurrence line. Among matched headings we pick the one whose matched token has the
        greatest IDF; ties broken by earliest line.
        """
        best: _Heading | None = None
        best_idf = -1.0
        for h in note.headings:
            h_tokens = set(_tokenize(h.text))
            matched = q_set & h_tokens
            if not matched:
                continue
            top = max(stats.idf.get(t, 0.0) for t in matched)
            if top > best_idf or (top == best_idf and best is not None and h.line < best.line):
                best_idf = top
                best = h
        return best

    @staticmethod
    def _title_excerpt(note: _Note) -> str:
        """Title line + following non-blank body line, collapsed, ≤ EXCERPT_MAX_CHARS."""
        parts = [note.title]
        # first non-blank body line after the title line
        for i in range(note.title_line, len(note.body_lines)):
            line = note.body_lines[i].strip()
            if line and not _HEADING_RE.match(note.body_lines[i]):
                parts.append(line)
                break
        return _collapse(" ".join(parts))

    @staticmethod
    def _heading_excerpt(note: _Note, heading: _Heading) -> str:
        """Heading text + first non-empty body line beneath it, collapsed, truncated."""
        parts = [heading.text]
        for i in range(heading.line, len(note.body_lines)):
            line = note.body_lines[i].strip()
            if line and not _HEADING_RE.match(note.body_lines[i]):
                parts.append(line)
                break
        return _collapse(" ".join(parts))

    @staticmethod
    def _body_match(note: _Note, q_set: set[str]) -> tuple[int | None, str, str]:
        """Find first matched body token → (1-based line, enclosing-heading slug, excerpt window).

        Anchor = slug of the nearest enclosing heading above the first matched body line, or ``""``
        if none precedes. Returns ``(None, "", "")`` when no body line contains a q-token.
        """
        # Map line index → enclosing heading slug (nearest H2-H6 above).
        first_line: int | None = None
        first_idx = -1
        for idx, raw in enumerate(note.body_lines):
            if _HEADING_RE.match(raw):
                continue
            line_tokens = set(_tokenize(raw))
            if q_set & line_tokens:
                first_line = idx + 1  # 1-based
                first_idx = idx
                break
        if first_line is None:
            return None, "", ""

        # nearest enclosing heading above first_idx
        anchor = ""
        for h in note.headings:
            if h.line - 1 <= first_idx:
                anchor = h.slug
            else:
                break

        excerpt = _window_excerpt(note.body_lines[first_idx], q_set)
        return first_line, anchor, excerpt


def _first_field_line(note: _Note, field_name: str, token: str) -> int:
    """1-based line where ``token`` first occurs in ``field_name`` (tie-break order, ADR-0012 §6).

    ``title`` → the H1 line; ``headings`` → the first heading text containing the token; ``body`` →
    the first body line containing the token; ``tags``/``aliases``/``summary`` → frontmatter, no
    body line, so a large sentinel that sorts last (the field-weight rank already orders them).
    """
    if field_name == "title":
        return note.title_line
    if field_name == "headings":
        for h in note.headings:
            if token in set(_tokenize(h.text)):
                return h.line
        return 1 << 30  # pragma: no cover - caller guarantees a heading match
    if field_name == "body":
        for idx, raw in enumerate(note.body_lines):
            if _HEADING_RE.match(raw):
                continue
            if token in set(_tokenize(raw)):
                return idx + 1
        return 1 << 30  # pragma: no cover - caller guarantees a body match
    return 1 << 30  # tags/aliases/summary: frontmatter, no body line


def _collapse(text: str) -> str:
    """Whitespace-collapse to a single line and truncate to EXCERPT_MAX_CHARS."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) > EXCERPT_MAX_CHARS:
        collapsed = collapsed[:EXCERPT_MAX_CHARS]
    return collapsed


def _window_excerpt(line: str, q_set: set[str]) -> str:
    """±EXCERPT_WINDOW_TOKENS token window around the first matched term on ``line``.

    The window is sliced from the ORIGINAL line text (preserving case and intra-token punctuation
    such as ``per-repo``/``compare-and-swap``) so body excerpts read naturally and stay consistent
    with the case-preserving heading/title excerpts. Matching still uses the §3 lowercased token
    identity. ``str.lower()`` is length-preserving for the ASCII text we slice, so the spans the
    regex reports over the lowered line index the original line directly.
    """
    spans = [(m.start(), m.end(), m.group()) for m in _TOKEN_RE.finditer(line.lower())]
    if not spans:
        return _collapse(line)
    hit = -1
    for i, (_s, _e, tok) in enumerate(spans):
        if tok in q_set and tok not in STOPWORDS:
            hit = i
            break
    if hit == -1:
        return _collapse(line)
    lo = max(0, hit - EXCERPT_WINDOW_TOKENS)
    hi = min(len(spans), hit + EXCERPT_WINDOW_TOKENS + 1)
    start = spans[lo][0]
    end = spans[hi - 1][1]
    return _collapse(line[start:end])


@dataclass
class _CorpusStats:
    """Repo-wide BM25F statistics: per-field ``avgdl``, per-term IDF, and ``max_indeg``."""

    avgdl: dict[str, float]
    idf: dict[str, float]
    n_docs: int
    max_indeg: int

    @classmethod
    def build(cls, notes: list[_Note]) -> _CorpusStats:
        n = len(notes)
        fields = _FIELDS
        len_sum: dict[str, int] = dict.fromkeys(fields, 0)
        df: dict[str, int] = {}
        for note in notes:
            present: set[str] = set()
            for f in fields:
                toks = note.field_tokens[f]
                len_sum[f] += len(toks)
                present.update(toks)
            for t in present:
                df[t] = df.get(t, 0) + 1
        avgdl = {f: (len_sum[f] / n if n else 0.0) for f in fields}
        idf = {t: max(0.0, math.log(1 + (n - dft + 0.5) / (dft + 0.5))) for t, dft in df.items()}
        max_indeg = max((note.indeg for note in notes), default=0)
        return cls(avgdl=avgdl, idf=idf, n_docs=n, max_indeg=max_indeg)


def _lexical(note: _Note, q_set: list[str], stats: _CorpusStats) -> float:
    """BM25F lexical score normalized to [0,1) via the pivot (ADR-0012 §4)."""
    fields = _FIELDS
    # per-field term frequencies and lengths
    tf: dict[str, dict[str, int]] = {}
    len_f: dict[str, int] = {}
    for f in fields:
        toks = note.field_tokens[f]
        len_f[f] = len(toks)
        counts: dict[str, int] = {}
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
        tf[f] = counts

    raw = 0.0
    for t in q_set:  # iterate in sorted order (caller passes sorted set)
        # ftd(t) = sum_f w_f * tf(t,f) / (1 - b + b*len_f/avgdl_f)
        ftd = 0.0
        for f in fields:
            tf_tf = tf[f].get(t, 0)
            if tf_tf == 0:
                continue
            avgdl_f = stats.avgdl[f]
            b = FIELD_B[f]
            denom = 1.0 if avgdl_f == 0 else (1 - b + b * len_f[f] / avgdl_f)
            ftd += FIELD_WEIGHTS[f] * tf_tf / denom
        if ftd <= 0:
            continue
        idf = stats.idf.get(t, 0.0)
        raw += idf * ftd * (K1 + 1) / (ftd + K1)
    if raw <= 0:
        return 0.0
    return raw / (raw + PIVOT)


def _structural(d_moc: int, indeg: int, max_indeg: int) -> float:
    """Degree-surrogate structural score (ADR-0012 §5)."""
    indeg_norm = indeg / max(1, max_indeg)
    return STRUCT_ALPHA * (1.0 / (1 + d_moc)) + STRUCT_BETA * indeg_norm


def _combined(lex: float, struct: float, fm: float) -> float:
    """Combined [0,1] score (ADR-0012 §5, amended for issue #146).

    **Structure amplifies lexical evidence; it never substitutes for it.** A candidate with no
    lexical match contributes no structural term, so it can score at most its frontmatter boost
    (+0.10 for ``active``) — below ``FLOOR``.

    Why this is a correction and not a new policy: ADR-0012 §6 names the admission check a
    *mandatory lexical-evidence* gate, but ``_passes_gate``'s second branch admits a ``d_moc == 0``
    note whose only overlap is with ``moc_label_tokens`` — the MOC's LINK TEXT, which is not one of
    the note's own scoring ``_FIELDS``. Such a candidate reaches here with ``lex == 0``, and used to
    take the full structural term:

        0.65*0 + 0.35*1.0 + 0.0 = 0.35  >  FLOOR = 0.18

    so an empty husk surfaced as a hit. That is issue #146, measured on the live corpus as empty MOC
    stubs beating the correct answer in 3 of 4 queries. Regression:
    ``tests/core/test_wiki_lexical_evidence_146.py``.

    Blast radius is exactly the ``lex == 0`` set: any candidate with lexical evidence scores
    byte-identically to before.

    What this does NOT reach: a husk's mere EXISTENCE can still flip an honest ``not_found`` to
    ``ok``, because ``moc_label_tokens`` comes from the MOC's *body* link labels — so the bullet
    pointing at the husk puts its topic words into the MOC's own scoring fields. Whatever admits the
    husk therefore admits the MOC too. That is the thin-page half of #146 and needs a content/length
    prior; it is pinned as a ``strict=True`` xfail in the regression module.
    """
    struct_term = W_STRUCT * struct if lex > 0 else 0.0
    return max(0.0, min(1.0, W_LEX * lex + struct_term + fm))


def _fm(status: str) -> float:
    """Frontmatter/status boost (ADR-0012 §8; LIVE since the #56 addendum flipped Phase-1b on).

    ``active`` promotes (+0.10), ``deprecated`` demotes (−0.15); ``stub``/``contested``/absent/
    unknown stay neutral (0.0) — an un-triaged note is never auto-promoted above a curated one.
    """
    if not FM_ENABLED:
        return 0.0
    if status == "active":
        return 0.10
    if status == "deprecated":
        return -0.15
    return 0.0


def _passes_gate(cand: _Candidate, q_tokens: list[str]) -> bool:
    """Mandatory lexical-evidence gate (ADR-0012 §6; theme set widened by the #56 addendum).

    The d_moc==0 theme-token set includes the note's ``aliases`` tokens (an alias IS an alternate
    title, so a linked theme reachable by its alias passes exactly as one reachable by its title).
    """
    if cand.lex > 0:
        return True
    if cand.d_moc == 0:
        q_set = set(q_tokens)
        theme = set(
            cand.moc_label_tokens
            | set(cand.note.field_tokens["tags"])
            | set(cand.note.field_tokens["title"])
            | set(cand.note.field_tokens["aliases"])
        )
        if q_set & theme:
            return True
    return False


def _order_key(c: _Candidate) -> tuple[float, int, float, int, str]:
    """Total-order sort key (ADR-0012 §7): no ties survive thanks to the (repo, path) tail."""
    return (-c.score, _REASON_RANK[c.match_reason], -c.lex, -c.note.indeg, c.note.path)


# --- derived reader-cache builder (ADR-0012 §2, issue #26) --------------------------------------


@dataclass(frozen=True)
class IndexBuildResult:
    """Outcome of a deterministic reader-cache build (ADR-0012 §2, issue #26)."""

    note_count: int
    curated_commit: str
    notes_path: Path


def build_cache(repo: Repo) -> IndexBuildResult:
    """Deterministically (re)build the ADR-0012 §2 reader cache for ``repo`` from its markdown.

    Reader/worker-class code ONLY — never the sandboxed backend, never in the ADR-0008 INGEST
    allowlist (invariant #2 / C10). Parses ``index.md`` + ``wiki/**`` from the owner tree and
    writes ``_kb/index/<repo>.notes.json`` (per-file ``source_digest`` + serialized parse with
    ``indeg`` excluded), stamped with the curated tip sha. Raises :class:`GitError` if the repo
    has no curated tip to stamp. The build is independent of ``index.enabled`` (that flag gates the
    READ path); callers that should honor the kill-switch check it before calling (the worker does;
    ``agora index build`` is an explicit act).

    Raises :class:`~agora_kb.config.ConfigError` when the repo directory name is not a safe filename
    component (``My Knowledge``, ``내지식``, …) and so cannot address a cache file — issue #108. The
    cache path is resolved FIRST, before any note is parsed, and the raise is TRANSLATED from the
    layout guard's :class:`InvalidWriterError` into the error class every caller already handles:
    ``agora index build`` prints it as a one-line cause+remedy, the curator's
    ``rebuild_index_cache`` degrades to the ``index_cache_unbuilt`` signal. This is the WRITE-side
    counterpart of the read path's silent no-cache fallback (:meth:`Wiki._usable_cache_entries`) —
    both sides agree that no cache exists (neither invents a stem, so they can never address
    DIFFERENT files), but a build that produced nothing must SAY so rather than report success.
    """
    from ..config import ConfigError

    layout = repo.layout
    try:
        notes_path = layout.index_notes_path()
    except InvalidWriterError as exc:
        raise ConfigError(f"no reader cache was written — {exc}") from exc
    commit = repo.branch_commit()
    wiki = Wiki(layout)
    notes_payload: dict[str, dict] = {}
    count = 0
    for rel, basename, is_index, path in wiki._iter_note_files():
        text = _read_tolerant(path)
        note = _parse_note(path=rel, basename=basename, is_index=is_index, raw_text=text)
        notes_payload[rel] = {"sha": index_cache.source_digest(text), "note": _note_to_dict(note)}
        count += 1
    payload = index_cache.CachePayload(
        cache_schema_version=index_cache.CACHE_SCHEMA_VERSION,
        curated_commit=commit,
        notes=notes_payload,
    )
    index_cache.write_payload(notes_path, payload)
    return IndexBuildResult(note_count=count, curated_commit=commit, notes_path=notes_path)
