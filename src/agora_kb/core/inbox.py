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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import frontmatter
from .atomicio import atomic_write_text
from .hashing import content_sha256
from .ids import event_id_timestamp, is_valid_event_id, new_event_id
from .layout import RepoLayout, validate_writer
from .models import Confidence, InboxItem, Kind

__all__ = ["Inbox", "WriteReceipt", "failed_event_count"]


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
        now: datetime | None = None,
    ) -> WriteReceipt:
        """Append one immutable event to ``_kb/inbox/<writer>/`` and return a :class:`WriteReceipt`.

        ``text`` is the knowledge body (or an extraction summary of ``raw_ref``); it must be
        non-empty.
        ``now`` is injectable for tests. Raises ``ValueError`` for empty text and
        ``InvalidWriterError`` for an unsafe writer.
        """
        if not text or not text.strip():
            raise ValueError("inbox item text must be non-empty")
        validate_writer(writer)

        # Best-effort delivery idempotency (authoritative dedup is the curator at claim time,
        # ADR-0011).
        if event_key is not None:
            existing = self._find_by_event_key(writer, event_key)
            if existing is not None:
                return WriteReceipt(id=existing, queued=False, inbox_depth=self.depth())

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
            body=text,
        )
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


# --- the other spool count ----------------------------------------------------------------------
def failed_event_count(layout: RepoLayout) -> int:
    """Number of TERMINAL-failure events under ``_kb/failed/`` (0 when the dir is absent).

    The worker writes terminal failures NESTED at ``failed/<date>/<run-id>/<event>.md`` (with an
    ``error.json`` retry record alongside, ``worker._fail``), NOT as direct children of ``failed/``
    — so this RECURSIVELY globs ``*.md``. Events are the only ``.md`` under ``failed/``, so the
    count tracks terminally-failed events exactly.

    ONE implementation for the MCP face (``kb_status.failed``) and the CLI (``agora status``'s
    ``failed_events``) — issue #96 crit 8 requires the two to agree by construction, not by two
    copies of the same glob. Cost is O(retained failure history): ``_kb/failed/`` is never pruned,
    so this is a linear scan on a monotonically growing tree, bounded per event by
    ``curator.max_attempts``. Fine at beta scale; a retention policy is a separate issue.
    """
    failed_dir = layout.failed_dir
    if not failed_dir.is_dir():
        return 0
    return sum(1 for _ in failed_dir.rglob("*.md"))
