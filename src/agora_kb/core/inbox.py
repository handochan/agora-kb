"""The write path: append an immutable event to the per-writer inbox (DESIGN §2.2, ADR-0002).

``Inbox.write`` is the **only** way knowledge enters a repo. It is non-blocking and conflict-free:
each writer appends to its own namespace (``_kb/inbox/<writer>/<id>.md``) so concurrent writers
touch disjoint files and never race (CQRS write side). The shared wiki is advanced later by the
single curator; a captured item is durable immediately but becomes queryable only after
consolidation (eventual consistency, DESIGN §2.2).

Invariants upheld here:
- **Append-only / immutable** (invariant 3): a write only ever *creates* a new file via an atomic
  same-directory rename; it never edits or reorders existing events. Re-using an id is refused.
- **Per-writer namespacing + tenant isolation** (invariants 3, 5): the writer name is validated to a
  safe path component (:func:`agora_kb.core.layout.validate_writer`), confining the write to this
  repo's inbox.

``event_key`` gives delivery idempotency: a retry with the same ``writer`` + ``event_key`` returns
the existing event instead of creating a duplicate. Per ADR-0011 the **authoritative** dedup
happens at claim time inside the curator lock; this write-time check is a best-effort optimization,
so it is a plain scan of the writer's pending inbox (race-tolerant by design).

An event may carry **attachments**: the original bytes of the artefact it summarises (ADR-0041
D4.2). They are staged inside the writer's own namespace at
``_kb/inbox/<writer>/_attach/<sha256>.<ext>``, written *before* the event that names them, so the
one delivery that is the event is never observable without its bytes; APPLY later materialises them
into ``raw/_blob/`` and the curator remains the sole writer of ``raw/`` (ADR-0020 decision 3). The
staged file is content-addressed and immutable like the event itself, and it travels with the event
through the spool (:func:`carry_attachments`).

The module also owns the one operation that puts an **already-existing** event back into the inbox
(:func:`return_event_to_inbox`, issue #99): the curator's retry/recovery paths and ``agora requeue``
both need it, and it is the single operation that could break the append-only rule stated above, so
it lives beside that rule rather than in a caller. It is a *location-only* transition — one
``os.replace`` of an immutable file, never a rewrite and never an ``Inbox.write`` (which would mint
a NEW id and duplicate the knowledge). See the ADR-0002 spool-custodian appendix.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from . import frontmatter
from .atomicio import atomic_write_text, fsync_dir
from .hashing import content_sha256
from .ids import event_id_timestamp, is_valid_event_id, new_event_id
from .layout import (
    ATTACHMENT_DIRNAME,
    InvalidWriterError,
    RepoLayout,
    attachment_basename,
    attachment_dir,
    attachment_ext_for,
    validate_writer,
)
from .models import Attachment, Confidence, InboxItem, Kind, normalize_media_type

__all__ = [
    "Inbox",
    "assert_writable_repo_schema",
    "WriteReceipt",
    "AttachmentPayload",
    "AttachmentError",
    "AttachmentTooLargeError",
    "AttachmentIntegrityError",
    "AttachmentContainmentError",
    "StagedAttachment",
    "AttachmentCarry",
    "attachment_sha256",
    "default_attachment_byte_cap",
    "parse_attachments",
    "event_attachments",
    "read_attachment",
    "carry_attachments",
    "failed_event_count",
    "iter_failed_events",
    "InboxReturn",
    "InboxReturnStatus",
    "resolve_inbox_return",
    "return_event_to_inbox",
]

#: One attachment as a caller hands it in: ``(filename, media_type, data)``. ``filename`` and
#: ``media_type`` are best-effort DISPLAY metadata (a browser's multipart headers, a shell's
#: ``file(1)`` guess) and may be ``None``; ``data`` is the artefact's original, opaque bytes.
AttachmentPayload = tuple[str | None, str | None, bytes]


@dataclass(frozen=True)
class WriteReceipt:
    """Result of a capture (the payload of the ``kb_remember`` MCP tool).

    ``queued`` is True iff this call created a new inbox event; an idempotent re-delivery (same
    ``writer`` + ``event_key``) returns the existing ``id`` with ``queued=False``.
    """

    id: str
    queued: bool
    inbox_depth: int

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "queued": self.queued, "inbox_depth": self.inbox_depth}


class AttachmentError(ValueError):
    """Base class for every refusal on the attachment path (ADR-0041 D4.2)."""


class AttachmentTooLargeError(AttachmentError):
    """An attachment exceeds the per-file byte cap (:func:`default_attachment_byte_cap`)."""


class AttachmentIntegrityError(AttachmentError):
    """Staged bytes do not hash to the digest that names them.

    Raised on WRITE when the content-addressed staging path is already occupied by bytes with a
    different digest, and on READ when a staged file no longer hashes to its own name. Both are the
    same statement — a content-addressed file whose content is not what its name says is not a
    candidate for ``raw/_blob/``, whose whole admission story rests on that equality (ADR-0041
    D1.4).
    """


class AttachmentContainmentError(AttachmentError):
    """A staging path resolved outside the writer's own inbox namespace (invariant 5)."""


