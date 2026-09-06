"""KB wiki schema 1 → schema 2 CONVERTER — the ``agora import --from-kb`` op (ADR-0041 D6).

D6 is a **clean break**: there is no in-place migrator, no dual-layout reader and no compatibility
shim. ``agora import --from-kb <v1-repo> <dest>`` is the *only* crossing, and it is a CONVERTER —
it reads a schema-1 repo and writes a **new** schema-2 repo, never mutating the source (the same
contract :mod:`agora_kb.ingest.vault_import` states for a vault: "SRC is read-only").

The seven rules of D6, implemented literally and in this order:

1. ``type:`` → ``kind:`` per the frozen D2.5 table, and the note MOVES to its kind directory
   (:func:`_plan_note`);
2. the v1 **path domain becomes** ``subjects: [<domain>]`` whenever the source taxonomy declares
   that domain. The v1 path domain is a genuine curator assertion; discarding it would lose exactly
   the information the flip is supposed to preserve, so it is never silently dropped. ``[]`` is the
   *initial* value for a new unclassifiable note (D2.2), and the conversion writes it in exactly two
   cases, **only one of which is reported**: a path domain the source taxonomy **no longer
   declares** is dropped WITH a per-note warning — writing it would mint a repo its own lint
   rejects, because L1-5 grades ``subjects:`` against the taxonomy this converter copies over
   verbatim, and the warning is what keeps that drop from being silent; a note with **no path domain
   at all** (the root ``index.md``) writes ``[]`` STRUCTURALLY and needs no warning, because there
   was never a subject to carry and a report line that fires on every single conversion trains its
   reader to ignore the channel. Recorded in the ADR-0041 *D6 rule 2 as shipped* addendum
   (:func:`_plan_note`);
3. ``wiki/<d>/<d>-moc.md`` → ``wiki/maps/<d>.md``, basename ``<d>`` — the ``-moc`` suffix was the
   kind marker in the NAME and the kind is now the DIRECTORY. Every ``[[<d>-moc]]`` in
   ``related:``/``children:`` and every body link to it is rewritten (:func:`_rewrite_body`,
   :func:`_rewrite_link_array`);
4. ``wiki/<d>/daily/<d>-YYYY-MM-DD.md`` → ``wiki/notes/<yyyy>/<mm>/<yyyy>-<mm>-<dd>.md``, and
   **same-date dailies from different domains MERGE into one journal** (D2.6): their ``## ``
   sections concatenate in domain order, ``sources:`` unions, ``run_id`` comes from the FIRST, and
   each merged section keeps its origin domain as a ``subjects:`` entry (:func:`_merge_journal`);
5. ``raw/`` is copied **byte-identically** and ``sources:`` strings are **NOT rewritten** — the
   payoff of D3.4 and the single largest reason the conversion is cheap (:func:`_copy_tree`);
6. ``_meta/kb.yaml`` is minted at the destination with a **NEW** ``kb_id`` (the destination is a new
   KB, not a continuation) and every note is stamped with it;
7. basename collisions introduced by the conversion are a **HARD failure with a named list**, never
   a silent rename — a converter that renames silently is a converter that loses ``[[basename]]``
   edges (:func:`_assert_no_collisions`).

Two properties this module holds itself to, both testable rather than merely stated:

* **The source is never opened for writing.** Every write lands under ``dest``; ``src`` is read
  through :func:`~agora_kb.schema.notes.parse_all_notes` and :meth:`Path.read_bytes` only.
* **Purity.** ``import_date`` is INJECTED (the CLI derives it once at its own boundary) so the
  conversion is a deterministic function of its inputs, exactly as ``import_vault`` is. The one
  deliberate exception is the minted ``kb_id``, which is a ULID and therefore time-seeded — and it
  is injectable (``kb_id=``) so a test can pin it.
"""

from __future__ import annotations

import posixpath
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as _CalendarDate
from pathlib import Path

import yaml

from agora_kb.core import frontmatter
from agora_kb.core.layout import KIND_DIRECTORIES, RepoLayout
from agora_kb.core.repo import GitError, Repo
from agora_kb.schema import Taxonomy, emit_schema, lint
from agora_kb.schema.emit import materialize_kind_directories
from agora_kb.schema.lint import LintResult
from agora_kb.schema.notes import (
    Note,
    kind_directory_segment,
    kind_from_type,
    parse_all_notes,
)

__all__ = [
    "CONVERTER_SOURCE_SCHEMA_VERSION",
    "CONVERTER_TARGET_SCHEMA_VERSION",
    "ConvertReport",
    "ConvertedNote",
    "KbConvertError",
    "convert_kb",
]

#: The KB wiki schema this converter READS. Named rather than repeated as a literal so the refusal
#: message, the ``parse_all_notes`` version and the guard can never disagree.
CONVERTER_SOURCE_SCHEMA_VERSION = 1

#: The KB wiki schema this converter WRITES (ADR-0041 D1) — the taxonomy it emits, the layout it
#: composes and the lint ruleset it grades its own output with are all this one number.
CONVERTER_TARGET_SCHEMA_VERSION = 2

# OKF v0.1 bundle-root version (ADR-0014 D2) — emitted on the root ``index.md`` ONLY, mirroring
# ``curator.apply`` and ``Repo.init``'s schema-2 seed.
_OKF_VERSION = "0.1"

# A bare ``YYYY-MM-DD`` calendar date (ADR-0010 §2 / ADR-0041 D2.6's journal basename).
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

# The ADR-0010 §2.6 status vocabulary, carried into schema 2 unchanged (lint L1-11).
_STATUS_VALUES = frozenset({"active", "stub", "contested", "deprecated"})

# A body markdown link ``[label](target)`` — the ADR-0014 D3 body graph edge. An IMAGE embed
# ``![label](target)`` is excluded (it is an attachment, not a note edge).
_MDLINK_RE = re.compile(r"(?<!\!)\[(?P<label>[^\]\r\n]*)\]\((?P<target>[^)\r\n]+)\)")

# A body IMAGE embed ``![label](target)`` — the complement of :data:`_MDLINK_RE`, and deliberately
# a SEPARATE grammar with a separate rewriter: an image is an attachment, never a note edge
# (ADR-0010 §3.5), so it must never reach the basename map. It is matched only so a target the
# conversion COPIES (``assets/**``) can be re-spelled for the note's new directory.
_IMAGE_RE = re.compile(r"\!\[(?P<label>[^\]\r\n]*)\]\((?P<target>[^)\r\n]+)\)")

# A body ``[[basename]]`` / ``[[basename|display]]`` wikilink. A produced Agora note has none in
# its BODY (D3 moved the body edge to a markdown link) but a hand-edited v1 note may, and they are
# tolerated on read — so the rename rewrite has to reach them too (D6 rule 3).
_BODY_WIKILINK_RE = re.compile(r"(?<!\!)\[\[(?P<inner>[^\[\]\r\n]*)\]\]")

