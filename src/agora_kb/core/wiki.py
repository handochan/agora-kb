"""Deterministic, model-free read path for a knowledge repo (ADR-0012, refining ADR-0009).

``core.wiki`` is the ONLY component that computes a :class:`SearchHit` field. It is a pure-Python
stdlib scorer — the oracle and test reference — so retrieval is fully deterministic and reproducible
without any model, sqlite, or external dependency. FTS5 / ripgrep are (per the ADR) prefilter-only
accelerators that may over-approximate the candidate set but never change output; this Phase-1a
implementation does not wire them, and a full pure-Python scan is always the source of truth.

The algorithm is the LEXICAL-UNION FRONTIER with a single pure-Python scorer (ADR-0012 §4-§7):

1. **SEED** navigation roots from ``index.md`` + every in-scope ``<domain>-moc.md``.
2. **FRONTIER** = BFS over the ``[[wikilink]]`` graph (``max_hops=2``) UNION lexical candidates.
3. **TOKENIZE** the question; an all-stopword question yields ``not_found`` immediately.
4. **LEXICAL** BM25F over {title, tags, headings, body} with repo-wide IDF.
5. **STRUCTURAL** degree surrogate ``alpha/(1+d_moc) + beta*indeg_norm`` (no iterative PageRank).
6. **COMBINE** ``w_lex*lex + w_struct*struct + fm`` (``fm=0`` in Phase-1a) behind a mandatory
   lexical-evidence gate that drops structurally-strong but lexically-empty notes.
7. **ORDER** by a total order (no ties survive) and truncate to ``limit``.

Float determinism: ``lex``/``struct``/``fm`` are each rounded to 6 decimals, then combined in the
fixed order ``w_lex*lex_r + w_struct*struct_r + fm_r``, clamped to ``[0,1]`` and rounded to 6
decimals (ADR-0012 §6.1). The absolute ``(repo, path)`` tie-break absorbs residual sub-ULP equality.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import frontmatter
from .layout import RepoLayout

__all__ = ["SearchHit", "QueryResult", "Wiki"]

# --- frozen configuration (ADR-0012 §1 defaults; normative for tests) ---------------------------
K1 = 1.2  # BM25 term-freq saturation
B = 0.75  # BM25 length normalization
FIELD_WEIGHTS: dict[str, float] = {"title": 3.0, "tags": 2.5, "headings": 2.0, "body": 1.0}
PIVOT = 1.5  # lexical normalization pivot: lex = raw / (raw + pivot)
W_LEX = 0.65  # combined-score weight on lexical
W_STRUCT = 0.35  # combined-score weight on structural
STRUCT_ALPHA = 0.7  # structural: weight on MOC-distance term
STRUCT_BETA = 0.3  # structural: weight on in-degree term
FM_ENABLED = False  # PHASE-1a: fm=0 for ALL notes (flips true in Phase-1b with schema emitter+LINT)
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
# [[target|label]] or [[target#anchor]] or [[target]] wikilink.
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
# [label](url) markdown link.
_MDLINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# A token of the §3 tokenizer alphabet.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """The single shared tokenizer (ADR-0012 §3): lowercased ``[a-z0-9]+`` minus stopwords."""
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


def _tokenize_tags(tags: tuple[str, ...]) -> list[str]:
    """Tokenize tags with kebab expansion (ADR-0012 §3).

    A HYPHENATED kebab tag ``single-writer`` contributes the full token ``single-writer`` AND its
    split parts ``single``, ``writer``. A PLAIN single-word tag ``inbox`` contributes its single
    token ONCE (the full form and the split part coincide, so emitting both would double-count
    ``tf(t, 'tags')`` and inflate ``len_f['tags']``/``avgdl``, distorting BM25F — ADR-0012 §3).
    """
    out: list[str] = []
    for tag in tags:
        lowered = tag.lower()
        # Split parts under the [a-z0-9]+ alphabet (e.g. "single-writer" -> ["single", "writer"]).
        parts = _TOKEN_RE.findall(lowered)
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
    """GitHub/Obsidian-compatible slug (ADR-0012 §6)."""
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
                    n = slug_counts.get(base_slug, 0)
                    slug_counts[base_slug] = n + 1
                    slug = base_slug if n == 0 else f"{base_slug}-{n}"
                    headings.append(_Heading(level=level, text=text, slug=slug, line=idx))
        # outlinks are collected from the FULL body (links inside fences are rare; the ADR walks
        # the wikilink graph over the note's links — we collect from non-fence lines to match the
        # body-text model, but wikilinks are content, so scan every line for link targets).
        for m in _WIKILINK_RE.finditer(line):
            target = _link_target(m.group(1))
            if target and target not in seen_targets:
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

    # field tokens (ADR-0012 §2/§3). headings appear in BOTH headings and body (double-count).
    heading_text = " ".join(h.text for h in headings)
    body_text = " ".join(body_lines)
    field_tokens = {
        "title": _tokenize(title),
        "tags": _tokenize_tags(tags),
        "headings": _tokenize(heading_text),
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
    anchor: str  # heading/wikilink slug; MAY be "" for a pre-heading lexical match
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
        """
        notes = self._load_notes()
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
        candidates = self._frontier(notes, by_basename, seeds)

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
            combined = W_LEX * cand.lex + W_STRUCT * cand.struct + cand.fm
            cand.score = round(max(0.0, min(1.0, combined)), 6)
            self._assign_reason_and_extract(cand, q_tokens, stats)
            eligible.append(cand)

        # not_found gates (b) zero eligible, (c) best < floor.
        if not eligible:
            return QueryResult(query=question, status="not_found", hits=())
        best = max(c.score for c in eligible)
        if best < FLOOR:
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

    # --- note loading --------------------------------------------------------------------------
    def _load_notes(self) -> list[_Note]:
        """Parse ``index.md`` + every ``wiki/**/*.md`` into notes, in sorted path order."""
        notes: list[_Note] = []
        root = self.layout.root
        index_path = self.layout.index_file
        if index_path.is_file():
            notes.append(
                _parse_note(
                    path="index.md",
                    basename="index",
                    is_index=True,
                    raw_text=index_path.read_text(encoding="utf-8"),
                )
            )
        wiki_dir = self.layout.wiki_dir
        if wiki_dir.is_dir():
            md_files = sorted(
                (p for p in wiki_dir.rglob("*.md") if p.is_file()),
                key=lambda p: p.relative_to(root).as_posix(),
            )
            for p in md_files:
                rel = p.relative_to(root).as_posix()
                notes.append(
                    _parse_note(
                        path=rel,
                        basename=Path(rel).stem,
                        is_index=False,
                        raw_text=p.read_text(encoding="utf-8"),
                    )
                )
        return notes

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
        """Extract ``[[basename]]`` targets of a MOC/index with their link-label tokens.

        The label is the visible ``[[basename|label]]`` text if present, else the surrounding
        list-item / line text (with link punctuation stripped). Returns target-basename →
        token-set pairs in document order.
        """
        out: list[tuple[str, set[str]]] = []
        for raw_line in note.raw_lines:
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
    ) -> list[_Candidate]:
        """Stage 2 FRONTIER: BFS over the wikilink graph ∪ lexical candidates (de-duped)."""
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

        # Reason 1: linked-theme — d_moc==0 AND q intersects title/tags/MOC-label union.
        if cand.d_moc == 0:
            theme_tokens = (
                set(note.field_tokens["title"])
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
        field-weight order (title>tags>headings>body) of the highest-weight field that carries the
        token, then by first-occurrence line. Returns that winning token's owning field — the
        highest-weight field it occupies — which decides reason 2 (title/headings) vs reason 3.
        """
        # Field rank for the "where the term lands" / tie-break order (lower rank = higher weight).
        field_rank = {"title": 0, "tags": 1, "headings": 2, "body": 3}
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
    the first body line containing the token; ``tags`` → frontmatter, no body line, so a large
    sentinel that sorts last (the field-weight rank already orders tags ahead of headings/body).
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
    return 1 << 30  # tags: no line


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
        fields = ("title", "tags", "headings", "body")
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
    fields = ("title", "tags", "headings", "body")
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
            denom = 1.0 if avgdl_f == 0 else (1 - B + B * len_f[f] / avgdl_f)
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


def _fm(status: str) -> float:
    """Frontmatter/status boost. Phase-1a: always 0.0 (``fm_enabled=false``)."""
    if not FM_ENABLED:
        return 0.0
    if status == "active":  # pragma: no cover - Phase-1b
        return 0.10
    if status == "deprecated":  # pragma: no cover - Phase-1b
        return -0.15
    return 0.0


def _passes_gate(cand: _Candidate, q_tokens: list[str]) -> bool:
    """Mandatory lexical-evidence gate (ADR-0012 §6)."""
    if cand.lex > 0:
        return True
    if cand.d_moc == 0:
        q_set = set(q_tokens)
        theme = set(
            cand.moc_label_tokens
            | set(cand.note.field_tokens["tags"])
            | set(cand.note.field_tokens["title"])
        )
        if q_set & theme:
            return True
    return False


def _order_key(c: _Candidate) -> tuple[float, int, float, int, str]:
    """Total-order sort key (ADR-0012 §7): no ties survive thanks to the (repo, path) tail."""
    return (-c.score, _REASON_RANK[c.match_reason], -c.lex, -c.note.indeg, c.note.path)