def attachment_sha256(data: bytes) -> str:
    """Hex SHA-256 of ``data`` **verbatim** — the content address of an attachment.

    Deliberately NOT :func:`agora_kb.core.hashing.content_sha256`: that one hashes NFC/LF-normalised
    *text* for tier-2 capture dedup (DATA-MODEL §11.2), which is exactly the wrong thing for opaque
    bytes — normalising a PDF is not a no-op, so the digest would no longer identify the file it
    names, and the ``raw/_blob/`` self-check (``hash(bytes) == basename``) would fail on every
    binary. One spelling, exported, so the staging writer, APPLY's re-hash and any verifier compute
    the same number.
    """
    return hashlib.sha256(data).hexdigest()


def default_attachment_byte_cap() -> int:
    """The per-attachment byte ceiling: the SAME number as the per-FILE upload cap.

    Read off :class:`agora_kb.config.WebUploadConfig`'s ``max_bytes`` default rather than restated
    here (issue #66's footgun bound, ADR-0025), because a second constant is how two limits end up
    disagreeing about the same artefact: the web face already refuses an oversize upload before
    extraction, and this is the floor for every OTHER writer (``agora capture``, MCP, a future
    face) that never passes through that check. A caller with an operator-configured limit in scope
    — the web face has one per repo — passes it explicitly to :meth:`Inbox.write` instead.

    Imported lazily: :mod:`agora_kb.config` imports from :mod:`agora_kb.core`, so a module-level
    import here would be a cycle (the same reason :func:`assert_writable_repo_schema` defers its).
    """
    from ..config import WebUploadConfig

    return WebUploadConfig().max_bytes


def assert_writable_repo_schema(layout: RepoLayout) -> None:
    """Refuse a WRITE into a repo whose KB wiki schema this build will not write (ADR-0041 D6).

    The ONE spelling of D6's write refusal, called by :meth:`Inbox.write` below and imported by
    the other four call sites D6 names exhaustively (``agora curate`` / ``watch`` / ``requeue`` in
    ``cli.py`` and the ``kb_curate`` MCP handler), which reach the repo without going through
    ``Inbox.write``. It lives beside the write primitive rather than in :mod:`agora_kb.config`
    because it is the WRITE BOUNDARY's rule, and because a second copy of a three-line predicate is
    exactly how two call sites end up disagreeing about which repos are writable.

    **Why the gate is at the one write primitive rather than at each face.** With
    ``SUPPORTED_KB_SCHEMA_VERSIONS`` widened to ``{1, 2}`` every entry-point guard
    (:func:`~agora_kb.config.guard_repo_schema_version`, the MCP server construction, the web
    ``build_app``) PASSES for a schema-1 repo — that is the whole point of keeping 1 in the set, so
    reads keep working. A construction-time guard therefore structurally cannot let ``kb_query``
    through while refusing ``kb_remember`` on the same server object. ADR-0041 D6 names this
    function's location exactly: *"``Inbox.write`` itself — one call covers ``kb_remember``, the
    web upload route, and every future writer, which is why it goes there rather than at each
    face."*

    **Why a capture REFUSES rather than succeeds.** The curator will not write a schema-1 tree
    (APPLY composes schema-2 paths and frontmatter), so an event accepted here could never drain.
    An inbox that can never drain — and that a re-import into a NEW repo would orphan — is silent
    data loss dressed as success, which is strictly worse than a loud refusal naming the one
    crossing that exists (``agora import --from-kb``).

    **Why the CANONICAL reader, and why "declares nothing" is not a refusal.** The version comes
    from :func:`~agora_kb.config.read_canonical_kb_schema_version` — ``_meta/taxonomy.yaml`` ALONE
    (ADR-0010 §5.1) — never from the ``_kb/repo.yaml`` mirror the broader
    :func:`~agora_kb.config.read_kb_schema_version` falls back to: that mirror is git-IGNORED and
    operator-local, so letting it gate a shared repo's write path would let one machine's untracked
    edit decide whether everybody's captures are accepted. The canonical reader returns ``None``
    for a directory that declares nothing determinable (no ``_meta/taxonomy.yaml``, an unparseable
    one, a non-integer version) and that is treated as UNKNOWN, never as "schema 1": an
    uninitialized directory has no schema-1 tree to corrupt and no curator that could drain it
    either way, so refusing there would convert "wrong cwd" into a confusing schema complaint. A
    readable taxonomy with no ``schema_version`` key is a pre-#98 repo and correctly reads as 1.

    That last rule is the reason the CLI call sites do NOT pass their loaded
    :class:`~agora_kb.config.RepoConfig` here: ``load_repo_config`` collapses "no taxonomy file at
    all" to the default ``schema_version: 1``, so ``agora curate`` in the wrong directory would
    answer a lost operator with a schema refusal instead of its ordinary "nothing was changed".
    """
    from ..config import assert_writable_kb_schema_version, read_canonical_kb_schema_version

    version = read_canonical_kb_schema_version(layout)
    if version is not None:
        assert_writable_kb_schema_version(version, repo=layout.root)