# The first ATX H1 of a body (``# Title`` at line start). The journal merge lifts each contributing
# daily's H1 into its ``## `` section heading (D6 rule 4) rather than dropping it — a merge that
# discards the titles of the notes it merges is a lossy migration wearing a merge's name.
_H1_RE = re.compile(r"\A#[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)

# The D2 common base, in the ADR's own key order, with the ADR-0014 D2 OKF mirrors interleaved at
# the positions ``curator.apply._common_frontmatter`` puts them — so a converted note and a note
# APPLY re-renders read IDENTICALLY in a diff. The per-kind additions follow.
_FM_ORDER: tuple[str, ...] = (
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
    # The #169 derived Obsidian mirror, at the position `curator.apply._stamp_source_links` puts it
    # (immediately after the key it mirrors) — so a converted note that carries one still reads
    # identically to an APPLY re-render rather than trailing the key at the end of the block.
    "source_links",
    "related",
    "children",
    "date",
    "run_id",
    "origin",
    "confidence",
    "body_status",
)


# Every TOP-LEVEL source entry the conversion carries, converts or regenerates at the destination.
# Anything else is left behind — legitimately (a converter owes the D1 canonical set, not an
# operator's `README.md` or `.obsidian/`) but never SILENTLY: :func:`_uncarried_top_level` names
# them and the report prints the list, because "converted N notes … lint: clean" over a tree that
# quietly lost the operator's own files reads as a complete crossing when it is a partial one.
#
# `.git` and `_kb/` are excluded from the listing rather than carried: the destination git-inits its
# own history, and `_kb/` is derived + git-ignored operator-local state (the reader cache, gold
# packs, the failure spool) that the destination rebuilds. The one item inside it that is NOT
# derived — a pending inbox — is orphaned by the crossing by DESIGN, which is the stated reason D6
# makes a schema-1 inbox write refuse rather than succeed.
_CARRIED_TOP_LEVEL = frozenset(
    {
        "wiki",  # every note is converted (rules 1-4)
        "raw",  # copied byte-identically (rule 5)
        "assets",  # copied (D1 canonical)
        "log.md",  # copied (D1 canonical)
        "index.md",  # the root map — a NOTE, converted
        "_meta",  # re-emitted, carrying the source taxonomy across
        "_templates",  # re-emitted for schema 2
        "AGENTS.md",  # re-emitted schema doc …
        "CLAUDE.md",  # … and its three symlinks
        "QWEN.md",
        "GEMINI.md",
        ".git",
        "_kb",
    }
)


class KbConvertError(ValueError):
    """A conversion that must not proceed: a wrong-schema source, an occupied destination, or a
    collision/merge failure D6 rule 7 requires be named rather than silently renamed away."""


@dataclass(frozen=True)
class ConvertedNote:
    """One note of the DESTINATION, and every source note that became it (ADR-0041 D6).

    ``src_paths`` is normally one v1 repo-relative POSIX path; a merged journal (rule 4) names every
    daily that contributed, in domain order. ``dest_path`` is the schema-2 repo-relative POSIX path,
    ``kind`` its D2.5 kind and ``subjects`` the D2.2 subject list the v1 path domain became.
    ``renamed`` is True when the basename changed (a ``<d>-moc`` map, a ``<d>-YYYY-MM-DD`` daily) —
    the two renames D6 introduces and the report has to enumerate.
    """

    src_paths: tuple[str, ...]
    dest_path: str
    kind: str
    subjects: tuple[str, ...]
    src_basenames: tuple[str, ...]
    dest_basename: str
    renamed: bool = False
    merged: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConvertReport:
    """The full outcome of one ``agora import --from-kb`` run (ADR-0041 D6).

    ``notes`` is one :class:`ConvertedNote` per DESTINATION note, in destination-path order.
    ``renames`` is every ``(v1 basename, v2 basename)`` pair the conversion introduced and
    ``merges`` every ``(journal basename, contributing v1 paths)`` group — the two listings D6
    obliges the converter to produce, kept as their own fields so a caller can print them without
    re-deriving them from ``notes``. ``summary`` is a counts mapping, ``warnings`` the run-level
    findings, and ``lint`` the post-conversion :class:`~agora_kb.schema.lint.LintResult` over the
    destination under the **v2** ruleset — the honest "what still needs hands" surface the
    ``agora import`` contract already promises. ``skipped`` is every TOP-LEVEL source entry the
    conversion did not carry over (:func:`_uncarried_top_level`) — report-only, never a refusal,
    and never a deletion, since the source is untouched.

    Frozen, tuple-valued and deterministic: a report is a value object, like
    :class:`~agora_kb.ingest.vault_import.ImportReport`.
    """

    kb_id: str
    notes: tuple[ConvertedNote, ...]
    renames: tuple[tuple[str, str], ...]
    merges: tuple[tuple[str, tuple[str, ...]], ...]
    summary: dict[str, int]
    warnings: tuple[str, ...]
    lint: LintResult
    skipped: tuple[str, ...] = ()


# --- internal planning ---------------------------------------------------------------------------


@dataclass
class _Plan:
    """One SOURCE note's decided destination, before the merge and the rewrite passes."""

    note: Note
    src_rel: str
    src_basename: str
    kind: str
    subject: str | None
    dest_rel: str
    dest_basename: str
    date: str | None = None
    warnings: list[str] | None = None


def _canonical_date(value: object) -> str | None:
    """Return ``value`` as ``YYYY-MM-DD`` when it is a bare calendar-date scalar, else ``None``.

    ``yaml.safe_load`` turns an unquoted ``2026-01-12`` into a :class:`datetime.date`, so the same
    on-disk byte sequence reaches this module as two different Python types. Both canonicalize to
    the string schema 2 stores; a ``datetime`` (which is NOT a bare date) and anything else do not.
    """
    if isinstance(value, datetime):
        # A `datetime` is NOT a bare calendar date (it is a `date` subclass, so it must be rejected
        # BEFORE the `date` branch below or it would canonicalize by silently dropping its time).
        return None
    if isinstance(value, _CalendarDate):
        return value.isoformat()
    if isinstance(value, str) and _DATE_RE.match(value):
        return value
    return None


def _v1_kind_from_path(rel_path: str) -> str | None:
    """Infer a schema-2 kind from a SCHEMA-1 path, the fallback when ``type:`` names none.

    The ADR-0010 §1 folder rules, read through the frozen D2.5 table: the root ``index.md`` is
    ``index``, ``wiki/<d>/<d>-moc.md`` is ``map``, ``wiki/<d>/themes/**`` is ``concept`` and
    ``wiki/<d>/daily/**`` is ``note``. ``None`` for anything else — a note the source's own lint
    would have rejected, which :func:`convert_kb` names rather than guesses at.
    """
    if rel_path == "index.md":
        return "index"
    parts = rel_path.split("/")
    if len(parts) < 3 or parts[0] != "wiki":
        return None
    domain = parts[1]
    if len(parts) == 3 and parts[2] == f"{domain}-moc.md":
        return "map"
    if len(parts) >= 4 and parts[2] == "themes":
        return "concept"
    if len(parts) >= 4 and parts[2] == "daily":
        return "note"
    return None


def _plan_note(note: Note, *, domains: frozenset[str]) -> _Plan:
    """Decide one source note's kind, subject and destination path (D6 rules 1–4).

    The kind comes from ``type:`` through the frozen D2.5 table and falls back to the v1 PATH shape
    when the note declares none. A ``type: index`` note that is not the root ``index.md`` is demoted
    to a ``map``: schema 2 has exactly one ``index`` (D1.2) and inventing a second would produce a
    repo lint L1-13 rejects, while a navigation note is exactly what a map is.

    **A note that declares NO kind at all and fits no v1 layout becomes a ``concept``, with a
    warning.** That is ADR-0014 D1's tolerant read applied where it belongs: a stray
    ``wiki/README.md`` is precisely the file that read exists for, it violates no row of the D2.5
    table (it names no ``type:`` to translate), and refusing a whole conversion over one such file
    would strand a real KB on a note nobody meant as knowledge. The destination lint then grades
    the result honestly.

    **A ``type: daily`` with no derivable date is a HARD failure**, and the asymmetry is deliberate:
    that note DOES declare its kind, and schema 2 gives a journal no legal path without a date —
    D2.6 makes basename, ``date:`` and the ``<yyyy>/<mm>`` shard three views of one value, which
    lint L1-14 asserts. Silently re-kinding it to a concept would be the converter overruling rule 1
    on the one note where it was told what the kind is.
    """
    rel = note.rel_path
    basename = note.basename
    warnings: list[str] = []

    kind = kind_from_type(note.type) or _v1_kind_from_path(rel)
    if kind is None:
        kind = "concept"
        warnings.append(
            f"declares no usable 'type:' ({note.type!r}) and fits no ADR-0010 layout; converted to "
            f"a concept (ADR-0014 D1 tolerant read — the destination lint grades the result)"
        )
    if kind == "index" and rel != "index.md":
        kind = "map"
        warnings.append(
            "declared 'type: index' outside the repo root; converted to a map "
            "(schema 2 has exactly one index, at index.md — ADR-0041 D1.2)"
        )
    # D6 rule 2: the v1 PATH domain becomes the subject. A domain the source taxonomy does not
    # declare is dropped with a warning rather than written, because lint L1-5 grades `subjects:`
    # against the destination taxonomy — which is the source's own, copied verbatim.
    domain = kind_directory_segment(rel)
    subject: str | None = None
    if domain is not None:
        if domain in domains:
            subject = domain
        else:
            warnings.append(
                f"path domain {domain!r} is not in the source taxonomy domains; "
                f"converted with 'subjects: []' rather than an undeclared subject"
            )

    date: str | None = None
    if kind == "index":
        dest_basename, dest_rel = "index", "index.md"
    elif kind == "map":
        # D6 rule 3, stated exactly: only the `<domain>-moc` shape is renamed. A map basenamed
        # anything else keeps its name — stripping a `-moc` suffix that was never the domain's would
        # be a rename D6 does not authorise, and a rename is what breaks `[[basename]]` edges.
        dest_basename = domain if domain is not None and basename == f"{domain}-moc" else basename
        dest_rel = f"wiki/{KIND_DIRECTORIES['map']}/{dest_basename}.md"
    elif kind == "note":
        date = _canonical_date(note.frontmatter.get("date"))
        if date is None and _DATE_RE.match(basename[-10:]):
            date = basename[-10:]
        if date is None:
            raise _UndatedDaily(rel)
        dest_basename = date
        dest_rel = f"wiki/{KIND_DIRECTORIES['note']}/{date[:4]}/{date[5:7]}/{date}.md"
    else:
        directory = KIND_DIRECTORIES.get(kind)
        if directory is None:  # pragma: no cover - the D2.5 table has no other kind
            raise ValueError(f"{rel}: no schema-2 directory for kind {kind!r}")
        dest_basename = basename
        dest_rel = f"wiki/{directory}/{basename}.md"

    return _Plan(
        note=note,
        src_rel=rel,
        src_basename=basename,
        kind=kind,
        subject=subject,
        dest_rel=dest_rel,
        dest_basename=dest_basename,
        date=date,
        warnings=warnings,
    )


class _UndatedDaily(Exception):
    """Internal: a ``type: daily`` note carrying no date at all (collected, then named)."""

    def __init__(self, rel_path: str) -> None:
        super().__init__(rel_path)
        self.rel_path = rel_path


# --- the journal merge (D6 rule 4 / D2.6) ---------------------------------------------------------


def _split_h1(body: str) -> tuple[str | None, str]:
    """Split a leading ``# Title`` off ``body``; return ``(title_or_None, remaining_body)``."""
    text = body.strip("\n")
    match = _H1_RE.match(text)
    if match is None:
        return None, text
    return match.group("title").strip(), text[match.end() :].strip("\n")


def _ordered_union(values: Iterable[Iterable[str]]) -> list[str]:
    """Concatenate iterables preserving first-seen order and dropping repeats (a stable union)."""
    out: dict[str, None] = {}
    for group in values:
        for item in group:
            out.setdefault(item, None)
    return list(out)


def _str_list(value: object) -> list[str]:
    """The string elements of a frontmatter list value, else ``[]`` (tolerant accessor)."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    if isinstance(value, str):
        return [value]
    return []


# --- link / basename rewriting (D6 rule 3) ----------------------------------------------------


def _stem(target: str) -> str:
    """The note basename a markdown-link TARGET path names (filename minus dir minus ``.md``)."""
    last = target.rsplit("/", 1)[-1]
    return last[: -len(".md")] if last.endswith(".md") else last


#: The repo-relative prefixes the conversion copies to the IDENTICAL destination path (rule 5 plus
#: the two D1-canonical trees carried with it). A link into one of these is not a note edge and is
#: never resolved through the basename map — but its SPELLING still has to move, because the note
#: doing the linking moved and the file did not.
_CARRIED_VERBATIM_PREFIXES = ("raw/", "assets/")


def _carried_verbatim(resolved: str) -> bool:
    """True iff the conversion copies ``resolved`` to the SAME destination-relative path.

    Exactly the set :func:`convert_kb` copies with :func:`_copy_tree` / ``shutil.copyfile``:
    ``raw/**`` (rule 5, byte-identical), ``assets/**`` and ``log.md`` (D1 canonical). Kept as one
    named predicate so the link rewriter and the copier can never disagree about which files
    survive the crossing at their own path — the premise the whole re-spelling rests on.
    """
    return resolved == "log.md" or resolved.startswith(_CARRIED_VERBATIM_PREFIXES)


def _retarget(
    target: str,
    *,
    src_root: Path,
    src_rel: str,
    dest_rel: str,
    src_to_dest: dict[str, str],
    dest_by_basename: dict[str, str],
    renames: dict[str, str],
) -> str | None:
    """Return the DESTINATION-relative spelling of one body markdown-link target, or ``None``.

    ``None`` means "leave this link verbatim": an external URL, an absolute path, an escape out of
    the repo, or a target that resolves to no converted note and to no carried file. Resolution is
    tried in the order that loses the least:

    1. a target that resolves to a file the conversion COPIES VERBATIM (:func:`_carried_verbatim` —
       ``raw/**``, ``assets/**``, ``log.md``) is RE-SPELLED, not left alone. The file lands at the
       identical repo-relative path, but the LINKING note moved two directory levels, so a
       ``../../../raw/<d>/x.md`` written from ``wiki/<d>/themes/`` resolves ABOVE the destination
       root from ``wiki/concepts/`` — a dead link, silently, in the tier that carries the evidence.
       Recomputing the relative path cannot rewrite provenance (rule 5's concern), because the file
       IDENTITY is unchanged: the same bytes at the same repo-relative path, named from a new
       directory. This is checked BEFORE the ``.md`` test on purpose — an ``assets/`` attachment is
       usually not markdown, and it is exactly as broken as an evidence link;
    2. the LITERAL ``.md`` path, normalized against the linking note's own directory (exact);
    3. only when that path exists nowhere in ``src_root``, the BASENAME through the D6 rule-3 rename
       map — how a ``[[<d>-moc]]``-shaped link and a hand-written relative path that no longer
       resolves still find their note, since basenames are the identity (ADR-0010 D5).

    Rewriting is not cosmetic here: EVERY note moves under the flip, so a relative link left alone
    would point at nothing on disk. The basename it carries is unchanged (except for the two D6
    renames), so nothing downstream that keys on basenames — lint L1-2, the ranker's link graph,
    the graph face — sees a difference; what changes is that the link works again in Obsidian.
    """
    cleaned = target.strip()
    fragment = ""
    if "#" in cleaned:
        cleaned, _, rest = cleaned.partition("#")
        fragment = f"#{rest}"
    if cleaned.startswith("/") or "://" in cleaned or ":" in cleaned or "\\" in cleaned:
        return None
    if not cleaned:
        return None

    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(src_rel), cleaned))
    if resolved == ".." or resolved.startswith("../"):
        return None  # an escape out of the repo: never a note edge and never carried
    if _carried_verbatim(resolved) and (src_root / resolved).is_file():
        # Step 1: the SAME file at the SAME repo-relative path, renamed from the note's new home.
        relative = posixpath.relpath(resolved, posixpath.dirname(dest_rel) or ".")
        return f"{relative}{fragment}"
    if not cleaned.endswith(".md"):
        return None
    dest_target = src_to_dest.get(resolved)
    if dest_target is None:
        # The basename fallback is for a target that resolves to NOTHING — a hand-written relative
        # path that rotted, or a `[[<d>-moc]]`-shaped link. A target that DOES resolve in the source
        # but is not a converted note is a deliberate reference to a NON-note: `raw/` evidence, an
        # `assets/` file, an escape out of the repo. Re-pointing one of those at whatever wiki note
        # happens to share its filename stem would silently rewrite the operator's own provenance
        # text — in the converter whose rule 5 exists to leave provenance alone (D6 rule 5 / D3.4).
        # So the fallback is GATED: only a target that exists nowhere in the source may take it.
        if (src_root / resolved).exists():
            return None
        base = _stem(cleaned)
        dest_target = dest_by_basename.get(renames.get(base, base))
    if dest_target is None:
        return None
    relative = posixpath.relpath(dest_target, posixpath.dirname(dest_rel) or ".")
    return f"{relative}{fragment}"


def _rewrite_body(
    body: str,
    *,
    src_root: Path,
    src_rel: str,
    dest_rel: str,
    src_to_dest: dict[str, str],
    dest_by_basename: dict[str, str],
    renames: dict[str, str],
) -> str:
    """Rewrite every body link for the destination layout (D6 rule 3, generalized to every move).

    Markdown links get a destination-relative target; a stray ``[[basename]]`` (tolerated on read,
    never produced by Agora) gets the renamed basename. Anything that does not resolve is left
    BYTE-IDENTICAL — a converter that drops a link it cannot follow loses the operator's own text.

    **IMAGE EMBEDS are rewritten too, and only in the carried-verbatim direction.** ``![alt](…)`` is
    excluded from the note link graph on purpose (ADR-0010 §3.5 — an asset is not a basename edge)
    and this keeps it excluded: an image target never reaches the basename map and never becomes an
    edge. But an embed pointing at ``../../assets/pic.png`` is broken by the flip exactly as an
    evidence link is, and an image that silently stops rendering is a worse failure than a link that
    does, because nothing in the note's text shows the reader anything is missing. So an image whose
    target resolves to a carried file is re-spelled; every other image is left byte-identical.
    """

    def _md(match: re.Match[str]) -> str:
        new_target = _retarget(
            match.group("target"),
            src_root=src_root,
            src_rel=src_rel,
            dest_rel=dest_rel,
            src_to_dest=src_to_dest,
            dest_by_basename=dest_by_basename,
            renames=renames,
        )
        if new_target is None:
            return match.group(0)
        return f"[{match.group('label')}]({new_target})"

    def _wl(match: re.Match[str]) -> str:
        inner = match.group("inner")
        target, sep, display = inner.partition("|")
        anchor = ""
        if "#" in target:
            target, _, rest = target.partition("#")
            anchor = f"#{rest}"
        renamed = renames.get(target.strip())
        if renamed is None:
            return match.group(0)
        return f"[[{renamed}{anchor}{sep}{display}]]"

    def _img(match: re.Match[str]) -> str:
        cleaned = match.group("target").strip()
        fragment = ""
        if "#" in cleaned:
            cleaned, _, rest = cleaned.partition("#")
            fragment = f"#{rest}"
        if not cleaned or cleaned.startswith("/") or "://" in cleaned or ":" in cleaned:
            return match.group(0)
        if "\\" in cleaned:
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(src_rel), cleaned))
        if not _carried_verbatim(resolved) or not (src_root / resolved).is_file():
            return match.group(0)
        relative = posixpath.relpath(resolved, posixpath.dirname(dest_rel) or ".")
        return f"![{match.group('label')}]({relative}{fragment})"

    return _IMAGE_RE.sub(_img, _BODY_WIKILINK_RE.sub(_wl, _MDLINK_RE.sub(_md, body)))


def _rewrite_link_array(value: object, renames: dict[str, str]) -> list[str]:
    """Rewrite a ``related:`` / ``children:`` ``[[basename]]`` array through the rename map."""
    out: list[str] = []
    for entry in _str_list(value):
        text = entry.strip()
        inner = text[2:-2].strip() if text.startswith("[[") and text.endswith("]]") else text
        out.append(f"[[{renames.get(inner, inner)}]]")
    return out


# --- destination frontmatter (D2) -------------------------------------------------------------


def _provenance_block(existing: object) -> dict[str, object]:
    """The D2.3 ``provenance:`` block, preserving a well-shaped existing one.

    Two lists, deliberately unequal: ``writers`` holds AUTHENTICATED principals and is trusted,
    ``agents`` holds agent SELF-DECLARATIONS and is recorded but never trusted. A schema-1 note has
    no block at all, so the honest converted value is two empty lists — the conversion authenticates
    nobody and must not invent a writer.
    """
    if isinstance(existing, dict):
        return {
            "writers": _str_list(existing.get("writers")),
            "agents": _str_list(existing.get("agents")),
        }
    return {"writers": [], "agents": []}


def _dest_frontmatter(
    source_fm: dict[str, object],
    *,
    kind: str,
    kb_id: str,
    subjects: list[str],
    renames: dict[str, str],
    title: str,
    fallback_date: str,
    warnings: list[str],
) -> dict[str, object]:
    """Build one destination note's ADR-0041 D2 frontmatter from its schema-1 frontmatter.

    Everything the source declared is PRESERVED (including keys no rule names — the contested
    triple, ``origin``, an OKF ``resource``) and re-ordered into D2's own block; what the conversion
    MATERIALIZES is the schema-2 base the v1 note could not carry: ``kind`` (mirroring the
    directory, D2.1), the ``type:`` OKF mirror of it (OD-3), ``kb`` (the new ``_meta/kb.yaml``
    ULID, D1.5), ``subjects`` (the v1 path domain, D6 rule 2), ``derived: false`` (D2.4 reserves
    ``true`` for a proposal plane with no day-1 producer) and ``provenance``.

    Two v1 keys are the exception to "everything is preserved", and both are RETIREMENTS the flip
    performs rather than content the conversion drops — each one appended to ``warnings``, because
    a silent frontmatter deletion is exactly what an operator cannot audit:

    * ``body_status:`` — schema 2's L1-4 admits it ONLY as ``pending`` (its ABSENCE is the second
      state, "authored"), while v1's lint never graded the value at all. ADR-0010's ADR-0041
      amendment banner puts this on the D6 importer BY NAME: *"a v1 note carrying any other literal
      lints clean today and must be normalised or stripped by ``agora import --from-kb``"*. Carried
      through verbatim it would mint a repo that fails its own lint on note one.
    * ``domain:`` — the v1 topic key whose successor is ``subjects:`` (D2.2). Dropped ONLY when its
      value is already in the materialised ``subjects``, i.e. when the information MOVED rather
      than vanished. A merged journal is why this matters: ``merged_fm`` starts from the FIRST
      contributor, so a carried-through ``domain: finance`` on a journal subjected to both
      ``finance`` and ``general`` is an actively false single-subject assertion. A ``domain:`` the
      conversion could NOT carry into ``subjects`` (rule 2's undeclared-domain case) is KEPT
      verbatim — there it is the only surviving record of what the v1 curator asserted.
    """
    fm: dict[str, object] = dict(source_fm)

    fm["title"] = title
    fm["kind"] = kind
    fm["type"] = kind  # the DERIVED OKF mirror (OD-3); schema 2 reads the directory, never this
    fm["kb"] = kb_id
    fm["subjects"] = list(subjects)
    fm["aliases"] = _str_list(fm.get("aliases"))
    fm["tags"] = _str_list(fm.get("tags"))

    for key in ("created", "updated"):
        fm[key] = _canonical_date(fm.get(key)) or fallback_date
    fm["timestamp"] = f"{fm['updated']}T00:00:00Z"

    status = fm.get("status")
    fm["status"] = status if isinstance(status, str) and status in _STATUS_VALUES else "active"

    summary = fm.get("summary")
    fm["summary"] = summary.strip() if isinstance(summary, str) and summary.strip() else title
    description = fm.get("description")
    fm["description"] = (
        description if isinstance(description, str) and description.strip() else fm["summary"]
    )

    fm["derived"] = fm.get("derived") is True
    fm["provenance"] = _provenance_block(fm.get("provenance"))

    if kind == "index":
        fm["okf_version"] = _OKF_VERSION
    else:
        fm.pop("okf_version", None)

    # Per-kind additions. Their SHAPE carries over from v1 unchanged (D2); only the `[[basename]]`
    # arrays move, because D6 rule 3 renamed two families of basename.
    if kind in ("concept", "summary", "entity"):
        fm["sources"] = _str_list(fm.get("sources"))
        fm["related"] = _rewrite_link_array(fm.get("related"), renames)
    elif kind == "note":
        fm["sources"] = _str_list(fm.get("sources"))
    elif kind in ("map", "index"):
        fm["children"] = _rewrite_link_array(fm.get("children"), renames)

    # The two v1 retirements (see the docstring): a `body_status:` literal schema 2's L1-4 rejects,
    # and a `domain:` whose assertion has already moved into `subjects:`.
    body_status = fm.get("body_status")
    if "body_status" in fm and body_status != "pending":
        fm.pop("body_status")
        warnings.append(
            f"dropped the v1 'body_status: {body_status!r}' literal — schema 2's L1-4 admits only "
            f"'pending', its ABSENCE being the authored state (ADR-0010's ADR-0041 amendment "
            f"banner puts this normalisation on 'agora import --from-kb' by name)"
        )
    domain_value = fm.get("domain")
    if isinstance(domain_value, str) and domain_value in subjects:
        fm.pop("domain")
        warnings.append(
            f"dropped the retired v1 'domain: {domain_value}' key — its successor is "
            f"'subjects: {list(subjects)}', which already carries it (ADR-0041 D2.2)"
        )

    return _ordered(fm)


def _ordered(fm: dict[str, object]) -> dict[str, object]:
    """Reorder ``fm`` into the D2 key order; unknown keys keep their order, after the known ones."""
    ordered: dict[str, object] = {key: fm[key] for key in _FM_ORDER if key in fm}
    for key, value in fm.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


# --- the source taxonomy + the destination tree -----------------------------------------------


def _read_source_taxonomy(src_layout: RepoLayout) -> Taxonomy:
    """Read the SOURCE ``_meta/taxonomy.yaml`` into a :class:`Taxonomy` declaring schema 2.

    The closed vocabulary travels with the KB: the destination's ``domains`` / ``allowed_tags`` /
    ``taxonomy_policy`` are the source's, verbatim, so every ``subjects:`` entry D6 rule 2 writes
    and every tag a note carries is already declared (lint L1-5 by construction). Only
    ``schema_version`` changes, which is the whole point of the crossing.

    Reads the canonical file DIRECTLY rather than through ``load_repo_config``: ``_kb/repo.yaml`` is
    git-ignored and operator-local, and a converter writing a shared git-tracked tree must not let
    an untracked local edit decide what vocabulary the new KB declares.
    """
    path = src_layout.meta_dir / "taxonomy.yaml"
    raw: object = None
    if path.is_file():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise KbConvertError(f"{path}: source taxonomy is not parseable YAML: {exc}") from exc
    meta = raw if isinstance(raw, dict) else {}

    # `allowed_tags` is a MAPPING `{tag: {...}}` on disk (emit.py) whose KEYS are the closed set;
    # a plain list is accepted too so a hand-written taxonomy still converts.
    allowed = meta.get("allowed_tags")
    if isinstance(allowed, dict):
        tags = tuple(str(t) for t in allowed)
    else:
        tags = tuple(_str_list(allowed))
    domains = tuple(_str_list(meta.get("domains")))
    reserved = sorted(d for d in domains if d.startswith("_"))
    if reserved:
        raise KbConvertError(
            f"{path}: source domain(s) {reserved} begin with '_', which schema 2 RESERVES inside "
            f"raw/ for the content-addressed capture tree (raw/_blob/) and the long-document tier "
            f"(raw/_pages/) — rename them in the source before converting (ADR-0041 D1.4, L1-23)"
        )
    policy = meta.get("taxonomy_policy")
    return Taxonomy(
        schema_version=CONVERTER_TARGET_SCHEMA_VERSION,
        taxonomy_policy=policy if isinstance(policy, str) and policy else "open",
        allowed_tags=tags,
        domains=domains,
    )


def _copy_tree(src_dir: Path, dest_dir: Path) -> int:
    """Copy ``src_dir`` under ``dest_dir`` BYTE-IDENTICALLY; return the file count (D6 rule 5).

    ``raw/`` is the evidence tier and is NEVER MOVED (D1.4): the ``<domain>`` segment survives as a
    shard key, which is exactly why ``sources:`` strings need no rewriting at all. Symlinks are
    skipped rather than followed — a source repo is not necessarily trusted, and following one would
    turn the copy into a read of an arbitrary host file.
    """
    if not src_dir.is_dir() or src_dir.is_symlink():
        return 0
    copied = 0
    for path in sorted(src_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        out = dest_dir / path.relative_to(src_dir)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, out)
        copied += 1
    return copied


def _uncarried_top_level(src: Path) -> tuple[str, ...]:
    """Top-level source entries the conversion does NOT carry over, as sorted POSIX names.

    Report-only: none of these is a refusal and none is copied. The source is untouched, so nothing
    is destroyed — but an operator whose KB doubles as an Obsidian vault has a `.obsidian/`, a
    `README.md` and an attachments folder in that tree, and a report that never names them lets
    "converted N note(s) … lint: clean" mean more than it does. A directory is suffixed ``/`` so
    the listing reads like the tree it describes.
    """
    out: list[str] = []
    try:
        entries = sorted(src.iterdir(), key=lambda p: p.name)
    except OSError:  # pragma: no cover - an unreadable source dir fails earlier
        return ()
    for entry in entries:
        if entry.name in _CARRIED_TOP_LEVEL:
            continue
        out.append(f"{entry.name}/" if entry.is_dir() and not entry.is_symlink() else entry.name)
    return tuple(out)


def _assert_no_collisions(
    dest_by_path: dict[str, list[str]], dest_by_basename: dict[str, list[str]]
) -> None:
    """D6 rule 7: any collision the conversion introduces is a HARD failure with a NAMED list.

    Never a silent rename. A converter that renames silently is a converter that loses
    ``[[basename]]`` edges: every link in the repo resolves on the basename (ADR-0010 D5), so an
    invented ``foo-2`` disconnects every note that pointed at ``foo`` — and does it quietly, which
    is how a migration ends up "clean" over knowledge it has already broken.
    """
    collisions = [
        f"  {basename!r} claimed by {sources}"
        for basename, sources in sorted(dest_by_basename.items())
        if len(sources) > 1
    ]
    path_collisions = [
        f"  {path!r} claimed by {sources}"
        for path, sources in sorted(dest_by_path.items())
        if len(sources) > 1
    ]
    if not collisions and not path_collisions:
        return
    lines = ["the conversion would collide; nothing was written (ADR-0041 D6 rule 7)"]
    if collisions:
        lines.append("duplicate destination basenames:")
        lines.extend(collisions)
    if path_collisions:
        lines.append("duplicate destination paths:")
        lines.extend(path_collisions)
    lines.append(
        "resolve them in the SOURCE repo (rename one note) and convert again — this converter "
        "never renames silently, because a renamed basename breaks every [[basename]] edge to it"
    )
    raise KbConvertError("\n".join(lines))


# --- the public entry point --------------------------------------------------------------------


def _assert_convertible(src: Path, dest: Path) -> RepoLayout:
    """Refuse a source that is not schema 1, a nested source/destination pair, and an occupied dest.

    The CONTAINMENT refusal is the one that guards D6's headline property — *"never mutating the
    source"* — rather than merely restating it. A ``dest`` inside ``src`` is an easy reflex
    (``agora import --from-kb ~/kb ~/kb/converted``) and it writes a whole schema-2 repo, its own
    ``.git`` included, INTO the tree this converter promises not to touch: the source's next
    ``git add -A`` commits it, and the CLI's closing ``<src> was NOT modified`` note becomes false.
    The mirror (a ``src`` inside ``dest``) is refused for the same reason from the other side.
    Both are decided on ``resolve()``d paths, so a symlinked ``dest`` cannot slip past.
    """
    if not src.exists():
        raise FileNotFoundError(f"source repo does not exist: {src}")
    if not src.is_dir():
        raise NotADirectoryError(f"source repo is not a directory: {src}")

    # `Path.resolve()` is non-strict: it resolves what exists and appends the rest, so an ABSENT
    # `dest` (the normal case) resolves fine and a symlinked parent still collapses to its target.
    src_resolved, dest_resolved = src.resolve(), dest.resolve()
    if src_resolved == dest_resolved or dest_resolved.is_relative_to(src_resolved):
        raise KbConvertError(
            f"{dest} is inside the source repo {src}; 'agora import --from-kb' writes a NEW repo "
            f"and NEVER modifies the source (ADR-0041 D6), and a destination nested in the source "
            f"would plant a whole schema-2 tree — its own .git included — in the tree it is "
            f"reading. Choose a destination outside {src}"
        )
    if src_resolved.is_relative_to(dest_resolved):
        raise KbConvertError(
            f"the source repo {src} is inside the destination {dest}; 'agora import --from-kb' "
            f"writes a NEW repo at the destination and would be writing around the source it is "
            f"reading (ADR-0041 D6). Choose a destination outside {src}"
        )

    from agora_kb.config import read_canonical_kb_schema_version

    src_layout = RepoLayout(src)
    declared = read_canonical_kb_schema_version(src_layout)
    if declared != CONVERTER_SOURCE_SCHEMA_VERSION:
        found = "no _meta/taxonomy.yaml" if declared is None else f"schema {declared}"
        raise KbConvertError(
            f"{src} is not a KB wiki schema-{CONVERTER_SOURCE_SCHEMA_VERSION} repo ({found}); "
            f"'agora import --from-kb' converts schema "
            f"{CONVERTER_SOURCE_SCHEMA_VERSION} → {CONVERTER_TARGET_SCHEMA_VERSION} and is the "
            f"ONLY crossing (ADR-0041 D6)"
        )

    if dest.exists():
        # An EMPTY directory is indistinguishable from an absent one for every purpose this guard
        # serves (there is nothing to clobber and no other layout to mix with), and `mkdir -p` is a
        # normal reflex. Anything else is refused: converting INTO a populated tree is precisely the
        # two-layouts-in-one-repo state D6's refusal exists to prevent.
        if not dest.is_dir() or any(dest.iterdir()):
            raise KbConvertError(
                f"{dest} already exists and is not empty; 'agora import --from-kb' writes a NEW "
                f"repo and never converts in place (ADR-0041 D6 — there is no in-place migrator)"
            )
    return src_layout


def convert_kb(
    src: Path,
    dest: Path,
    *,
    import_date: str,
    kb_id: str | None = None,
    name: str | None = None,
) -> ConvertReport:
    """Convert the KB wiki schema-1 repo at ``src`` into a NEW schema-2 repo at ``dest``.

    The ADR-0041 D6 converter, and the only crossing between the two schemas. ``src`` is read
    NON-DESTRUCTIVELY — it is never modified, and this function opens nothing under it for writing.
    ``dest`` must not already hold anything: the destination is a NEW knowledge base, which is also
    why rule 6 mints a NEW ``kb_id`` rather than carrying one over.

    ``import_date`` is the INJECTED ``YYYY-MM-DD`` the caller derives once at its own boundary (the
    CLI), so this function reads no wall clock: it pins the destination's init/admin commit dates
    and is the fallback ``created``/``updated`` for a source note that carried an unusable one.
    ``kb_id`` overrides the minted ULID (a test pins it; production mints). ``name`` is the
    ``_meta/kb.yaml`` display name and defaults to the destination directory name.

    Raises :class:`KbConvertError` for every refusal: a source that is not schema 1, a ``dest``
    nested inside ``src`` (or the reverse — the guard on "never mutating the source"), an occupied
    destination, a source domain beginning with ``_`` (D1.4's reserved ``raw/`` prefix), a
    ``type: daily`` carrying no date at all, and any destination basename/path collision (rule 7,
    with the colliding names listed). Raises ``FileNotFoundError`` / ``NotADirectoryError`` when
    ``src`` is missing or is not a directory. In every case NOTHING has been written to ``dest``:
    the whole plan is decided, and every collision checked, before the first byte lands. A note
    that declares no kind at all is NOT a refusal — it converts to a concept and is reported
    (:func:`_plan_note`).
    """
    src = Path(src)
    dest = Path(dest)
    src_layout = _assert_convertible(src, dest)

    taxonomy = _read_source_taxonomy(src_layout)
    domains = frozenset(taxonomy.domains)
    domain_order = {domain: index for index, domain in enumerate(taxonomy.domains)}

    # --- pass 1: plan every source note (D6 rules 1-4) ------------------------------------------
    # `schema_version=1` is passed EXPLICITLY rather than resolved: the source is schema 1 by the
    # guard above, and pinning it here is what makes `Note.kind` / `Note.subjects` the v1 derivation
    # (type-through-the-D2.5-table, domain-from-the-path) no matter what the file on disk says.
    notes = parse_all_notes(src_layout, schema_version=CONVERTER_SOURCE_SCHEMA_VERSION)
    plans: list[_Plan] = []
    undated: list[str] = []
    for note in notes:
        try:
            plans.append(_plan_note(note, domains=domains))
        except _UndatedDaily as exc:
            undated.append(f"  {exc.rel_path!r}")
    if undated:
        raise KbConvertError(
            "\n".join(
                [
                    "the source cannot be converted; nothing was written (ADR-0041 D6)",
                    "dailies with no date — schema 2 basenames a journal by its date and shards "
                    "it under <yyyy>/<mm>, so an undated one has no legal path (D2.6, L1-14):",
                    *undated,
                    "give each one a 'date: YYYY-MM-DD' (or a '<domain>-YYYY-MM-DD' basename) in "
                    "the SOURCE repo and convert again",
                ]
            )
        )

    # Stable ordering: the taxonomy's own domain order (D6 rule 4's "domain order"), then the source
    # path, so the merge and every listing is a deterministic function of the source repo alone.
    def _order(plan: _Plan) -> tuple[int, str]:
        return (domain_order.get(plan.subject or "", len(domain_order)), plan.src_rel)

    plans.sort(key=_order)

    # --- the D6 rule-3/4 rename map, built BEFORE any rewrite ------------------------------------
    renames: dict[str, str] = {
        plan.src_basename: plan.dest_basename
        for plan in plans
        if plan.dest_basename != plan.src_basename
    }

    # --- pass 2: merge same-date dailies into ONE journal (D6 rule 4 / D2.6) ---------------------
    journals: dict[str, list[_Plan]] = {}
    singles: list[_Plan] = []
    for plan in plans:
        if plan.kind == "note" and plan.date is not None:
            journals.setdefault(plan.date, []).append(plan)
        else:
            singles.append(plan)

    dest_by_path: dict[str, list[str]] = {}
    dest_by_basename: dict[str, list[str]] = {}
    for plan in singles:
        dest_by_path.setdefault(plan.dest_rel, []).append(plan.src_rel)
        dest_by_basename.setdefault(plan.dest_basename, []).append(plan.src_rel)
    for date, group in journals.items():
        # A merged journal claims its date ONCE, naming every daily behind it: the merge is not a
        # collision (D6 rule 4), but a CONCEPT that happens to be basenamed `2026-01-12` is — and
        # the operator needs to see which files are on each side of that.
        rel = group[0].dest_rel
        merged_from = f"merged journal from {[plan.src_rel for plan in group]}"
        dest_by_path.setdefault(rel, []).append(merged_from)
        dest_by_basename.setdefault(date, []).append(merged_from)
    _assert_no_collisions(dest_by_path, dest_by_basename)

    src_to_dest = {plan.src_rel: plan.dest_rel for plan in plans}
    dest_by_basename_path = {plan.dest_basename: plan.dest_rel for plan in plans}

    # --- pass 3: render every destination note ---------------------------------------------------
    # The id is minted HERE — after every refusal has fired and before the first note is rendered —
    # because D6 rule 6 stamps it into every note (`kb:`), and minting it later would mean either a
    # second pass over the rendered text or an id that a failed run had already burned.
    kb_id = kb_id or _mint_kb_id()
    rendered: dict[str, str] = {}
    records: list[ConvertedNote] = []
    merges: list[tuple[str, tuple[str, ...]]] = []

    for plan in singles:
        new_body = _rewrite_body(
            plan.note.body.strip("\n"),
            src_root=src,
            src_rel=plan.src_rel,
            dest_rel=plan.dest_rel,
            src_to_dest=src_to_dest,
            dest_by_basename=dest_by_basename_path,
            renames=renames,
        )
        # The plan's own warning list is the sink, so a frontmatter retirement lands on the SAME
        # per-note record as the planning warnings rather than in a second, parallel channel.
        plan_warnings = plan.warnings if plan.warnings is not None else []
        fm = _dest_frontmatter(
            plan.note.frontmatter,
            kind=plan.kind,
            kb_id=kb_id,
            subjects=[plan.subject] if plan.subject else [],
            renames=renames,
            title=_note_title(plan),
            fallback_date=import_date,
            warnings=plan_warnings,
        )
        rendered[plan.dest_rel] = frontmatter.render(fm, new_body)
        records.append(
            ConvertedNote(
                src_paths=(plan.src_rel,),
                dest_path=plan.dest_rel,
                kind=plan.kind,
                subjects=tuple([plan.subject] if plan.subject else []),
                src_basenames=(plan.src_basename,),
                dest_basename=plan.dest_basename,
                renamed=plan.dest_basename != plan.src_basename,
                warnings=tuple(plan_warnings),
            )
        )

    for date in sorted(journals):
        group = journals[date]
        fm, body, subjects, warnings = _merge_journal(
            date,
            group,
            kb_id=kb_id,
            src_root=src,
            src_to_dest=src_to_dest,
            dest_by_basename=dest_by_basename_path,
            renames=renames,
            fallback_date=import_date,
        )
        rel = group[0].dest_rel
        rendered[rel] = frontmatter.render(fm, body)
        merges.append((date, tuple(plan.src_rel for plan in group)))
        records.append(
            ConvertedNote(
                src_paths=tuple(plan.src_rel for plan in group),
                dest_path=rel,
                kind="note",
                subjects=tuple(subjects),
                src_basenames=tuple(plan.src_basename for plan in group),
                dest_basename=date,
                renamed=any(plan.dest_basename != plan.src_basename for plan in group),
                merged=len(group) > 1,
                warnings=tuple(warnings),
            )
        )

    # --- pass 4: WRITE the destination (the first byte of the run) --------------------------------
    dest_layout = RepoLayout(dest)
    dest.mkdir(parents=True, exist_ok=True)
    emit_schema(
        dest_layout, taxonomy=taxonomy, schema_version=CONVERTER_TARGET_SCHEMA_VERSION, force=True
    )

    from agora_kb.config import KbIdentity, write_kb_identity

    write_kb_identity(dest_layout, KbIdentity(kb_id=kb_id, name=name or dest.name or "knowledge"))

    # The kind CONTAINERS are the schema; their population is not. `wiki/summaries/` and
    # `wiki/entities/` ship EMPTY (OD-7 / OD-8) and `wiki/people/` is populated only by a human.
    # This is `agora repo init`'s OWN helper, deliberately — not a local `mkdir`. Each container
    # gets a `.gitkeep`, because git cannot track an empty directory and the commit two lines below
    # would otherwise drop every unpopulated container: a converted repo and an init'd one would be
    # different trees at the same schema, and the containers would vanish on `agora sync` + clone.
    materialize_kind_directories(dest_layout)

    # `raw/` is rule 5. `assets/` and `log.md` are not named by D6 at all, and are copied for the
    # same reason: D1 marks both CANONICAL, and a converter that silently drops canonical data is
    # a lossy migration. `log.md` is the append-only curator run log — it records runs against the
    # SOURCE repo, which is exactly why keeping it is more honest than starting a KB whose
    # knowledge appears from nowhere; it sits outside `wiki/`, so lint never reads it either way.
    raw_files = _copy_tree(src_layout.raw_dir, dest_layout.raw_dir)
    asset_files = _copy_tree(src / "assets", dest / "assets")
    if src_layout.log_file.is_file() and not src_layout.log_file.is_symlink():
        shutil.copyfile(src_layout.log_file, dest_layout.log_file)

    for rel, text in sorted(rendered.items()):
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8", newline="\n")

    when = datetime.fromisoformat(f"{import_date}T00:00:00+00:00").astimezone(UTC)
    repo = Repo(dest_layout)
    repo.init(when=when, schema_version=CONVERTER_TARGET_SCHEMA_VERSION, kb_id=kb_id)
    try:
        repo.commit_all("chore: convert schema-1 KB (agora import --from-kb)", when=when)
    except GitError:
        pass  # nothing to commit beyond the init tree (an empty source), not an error

    lint_result = lint(
        dest_layout, taxonomy=taxonomy, schema_version=CONVERTER_TARGET_SCHEMA_VERSION
    )

    records.sort(key=lambda record: record.dest_path)
    skipped = _uncarried_top_level(src)
    summary = {
        "source_notes": len(plans),
        "notes": len(records),
        "renamed": sum(1 for record in records if record.renamed),
        "merged_journals": sum(1 for record in records if record.merged),
        "merged_sources": sum(len(record.src_paths) for record in records if record.merged),
        "subjects_assigned": sum(1 for record in records if record.subjects),
        "raw_files": raw_files,
        "asset_files": asset_files,
        "skipped_paths": len(skipped),
        "lint_findings": len(lint_result.findings),
    }
    return ConvertReport(
        kb_id=kb_id,
        notes=tuple(records),
        renames=tuple(sorted(renames.items())),
        merges=tuple(sorted(merges)),
        summary=summary,
        warnings=tuple(w for record in records for w in record.warnings),
        lint=lint_result,
        skipped=skipped,
    )


def _note_title(plan: _Plan) -> str:
    """One converted note's ``title:``, preserving what the source DECLARED (ADR-0010 §2.1).

    The declared ``title:`` wins because it is what the v1 curator wrote and what every consumer of
    the frontmatter reads; the body H1 is the fallback for a hand-written note that never carried
    one, and the basename is the floor. Taking the H1 FIRST would let a note whose prose heading
    drifted from its declared title be silently re-titled by the conversion.
    """
    declared = plan.note.frontmatter.get("title")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    heading, _ = _split_h1(plan.note.body)
    return heading or plan.src_basename


def _mint_kb_id() -> str:
    """Mint the destination's NEW ``kb_id`` (D6 rule 6): the destination is a new KB, not a
    continuation, so it is never carried over from the source."""
    from agora_kb.core.ids import new_ulid

    return new_ulid()


def _merge_journal(
    date: str,
    group: list[_Plan],
    *,
    kb_id: str,
    src_root: Path,
    src_to_dest: dict[str, str],
    dest_by_basename: dict[str, str],
    renames: dict[str, str],
    fallback_date: str,
) -> tuple[dict[str, object], str, list[str], list[str]]:
    """Collapse every same-date v1 daily into ONE schema-2 journal (D6 rule 4 / D2.6).

    v1 wrote one daily per domain per run and namespaced the basename ``<domain>-YYYY-MM-DD`` for
    one stated reason: bare dates would collide across domains. Schema 2 takes the domain out of the
    path, so the reason is gone and the journal becomes ONE note per ``run_date``, repo-wide — which
    is what makes the note↔``run_id`` relation 1:1 and lets lint L1-14 be a clean identity check.

    The merge rule, stated so the degenerate case is visibly an identity: **lists union in order**
    (``tags``, ``aliases``, ``sources`` and the contributing domains as ``subjects``), **the body
    concatenates one ``## <contributor title>`` section per contributor in domain order**, and every
    remaining scalar — ``run_id`` included, exactly as D6 says — is taken from the FIRST contributor
    in domain order. With a single contributor every clause reduces to that daily's own value and
    the only thing that changed is the basename.
    """
    first = group[0]
    sections: list[str] = []
    warnings: list[str] = []
    for plan in group:
        heading, body = _split_h1(plan.note.body)
        heading = heading or _note_title(plan)
        rewritten = _rewrite_body(
            body,
            src_root=src_root,
            src_rel=plan.src_rel,
            dest_rel=plan.dest_rel,
            src_to_dest=src_to_dest,
            dest_by_basename=dest_by_basename,
            renames=renames,
        )
        sections.append(f"## {heading}\n\n{rewritten}".rstrip())
        warnings.extend(plan.warnings or ())
    if len(group) > 1:
        warnings.append(
            f"merged {len(group)} same-date dailies into one journal basenamed {date!r} "
            f"(ADR-0041 D2.6 — one journal per run_date, repo-wide)"
        )

    subjects = _ordered_union([plan.subject] if plan.subject else [] for plan in group)
    merged_fm: dict[str, object] = dict(first.note.frontmatter)
    merged_fm["aliases"] = _ordered_union(
        _str_list(plan.note.frontmatter.get("aliases")) for plan in group
    )
    merged_fm["tags"] = _ordered_union(
        _str_list(plan.note.frontmatter.get("tags")) for plan in group
    )
    merged_fm["sources"] = _ordered_union(
        _str_list(plan.note.frontmatter.get("sources")) for plan in group
    )
    summaries = _ordered_union(
        [s.strip()]
        for s in (plan.note.frontmatter.get("summary") for plan in group)
        if isinstance(s, str) and s.strip()
    )
    merged_fm["summary"] = " ".join(summaries) if summaries else date
    merged_fm["date"] = date
    fm = _dest_frontmatter(
        merged_fm,
        kind="note",
        kb_id=kb_id,
        subjects=subjects,
        renames=renames,
        title=date,
        fallback_date=fallback_date,
        warnings=warnings,
    )
    return fm, f"# {date}\n\n" + "\n\n".join(sections), subjects, warnings
