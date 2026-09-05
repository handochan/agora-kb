"""Filesystem layout for a single knowledge repo.

Pure path resolution over the per-repo layout defined in docs/DESIGN.md §3. The knowledge itself
(`raw/`, `wiki/`, `index.md`, `log.md`) is git-tracked; `_kb/` is the git-ignored operational spool
(inbox / processing / processed / failed / state / lock). This module computes paths only — it never
touches git or the network and creates no directories on its own (callers create what they write).

Writer namespacing is a hard tenant-isolation boundary (DESIGN §7, invariant 5): a writer name is a
directory component, validated to a safe charset with no path separators or ``..`` so a caller can
never escape the repo's inbox. Event ids are validated by :mod:`agora_kb.core.ids`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .pathsafe import DEFAULT_MAX_BYTES, is_safe_component, is_safe_filename_stem

__all__ = [
    "RepoLayout",
    "InvalidWriterError",
    "InvalidNoteBasenameError",
    "InvalidAttachmentError",
    "InvalidAttachmentExtError",
    "validate_writer",
    "safe_path_component",
    "validate_attachment_digest",
    "validate_attachment_ext",
    "attachment_ext_for",
    "attachment_basename",
    "attachment_dir",
    "ATTACHMENT_DIRNAME",
    "DEFAULT_ATTACHMENT_EXT",
    "BLOB_PREFIX",
    "SIDECAR_SUFFIX",
    "blob_ref",
    "KIND_DIRECTORIES",
    "WIKI_KINDS",
    "CLAIM_BEARING_KINDS",
]

# A writer is a single path component: starts alphanumeric, then alphanumerics plus ._- . No
# slashes, no leading dot, and ".."/"." are rejected by the leading-alphanumeric anchor. This is
# what keeps a write confined to ``_kb/inbox/<writer>/`` (tenant isolation, DESIGN §7).
_WRITER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_WRITER_MAX = 128  # keep within a single filesystem path component (NAME_MAX is ~255 bytes)


class InvalidWriterError(ValueError):
    """Raised when a writer name is unsafe as a filesystem path component."""


class InvalidNoteBasenameError(ValueError):
    """Raised when a note basename cannot be composed into a schema-2 wiki path (ADR-0041 D4.4)."""


class InvalidAttachmentError(ValueError):
    """Raised when an attachment's content digest cannot address a file (ADR-0041 D1.4/D4.2)."""


class InvalidAttachmentExtError(InvalidAttachmentError):
    """Raised when an attachment extension violates the D1.4 ``<ext>`` grammar."""


#: The per-writer attachment staging directory name, and the SAME name beside an event wherever the
#: spool has moved it (``_kb/inbox/<writer>/_attach/`` → ``processing/<run-id>/events/_attach/`` →
#: ``processed/<date>/_attach/``). One name, so a reader that has an event path can always find its
#: bytes by looking beside it (:func:`attachment_dir`); the leading ``_`` keeps it out of the
#: ``<id>.md`` event namespace, which every spool glob addresses by extension.
ATTACHMENT_DIRNAME = "_attach"

#: The extension used when a filename carries none this grammar accepts. ADR-0041 D1.4's own
#: example (``raw/_blob/ab/<correct-sha>.bin``) uses it, and it is the no-loss choice: the display
#: ``filename`` is preserved verbatim on the attachment record and in the ``raw/_blob/`` sidecar, so
#: falling back here loses nothing a reader needs, while REFUSING would make ``agora capture
#: --file`` reject a perfectly capturable artefact over its name.
DEFAULT_ATTACHMENT_EXT = "bin"

# ADR-0041 D1.4, normative: an attachment/blob extension is EXACTLY ONE component —
# `[a-z0-9]{1,16}`, lowercase, containing no `.`. A dotted compound (`tar.gz`, `meta.yaml`) is
# forbidden so the composer can never mint a `<sha256>.meta.yaml`-shaped artefact name, which lint
# L1-8b (a pure `endswith(".meta.yaml")` suffix test) would then permanently refuse to let anything
# cite. `meta` is excluded outright by the same rule.
_ATTACHMENT_EXT_RE = re.compile(r"\A[a-z0-9]{1,16}\Z")
_RESERVED_ATTACHMENT_EXTS = frozenset({"meta"})
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


# KB WIKI SCHEMA 2 (ADR-0041 D1): the FIRST path segment under ``wiki/`` IS the note's kind, and
# this mapping is the ONLY place the two are related for PATH COMPOSITION. ``index`` is absent on
# purpose — the root map lives at ``<repo>/index.md``, outside ``wiki/`` (D1.2) — and so is
# ``person``: ``wiki/people/<person>/**`` is a HUMAN-OWNED namespace the curator may never write
# (D3.3), so no code path composes a path into it.
KIND_DIRECTORIES: dict[str, str] = {
    "concept": "concepts",
    "summary": "summaries",
    "note": "notes",
    "map": "maps",
    "entity": "entities",
}

