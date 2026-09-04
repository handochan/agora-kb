"""Derived reader-cache mechanics for the deterministic query path (ADR-0012 §2, issue #26).

This module holds the *mechanics* of the ``_kb/index/`` reader cache — the on-disk parsed-note
payload, its content-addressed digest, and the exact in-memory inverted-index candidate prefilter.
It is deliberately free of any :mod:`core.wiki` ``_Note`` knowledge — it operates on plain dicts and
token lists — so ``core.wiki`` can import it without an import cycle (``core.wiki`` owns the note
model + parsing; this module owns the cache serialization + candidate selection).

Invariants honored (ADR-0012 §2, invariants #1/#2):

* The cache is git-ignored, NEVER canonical, and fully rebuildable from the markdown at the curated
  commit. The whole-cache validity gate is ``(curated_commit, cache_schema_version)``; the per-file
  gate is a ``source_digest`` over the EXACT tolerant-decoded text the parser consumes.
* ``source_digest`` is NOT :func:`core.hashing.content_sha256`: that function normalizes
  NFC/CRLF/trailing-whitespace for content-identity dedup, which would wrongly equate two
  byte-divergent notes that PARSE differently (e.g. CRLF vs LF changes ``raw_lines`` and therefore
  the MOC link-label extraction and excerpt). The cache reuse gate must be a strict refinement of
  content identity, so it digests the exact parser input.
* The candidate prefilter (the inverted index) is EXACT — it returns precisely the notes that carry
  a query token in some field, i.e. exactly the notes that can have ``lex > 0`` (ADR-0012 §4). The
  pure-Python BM25F scorer in ``core.wiki`` still rescans and scores, so the prefilter changes only
  which notes are scored, never any ``SearchHit`` field.

Deferred (ADR-0012 §9, issue #28 scale): the OPTIONAL SQLite-FTS5 and ripgrep *candidate*
prefilters. In the current architecture the scorer loads every note (repo-wide IDF), so building the
inverted index from the already-loaded ``field_tokens`` is both exact AND free — an
over-approximating external accelerator offers no candidate-loading saving here and only adds parity
risk (ripgrep cannot see tokens SYNTHESIZED by the tokenizer, e.g. abutting link labels, so it
under-approximates; a committed-snapshot FTS DB under-approximates against a diverged working tree).
They belong to a future reader that avoids loading all notes; wiring them now would be speculative.

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
    "build_inverted_index",
    "inverted_candidates",
]

# Bump on ANY change to the serialized note shape, the tokenizer, or the parser
# (see the module docstring).
# 2: issue #56 (ADR-0012 addendum) — NFC + CJK-bigram tokenizer, and aliases/summary joined
#    field_tokens; a v1 cache carries stale derived tokens, so the whole cache is invalidated.
# 3: issue #153 (ADR-0041 D5) — KB wiki schema 2 flips the wiki axis, and BOTH bump triggers fire.
#    ``is_moc`` is PARSER-COMPUTED and serialized (``_parse_note`` sets it from the path predicate,
#    which now reads ``wiki/maps/…`` instead of ``wiki/<domain>/<domain>-moc.md``), so a v2 entry
#    whose ``source_digest`` still matches would carry a stale map verdict forever; and the
#    serialized ``_Note`` shape GAINS ``kind`` + ``subjects``. The fresh-repo argument does not
#    cover this: ``SUPPORTED_KB_SCHEMA_VERSIONS`` is ``{1, 2}``, so a schema-1 repo with a
#    populated ``_kb/index/`` is reachable by a schema-2 build — exactly the stale-cache case.
CACHE_SCHEMA_VERSION = 3


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
    §2). The caller still checks ``curated_commit`` against the live curated tip. NOTE: this
    validates only the entry ENVELOPE (``{"sha": str, "note": dict}``); a structurally-corrupt inner
    ``note`` dict is caught defensively at reuse time in ``core.wiki._load_notes`` (which re-parses
    that one file rather than crashing the query).
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
    # Validate each entry's envelope: {"sha": str, "note": dict}. A malformed envelope voids the
    # cache (fall back to scan) rather than risking a partial/mis-typed reuse.
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


# --- in-memory inverted index (the exact candidate prefilter) -----------------------------------


def build_inverted_index(
    field_tokens_by_path: dict[str, dict[str, list[str]]],
) -> dict[str, set[str]]:
    """Build ``token -> {paths}`` from every note's per-field tokens (EXACT candidate selector).

    A path is indexed under a token iff that token appears in ANY of the note's scoring fields
    (the six-field ``_FIELDS`` set since the #56 addendum) — the exact condition under which the
    note can have ``lex > 0`` (ADR-0012 §4), so the resulting
    candidate set is precisely the set of notes that could pass the lexical branch of the §6 gate.
    Because it is fed the tokenizer's OUTPUT (including tokens synthesized from abutting link labels
    or kebab-tag splits), it is exact where a raw-bytes accelerator would under-approximate.
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
