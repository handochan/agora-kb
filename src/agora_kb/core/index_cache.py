"""Derived reader-cache mechanics for the deterministic query path (ADR-0012 §2/§9, issue #26).

This module holds the *mechanics* of the ``_kb/index/`` reader cache — the on-disk parsed-note
payload, its content-addressed digest, and the three interchangeable candidate PREFILTERs
(in-memory inverted index, SQLite FTS5, ripgrep). It is deliberately free of any :mod:`core.wiki`
``_Note`` knowledge — it operates on plain dicts and token lists — so ``core.wiki`` can import it
without an import cycle (``core.wiki`` owns the note model + parsing; this module owns the cache
serialization + accelerators).

Invariants honored (ADR-0012 §2, invariants #1/#2):

* The cache is git-ignored, NEVER canonical, and fully rebuildable from the markdown at the curated
  commit. The whole-cache validity gate is ``(curated_commit, cache_schema_version)``; the per-file
  gate is a ``source_digest`` over the EXACT tolerant-decoded text the parser consumes.
* ``source_digest`` is NOT :func:`core.hashing.content_sha256`: that function normalizes
  NFC/CRLF/trailing-whitespace for content-identity dedup, which would wrongly equate two
  byte-divergent notes that PARSE differently (e.g. CRLF vs LF changes ``raw_lines`` and therefore
  the MOC link-label extraction and excerpt). The cache reuse gate must be a strict refinement of
  content identity, so it digests the exact parser input.
* Candidate prefilters may only OVER-approximate the set of notes that could pass the §6 gate; the
  pure-Python BM25F scorer in ``core.wiki`` rescans and scores, so a prefilter changes only speed,
  never output (ADR-0012 §0a/§9). ``bm25()``/``snippet()`` are never used — FTS5 is a ``MATCH``
  path-selector only.

``CACHE_SCHEMA_VERSION`` MUST be bumped whenever the serialized ``_Note`` shape, the tokenizer, or
the parser changes — the cached ``field_tokens``/``headings``/``outlinks`` are DERIVED values, so a
parser change must invalidate a cache whose per-file ``source_digest`` still matches (ADR-0012 §1
scoring constants affect scores, which are NEVER cached, so they need not bump this).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .atomicio import fsync_dir

__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CachePayload",
    "source_digest",
    "serialize_payload",
    "read_payload",
    "write_payload",
    "probe_fts5",
    "norm_tokens_row",
    "build_fts5",
    "fts5_candidates",
    "ripgrep_available",
    "ripgrep_candidates",
    "build_inverted_index",
    "inverted_candidates",
]

# Bump on ANY change to the serialized note shape, the tokenizer, or the parser
# (see the module docstring).
CACHE_SCHEMA_VERSION = 1

# FTS5 field order for the space-joined norm_tokens row (fixed for determinism;
# MATCH is order-free).
_FTS_FIELDS = ("title", "tags", "headings", "body")


def source_digest(text: str) -> str:
    """Hex SHA-256 of the EXACT parser input text (ADR-0012 §2 per-file gate, issue #26).

    ``text`` is the tolerant-decoded (``errors="replace"``) file content that ``_parse_note``
    consumes. This is a strict refinement of :func:`core.hashing.content_sha256` (which normalizes
    for dedup): equal ``source_digest`` implies identical parser input, hence an identical note,
    so reusing a cached parse on a match is safe; any byte difference that could change the parse
    (CRLF↔LF, trailing whitespace, NFC↔NFD) yields a different digest and forces a re-parse.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachePayload:
    """The parsed-note cache read from / written to ``_kb/index/<repo>.notes.json`` (ADR-0012 §2).

    ``notes`` maps a repo-relative POSIX path to ``{"sha": <source_digest>, "note": <serialized
    _Note minus indeg>}``. ``indeg`` and all scores are NEVER stored — they are recomputed globally
    at load, so a partial cache is byte-identical to a full scan (a stored ``indeg`` would be a
    stale-global bug).
    """

    cache_schema_version: int
    curated_commit: str
    notes: dict[str, dict]


def serialize_payload(payload: CachePayload) -> str:
    """Serialize a :class:`CachePayload` to canonical, byte-identical-across-clones JSON text.

    ``sort_keys=True`` + POSIX-path-keyed ``notes`` + a payload carrying NO floats (scores/idf are
    excluded) make two clones of the same commit produce byte-identical bytes (invariant #1).
    ``ensure_ascii=False`` keeps non-ASCII note text compact and stable; a trailing ``\\n`` matches
    the repo's LF text convention.
    """
    doc = {
        "cache_schema_version": payload.cache_schema_version,
        "curated_commit": payload.curated_commit,
        "notes": payload.notes,
    }
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def read_payload(path: Path) -> CachePayload | None:
    """Read + structurally validate the cache at ``path``; return ``None`` on ANY problem.

    Returns ``None`` (never raises) when the file is absent, unreadable/locked, not valid JSON, has
    the wrong shape, or carries a ``cache_schema_version`` != :data:`CACHE_SCHEMA_VERSION`. The read
    path treats ``None`` as "no usable cache" and falls back to a full pure-Python scan (ADR-0012
    §2). The caller still checks ``curated_commit`` against the live curated tip.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    version = doc.get("cache_schema_version")
    commit = doc.get("curated_commit")
    notes = doc.get("notes")
    if not isinstance(version, int) or version != CACHE_SCHEMA_VERSION:
        return None
    if not isinstance(commit, str) or not isinstance(notes, dict):
        return None
    # Validate each entry's shape: {"sha": str, "note": dict}. A malformed entry voids the cache
    # (fall back to scan) rather than risking a partial/mis-typed reuse.
    for value in notes.values():
        if not isinstance(value, dict):
            return None
        if not isinstance(value.get("sha"), str) or not isinstance(value.get("note"), dict):
            return None
    return CachePayload(cache_schema_version=version, curated_commit=commit, notes=notes)


def write_payload(dest: Path, payload: CachePayload) -> None:
    """Atomically write ``payload`` to ``dest`` (temp + ``os.replace`` + directory fsync).

    ``dest``'s parent is created if needed (``RepoLayout`` creates no directories). The write is a
    same-filesystem atomic swap so a concurrent reader sees whole-old-or-whole-new bytes; writers
    at the same commit produce byte-identical content (last-writer-wins is harmless).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(serialize_payload(payload))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    fsync_dir(dest.parent)


# --- in-memory inverted index (the exact, zero-extra-IO default prefilter) ----------------------


def build_inverted_index(
    field_tokens_by_path: dict[str, dict[str, list[str]]],
) -> dict[str, set[str]]:
    """Build ``token -> {paths}`` from every note's per-field tokens (EXACT candidate selector).

    A path is indexed under a token iff that token appears in ANY of the note's four fields — the
    exact condition under which the note can have ``lex > 0`` (ADR-0012 §4), so the resulting
    candidate set is precisely the set of notes that could pass the lexical branch of the §6 gate.
    """
    index: dict[str, set[str]] = {}
    for path, field_tokens in field_tokens_by_path.items():
        seen: set[str] = set()
        for toks in field_tokens.values():
            seen.update(toks)
        for tok in seen:
            index.setdefault(tok, set()).add(path)
    return index


def inverted_candidates(index: dict[str, set[str]], q_tokens: list[str]) -> set[str]:
    """Union of the inverted-index posting lists for the distinct ``q_tokens``."""
    out: set[str] = set()
    for tok in set(q_tokens):
        out |= index.get(tok, set())
    return out


# --- SQLite FTS5 prefilter (optional; ADR-0012 §9 — MATCH path-selector only) --------------------

_fts5_probe: bool | None = None


def probe_fts5() -> bool:
    """Return ``True`` iff this CPython's bundled SQLite supports FTS5 (probed once, memoized)."""
    global _fts5_probe
    if _fts5_probe is not None:
        return _fts5_probe
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE _probe USING fts5(x, tokenize='ascii')")
        finally:
            con.close()
        _fts5_probe = True
    except sqlite3.Error:
        _fts5_probe = False
    return _fts5_probe


def norm_tokens_row(field_tokens: dict[str, list[str]]) -> str:
    """Space-join a note's four fields' tokens in fixed order (the FTS5 ``norm_tokens`` cell)."""
    parts: list[str] = []
    for f in _FTS_FIELDS:
        parts.extend(field_tokens.get(f, ()))
    return " ".join(parts)


def build_fts5(dest: Path, rows: list[tuple[str, str]], *, curated_commit: str) -> None:
    """Build the FTS5 prefilter DB at ``dest`` from ``(path, norm_tokens)`` rows (sorted by caller).

    The table is populated with the Python tokenizer's output (``tokenize='ascii'``) so the FTS5 and
    pure-Python vocabularies match by construction (ADR-0012 §2). A ``meta`` row stamps the
    ``curated_commit`` the DB was built at so the reader can reject a DB left stale by a partial
    rebuild (notes.json refreshed but this file not). Built via a temp file + ``os.replace`` so
    a concurrent reader never sees a half-built DB. The DB's exact bytes need not be deterministic —
    only its ``MATCH`` results are (and those are, since rows are inserted in sorted path order).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
    tmp.unlink(missing_ok=True)
    try:
        con = sqlite3.connect(str(tmp))
        try:
            con.execute(
                "CREATE VIRTUAL TABLE notes USING fts5("
                "norm_tokens, path UNINDEXED, tokenize='ascii')"
            )
            con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            con.execute(
                "INSERT INTO meta(key, value) VALUES ('curated_commit', ?)", (curated_commit,)
            )
            con.executemany(
                "INSERT INTO notes(norm_tokens, path) VALUES (?, ?)",
                [(norm, path) for path, norm in rows],
            )
            con.commit()
        finally:
            con.close()
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    fsync_dir(dest.parent)


def fts5_candidates(dest: Path, q_tokens: list[str], *, expected_commit: str) -> set[str] | None:
    """Return the set of note paths matching any ``q_token`` via FTS5 ``MATCH``, else ``None``.

    Returns ``None`` (fall back to another prefilter / scan) when the DB is absent, its stamped
    ``curated_commit`` != ``expected_commit`` (stale — a partial rebuild), or any SQLite/OS error
    occurs. Opened read-only + immutable so the reader takes no lock and is unaffected by a
    concurrent writer's ``os.replace``. The MATCH expression is an OR of PHRASE-QUOTED distinct
    tokens (``"a" OR "b"``) — phrase-quoting defangs FTS5 operator injection; ``q_tokens`` are
    ``[a-z0-9]+`` so they can never contain a quote. ``bm25()``/``snippet()`` are never used.
    """
    tokens = sorted({t for t in q_tokens if t})
    if not tokens:
        return set()
    if not dest.is_file():
        return None
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"{dest.as_uri()}?mode=ro&immutable=1", uri=True)
        stamped = con.execute("SELECT value FROM meta WHERE key = 'curated_commit'").fetchone()
        if stamped is None or stamped[0] != expected_commit:
            return None
        expr = " OR ".join(f'"{t}"' for t in tokens)
        cur = con.execute("SELECT path FROM notes WHERE notes MATCH ?", (expr,))
        return {row[0] for row in cur.fetchall()}
    except (sqlite3.Error, OSError):
        return None
    finally:
        if con is not None:
            con.close()


# --- ripgrep prefilter (optional; ADR-0012 §9 — file-content candidate selector) ----------------

_rg_probe: str | None | bool = False  # False = not yet probed; str path or None after probing


def ripgrep_available() -> bool:
    """Return ``True`` iff a ripgrep binary is on ``PATH`` (probed once, memoized)."""
    global _rg_probe
    if _rg_probe is False:
        _rg_probe = shutil.which("rg")
    return _rg_probe is not None


def ripgrep_candidates(wiki_dir: Path, index_file: Path, q_tokens: list[str]) -> set[str] | None:
    """Return repo-relative POSIX paths of ``*.md`` files whose CONTENT matches any ``q_token``.

    ``rg -F -S -g '*.md' --files-with-matches -e <t1> -e <t2> ...`` over ``wiki_dir`` + ``index.md``
    (multiple ``-e`` fixed strings = OR = the per-token union, ADR-0012 §9). Fixed-string +
    smart-case makes this a SUPERSET of the tokenizer matches for content-borne fields (it also
    matches inside frontmatter/code/URLs the tokenizer drops — harmless over-approximation).

    NOTE (caller duty): a note whose title is derived from its BASENAME (no H1, no frontmatter
    ``title:``) has title tokens absent from the file text, so ripgrep alone is NOT a superset — the
    caller (``core.wiki._lexical_candidate_paths``) unions this with the exact title-token matches
    from the loaded ``field_tokens``. Returns ``None`` on any error so the caller falls back.
    """
    tokens = sorted({t for t in q_tokens if t})
    if not tokens:
        return set()
    root = wiki_dir.parent
    targets: list[str] = []
    if wiki_dir.is_dir():
        targets.append(str(wiki_dir))
    if index_file.is_file():
        targets.append(str(index_file))
    if not targets:
        return set()
    cmd = ["rg", "-F", "-S", "-g", "*.md", "--files-with-matches"]
    for t in tokens:
        cmd += ["-e", t]
    cmd += ["--", *targets]
    try:
        cp = subprocess.run(  # noqa: S603 — argv list, no shell.
            cmd, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    # rg exit codes: 0 = matches, 1 = no matches (not an error), 2 = real error.
    if cp.returncode not in (0, 1):
        return None
    out: set[str] = set()
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rel = Path(line).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        out.add(rel)
    return out
