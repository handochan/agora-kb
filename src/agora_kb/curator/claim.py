"""Per-repo curator lock + the atomic FIFO claim (ADR-0008 step 1, ADR-0011 §0 / §5 tier-1).

This is the first DETERMINISTIC step of the transactional curator loop (ADR-0008): take the per-repo
lock, atomically move a FIFO snapshot of the inbox into ``processing/<run-id>/events/``, and write
the run manifest (``phase='claimed'``). Everything here is model-free and owns no cognition.

Two pieces live here:

* :func:`curator_lock` — a non-blocking ``fcntl.flock`` over ``_kb/curator.lock`` (DESIGN §3). If
  the lock is already held, a run is in progress (ADR-0008 step 1 / DESIGN §4 step 1: "if held,
  exit"), so we raise :class:`LockHeld` rather than block — exactly one curator advances a repo at a
  time (ADR-0002 single writer).
* :func:`claim` — under the held lock, snapshot the inbox in FIFO (event-id == chronological) order,
  apply **tier-1** ``writer:event_key`` de-duplication AUTHORITATIVELY at claim time (ADR-0011 §5
  tier-1: the FIFO-earliest of any duplicate key wins; later same-key retries are dropped), cap the
  snapshot at ``max_candidates`` DISTINCT tier-2 content groups (INGEST-CONTRACT §1.3
  ``curator.limits.max_candidates_per_run``, ADR-0024 OD-3a / #60: only the FIFO **head** is
  claimed; the remainder stays untouched in the inbox for the next trigger), then ATOMICALLY move
  the selected immutable event files via ``os.replace`` (same-filesystem rename, byte-for-byte)
  into ``processing/<run-id>/events/`` and write the manifest. Later inbox arrivals are ignored —
  the snapshot is the run's sole event universe (DESIGN §4 step 2).

Events are NEVER mutated — lifecycle is by file location only (DATA-MODEL §1). The move is a rename,
so an event in ``processing/`` is the identical inode/bytes that was in ``inbox/``; recovery can
return it unchanged (ADR-0008 step 6).
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..core import frontmatter
from ..core.hashing import content_sha256
from ..core.ids import is_valid_event_id
from ..core.layout import RepoLayout
from ..core.state import CuratorState
from .constants import DEFAULT_MAX_CANDIDATES_PER_RUN
from .manifest import RunManifest, write_manifest

__all__ = ["curator_lock", "claim", "LockHeld"]


class LockHeld(RuntimeError):
    """Raised by :func:`curator_lock` when the per-repo curator lock is already held.

    Signals that a consolidation run is already in progress for this repo (ADR-0008 step 1 / DESIGN
    §4 step 1): the caller exits rather than blocking, preserving the single-writer invariant
    (ADR-0002).
    """


@contextmanager
def curator_lock(layout: RepoLayout) -> Iterator[None]:
    """Hold the exclusive, non-blocking per-repo curator lock for the duration of the ``with``.

    Acquires ``fcntl.flock(LOCK_EX | LOCK_NB)`` on ``_kb/curator.lock`` (DESIGN §3). A second
    acquisition while the lock is held raises :class:`LockHeld` (the ``LOCK_NB`` non-blocking flag
    turns contention into an immediate ``BlockingIOError``, which we surface as ``LockHeld`` so a
    run in progress is reported, not waited on — ADR-0008 step 1). The lock is released and the fd
    closed on exit, even on error.
    """
    layout.lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(layout.lock_file, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockHeld(f"curator lock already held: {layout.lock_file}") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def claim(
    layout: RepoLayout,
    *,
    base_commit: str,
    run_id: str,
    started: str,
    state: CuratorState,
    max_candidates: int = DEFAULT_MAX_CANDIDATES_PER_RUN,
) -> RunManifest | None:
    """Claim a FIFO inbox snapshot into ``processing/<run-id>/events/`` and write the manifest.

    MUST be called under the held :func:`curator_lock` (the tier-1 dedup is only authoritative
    inside the lock, ADR-0011 §5). Steps:

    1. Snapshot ``_kb/inbox/*/*.md`` in FIFO order — sort by event id, which is time-sortable
       (DATA-MODEL §10), so lexicographic order == chronological order.
    2. Apply **tier-1** ``writer:event_key`` de-dup (ADR-0011 §5): drop any event whose
       ``writer:event_key`` already appeared earlier in THIS snapshot OR is already recorded in
       ``state.event_keys`` (a prior run delivered it), keeping the FIFO-earliest. Events with no
       ``event_key`` are never de-duplicated.
    3. Cap the snapshot at ``max_candidates`` candidates (INGEST-CONTRACT §1.3
       ``curator.limits.max_candidates_per_run``, default 32 — "the FIFO claim caps the snapshot so
       a run never overflows context"; ADR-0024 OD-3a / #60). The counted unit is the CANDIDATE —
       a distinct canonical ``content_sha256`` group, exactly what tier-2 later collapses to and
       what drives PASS-1 prompt size — so the claimed set is the longest contiguous FIFO **head**
       whose distinct-content-group count stays within the cap (a byte-equivalent duplicate inside
       the head is claimed with its group and costs no cap slot). Everything past the cut stays
       untouched in the inbox — no file moves — so the next trigger naturally continues FIFO
       (append-only + single-writer invariants unchanged).
    4. ATOMICALLY ``os.replace`` each selected file into ``processing/<run-id>/events/`` — a
       same-filesystem rename, so the event is byte-for-byte the inbox file (immutable, DATA-MODEL
       §1). ``_kb/`` is one tree, so source and destination share a filesystem.
    5. Write the manifest with ``phase='claimed'`` (DATA-MODEL §5) and return it.

    Returns ``None`` (a no-op) if the inbox is empty OR every event is dropped by tier-1 dedup —
    there is nothing to consolidate, so no run directory or manifest is created. The cap can never
    turn a non-empty selection into a no-op (``max_candidates >= 1`` always keeps the head event);
    that precondition is ENFORCED here — ``max_candidates < 1`` raises ``ValueError`` (fail-loud,
    mirroring the config-side ``ge=1`` bound) rather than silently claiming an empty run.

    ``state`` is read-only here (tier-1 lookups only); the worker records the claimed keys into
    ``state.event_keys`` as part of finalization (ADR-0011 §4.3), not in this function.
    """
    if max_candidates < 1:
        raise ValueError(f"max_candidates must be >= 1 (got {max_candidates})")
    snapshot = _fifo_snapshot(layout)
    if not snapshot:
        return None

    selected = _dedup_tier1(snapshot, state)
    if not selected:
        return None

    capped = _cap_candidate_head(selected, max_candidates)

    events_dir = layout.processing_dir / run_id / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    event_ids: list[str] = []
    for event_id, src in capped:
        # Same-filesystem rename: the event keeps its exact bytes (immutable; DATA-MODEL §1) and the
        # inbox entry disappears in one atomic step (claimed lifecycle, ADR-0008 step 1).
        os.replace(src, events_dir / f"{event_id}.md")
        event_ids.append(event_id)

    manifest = RunManifest(
        run_id=run_id,
        base_commit=base_commit,
        event_ids=tuple(event_ids),
        phase="claimed",
        prose_complete=False,
        published_commit=None,
        started=started,
    )
    write_manifest(layout, manifest)
    return manifest


def _fifo_snapshot(layout: RepoLayout) -> list[tuple[str, str, str | None, str, Path]]:
    """Read every pending inbox event into a FIFO list of ``(id, writer, event_key, sha, path)``.

    Sorted by event id (time-sortable ⇒ chronological, DATA-MODEL §10). The ``writer`` for the
    tier-1 composite is read from the event's immutable FRONTMATTER (DATA-MODEL §1) — the SAME
    authoritative source the worker uses when recording ``state.event_keys`` at finalization
    (worker._record_event_keys) — so the key looked up at claim time always matches the key recorded
    at finalize, even for a hand-placed/recovered event whose directory name might differ from its
    frontmatter ``writer``. Falls back to the parent-directory inbox namespace only if frontmatter
    omits it. ``event_key`` is read from frontmatter. ``sha`` is the canonical
    :func:`~agora_kb.core.hashing.content_sha256` RE-derived from the event BODY (never trusted from
    frontmatter) — the SAME derivation ``bundle._read_events`` applies later, so the cap's
    distinct-group count at claim time agrees byte-for-byte with tier-2's grouping (DATA-MODEL
    §11.2). A file whose frontmatter is unreadable is skipped (it is not a well-formed event).
    """
    inbox = layout.inbox_dir
    if not inbox.exists():
        return []
    rows: list[tuple[str, str, str | None, str, Path]] = []
    for path in inbox.glob("*/*.md"):
        try:
            fm, body = frontmatter.parse(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        event_id = fm.get("id")
        if not isinstance(event_id, str) or not is_valid_event_id(event_id):
            # The id is interpolated straight into the claim destination
            # (``processing/<run>/events/<id>.md``), so an unvalidated one escapes the processing
            # dir — ``id: ../../../../wiki/PWNED`` lands in the git-tracked read model that only the
            # curator may write (invariant 2, issue #124). ``Inbox.write`` GENERATES ids, so no
            # normal write path can produce this; a hand-placed or imported file can. Skipping is
            # the same fail-closed posture as the unparseable-frontmatter branch above and loses
            # nothing: the event stays in ``inbox/``, still counted by ``depth()``.
            continue
        fm_writer = fm.get("writer")
        writer = fm_writer if isinstance(fm_writer, str) and fm_writer else path.parent.name
        key = fm.get("event_key")
        event_key = key if isinstance(key, str) else None
        rows.append((event_id, writer, event_key, content_sha256(body), path))
    # FIFO == chronological: the event id sorts lexicographically into time order (DATA-MODEL §10).
    rows.sort(key=lambda r: r[0])
    return rows


def _dedup_tier1(
    snapshot: list[tuple[str, str, str | None, str, Path]],
    state: CuratorState,
) -> list[tuple[str, str, Path]]:
    """Tier-1 ``writer:event_key`` de-dup over the FIFO snapshot (ADR-0011 §5, authoritative).

    Returns ``(event_id, sha, path)`` for the events to claim, in FIFO order. Drops any event whose
    ``writer:event_key`` already appeared earlier in this snapshot (the FIFO-earliest wins) OR is
    already in ``state.event_keys`` (delivered by a prior run). Events without an ``event_key`` are
    always kept.
    """
    seen: set[str] = set()
    selected: list[tuple[str, str, Path]] = []
    for event_id, writer, event_key, sha, path in snapshot:
        if event_key is not None:
            composite = f"{writer}:{event_key}"
            if composite in seen or state.event_key_id(writer, event_key) is not None:
                continue  # a duplicate same-key retry — keep the FIFO-earliest only.
            seen.add(composite)
        selected.append((event_id, sha, path))
    return selected


def _cap_candidate_head(
    selected: list[tuple[str, str, Path]],
    max_candidates: int,
) -> list[tuple[str, Path]]:
    """Slice the tier-1-deduped FIFO head to at most ``max_candidates`` candidates (§1.3, #60).

    The counted unit is the CANDIDATE — a distinct canonical ``content_sha256`` group (what tier-2
    collapses to, ADR-0011 §5) — because that is what actually drives PASS-1 prompt size (ADR-0024
    OD-3a). Walks the FIFO order and stops BEFORE the first event that would open group
    ``max_candidates + 1``, so the claimed set is a contiguous FIFO-head prefix: byte-equivalent
    duplicates inside the head ride along with their group (no cap slot, they collapse at tier-2
    with their provenance unioned), and everything past the cut — including a late duplicate of a
    claimed group — stays in the inbox untouched for the next trigger. Returns ``(event_id, path)``
    rows for the events to move.
    """
    shas: set[str] = set()
    head: list[tuple[str, Path]] = []
    for event_id, sha, path in selected:
        if sha not in shas:
            if len(shas) >= max_candidates:
                break  # this event would open candidate cap+1 — the FIFO cut point.
            shas.add(sha)
        head.append((event_id, path))
    return head