# The closed kind vocabulary as it appears UNDER ``wiki/`` (D3.1's segment-1 set), i.e. the
# directories above plus the human-owned ``people/``. The lint-facing rule that rejects any other
# directory (L1-22) reads its own copy in ``schema/``; this one exists for path composition.
WIKI_KINDS: frozenset[str] = frozenset({*KIND_DIRECTORIES, "person"})

#: The CLAIM-BEARING kinds — the tiers that carry an atomic, sourced claim (ADR-0041 D2.5).
#:
#: It lives HERE, next to the kind vocabulary itself, because three modules ask the same question
#: of it and an operator cannot act on three answers: ``schema.lint._V2_SOURCED_KINDS`` gates
#: L1-7 / L1-8 / L1-8b / L1-10 / L1-19 and the L2-1 orphan warning that stops a curator run
#: (``curator.lint.max_orphans``, ADR-0022 §E), ``faces.mcp_server.ORPHAN_KINDS`` is the SAME
#: orphan population as the dashboard reports it, and ``core.gold.GOLD_KINDS`` is what a context
#: pack may contain. All three used to be independently-declared literals whose docstrings merely
#: PROMISED to stay equal; each now binds to this object, so the promise is structural.
#:
#: The set is schema-agnostic on purpose (:attr:`Note.kind <agora_kb.schema.notes.Note.kind>` is
#: derived on both schemas and the frozen D2.5 table maps ``type: theme`` → ``concept``), and
#: ``entity`` is deliberately absent: D2 lets an entity carry empty ``sources:`` while
#: ``status: stub``, and ``wiki/entities/`` has no day-1 producer (OD-8). ``map`` / ``index`` /
#: ``note`` are navigation and journal tiers, not claims; ``person`` is human-owned (D3.3).
CLAIM_BEARING_KINDS: frozenset[str] = frozenset({"concept", "summary"})

# Kinds whose notes are FLAT under their kind directory (D1.1: free sub-folders are permitted but
# nothing composes one). ``note`` is the exception — it is date-sharded, see :meth:`note_path_for`.
_FLAT_KINDS = frozenset(KIND_DIRECTORIES) - {"note"}

_RUN_DATE_RE = re.compile(r"\A(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\Z")


def validate_writer(writer: str) -> str:
    """Return ``writer`` unchanged if it is a safe path component, else raise.

    Guards the inbox namespace against path traversal (``..``, ``/``) so a write can never land
    outside ``_kb/inbox/<writer>/`` (tenant isolation, DESIGN §7).
    """
    if not isinstance(writer, str) or not _WRITER_RE.match(writer) or len(writer) > _WRITER_MAX:
        raise InvalidWriterError(
            f"invalid writer {writer!r}: must match {_WRITER_RE.pattern}, "
            f"be 1-{_WRITER_MAX} chars, with no path separators or '..'"
        )
    return writer


def safe_path_component(value: str) -> str:
    """Validate ``value`` as a safe single path component (same charset/guards as a writer).

    Reused by the harvester to confine a connector's cursor file to ``_kb/harvest/`` (ADR-0007):
    a DATA-MODEL §8 connector key like ``file:claude-code`` contains a ``:`` that is not a safe
    filename, so the caller sanitizes it (``:`` → ``-``) and passes the *derived* stem here to
    reject any residual path separator / ``..`` before it is interpolated into a path. Raises
    :class:`InvalidWriterError` on an unsafe component (the same guard the inbox namespace uses).
    """
    return validate_writer(value)


# --- attachment addressing (ADR-0041 D4.2 transport; D1.4 destination grammar) -------------------
def validate_attachment_digest(sha256: str) -> str:
    """Return ``sha256`` unchanged if it is 64 lowercase hex characters, else raise.

    The digest is the attachment's NAME — in the staging area and, later, in
    ``raw/_blob/<ab>/<sha256>.<ext>`` — so validating it here is a path-escape control, not a
    formatting nicety: without it a caller-supplied ``../../../wiki/PWNED`` reaches a filesystem
    join. It is the same 64-hex shape :class:`agora_kb.core.models.Attachment` validates on the
    model, checked again at the composition site because character filtering at one layer is not a
    substitute for it at the other (``core/pathsafe.py``'s "what this module is NOT").
    """
    if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
        raise InvalidAttachmentError(
            f"attachment digest must be 64 lowercase hex characters, got {sha256!r}"
        )
    return sha256


