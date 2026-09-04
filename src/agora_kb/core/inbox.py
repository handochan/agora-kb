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

The module also owns the one operation that puts an **already-existing** event back into the inbox
(:func:`return_event_to_inbox`, issue #99): the curator's retry/recovery paths and ``agora requeue``
both need it, and it is the single operation that could break the append-only rule stated above, so
it lives beside that rule rather than in a caller. It is a *location-only* transition — one
``os.replace`` of an immutable file, never a rewrite and never an ``Inbox.write`` (which would mint
a NEW id and duplicate the knowledge). See the ADR-0002 spool-custodian appendix.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from . import frontmatter
from .atomicio import atomic_write_text
from .hashing import content_sha256
from .ids import event_id_timestamp, is_valid_event_id, new_event_id
from .layout import InvalidWriterError, RepoLayout, validate_writer
from .models import Confidence, InboxItem, Kind

__all__ = [
    "Inbox",
    "assert_writable_repo_schema",
    "WriteReceipt",
    "failed_event_count",
    "iter_failed_events",
    "InboxReturn",
    "InboxReturnStatus",
    "resolve_inbox_return",
    "return_event_to_inbox",
]


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
        now: datetime | None = None,
    ) -> WriteReceipt:
        """Append one immutable event to ``_kb/inbox/<writer>/`` and return a :class:`WriteReceipt`.

        ``text`` is the knowledge body (or an extraction summary of ``raw_ref``); it must be
        non-empty.
        ``now`` is injectable for tests. Raises ``ValueError`` for empty text,
        ``InvalidWriterError`` for an unsafe writer, and
        :class:`~agora_kb.config.ReadOnlySchemaVersionError` for a repo whose KB wiki schema this
        build will not write (ADR-0041 D6 — see :func:`assert_writable_repo_schema`).
        """
        if not text or not text.strip():
            raise ValueError("inbox item text must be non-empty")
        validate_writer(writer)
        assert_writable_repo_schema(self._layout)

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
    return sorted(failed_dir.rglob("*.md"), key=lambda path: (path.stem, path.as_posix()))


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
    """
    verdict = resolve_inbox_return(layout, event_path)
    dest = verdict.dest
    if not verdict.ok or dest is None:
        return verdict
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(event_path, dest)
    except OSError as exc:
        return InboxReturn(status="error", source=event_path, dest=dest, detail=_one_line(exc))
    return verdict