class Inbox:
    """Append-only write path for one knowledge repo."""

    def __init__(self, layout: RepoLayout) -> None:
        self._layout = layout

    @property
    def layout(self) -> RepoLayout:
        return self._layout

    def write(
        self,
        *,
        text: str,
        writer: str,
        source: str,
        target: str = "personal",
        domain: str | None = None,
        tags: tuple[str, ...] | list[str] | None = None,
        cwd: str | None = None,
        kind: Kind = Kind.capture,
        confidence: Confidence | None = None,
        event_key: str | None = None,
        raw_ref: str | None = None,
        attachments: Sequence[AttachmentPayload] | None = None,
        max_attachment_bytes: int | None = None,
        now: datetime | None = None,
    ) -> WriteReceipt:
        """Append one immutable event to ``_kb/inbox/<writer>/`` and return a :class:`WriteReceipt`.

        ``text`` is the knowledge body (or an extraction summary of ``raw_ref``); it must be
        non-empty.
        ``now`` is injectable for tests. Raises ``ValueError`` for empty text,
        ``InvalidWriterError`` for an unsafe writer, and
        :class:`~agora_kb.config.ReadOnlySchemaVersionError` for a repo whose KB wiki schema this
        build will not write (ADR-0041 D6 — see :func:`assert_writable_repo_schema`).

        ``attachments`` carries the artefact's ORIGINAL BYTES alongside the text that summarises
        them (ADR-0041 D4.2), as ``(filename, media_type, data)`` triples. Each payload is hashed,
        refused above ``max_attachment_bytes`` (default: :func:`default_attachment_byte_cap`), and
        written to ``_kb/inbox/<writer>/_attach/<sha256>.<ext>`` **before** the event that names it,
        so an event is never visible in the inbox citing bytes that are not on disk — the ordering
        is the whole reason the transport lives inside the write instead of beside it (D4.2's
        rejected alternative). Staging is idempotent: identical bytes under the same extension name
        one file, and re-staging them is a no-op. Raises :class:`AttachmentTooLargeError`,
        :class:`AttachmentIntegrityError`, :class:`AttachmentContainmentError` or
        :class:`~agora_kb.core.layout.InvalidAttachmentExtError` on refusal — a failed attachment
        never yields an event, so the capture is loud rather than half-delivered.

        An idempotent re-delivery (same ``writer`` + ``event_key``) still returns the FIRST event
        and stages nothing: the retry is the same capture, and its bytes were staged with it.

        **Nothing is staged until the event is known to be valid.** Every refusal this method can
        reach on ordinary input — an empty body, an unsafe writer, a read-only schema, an
        attachment refusal, and the model's own ``source``/``target``/``domain``/``tags``
        validation — happens BEFORE the first byte is written, so a typo'd argument leaves the
        filesystem untouched rather than an orphaned payload nothing ever collects. The one
        remaining leftover is the id collision at ``_append`` (two writes in the same millisecond
        with the same random suffix): the staged bytes are content-addressed and immutable, so the
        retry re-uses them rather than duplicating them, and the failure direction is the safe one
        — bytes with no event are inert, an event with no bytes is a broken citation.
        """
        if not text or not text.strip():
            raise ValueError("inbox item text must be non-empty")
        validate_writer(writer)
        assert_writable_repo_schema(self._layout)

        # Validate + hash the payloads BEFORE the idempotency check: an oversize or unstageable
        # attachment is a caller bug, and reporting it only on the first delivery would make the
        # refusal depend on retry timing.
        prepared = self._prepare_attachments(attachments, max_attachment_bytes)

        # Best-effort delivery idempotency (authoritative dedup is the curator at claim time,
        # ADR-0011).
        if event_key is not None:
            existing = self._find_by_event_key(writer, event_key)
            if existing is not None:
                return WriteReceipt(id=existing, queued=False, inbox_depth=self.depth())

        # Build the item BEFORE staging anything. Construction is pure and in-memory, but it is
        # where `source`/`target`/`domain`/`tags` are validated — so staging first would let an
        # ordinary typo (`--source manuel`) leave a full-size payload orphaned in
        # `_kb/inbox/<writer>/_attach/` with no event that will ever cite it and nothing that
        # sweeps the directory. The ORDERING D4.2 requires is between the two DISK writes (bytes,
        # then event), and this preserves it exactly: `_append` below is the event's only write.
        item = InboxItem(
            id=new_event_id(now=now),
            source=source,
            writer=writer,
            target=target,
            cwd=cwd,
            domain=domain,
            tags=tuple(tags or ()),
            created=(now or datetime.now(UTC)),
            kind=kind,
            confidence=confidence,
            event_key=event_key,
            content_sha256=content_sha256(text),
            raw_ref=raw_ref,
            attachments=tuple(record for record, _ in prepared),
            body=text,
        )

        for record, data in prepared:
            self._stage_attachment(writer, record, data)

        self._append(item)
        return WriteReceipt(id=item.id, queued=True, inbox_depth=self.depth())

    def depth(self) -> int:
        """Number of pending events across all writers (the consolidation backlog)."""
        inbox = self._layout.inbox_dir
        if not inbox.exists():
            return 0
        return sum(1 for _ in inbox.glob("*/*.md"))

    def last_write(self) -> datetime | None:
        """Timestamp of the most recent pending inbox event, or ``None`` when the inbox is empty.

        Derived from the NEWEST event id, not file mtime: ids are time-sortable (lexicographic order
        == chronological, :mod:`agora_kb.core.ids`), so the largest pending id carries the latest
        write instant — stable across filesystems and clock-skew-free. Feeds the curator's *idle*
        trigger (no writes for ``idle_minutes`` while a backlog exists, DESIGN §4). Foreign or
        malformed filenames under ``inbox/`` are ignored; only valid event ids are considered.
        """
        inbox = self._layout.inbox_dir
        if not inbox.exists():
            return None
        newest: str | None = None
        for path in inbox.glob("*/*.md"):
            stem = path.stem
            if is_valid_event_id(stem) and (newest is None or stem > newest):
                newest = stem
        return event_id_timestamp(newest) if newest is not None else None

    # --- internals ------------------------------------------------------------------------------
    @staticmethod
    def _prepare_attachments(
        payloads: Sequence[AttachmentPayload] | None,
        max_attachment_bytes: int | None,
    ) -> list[tuple[Attachment, bytes]]:
        """Validate/hash each payload into an :class:`Attachment` record + its bytes.

        Pure apart from the config read behind the default cap: nothing is written here, so a
        refusal leaves the filesystem untouched. Duplicate payloads (same digest AND extension)
        collapse to the FIRST occurrence — they address one staged file and one ``raw/_blob/``
        destination, so keeping both would make APPLY choose between two ``filename:`` values for
        one sidecar.
        """
        if not payloads:
            return []
        cap = (
            default_attachment_byte_cap() if max_attachment_bytes is None else max_attachment_bytes
        )
        prepared: list[tuple[Attachment, bytes]] = []
        seen: set[tuple[str, str]] = set()
        for filename, media_type, data in payloads:
            if not isinstance(data, bytes | bytearray):
                raise AttachmentError(
                    f"attachment payload must be bytes, got {type(data).__name__}"
                )
            data = bytes(data)
            if len(data) > cap:
                raise AttachmentTooLargeError(
                    f"attachment {filename or '<unnamed>'!r} is {len(data)} bytes, over the "
                    f"{cap}-byte per-file limit"
                )
            record = Attachment(
                sha256=attachment_sha256(data),
                ext=attachment_ext_for(filename),
                filename=filename,
                media_type=normalize_media_type(media_type),
                bytes=len(data),
            )
            key = (record.sha256, record.ext)
            if key in seen:
                continue
            seen.add(key)
            prepared.append((record, data))
        return prepared

    def _stage_attachment(self, writer: str, record: Attachment, data: bytes) -> Path:
        """Write ONE attachment into ``_kb/inbox/<writer>/_attach/`` and return its path.

        Create-only and idempotent: an existing file with this content address is verified (its
        bytes must still hash to its name) and reused, never rewritten — the staged artefact is as
        immutable as the event that names it (invariant 3).
        """
        writer_dir = self._layout.inbox_writer_dir(writer)
        dest = self._layout.inbox_attachment_path(writer, record.sha256, record.ext)
        # Containment, checked against the RESOLVED parents: `validate_writer` and the digest/ext
        # grammars close the character hole, but only `resolve()` sees an inode — a symlinked
        # `_attach/` would otherwise land a writer's bytes anywhere on the filesystem
        # (`core/pathsafe.py`: "character filtering and containment close different holes and
        # neither subsumes the other"). Deliberately BEFORE the mkdir, so a hostile link is caught
        # while the filesystem is still untouched. The residual TOCTOU is bounded by `_kb/` being
        # engine-owned.
        if not dest.parent.resolve().is_relative_to(writer_dir.resolve()):
            raise AttachmentContainmentError(
                f"attachment staging directory for writer {writer!r} resolves outside its inbox "
                f"namespace: {dest.parent} → {dest.parent.resolve()}"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            _assert_content_address(dest, dest.read_bytes(), record.sha256)
            return dest
        try:
            _atomic_write_bytes(dest, data)
        except FileExistsError:
            # Another writer of the SAME bytes won the race; content-addressing makes that a
            # no-op rather than a conflict — but verify what actually landed.
            _assert_content_address(dest, dest.read_bytes(), record.sha256)
        return dest

    def _append(self, item: InboxItem) -> None:
        """Atomically create the event file (append-only: never overwrite an existing id)."""
        dest = self._layout.inbox_item_path(item.writer, item.id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():  # an id collision would mean overwriting an immutable event — refuse.
            raise FileExistsError(f"inbox event already exists: {dest}")
        text = frontmatter.render(item.to_frontmatter(), item.body)
        self._atomic_write(dest, text)

    @staticmethod
    def _atomic_write(dest: Path, text: str) -> None:
        """Create the event file exclusively + durably (invariant 3, ADR-0002); a same-id write can
        never clobber an immutable event. See :func:`agora_kb.core.atomicio.atomic_write_text`.
        """
        atomic_write_text(dest, text, exclusive=True)

    def _find_by_event_key(self, writer: str, event_key: str) -> str | None:
        """Return the id of an existing pending event with this writer+event_key, else None."""
        writer_dir = self._layout.inbox_writer_dir(writer)
        if not writer_dir.exists():
            return None
        for path in sorted(writer_dir.glob("*.md")):  # FIFO: keep the earliest on a key collision
            try:
                fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if fm.get("event_key") == event_key:
                found = fm.get("id")
                return found if isinstance(found, str) else None
        return None


# --- attachment staging: the bytes that travel beside an event (ADR-0041 D4.2) ------------------
def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    """Create ``dest`` exclusively + durably from ``data`` — the bytes twin of the text writer.

    Same temp-file → fsync → ``os.link`` → parent-fsync shape as
    :func:`agora_kb.core.atomicio.atomic_write_text` (``exclusive=True``), so a staged attachment
    gets the identical create-only, crash-durable guarantee an inbox event gets: a second write to
    the same content address raises ``FileExistsError`` instead of clobbering an immutable file.
    It lives here rather than in ``atomicio`` because it is the only binary write in the core and
    ``atomicio``'s contract is text-with-``newline="\\n"``, which is exactly what bytes must not go
    through.
    """
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, dest)  # create-only: raises FileExistsError if dest exists
    finally:
        tmp.unlink(missing_ok=True)
    fsync_dir(dest.parent)


def _assert_content_address(path: Path, data: bytes, expected: str) -> None:
    """Raise unless ``data`` hashes to ``expected`` — ONE spelling of the integrity refusal.

    The staging writer (an occupied content address) and the reader (:func:`read_attachment`) ask
    the identical question, and an operator cannot act on two different phrasings of it.
    """
    actual = attachment_sha256(data)
    if actual != expected:
        raise AttachmentIntegrityError(
            f"staged attachment {path} does not match its content address: expected "
            f"{expected}, found {actual}"
        )


@dataclass(frozen=True)
class StagedAttachment:
    """One attachment record paired with the path its bytes occupy *right now*.

    The path is derived from where the EVENT is (``<event dir>/_attach/<sha256>.<ext>``), not from
    a stored location, so one resolver serves every lifecycle stage the event passes through.
    """

    record: Attachment
    path: Path

    @property
    def exists(self) -> bool:
        return self.path.is_file()


def parse_attachments(fm: Mapping[str, object]) -> tuple[Attachment, ...]:
    """Parse an event's ``attachments:`` frontmatter into validated records (``()`` when absent).

    FAIL-LOUD on a malformed list: a spool file that names bytes in a shape this build does not
    understand must not be silently read as "no attachments", which would drop the artefact and
    publish a note citing a blob nobody ever wrote. Callers on a disposal loop
    (:func:`carry_attachments`) catch the :class:`AttachmentError` and report it instead.
    """
    raw = fm.get("attachments")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise AttachmentError(f"'attachments' must be a list, got {type(raw).__name__}")
    out: list[Attachment] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise AttachmentError(f"attachment entry must be a mapping, got {type(entry).__name__}")
        try:
            out.append(Attachment(**entry))
        except (TypeError, ValueError) as exc:
            raise AttachmentError(_one_line(f"invalid attachment entry: {exc}")) from exc
    return tuple(out)


def event_attachments(event_path: Path) -> tuple[StagedAttachment, ...]:
    """Resolve one event file's attachments to the staged paths BESIDE it.

    Reads the event's own immutable frontmatter and joins each record onto
    ``<event dir>/_attach/``. Existence is NOT asserted — the caller decides what a missing file
    means (a mover reports it, APPLY fails the run) — but the digest and extension are re-validated
    by :class:`~agora_kb.core.models.Attachment` before they reach a path join, so a hand-edited
    spool file cannot address anything outside that directory.
    """
    fm, _ = frontmatter.parse(event_path.read_text(encoding="utf-8"))
    directory = attachment_dir(event_path.parent)
    return tuple(
        StagedAttachment(
            record=record, path=directory / attachment_basename(record.sha256, record.ext)
        )
        for record in parse_attachments(fm)
    )


def read_attachment(staged: StagedAttachment) -> bytes:
    """Read one staged attachment, verifying it against the digest that names it.

    The verification is the point: these bytes are on their way to
    ``raw/_blob/<ab>/<sha256>.<ext>``, whose integrity story is ``hash(bytes) == basename``, and
    checking it at the read is what keeps that self-check honest end to end. Raises
    :class:`AttachmentIntegrityError` on a mismatch and ``OSError`` when the file is absent.
    """
    data = staged.path.read_bytes()
    _assert_content_address(staged.path, data, staged.record.sha256)
    return data


@dataclass(frozen=True)
class AttachmentCarry:
    """What happened when an event's staged bytes were moved to follow it (never a raise)."""

    carried: tuple[Path, ...] = ()
    missing: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing and not self.errors


def carry_attachments(event_path: Path, *, source_dir: Path) -> AttachmentCarry:
    """Move an event's staged bytes from ``source_dir/_attach/`` to beside its NEW location.

    Called by every mover of an event file, AFTER its rename: ``claim()``
    (``inbox/<writer>/`` → ``processing/<run-id>/events/``), the processed/failed drains, and the
    :func:`return_event_to_inbox` back-edge. ``event_path`` is where the event is now;
    ``source_dir`` is the directory it came from.

    Two rules make it safe to run in a disposal loop:

    * **Never raises.** It runs on failure/recovery paths where an exception would strand every
      event after this one (the same reason :func:`resolve_inbox_return` returns a verdict).
    * **Never destroys a reference.** Content-addressed staging means several events can name ONE
      file, so a source file another event in ``source_dir`` still cites is COPIED rather than
      moved; only the last citation moves. An attachment already present at the destination is
      left alone (idempotent re-run after a crash mid-drain) — but its SOURCE is still released
      once nothing in ``source_dir`` cites it, so the copy branch leaves no orphan behind
      (:func:`_reclaim_carried_source`).

    A ``missing`` entry is not this function's failure to fix: the event has already moved, the
    bytes have not, and the caller decides whether that is a run failure or a note in a report.
    """
    dest_dir = attachment_dir(event_path.parent)
    src_dir = attachment_dir(source_dir)
    if dest_dir == src_dir:
        return AttachmentCarry()
    try:
        staged = event_attachments(event_path)
    except (OSError, ValueError) as exc:
        return AttachmentCarry(errors=(_one_line(exc),))
    if not staged:
        return AttachmentCarry()

    carried: list[Path] = []
    missing: list[str] = []
    errors: list[str] = []
    still_cited = _referenced_attachment_names(source_dir)
    for item in staged:
        name = item.path.name
        src = src_dir / name
        if item.exists:
            # Already at the destination — an earlier event in THIS drain copied it, or this is a
            # re-run after a crash. The destination is content-addressed and immutable, so it is
            # left alone; the SOURCE is still reclaimed when nothing left in `source_dir` cites it
            # (:func:`_reclaim_carried_source`). Without that reclaim the LAST citation to move
            # short-circuits here and its staged bytes are orphaned in `_kb/inbox/<writer>/_attach/`
            # forever: nothing sweeps that directory, so every artefact captured twice before a
            # curator run would leave a permanent copy of itself behind.
            carried.append(item.path)
            _reclaim_carried_source(src, item, still_cited=still_cited)
            continue
        if not src.is_file():
            missing.append(name)
            continue
        try:
            item.path.parent.mkdir(parents=True, exist_ok=True)
            if still_cited is None or name in still_cited:
                _atomic_write_bytes(item.path, src.read_bytes())
            else:
                os.replace(src, item.path)
            carried.append(item.path)
        except OSError as exc:
            errors.append(_one_line(exc))
    return AttachmentCarry(carried=tuple(carried), missing=tuple(missing), errors=tuple(errors))


def _reclaim_carried_source(
    src: Path, item: StagedAttachment, *, still_cited: frozenset[str] | None
) -> None:
    """Delete a staged file whose bytes already reached the destination and that nothing cites.

    The tidy-up half of :func:`carry_attachments`'s "only the last citation moves" rule. The move
    branch cannot express it: when several events in one directory cite ONE content-addressed file,
    every carry but the last COPIES (a sibling still cites it) and the last one finds the
    destination already populated, so the source would never be released by either branch.

    Three conditions, all required, and the safe answer to any doubt is to leave the file:

    * ``still_cited`` is KNOWN (not ``None``) and does not name the file — *unknown* means a sibling
      event could not be read, which the whole module treats as "still cited";
    * the destination's bytes hash to the digest that names them — a corrupt destination means the
      source is the only good copy, and APPLY will fail loudly on the corruption with those bytes
      still recoverable;
    * the unlink succeeds. A failure here is swallowed, NOT reported in ``errors``: the delivery
      succeeded (the bytes are at the destination), and a leftover file is untidy, never lossy — the
      opposite verdict would make a carry that lost nothing read as a failed one.
    """
    if still_cited is None or item.path.name in still_cited or not src.is_file():
        return
    try:
        if attachment_sha256(item.path.read_bytes()) != item.record.sha256:
            return
        src.unlink()
    except OSError:
        return


def _referenced_attachment_names(event_dir: Path) -> frozenset[str] | None:
    """Attachment filenames still cited by the events REMAINING in ``event_dir``.

    One pass over the directory per carry rather than one per attachment; the event being carried
    has already been renamed out, so it never counts itself. ``None`` means *unknown* — a sibling
    event could not be read, so which files it still cites cannot be established — and the caller
    treats that as "everything is still cited", i.e. it COPIES. Copying leaves recoverable bytes in
    two places; moving on a wrong guess takes them away from an event that still names them, and
    only one of those two errors is reversible.
    """
    if not event_dir.is_dir():
        return frozenset()
    names: set[str] = set()
    for path in event_dir.glob("*.md"):
        try:
            fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
            records = parse_attachments(fm)
        except (OSError, ValueError):
            return None
        names.update(attachment_basename(record.sha256, record.ext) for record in records)
    return frozenset(names)


# --- the other spool count ----------------------------------------------------------------------
def failed_event_count(layout: RepoLayout) -> int:
    """Number of TERMINAL-failure events under ``_kb/failed/`` (0 when the dir is absent).

    The worker writes terminal failures NESTED at ``failed/<date>/<run-id>/<event>.md`` (with an
    ``error.json`` retry record alongside, ``worker._fail``), NOT as direct children of ``failed/``
    — so this RECURSIVELY globs ``*.md``, minus the ``_attach/`` sidecar directories: an attachment
    that happens to be a markdown artefact (``<sha256>.md``) is BYTES, not an event, and the
    recursive glob is the one place in the spool where the two namespaces could be confused
    (:func:`_is_attachment_path`).

    ONE implementation for the MCP face (``kb_status.failed``) and the CLI (``agora status``'s
    ``failed_events``) — issue #96 crit 8 requires the two to agree by construction, not by two
    copies of the same glob. Cost is O(retained failure history): ``_kb/failed/`` is never pruned,
    so this is a linear scan on a monotonically growing tree, bounded per event by
    ``curator.max_attempts``. Fine at beta scale; a retention policy is a separate issue.
    """
    failed_dir = layout.failed_dir
    if not failed_dir.is_dir():
        return 0
    return sum(
        1 for path in failed_dir.rglob("*.md") if not _is_attachment_path(path, root=failed_dir)
    )


def iter_failed_events(layout: RepoLayout) -> list[Path]:
    """Every TERMINAL-failure event under ``_kb/failed/``, FIFO (empty when the dir is absent).

    The SAME set :func:`failed_event_count` counts, materialized and ordered for
    ``agora requeue``'s selector (#99 crit 7/9). Sorted by event id (time-sortable ⇒ chronological,
    DATA-MODEL §10) then by path as a total-order tiebreak, so requeue's preview and its execution
    walk one list in one order. ``failed_event_count`` deliberately does NOT call this: it is on
    the Prometheus scrape path (``faces/web/metrics.py``) and MCP ``kb_status``, and must stay a
    lazy count with no sort. ``tests/core/test_inbox_return.py`` locks the two to the same set.
    """
    failed_dir = layout.failed_dir
    if not failed_dir.is_dir():
        return []
    return sorted(
        (
            path
            for path in failed_dir.rglob("*.md")
            if not _is_attachment_path(path, root=failed_dir)
        ),
        key=lambda path: (path.stem, path.as_posix()),
    )


def _is_attachment_path(path: Path, *, root: Path) -> bool:
    """True if ``path`` lies inside an ``_attach/`` staging directory BELOW ``root``.

    The ONE predicate both spool counts use, because ``_kb/failed/`` is the only place events are
    addressed by a RECURSIVE glob: without it a captured ``.md`` artefact carried into
    ``failed/<date>/<run-id>/_attach/`` would be counted as a terminally-failed event and offered
    to ``agora requeue`` as one — which would then try to derive an inbox address from bytes that
    have no frontmatter.

    The question is asked of the path RELATIVE to the walked subtree, never of the absolute path:
    the repo may itself live under a directory called ``_attach`` (nothing forbids it), and testing
    the absolute components there would classify every terminally-failed event as bytes — making
    the whole of ``_kb/failed/`` invisible to ``agora status``, ``kb_status.failed`` and
    ``agora requeue`` at once. A path outside ``root`` (impossible from ``rglob``, but the
    predicate must stay total) is conservatively NOT an attachment.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return ATTACHMENT_DIRNAME in relative.parts


# --- the back-edge: returning an EXISTING event to the inbox (#99, ADR-0002 appendix) ------------
InboxReturnStatus = Literal["ok", "occupied", "unreadable", "unaddressable", "error"]


@dataclass(frozen=True)
class InboxReturn:
    """Verdict for returning ONE immutable event file to its inbox address (location-only).

    Five statuses, not a ``bool``, because the callers must tell the outcomes apart: ``agora
    requeue`` reports an ``occupied`` destination as a non-destructive skip (#99 crit 3) while an
    ``unreadable``/``unaddressable`` file is an operator problem and an ``error`` is a filesystem
    problem — and the curator's no-loss floor (#124) must treat all four alike as "preserve, never
    drop". One symbol carries both readings.
    """

    status: InboxReturnStatus
    source: Path
    dest: Path | None  # None iff the address could not be derived
    detail: str = ""  # ONE line, operator-facing; "" when status == "ok"

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _one_line(text: object) -> str:
    """Collapse a detail to a single operator-facing line (exception texts can be multi-line)."""
    return " ".join(str(text).split())


def resolve_inbox_return(layout: RepoLayout, event_path: Path) -> InboxReturn:
    """PURE verdict: where this event WOULD go, or why it cannot. Creates nothing, writes nothing.

    Never calls ``dest.parent.mkdir`` — that is why ``agora requeue --dry-run`` can promise a
    byte-identical filesystem (#99 crit 5). The writer AND the event id come from the event's own
    immutable frontmatter (DATA-MODEL §1) and BOTH are validated: ``validate_writer`` via
    ``layout.inbox_item_path`` and ``core.ids.is_valid_event_id`` here. Without the id guard a
    frontmatter ``id: ../../../wiki/PWNED`` writes into the git-tracked read model — reachable the
    moment ``_kb/failed/`` (an operator-editable directory) becomes an input.
    """
    try:
        fm, _ = frontmatter.parse(event_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return InboxReturn(
            status="unreadable",
            source=event_path,
            dest=None,
            detail=_one_line(f"frontmatter is unreadable: {exc}"),
        )
    writer = fm.get("writer")
    event_id = fm.get("id")
    if not isinstance(writer, str) or not isinstance(event_id, str):
        return InboxReturn(
            status="unreadable",
            source=event_path,
            dest=None,
            detail="frontmatter is missing a string 'writer' or 'id'",
        )
    if not is_valid_event_id(event_id):
        return InboxReturn(
            status="unaddressable",
            source=event_path,
            dest=None,
            detail=_one_line(f"not a valid event id: {event_id!r}"),
        )
    try:
        dest = layout.inbox_item_path(writer, event_id)
    except InvalidWriterError as exc:
        # An unaddressable writer is a VERDICT, not a raise: every caller runs a disposal loop
        # (the curator's failure/recovery paths, requeue's batch) where an exception would strand
        # every event after this one.
        return InboxReturn(
            status="unaddressable", source=event_path, dest=None, detail=_one_line(exc)
        )
    if dest.exists():
        # An idempotent duplicate already holds the slot. Reported, never clobbered: overwriting it
        # would destroy an immutable event (invariant 3).
        return InboxReturn(
            status="occupied",
            source=event_path,
            dest=dest,
            detail="an inbox event with this id is already present — not overwritten",
        )
    return InboxReturn(status="ok", source=event_path, dest=dest)


def return_event_to_inbox(layout: RepoLayout, event_path: Path) -> InboxReturn:
    """Rename one event back to ``inbox/<writer>/<id>.md`` — the ONE mover (#99).

    Calls :func:`resolve_inbox_return`; on ``ok`` creates the writer dir and ``os.replace``s, then
    returns the SAME verdict the resolver produced — so a dry-run plan and a real run are produced
    by one code path, never two. An ``OSError`` from the mkdir/replace is caught and returned as
    ``error`` (the caller reports it; the curator's ``_fail`` treats it as a refusal and preserves
    the event terminally, #99 WU-0 / #124). The source is never deleted, never edited, never
    truncated.

    The event's staged attachments follow it (:func:`carry_attachments`), which is still
    location-only — the bytes are renamed, never rewritten. It runs AFTER the event's own rename
    and cannot change the verdict: the event IS back in the inbox either way, and reporting
    ``error`` for bytes that did not follow would tell ``agora requeue`` to preserve an event it
    has already moved. Bytes left behind stay in the source directory, recoverable by hand.
    """
    verdict = resolve_inbox_return(layout, event_path)
    dest = verdict.dest
    if not verdict.ok or dest is None:
        return verdict
    source_dir = event_path.parent
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(event_path, dest)
    except OSError as exc:
        return InboxReturn(status="error", source=event_path, dest=dest, detail=_one_line(exc))
    carry_attachments(dest, source_dir=source_dir)
    return verdict