def validate_attachment_ext(ext: str) -> str:
    """Return ``ext`` unchanged if it satisfies the ADR-0041 D1.4 ``<ext>`` grammar, else raise.

    Exactly one component: ``[a-z0-9]{1,16}``, lowercase, no ``.``, and never ``meta``. The dot-free
    rule is what makes ``<sha256>.<ext>`` and its ``<sha256>.<ext>.meta.yaml`` sidecar structurally
    distinguishable, so lint L1-8b ("cite the artefact, not its sidecar") keeps working unmodified;
    the ``meta`` exclusion is the ADR's own belt to that braces. No leading ``.`` and no ``_`` can
    survive the character class, so an attachment can neither become a dotfile nor claim one of the
    ``raw/`` reserved prefixes.
    """
    if not isinstance(ext, str) or not _ATTACHMENT_EXT_RE.match(ext):
        raise InvalidAttachmentExtError(
            f"attachment extension {ext!r} must be 1-16 lowercase letters/digits with no '.' "
            "(ADR-0041 D1.4)"
        )
    if ext in _RESERVED_ATTACHMENT_EXTS:
        raise InvalidAttachmentExtError(
            f"attachment extension {ext!r} is reserved: it would collide with the "
            "'<file>.meta.yaml' sidecar naming rule (ADR-0041 D1.4)"
        )
    return ext


def attachment_ext_for(filename: str | None) -> str:
    """Derive an attachment extension from a DISPLAY filename, or :data:`DEFAULT_ATTACHMENT_EXT`.

    Total by construction — it never raises and never invents a path component: whatever the caller
    was handed by a browser, a shell or an untrusted upload, the result is a valid
    :func:`validate_attachment_ext` token. A filename with no extension, a compound one
    (``a.tar.gz`` → ``gz``, i.e. the LAST component only), an over-long or non-``[a-z0-9]`` one, and
    the reserved ``meta`` all fall back to ``bin`` rather than being rejected or mangled into a new
    string. The original name survives verbatim on the attachment record, so the fallback loses
    nothing but the guess.

    **Deliberately NOT narrowed to the extractor's accepted extension set**, which is what ADR-0041
    D1.4 first said. ``agora capture --file`` exists precisely so an artefact nobody can extract
    today can be kept until somebody can; a composer restricted to that list would rename every one
    of those files to ``bin`` and throw away the only type information the operator had. The
    GRAMMAR is what carries the safety property (no ``.``, no leading ``_``, no dotfile, never
    ``meta``, so ``<sha256>.<ext>`` and its ``.meta.yaml`` sidecar stay structurally
    distinguishable and lint L1-8b keeps working); the extractor's list never was. What a FACE will
    accept remains the operator's ``web.extensions.allowed`` gate, one layer up (ADR-0025). See the
    ADR-0041 addendum *"as-built: the capture transport and the ``raw/_blob/`` sidecar"* §3.
    """
    if not filename:
        return DEFAULT_ATTACHMENT_EXT
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        return validate_attachment_ext(suffix)
    except InvalidAttachmentExtError:
        return DEFAULT_ATTACHMENT_EXT


def attachment_basename(sha256: str, ext: str) -> str:
    """``<sha256>.<ext>`` — the content-addressed filename, with both halves validated.

    ONE spelling for the staging file, the ``raw/_blob/`` destination and every mover in between,
    so the three can never disagree about what an attachment is called.
    """
    return f"{validate_attachment_digest(sha256)}.{validate_attachment_ext(ext)}"


def attachment_dir(event_dir: Path) -> Path:
    """The ``_attach/`` directory holding the staged bytes of the events in ``event_dir``.

    Pure path resolution, deliberately taking a DIRECTORY rather than a :class:`RepoLayout`: the
    attachment travels WITH its event through the spool (``inbox/<writer>/`` →
    ``processing/<run-id>/events/`` → ``processed/<date>/`` or ``failed/<date>/<run-id>/``), and
    every one of those directories addresses its events as ``<id>.md`` children. Keeping the
    sidecar directory name constant means a reader with an event path can always find the bytes
    beside it without knowing which lifecycle stage it is in.
    """
    return event_dir / ATTACHMENT_DIRNAME


# --- raw/_blob/ addressing: the ORIGINAL BYTES of a captured artefact (ADR-0041 D1.4 / D4.2) -----
#
# These three names came DOWN from ``curator/apply.py``, where they were private, when the read face
# gained a ``raw/`` door (#169, DRILLDOWN-169 D2). ``core`` may not import ``curator``
# (ADR-0001/0003), so the composer has to live below both: one spelling of the destination path for
# the single writer that materializes it and for every reader that resolves a ``sources:`` citation
# back to it. Two spellings would be two chances for a reader to look somewhere the writer never
# wrote.

#: The reserved ``raw/`` prefix the content-addressed originals live under (ADR-0041 D1.4). A
#: literal, not a token, so nothing model-supplied can ever reach this path segment.
BLOB_PREFIX = "raw/_blob"

#: The ``.meta.yaml`` sidecar suffix. It is appended to the FULL blob filename (``<sha256>.<ext>``),
#: never substituted for its extension — that is what keeps lint L1-8b (a pure ``endswith`` sidecar
#: test) working unmodified while ``.yaml`` stays a legal artefact extension (D1.4).
SIDECAR_SUFFIX = ".meta.yaml"


def blob_ref(sha256: str, ext: str) -> str:
    """``raw/_blob/<ab>/<sha256>.<ext>`` for one attachment — the ONE composition of that path.

    ``<ab>`` is the first two hex characters of the digest (a fan-out shard, ADR-0041 D1.4). Both
    halves of the basename are re-validated by :func:`attachment_basename` BEFORE the shard is
    sliced off it, so a record that reached here from a hand-edited spool file still cannot compose
    a path segment — and the shard is taken from the validated basename rather than from the raw
    argument, which makes it structurally impossible for the two to disagree.
    """
    basename = attachment_basename(sha256, ext)
    return f"{BLOB_PREFIX}/{basename[:2]}/{basename}"


@dataclass(frozen=True)
class RepoLayout:
    """Resolves the canonical paths of one knowledge repo rooted at ``root``.

    ``root`` is the repo working directory. All paths are derived; nothing is created here.
    """

    root: Path

    def __post_init__(self) -> None:
        # Normalise to an absolute path without resolving symlinks (the repo root itself may be a
        # symlinked checkout; we only need a stable absolute base for joining).
        object.__setattr__(self, "root", Path(self.root).absolute())

    # --- git-tracked knowledge ------------------------------------------------------------------
    @property
    def index_file(self) -> Path:
        return self.root / "index.md"

    @property
    def log_file(self) -> Path:
        return self.root / "log.md"

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def wiki_dir(self) -> Path:
        return self.root / "wiki"

    @property
    def schema_file(self) -> Path:
        return self.root / "AGENTS.md"

    # --- KB wiki schema 2: the kind-first wiki tree (ADR-0041 D1) -------------------------------
    # ADDITIVE. Nothing here is read by a schema-1 repo: these accessors NAME the schema-2 layout,
    # and which layout a repo is on is decided by its `_meta/taxonomy.yaml` schema_version, not by
    # the existence of a path. A schema-1 tree simply has no `wiki/concepts/` for these to resolve
    # to, and computing a path creates nothing (this module never touches the filesystem).

    @property
    def concepts_dir(self) -> Path:
        """``wiki/concepts/`` — kind ``concept``, the schema-2 successor to v1's ``type: theme``."""
        return self.wiki_dir / KIND_DIRECTORIES["concept"]

    @property
    def summaries_dir(self) -> Path:
        """``wiki/summaries/`` — kind ``summary``. SHIPS EMPTY: no day-1 producer (ADR-0041 OD-7).

        The container exists so the tier does not need a second migration when its contract
        (reserved ADR-0040) lands; nothing writes into it in this wave or the next.
        """
        return self.wiki_dir / KIND_DIRECTORIES["summary"]

    @property
    def notes_dir(self) -> Path:
        """``wiki/notes/`` — kind ``note`` (v1's ``type: daily``), date-sharded ``<yyyy>/<mm>/``."""
        return self.wiki_dir / KIND_DIRECTORIES["note"]

    @property
    def maps_dir(self) -> Path:
        """``wiki/maps/`` — kind ``map`` (v1's ``type: moc``, formerly ``<domain>-moc.md``).

        The ``-moc`` filename suffix is gone: the kind marker moved into the directory, which is
        what ADR-0041 D5 re-expresses ``_is_moc_path`` against. ``index.md`` is NOT in here — the
        root map keeps its own kind at the repo root (D1.2, :attr:`index_file`).
        """
        return self.wiki_dir / KIND_DIRECTORIES["map"]

    @property
    def entities_dir(self) -> Path:
        """``wiki/entities/`` — kind ``entity``. SHIPS EMPTY: no day-1 producer (ADR-0041 OD-8)."""
        return self.wiki_dir / KIND_DIRECTORIES["entity"]

    @property
    def people_dir(self) -> Path:
        """``wiki/people/`` — the HUMAN-OWNED namespace (ADR-0041 D3.3).

        Outside the subject of invariant 2: the curator NEVER writes under it (a curated diff
        touching it fails the run, D4.1), lint does not grade it, and its basenames are outside the
        global basename identity space. Read is first class. There is deliberately no
        ``note_path_for`` composition into this tree — nothing in Agora authors a person note.
        """
        return self.wiki_dir / "people"

    @property
    def blob_dir(self) -> Path:
        """``raw/_blob/`` — content-addressed original bytes (ADR-0041 D1.4/D4.2).

        ``raw/_blob/<ab>/<sha256>.<ext>`` plus its ``.meta.yaml`` sidecar, where ``<ab>`` is the
        first two hex characters of the digest. Immutable. Written ONLY by the deterministic APPLY
        pass and admitted ONLY by membership in ``raw_writes`` with matching bytes — the
        content-addressing is an ADDITIONAL integrity self-check, never a substitute for that
        authorship check (D1.4, normative). Populated by wave W2.5; the accessor is the reservation.
        """
        return self.raw_dir / "_blob"

    @property
    def pages_dir(self) -> Path:
        """``raw/_pages/`` — RESERVED PREFIX ONLY (ADR-0041 D1.4/D4.3).

        Nothing writes it, and the reservation grants it no gate exception: a file appearing here
        fails the final diff exactly like any other unauthored ``raw/`` path. It exists so the
        long-document contract (reserved ADR-0040) can populate it without re-arguing an exception,
        and so ``_pages`` is a reserved ``raw/`` domain name (the L1-23 namespace).
        """
        return self.raw_dir / "_pages"

    @property
    def meta_dir(self) -> Path:
        """``_meta/`` — repo-init/admin inputs, READ-ONLY during INGEST (taxonomy + KB identity)."""
        return self.root / "_meta"

    @property
    def kb_meta_file(self) -> Path:
        """``_meta/kb.yaml`` — the CLOSED-key-set KB identity (ADR-0041 D1.5).

        ``{kb_id, name, declared_kind}`` and nothing else: git-tracked, so POLICY must never live
        here (a git-tracked enforcing ``kind`` would let an upstream author unlock a downstream
        operator's personal-scope connectors). The enforcing values stay in git-ignored
        ``_kb/repo.yaml``. Loaded/written by ``agora_kb.config.load_kb_identity`` /
        ``write_kb_identity``, which enforce the closed key set.
        """
        return self.meta_dir / "kb.yaml"

    # --- _kb/ operational spool (git-ignored) ---------------------------------------------------
    @property
    def kb_dir(self) -> Path:
        return self.root / "_kb"

    @property
    def inbox_dir(self) -> Path:
        return self.kb_dir / "inbox"

    @property
    def processing_dir(self) -> Path:
        return self.kb_dir / "processing"

    @property
    def processed_dir(self) -> Path:
        return self.kb_dir / "processed"

    @property
    def failed_dir(self) -> Path:
        return self.kb_dir / "failed"

    @property
    def requeued_dir(self) -> Path:
        """Archived retry-budget records ``_kb/requeued/`` (issue #99, ADR-0011 §5.1).

        MUST live outside :attr:`failed_dir`: the budget derivation is
        ``failed_dir.rglob("error.json")`` and ``rglob`` descends into dotted dirs, so a tidier
        ``_kb/failed/.archive/`` would still be counted and ``agora requeue --reset-attempts``
        would silently reset nothing (verified; locked by a regression test). Not created here.
        """
        return self.kb_dir / "requeued"

    def requeued_record_path(self, *, date: str, run_id: str) -> Path:
        """Archive destination of one run's error record — the ``_kb/failed/`` twin (#99).

        The shape is preserved EXACTLY (``<date>/<run-id>/error.json``) so the mapping from a
        stored ``last_failure.record_path`` to its archived twin is structural rather than a string
        guess, and so two runs on one date can never collide. Both components come from a directory
        name read off disk — ``_kb/failed/`` is operator-editable — so both go through
        :func:`safe_path_component` (the DESIGN §7 guard the inbox/harvest/gold/index namespaces
        use); an unsafe component raises :class:`InvalidWriterError` and the caller reports + skips.
        """
        return (
            self.requeued_dir
            / safe_path_component(date)
            / safe_path_component(run_id)
            / "error.json"
        )

    @property
    def harvest_dir(self) -> Path:
        """Per-connector harvester cursor directory ``_kb/harvest/`` (ADR-0007, DATA-MODEL §6)."""
        return self.kb_dir / "harvest"

    @property
    def index_cache_dir(self) -> Path:
        """Derived READER cache directory ``_kb/index/`` (ADR-0012 §2, issue #26).

        Git-ignored, NEVER canonical, fully rebuildable from the markdown at the curated commit
        (invariant #1). Holds one parsed-note cache per repo (``<repo>.notes.json``; the ADR-0012 §9
        FTS5/ripgrep candidate accelerators are deferred to a load-avoiding reader, issue #28). Not
        created here (this module only computes paths); the writer mkdirs before writing.
        """
        return self.kb_dir / "index"

    @property
    def gold_dir(self) -> Path:
        """Derived gold context-pack directory ``_kb/gold/`` (ADR-0027, issue #37).

        Git-ignored, NEVER canonical, a pure function of ``(curated commit, pack spec)`` and fully
        rebuildable from the wiki (invariant #1) — same derived-state posture as ``_kb/harvest/``
        (cursor) and ``_kb/index/`` (reader cache), DATA-MODEL §6. Holds one ``<pack>.md`` pack plus
        its ``<pack>.meta.json`` sidecar per pack. Not created here (this module only computes
        paths); the writer mkdirs before writing.
        """
        return self.kb_dir / "gold"

    def gold_pack_path(self, pack: str) -> Path:
        """Path of one gold pack ``_kb/gold/<pack>.md`` (ADR-0027 decision 3, issue #37).

        ``pack`` is the pack name (v1 ships an implicit ``default`` pack). It is validated as a safe
        single path component (:func:`safe_path_component`) BEFORE the extension is appended, so a
        malformed/hostile pack name can never escape ``_kb/gold/`` — the same path-traversal guard
        the inbox / harvest / index namespaces use (DESIGN §7).
        """
        stem = safe_path_component(pack)
        return self.gold_dir / f"{stem}.md"

    def gold_meta_path(self, pack: str) -> Path:
        """Path of a gold pack's sidecar meta ``_kb/gold/<pack>.meta.json`` (ADR-0027 decision 3).

        The sidecar carries the wall-clock ``generated_at``/age, the ``(curated_sha, spec_hash)``
        invalidation key, and the per-input provenance — everything the byte-identical pack body
        must NOT carry so its bytes stay stable on rebuild (prompt-cache economics). ``pack`` is
        validated as a safe path component (as in :meth:`gold_pack_path`) before the extension.
        """
        stem = safe_path_component(pack)
        return self.gold_dir / f"{stem}.meta.json"

    @property
    def backup_state_path(self) -> Path:
        """Last push-only backup result ``_kb/backup.json`` (issue #64).

        Non-canonical operational metadata (the same derived-state posture as ``_kb/harvest/``
        cursors, DATA-MODEL §6): the outcome + instant of the last ``agora sync`` / watch-tick
        backup push, read by the ``agora doctor`` observability line. Expendable — losing it only
        blanks that line. Not created here (this module only computes paths).
        """
        return self.kb_dir / "backup.json"

    @property
    def state_file(self) -> Path:
        return self.kb_dir / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.kb_dir / "curator.lock"

    # --- inbox addressing -----------------------------------------------------------------------
    def inbox_writer_dir(self, writer: str) -> Path:
        """Per-writer inbox directory ``_kb/inbox/<writer>/`` (writer validated)."""
        return self.inbox_dir / validate_writer(writer)

    def inbox_item_path(self, writer: str, event_id: str) -> Path:
        """Path of one inbox event ``_kb/inbox/<writer>/<event_id>.md`` (writer validated)."""
        return self.inbox_writer_dir(writer) / f"{event_id}.md"

    def inbox_attachment_dir(self, writer: str) -> Path:
        """Attachment staging directory ``_kb/inbox/<writer>/_attach/`` (ADR-0041 D4.2).

        INSIDE the writer's own namespace, so an attachment inherits the tenant-isolation boundary
        the event already has (invariant 5): one ``validate_writer`` covers both halves of one
        delivery. It is not a second spool — the bytes are drained with their event, and the whole
        of ``_kb/`` stays git-ignored.
        """
        return attachment_dir(self.inbox_writer_dir(writer))

    def inbox_attachment_path(self, writer: str, sha256: str, ext: str) -> Path:
        """Path of one staged attachment ``_kb/inbox/<writer>/_attach/<sha256>.<ext>``.

        Content-addressed, so two events carrying identical bytes under the same extension name one
        file. Writer, digest and extension are ALL validated before the join
        (:func:`validate_writer`, :func:`attachment_basename`) — the digest and extension arrive
        from the same untrusted upload the bytes did.
        """
        return self.inbox_attachment_dir(writer) / attachment_basename(sha256, ext)

    # --- harvester cursor addressing ------------------------------------------------------------
    def harvest_cursor_path(self, connector: str) -> Path:
        """Path of one connector's cursor ``_kb/harvest/<stem>.json`` (DATA-MODEL §6, ADR-0007).

        ``connector`` is the operator's ``adapters.yaml`` connector key (e.g. ``file:claude-code``),
        which is NOT a safe filename — DATA-MODEL §8 keys contain a ``:``. The colon is sanitized to
        ``-`` and the resulting stem is validated as a safe single path component
        (:func:`safe_path_component`) so a malformed/hostile connector key can never escape
        ``_kb/harvest/`` (the same path-traversal guard the inbox namespace uses, DESIGN §7).
        """
        stem = safe_path_component(connector.replace(":", "-"))
        return self.harvest_dir / f"{stem}.json"

    # --- derived reader cache addressing (ADR-0012 §2, issue #26) --------------------------------
    def index_notes_path(self, repo: str | None = None) -> Path:
        """Path of the parsed-note cache ``_kb/index/<repo>.notes.json`` (ADR-0012 §2, issue #26).

        ``repo`` defaults to the repo directory name (the ``SearchHit.repo`` identity). It is
        checked with :func:`agora_kb.core.pathsafe.is_safe_filename_stem` BEFORE the extension is
        appended, so an unusual repo dir name can never escape ``_kb/index/`` (DESIGN §7). A name
        that is not a safe stem raises :class:`InvalidWriterError`; the read path treats that as
        "no cache" and falls back to a full scan (query never crashes).

        The predicate is the pathsafe **union** rule, not :func:`safe_path_component` (DRILLDOWN-169
        D17, issue #167): a derived cache filename is not a write namespace, so a non-ASCII repo
        directory such as ``내지식`` addresses ``_kb/index/내지식.notes.json`` rather than silently
        losing its cache, while every stem the legacy writer charset admits (``con``, ``foo-``,
        ``foo.``) keeps the cache it has today. ``safe_path_component`` itself is untouched — it
        remains the inbox/harvest tenant guard. The predicate never REWRITES, so the guard still
        refuses to invent a stem for a name it rejects.

        A repo directory named e.g. ``My Knowledge`` is perfectly legal on disk but is still NOT a
        safe stem (no whitespace), so no cache file can be addressed for it (issue #108). The raise
        carries an OPERATOR-facing message — what did not happen, why, and what still works — so
        every call site (read fallbacks, ``agora index status``/``clear``, and the ``build_cache``
        write path) can surface the same explanation verbatim instead of a bare regex mismatch.
        The message is deliberately plain prose with NO parentheses and NO raw regex: every one of
        those call sites interpolates it INSIDE its own ``(...)``, so a nested group or a
        ``\\A[A-Za-z0-9]...`` blob would land in front of the operator verbatim. The remedy clause
        follows the actual input — renaming the directory only helps when the stem CAME from the
        directory name (``repo is None``); an explicit stem is the caller's to fix.
        """
        name = repo if repo is not None else self.root.name
        if not is_safe_filename_stem(name):
            remedy = (
                "rename the repo directory to a safe name to enable the cache"
                if repo is None
                else "pass a cache stem that is a safe component to enable the cache"
            )
            # The rule sentence states the UNION predicate, not the legacy writer charset it
            # inherited: after D17 widened `is_safe_filename_stem`, a message naming only ASCII
            # would tell an operator that `내지식` is unusable — the exact belief #167 exists to
            # fix, in the one place they ever read the rule. The ASCII half keeps its POSITIONAL
            # constraint ("starts with a letter or digit") because neither half of the union
            # admits a leading '-': dropping the clause would promise that `-foo` is accepted
            # when `index_notes_path('-foo')` in fact raises. Remedy clause deliberately unchanged
            # so `cli.py` / `core/wiki.py` operator output stays recognisable.
            raise InvalidWriterError(
                f"repo name {name!r} is not usable as a cache filename component: it must be a "
                f"single filename component with no path separator, no leading dot and no "
                f"whitespace. Letters, digits, '.', '_' and '-' are always accepted up to "
                f"{_WRITER_MAX} characters when the name starts with a letter or digit, and "
                f"other scripts up to {DEFAULT_MAX_BYTES} UTF-8 "
                f"bytes. No cache file can be addressed for this repo, so search/query fall back "
                f"to a full scan; {remedy}"
            )
        return self.index_cache_dir / f"{name}.notes.json"

    # --- KB wiki schema 2: note path composition (ADR-0041 D1/D1.1/D2.6) -------------------------
    def note_path_for(
        self,
        kind: str,
        basename: str,
        *,
        run_date: str | None = None,
    ) -> Path:
        """Compose the schema-2 path of one note from its KIND and basename (ADR-0041 D1).

        The whole point of schema 2 is that the path is a function of the KIND, not of the subject:
        ``concept``/``summary``/``map``/``entity`` land flat under their kind directory, ``note``
        lands under the ``<yyyy>/<mm>`` date shard, and ``index`` is the root map at ``index.md``
        (D1.2 — it is the root OF the map tier, not a member of it). Free sub-folders under a kind
        are legal on disk (D1.1) but nothing COMPOSES one, so this returns the canonical location.

        ``run_date`` (``YYYY-MM-DD``) is REQUIRED for ``kind='note'`` and is where the shard comes
        from. It is a deterministic curator-owned fact injected by the caller, never parsed back
        out of a model-supplied basename — that is exactly the inversion D2.6 forbids, because it
        would make a curator-owned path segment a function of model output. For the flat kinds a
        supplied ``run_date`` is IGNORED (harmless: they have no shard), so a caller with a run
        date in scope need not branch.

        ``person`` is deliberately NOT composable: ``wiki/people/**`` is human-owned and the
        curator may never write it (D3.3), so a caller asking for a person path is a bug, not a
        path. Raises :class:`ValueError` for an unknown kind and
        :class:`InvalidNoteBasenameError` for a basename that is not a safe path component.
        """
        stem = _validate_note_basename(basename)
        if kind == "index":
            # D1.2: exactly one, at the repo root, basenamed `index`. A different basename with
            # kind `index` is a caller bug — silently relocating it would create a second root map.
            if stem != "index":
                raise InvalidNoteBasenameError(
                    f"kind 'index' has exactly one note, basenamed 'index' (ADR-0041 D1.2); "
                    f"got {basename!r}"
                )
            return self.index_file
        if kind == "person":
            raise ValueError(
                "kind 'person' has no composed path: wiki/people/** is human-owned and the "
                "curator never writes it (ADR-0041 D3.3)"
            )
        directory = KIND_DIRECTORIES.get(kind)
        if directory is None:
            raise ValueError(
                f"unknown note kind {kind!r}: expected one of "
                f"{sorted([*KIND_DIRECTORIES, 'index'])} (ADR-0041 D2.5)"
            )
        if kind in _FLAT_KINDS:
            return self.wiki_dir / directory / f"{stem}.md"
        # kind == "note": the D1.1 date shard, composed from run_date and asserted against the
        # basename (below) so a mismatched basename is a caller-side failure here rather than a
        # note that lints clean in the wrong month (D2.6).
        if run_date is None:
            raise ValueError(
                "kind 'note' is date-sharded: run_date (YYYY-MM-DD) is required to compose "
                "wiki/notes/<yyyy>/<mm>/ (ADR-0041 D1.1)"
            )
        match = _RUN_DATE_RE.match(run_date)
        if match is None:
            raise ValueError(f"run_date must be YYYY-MM-DD, got {run_date!r}")
        if stem != run_date:
            # D2.6: one journal per run_date, repo-wide, BASENAMED that date — so basename and
            # shard are two views of one curator-owned fact. Asserted here rather than trusted:
            # composing `wiki/notes/2026/01/finance-2026-01-12.md` would silently return a path
            # lint L1-14 hard-rejects ("basename is not YYYY-MM-DD"), turning a caller-side bug
            # into a failed run at the gate instead of a refusal at the composer.
            raise InvalidNoteBasenameError(
                f"kind 'note' is basenamed by its run_date: expected {run_date!r}, got "
                f"{basename!r} (ADR-0041 D2.6 — one journal per run_date, repo-wide)"
            )
        return self.wiki_dir / directory / match["y"] / match["m"] / f"{stem}.md"


def _validate_note_basename(basename: str) -> str:
    """Validate a note basename as a single, safe, non-reserved path component (ADR-0041 D4.4).

    Two controls, and the second is normative rather than tidy. (1) The component must pass
    :func:`agora_kb.core.pathsafe.is_safe_component` — the closed Unicode-CATEGORY allowlist that
    replaces the ASCII ``_SAFE_TOKEN_RE_PATTERN``, so separators, controls, bidi overrides and the
    Windows-hostile characters are unreachable without being enumerated, and a Korean title yields
    a Korean component instead of an empty one. (2) It must NOT begin with ``_``: pathsafe puts
    ``_`` in its allowed extras, so it would happily accept ``_blob``/``_pages``/``_kb`` — the
    exact reserved prefixes the ASCII regex used to exclude by construction. ADR-0041 D4.4 makes
    that rejection a precondition of the swap, and D1.4 layer 2 puts the SECOND layer over the
    other input (a taxonomy domain never passes through here, and a basename never passes through
    taxonomy load).

    A ``.md`` suffix is rejected rather than stripped: the caller composes an extension-less
    basename and a silent strip would hide a caller that is passing a filename or a path.
    """
    if not isinstance(basename, str) or not basename:
        raise InvalidNoteBasenameError(
            f"note basename must be a non-empty string, got {basename!r}"
        )
    if basename.startswith("_"):
        raise InvalidNoteBasenameError(
            f"note basename {basename!r} may not begin with '_': the leading underscore is "
            "reserved for the raw/_blob and raw/_pages namespaces (ADR-0041 D1.4/D4.4)"
        )
    if basename.endswith(".md"):
        raise InvalidNoteBasenameError(
            f"note basename {basename!r} must not carry the .md extension (it is appended here)"
        )
    if not is_safe_component(basename):
        raise InvalidNoteBasenameError(
            f"note basename {basename!r} is not a safe path component: it must be letters, "
            "numbers or combining marks plus '-', '_' and '.', with no path separator, no leading "
            "'.', no Windows reserved device stem, and at most 180 UTF-8 bytes"
        )
    return basename
