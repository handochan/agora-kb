"""``agora requeue`` — return TERMINAL-failure events from ``_kb/failed/`` to the inbox (issue #99).

A brain that is configured but unanswerable fails a real run on every ``agora watch`` tick, and at
``curator.max_attempts: 3`` the third consecutive failure moves the events TERMINALLY to
``_kb/failed/<date>/<run-id>/`` — roughly three minutes from the first failure, with no way back.
The files are on disk but outside the curator's sight, which for the operator is indistinguishable
from loss. This module is the way back.

**It is a spool custodian, not a writer** (ADR-0002 appendix, the five-clause rule). Every mutation
here is a *location-only* transition of an existing, immutable event — one ``os.replace``, bytes
untouched, id untouched (``Inbox.write`` would mint a NEW event and duplicate the knowledge, which
is precisely what DATA-MODEL §1 forbids). It holds ``curator_lock`` for the whole batch, derives
every destination from the moved file's own frontmatter or its existing spool address through the
DESIGN §7 guards, never touches ``wiki/``/``raw/``/``log.md``/git/``_kb/state.json``, publishes
nothing, and deletes nothing — an occupied destination is skipped, never clobbered.

Two structural choices carry most of the safety:

* **The plan and the execution share ONE resolver.** :func:`plan_requeue` calls
  :func:`~agora_kb.core.inbox.resolve_inbox_return` — the same pure function
  :func:`~agora_kb.core.inbox.return_event_to_inbox` calls before it moves anything — so
  ``--dry-run``'s preview cannot drift from the real run's result (#99 crit 5) and the inbox
  address is derived in exactly one place.
* **The selector is a FILTER, never a path join.** Operator input is matched against an enumerated
  set (:func:`~agora_kb.core.inbox.iter_failed_events`); it never reaches a filesystem API, so
  ``--run ../../..`` simply matches nothing instead of needing a traversal guard.

The retry budget is DERIVED from retained ``failed/**/error.json`` records (ADR-0011 §5.1), so
leaving them in place is what gives a requeued event exactly one more run — the loop-free default.
``--reset-attempts`` archives them under ``_kb/requeued/`` (outside ``failed_dir``, because
``rglob`` descends into dotted dirs) per the drain rule in :func:`archive_attempt_records`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from ..core import frontmatter

# `_one_line` is imported rather than re-spelled: it is the ONE collapsing rule shared by
# `InboxReturn.detail` and every report line here, so a cap added there applies to both at once.
from ..core.inbox import (
    InboxReturnStatus,
    _one_line,
    failed_event_count,
    iter_failed_events,
    resolve_inbox_return,
    return_event_to_inbox,
)
from ..core.layout import InvalidWriterError, RepoLayout
from ..core.state import CuratorState, StateStore
from .claim import curator_lock, is_already_delivered
from .worker import iter_attempt_records

__all__ = [
    "ArchivedRecord",
    "KeptRecord",
    "MOVING_OUTCOMES",
    "PLANNED_MOVES",
    "RequeueItem",
    "RequeueOutcome",
    "RequeueReport",
    "Selector",
    "StateUnreadable",
    "archive_attempt_records",
    "execute_requeue",
    "plan_requeue",
    "rel_to_repo",
    "run_requeue",
    "select_failed_events",
]


class StateUnreadable(RuntimeError):
    """``_kb/state.json`` could not be loaded and ``--force`` was not given (#99 crit 6).

    Refusing rather than silently proceeding is the whole point: a safety check that evaporates on
    a corrupt file is not a safety check. Nothing has moved when this is raised — the state load is
    the first thing under the lock. The message is one operator-facing line; the caller frames it
    (``agora requeue: _kb/state.json is unreadable — … (use --force …)``). Named like
    :class:`~agora_kb.curator.claim.LockHeld`: a refusal, not a crash.
    """


class RequeueOutcome(StrEnum):
    """What happened (or would happen) to ONE selected event.

    ``movable`` is plan-only — :func:`execute_requeue` turns it into ``requeued`` — which is what
    makes "the dry-run list equals the real result" a mechanical property rather than a hope.
    ``forced`` stays ``forced`` through execution so the report can say WHY a known-doomed event
    moved. A :class:`~enum.StrEnum` (the house convention, ``core.models.Kind``) because the values
    ARE the report's byte-locked slugs.
    """

    movable = "movable"
    requeued = "requeued"
    forced = "forced"
    already_delivered = "already-delivered"
    destination_exists = "destination-exists"
    unreadable = "unreadable"
    error = "error"


#: The outcomes that mean "this event moved, or would move". ONE definition, so the summary count,
#: the drain rule's moved-set, the renderer's verb and the caller's exit-code logic can never
#: disagree — all four test membership of THIS set, never a hand-written tuple.
MOVING_OUTCOMES: frozenset[RequeueOutcome] = frozenset(
    {RequeueOutcome.movable, RequeueOutcome.requeued, RequeueOutcome.forced}
)

#: The PLAN-side members of :data:`MOVING_OUTCOMES` — exactly what :func:`execute_requeue` acts on.
#: ``requeued`` is what execution PRODUCES, so it is excluded by derivation rather than by a second
#: hand-written literal that a future outcome could silently fall out of.
PLANNED_MOVES: frozenset[RequeueOutcome] = MOVING_OUTCOMES - {RequeueOutcome.requeued}

# Execution can only DOWNGRADE a planned item (the world changed between plan and move); it can
# never upgrade one. ``unaddressable`` collapses into ``unreadable`` here on purpose: the operator
# gets one "this file is not a usable event" class, while the primitive keeps the two distinct.
_EXECUTE_DOWNGRADE: dict[InboxReturnStatus, RequeueOutcome] = {
    "occupied": RequeueOutcome.destination_exists,
    "unreadable": RequeueOutcome.unreadable,
    "unaddressable": RequeueOutcome.unreadable,
    "error": RequeueOutcome.error,
}

# Pinned decline details (the text after "<outcome>: " on a report line). They live here, not in
# the renderer, so the CLI face and a future kb_requeue MCP tool say the same thing.
_DETAIL_ALREADY_DELIVERED = (
    "{composite} is already in state.event_keys — the claim would drop it; "
    "use --force to move it anyway"
)
_DETAIL_OCCUPIED = "{dest} is already present — not overwritten"

# KeptRecord reasons: a closed set, so a reader can enumerate every way --reset-attempts declines.
# `_KEEP_ERROR` is the ONE open-ended member — it is emitted as `f"{_KEEP_ERROR}: {errno text}"`
# because a filesystem refusal is only actionable with the OS's own message — so a consumer
# matching reasons must use `startswith(_KEEP_ERROR)` for it and equality for the other five.
_KEEP_SHARED = "records an event that is still terminal"
_KEEP_UNREADABLE = "unreadable record"
_KEEP_NO_IDS = "no event ids"
_KEEP_DEST_EXISTS = "destination already exists"
_KEEP_UNSAFE = "run directory name is not a safe path component"
_KEEP_SHAPE = "record is not at <date>/<run-id>/error.json"
_KEEP_ERROR = "could not be archived"


@dataclass(frozen=True)
class RequeueItem:
    """One selected ``_kb/failed/`` event and its verdict."""

    source: Path  # under _kb/failed/
    label: str  # frontmatter id when readable, else source.stem — what the report prints
    writer: str | None
    event_key: str | None
    dest: Path | None  # _kb/inbox/<writer>/<id>.md
    outcome: RequeueOutcome
    detail: str = ""


@dataclass(frozen=True)
class Selector:
    """WHICH terminal events this invocation is about (``--run`` / ``--event`` / ``--all``)."""

    kind: Literal["all", "run", "event"]
    value: str | None = None

    @property
    def label(self) -> str:
        """The report's ``selector=`` value."""
        return "all" if self.kind == "all" else f"{self.kind}:{self.value}"


@dataclass(frozen=True)
class ArchivedRecord:
    """One ``error.json`` the drain rule released, with the events whose budget it was holding."""

    source: str  # repo-relative POSIX
    dest: str  # repo-relative POSIX
    event_ids: tuple[str, ...]


@dataclass(frozen=True)
class KeptRecord:
    """One ``error.json`` ``--reset-attempts`` deliberately left in ``_kb/failed/``, and why."""

    source: str  # repo-relative POSIX
    reason: str


@dataclass(frozen=True)
class RequeueReport:
    """Everything one ``agora requeue`` invocation did, or would do. The face only renders it."""

    selector: Selector
    dry_run: bool
    items: tuple[RequeueItem, ...]
    archived: tuple[ArchivedRecord, ...] = ()
    kept: tuple[KeptRecord, ...] = ()
    failed_events_after: int = 0  # captured INSIDE the lock; PREDICTED in dry-run mode
    precheck_skipped: bool = False  # --force over an unreadable state.json

    @property
    def moved(self) -> tuple[RequeueItem, ...]:
        """The items that moved (or, in dry-run, would move) — see :data:`MOVING_OUTCOMES`."""
        return _moving(self.items)


def _moving(items: tuple[RequeueItem, ...]) -> tuple[RequeueItem, ...]:
    """The moved (or would-move) subset. ONE expression: :attr:`RequeueReport.moved` and the drain
    rule's input both call this, so the printed count and the reset scope cannot drift apart."""
    return tuple(item for item in items if item.outcome in MOVING_OUTCOMES)


# --- orchestration -------------------------------------------------------------------------------
def run_requeue(
    layout: RepoLayout,
    *,
    selector: Selector,
    dry_run: bool = False,
    force: bool = False,
    reset_attempts: bool = False,
    preflight: Callable[[CuratorState], None] | None = None,
) -> RequeueReport:
    """Scan → plan → move → archive, all under ONE ``curator_lock`` span (#99, ADR-0002 appendix).

    The whole batch shares one lock span because anything narrower makes the tier-1 pre-check and
    the budget report go stale mid-batch and makes ``--dry-run``'s promise unprovable. The cost is
    nil — N renames against a curate run that takes seconds to minutes — and ``LOCK_NB`` means the
    curator never waits on us: a concurrent run just gets its usual ``reason_lock_held`` no-op.
    :class:`~agora_kb.curator.claim.LockHeld` propagates to the caller, which reports it and exits
    non-zero with ``_kb/`` untouched (#99 crit 4).

    The ONE filesystem access before the lock is :func:`failed_event_count`, a pure read, and it may
    only short-circuit the case where there is genuinely nothing to do: ``curator_lock`` creates
    ``_kb/`` plus a 0-byte ``curator.lock`` on a pristine repo, so without this a ``--dry-run``
    there would change the filesystem and criterion 5 would be literally false.

    Ordering is binding: events move FIRST, records archive SECOND. A crash between them leaves
    "event moved, budget not reset" — conservative, self-correcting on the next failure — rather
    than "budget reset, events still terminal". A crash must never LOOSEN a safety budget.

    ``preflight`` is the caller's hook for anything that must reach the operator BEFORE the batch
    moves (#99 §4.3's ``--all`` UNRESOLVED warning). It runs inside the lock, on the state this
    invocation actually used, so the face neither re-reads ``state.json`` unlocked nor reports a
    value the batch never saw — and a held lock still produces its one clean line and nothing else,
    because the lock is taken first. The engine itself never prints.

    Raises :class:`StateUnreadable` when ``_kb/state.json`` cannot be loaded and ``force`` is False.
    """
    # ``--reset-attempts`` is the one flag with real work to do on an EMPTY spool, and that state is
    # exactly the crash-convergence case: a kill mid-archive leaves the events already in the inbox
    # with their retry records still retained, so short-circuiting it would make the leaked budget
    # permanent (see :func:`archive_attempt_records`). A pristine repo has no ``_kb/failed/`` at
    # all, so the crit-5 guarantee above is untouched by the exception — and ``is_dir()`` is the
    # same pure read ``failed_event_count`` already performs, not a second kind of access.
    if failed_event_count(layout) == 0 and not (reset_attempts and layout.failed_dir.is_dir()):
        return RequeueReport(selector=selector, dry_run=dry_run, items=(), failed_events_after=0)

    with curator_lock(layout):
        state, precheck_skipped = _load_precheck_state(layout, force=force)
        if preflight is not None and state is not None:
            preflight(state)
        items = plan_requeue(layout, state=state, selector=selector, force=force)
        if not dry_run:
            items = execute_requeue(layout, items)

        archived: list[ArchivedRecord] = []
        kept: list[KeptRecord] = []
        moved = _moving(items)
        # A NAMED selector that matched nothing archives NOTHING. `--reset-attempts` is documented
        # as acting on "the requeued events' retry records", and a mistyped `--run`/`--event` names
        # no events at all — draining the whole spool's budget there would silently hand every
        # pending event a fresh `max_attempts` on the strength of a typo, under a report that says
        # `matched=0`. `--all` is the opposite case: it asserts nothing, and its zero-match state is
        # exactly the crash residue the drain rule exists to converge on, so it still archives.
        if reset_attempts and (items or selector.kind == "all"):
            archived, kept = archive_attempt_records(
                layout,
                remaining_failed_ids=_remaining_terminal_ids(layout, moved),
                dry_run=dry_run,
            )

        # Captured INSIDE the lock so a curator run between release and print cannot invalidate the
        # printed number. PREDICTED in dry-run — the count the preview is promising to produce — so
        # `failed_events:` means one thing in both modes.
        failed_events_after = failed_event_count(layout) - (len(moved) if dry_run else 0)

    return RequeueReport(
        selector=selector,
        dry_run=dry_run,
        items=items,
        archived=tuple(archived),
        kept=tuple(kept),
        failed_events_after=failed_events_after,
        precheck_skipped=precheck_skipped,
    )


def _load_precheck_state(layout: RepoLayout, *, force: bool) -> tuple[CuratorState | None, bool]:
    """Load state for the tier-1 pre-check; return ``(state, precheck_skipped)``.

    Read ONCE, under the lock. Re-reading per event would narrow the race, not close it: the
    authority gap is ``worker.recover()``, which runs OUTSIDE the lock and writes
    ``state.event_keys`` (see :func:`~agora_kb.curator.claim.is_already_delivered`).

    ``--force`` relaxes the load requirement as well as the check itself: with an unreadable
    ``state.json`` the pre-check simply cannot run, and an operator who has explicitly accepted the
    zombie risk should not additionally be blocked by a corrupt file they may be trying to escape.
    Without ``--force`` this refuses (:class:`StateUnreadable`) — the ``_cmd_status`` triad, since
    ``StateStore.load`` deliberately raises rather than discarding ``event_keys``.
    """
    try:
        return StateStore(layout).load(), False
    except (ValidationError, OSError, ValueError) as exc:
        if not force:
            raise StateUnreadable(_one_line(exc)) from exc
        return None, True


def _remaining_terminal_ids(layout: RepoLayout, moved: tuple[RequeueItem, ...]) -> set[str]:
    """THE DRAIN RULE's input: the event ids still terminal when this invocation finishes.

    Two details are load-bearing:

    * **Keyed by BOTH the filename stem and the frontmatter ``id``.** ``error.json`` lists
      frontmatter ids while :func:`~agora_kb.core.inbox.iter_failed_events` yields paths, and the
      two can legitimately disagree — that is precisely why :func:`select_failed_events` has an id
      fallback pass. A stem-only key lets a hand-renamed terminal file's budget be reset while the
      event is still sitting in ``_kb/failed/`` (R8). Both keys are added, which is the
      conservative direction: an extra key can only KEEP a record, never release one.
    * **The moved set is subtracted by PATH, not by id.** In dry-run nothing has moved yet, so the
      planned movers are still on disk and must be excluded here; in a real run they are already
      gone and the subtraction is a no-op. That identity is what makes the ``archived:``/``kept:``
      lines byte-identical in both modes (#99 crit 5).
    """
    moved_sources = {item.source for item in moved}
    remaining: set[str] = set()
    for path in iter_failed_events(layout):
        if path in moved_sources:
            continue
        remaining.add(path.stem)
        event_id = _event_fields(path)[0]
        if event_id:
            remaining.add(event_id)
    return remaining


# --- selection -----------------------------------------------------------------------------------
def select_failed_events(layout: RepoLayout, selector: Selector) -> list[Path]:
    """Filter :func:`~agora_kb.core.inbox.iter_failed_events` — user input NEVER reaches a path API.

    ``--run X`` ⇒ ``path.parent.name == X`` (the terminal layout is
    ``failed/<run_id[:10]>/<run-id>/<event>.md``, ``worker._fail``). ``--event Y`` ⇒
    ``path.stem == Y``; if that matches nothing, ONE fallback pass matches Y against each
    candidate's frontmatter ``id``, because a hand-placed file's stem and its id can disagree and
    the report labels items by id — an operator who pastes back a printed id must get a hit.

    Traversal is not in the threat model here precisely BECAUSE this is a filter: ``../../..``
    matches zero paths, which is a clean "no such run" rather than an escape, and the selector
    stays free of ``validate_writer``'s charset (run ids and event ids are not writers).
    """
    candidates = iter_failed_events(layout)
    if selector.kind == "all":
        return candidates
    value = selector.value
    if selector.kind == "run":
        return [path for path in candidates if path.parent.name == value]
    by_stem = [path for path in candidates if path.stem == value]
    if by_stem:
        return by_stem
    return [path for path in candidates if _event_fields(path)[0] == value]


# --- plan / execute ------------------------------------------------------------------------------
def plan_requeue(
    layout: RepoLayout,
    *,
    state: CuratorState | None,
    selector: Selector,
    force: bool = False,
) -> tuple[RequeueItem, ...]:
    """PURE READ: what WOULD happen to every selected event. Creates nothing, writes nothing.

    One :func:`~agora_kb.core.inbox.resolve_inbox_return` call per selected event — the SAME
    resolver :func:`execute_requeue` runs — so the preview cannot drift from the result (#99 crit 5)
    and the inbox address is derived in exactly one place. ``state=None`` means the tier-1
    pre-check could not run; it is reachable ONLY under ``force``.

    Decline precedence is deterministic because the report's bytes are locked:
    ``unreadable``/``unaddressable`` > ``already-delivered`` > ``destination-exists`` > ``movable``.
    ``--force`` overrides exactly ONE step of that ladder, the tier-1 pre-check — never
    ``destination-exists`` (that would overwrite an immutable inbox event, invariant 3) and never
    ``unreadable``.

    The plan also tracks the destinations it has already PROMISED within this batch. ``_kb/failed/``
    is operator-editable input, so two files under two run directories can carry one event id (a
    restore that merged two failed trees; the ``--event`` id fallback matching several files) and
    resolve to one inbox address. The resolver is stateless and would call both ``movable``, so the
    preview would promise two moves and the real run deliver one — the exact drift criterion 5
    forbids. The second claimant is declined here with the SAME ``destination-exists`` detail
    execution would give it, so the two reports stay byte-identical.
    """
    items: list[RequeueItem] = []
    # Typed like ``InboxReturn.dest`` it holds; in practice only real addresses enter, because both
    # statuses that yield ``dest=None`` are declined before an item can promise anything.
    promised: set[Path | None] = set()
    for source in select_failed_events(layout, selector):
        verdict = resolve_inbox_return(layout, source)
        event_id, writer, event_key = _event_fields(source)
        common = {
            "source": source,
            "label": event_id or source.stem,
            "writer": writer,
            "event_key": event_key,
        }
        if verdict.status in ("unreadable", "unaddressable"):
            # Not a usable event at all — an operator problem, and the one class --force never
            # overrides: there is no address to move it to.
            items.append(
                RequeueItem(
                    **common, dest=None, outcome=RequeueOutcome.unreadable, detail=verdict.detail
                )
            )
            continue
        delivered = (
            state is not None
            and writer is not None
            and is_already_delivered(state, writer=writer, event_key=event_key)
        )
        if delivered and not force:
            items.append(
                RequeueItem(
                    **common,
                    dest=verdict.dest,
                    outcome=RequeueOutcome.already_delivered,
                    detail=_DETAIL_ALREADY_DELIVERED.format(composite=f"{writer}:{event_key}"),
                )
            )
            continue
        if verdict.status == "occupied" or verdict.dest in promised:
            items.append(
                RequeueItem(
                    **common,
                    dest=verdict.dest,
                    outcome=RequeueOutcome.destination_exists,
                    detail=_occupied_detail(layout, verdict.dest),
                )
            )
            continue
        # The resolver's four statuses are now exhausted: unreadable/unaddressable and occupied
        # both `continue`d above, so what is left is `ok`.
        promised.add(verdict.dest)
        items.append(
            RequeueItem(
                **common,
                dest=verdict.dest,
                outcome=RequeueOutcome.forced if delivered else RequeueOutcome.movable,
            )
        )
    return tuple(items)


def execute_requeue(layout: RepoLayout, items: tuple[RequeueItem, ...]) -> tuple[RequeueItem, ...]:
    """Rename-only. Move every movable/forced item and return the SAME list with final outcomes.

    May only DOWNGRADE an item (``movable`` → ``destination-exists`` / ``unreadable`` / ``error``),
    never upgrade one, so a decline in the preview is a decline in the result.

    Every ``OSError`` is contained PER ITEM — including one raised by the mover itself rather than
    returned as a verdict — so a 50-event batch that hits EROFS at item 30 still returns a
    complete, printable account of the 29 that already moved. There is no global ``try/except`` in
    ``cli.main``: an exception escaping here would hide work that genuinely happened. No journal is
    needed for the same reason ``claim()`` has none — each ``os.replace`` is atomic and idempotent,
    and a re-run simply finds the moved events out of the selection set and finishes the job.
    """
    done: list[RequeueItem] = []
    for item in items:
        if item.outcome not in PLANNED_MOVES:
            done.append(item)
            continue
        try:
            verdict = return_event_to_inbox(layout, item.source)
        except OSError as exc:
            done.append(replace(item, outcome=RequeueOutcome.error, detail=_one_line(exc)))
            continue
        if verdict.ok:
            outcome = (
                RequeueOutcome.forced
                if item.outcome is RequeueOutcome.forced
                else RequeueOutcome.requeued
            )
            done.append(replace(item, outcome=outcome, dest=verdict.dest))
            continue
        downgraded = _EXECUTE_DOWNGRADE[verdict.status]
        detail = (
            _occupied_detail(layout, verdict.dest)
            if downgraded is RequeueOutcome.destination_exists
            else verdict.detail
        )
        done.append(replace(item, outcome=downgraded, dest=verdict.dest, detail=detail))
    return tuple(done)


# --- the retry budget (--reset-attempts) ----------------------------------------------------------
def archive_attempt_records(
    layout: RepoLayout,
    *,
    remaining_failed_ids: Collection[str],
    dry_run: bool = False,
) -> tuple[list[ArchivedRecord], list[KeptRecord]]:
    """Restore the §5.1 retry budget by archiving the records the DRAIN RULE releases (#99 crit 8).

    **The drain rule:** a record is archived iff its ``event_ids`` are DISJOINT from
    ``remaining_failed_ids`` — the events that will still be under ``_kb/failed/`` when this
    invocation finishes. In words: *archive a record once none of the events it governs is terminal
    any more.* Four properties make it the rule rather than the obvious alternatives:

    * **Dry-run parity.** ``remaining`` is computed the same way in both modes (planned moves in
      dry-run, actual ones in a real run), so the emitted lines are byte-identical. Keying on "the
      run dir has no remaining ``*.md``" cannot do this — in dry-run nothing has moved, so it is
      false for every record and the preview can never predict the result.
    * **Crash convergence.** After a kill mid-archive, re-running finds the events already in the
      inbox, so ``remaining`` is empty for their records and the leftovers archive. Keying on "the
      ids I moved in THIS invocation" would make them permanently un-archivable — a silent budget
      corruption nobody would ever be told about.
    * **No still-TERMINAL event's budget can drop.** A record listing an event that is still under
      ``_kb/failed/`` is kept — criterion 9's safety property, in the exact terms the rule can
      enforce. See the scope note below for what this does NOT promise.
    * **Idempotent.** Running it twice is a no-op the second time.

    **Scope, stated precisely because it is easy to over-read.** The rule can only see
    ``_kb/failed/``, so a record is released as soon as none of its events is terminal — including
    when an event it lists is pending in ``_kb/inbox/`` with attempts already spent. Two disk states
    are byte-identical there: crash residue from an interrupted requeue (the leaked budget this flag
    exists to reclaim) and an event the curator is mid-way through retrying. Releasing both is the
    deliberate choice, because the alternative makes crash residue permanently un-reclaimable, and
    the direction of the error is the safe one — an event gets MORE attempts, never fewer. So
    ``--reset-attempts`` may hand a spent budget back to an event the selector did not name; every
    such record is printed on an ``archived:`` line. Relatedly, **the selector scopes EVENTS while
    the budget scopes RECORDS**: three consecutive failures write three records in three different
    run directories, so restoring one event's budget legitimately touches run dirs the selector did
    not name. A named selector that matched NO events archives nothing at all
    (:func:`run_requeue`), so a typo cannot reach any of this.

    Called ONLY under the already-held ``curator_lock`` and ONLY AFTER the moves. Rename-only and
    non-destructive: an occupied destination is kept, never clobbered, and the emptied
    ``_kb/failed/<date>/<run>/`` directory is left in place (nothing here ever deletes). Results are
    sorted by ``source`` so dry-run and the real run emit identical lines.
    """
    remaining = set(remaining_failed_ids)
    archived: list[ArchivedRecord] = []
    kept: list[KeptRecord] = []
    failed_root = layout.failed_dir
    if not failed_root.is_dir():
        return archived, kept
    # `iter_attempt_records` is THE budget derivation (worker.py) and skips records it cannot read,
    # exactly as `_event_attempt_counts` does. Requeue re-walks the record FILES so a skipped one is
    # reported as `kept (unreadable record)` instead of vanishing from an operator-facing list — an
    # unreadable record still holds a real budget, and silence about it would be the worst outcome.
    readable = dict(iter_attempt_records(layout))
    for record_path in sorted(failed_root.rglob("error.json")):
        source = rel_to_repo(layout, record_path)
        event_ids = readable.get(record_path)
        if event_ids is None:
            kept.append(KeptRecord(source=source, reason=_KEEP_UNREADABLE))
            continue
        if not event_ids:
            kept.append(KeptRecord(source=source, reason=_KEEP_NO_IDS))
            continue
        if set(event_ids) & remaining:
            kept.append(KeptRecord(source=source, reason=_KEEP_SHARED))
            continue
        if record_path.parent.parent.parent != failed_root:
            # `rglob` matches at ANY depth, but the archive is defined as the `_kb/failed/` TWIN:
            # <date>/<run-id>/error.json. A record at another depth would derive its twin from the
            # wrong parent names — releasing a real budget to an address `cli._record_pointer`
            # cannot follow. Keeping it is the conservative answer: the budget stays counted.
            kept.append(KeptRecord(source=source, reason=_KEEP_SHAPE))
            continue
        try:
            # Both components are directory names read off an operator-editable tree, so both go
            # through safe_path_component inside the layout (DESIGN §7).
            dest = layout.requeued_record_path(
                date=record_path.parent.parent.name, run_id=record_path.parent.name
            )
        except InvalidWriterError:
            kept.append(KeptRecord(source=source, reason=_KEEP_UNSAFE))
            continue
        if dest.exists():
            kept.append(KeptRecord(source=source, reason=_KEEP_DEST_EXISTS))
            continue
        if not dry_run:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(record_path, dest)
            except OSError as exc:
                # Contained per record for the same reason execute_requeue contains its moves: the
                # events have ALREADY moved by this point, and losing the report would hide that.
                kept.append(KeptRecord(source=source, reason=f"{_KEEP_ERROR}: {_one_line(exc)}"))
                continue
        archived.append(
            ArchivedRecord(
                source=source, dest=rel_to_repo(layout, dest), event_ids=tuple(event_ids)
            )
        )
    archived.sort(key=lambda record: record.source)
    kept.sort(key=lambda record: record.source)
    return archived, kept


# --- helpers -------------------------------------------------------------------------------------
def _event_fields(path: Path) -> tuple[str | None, str | None, str | None]:
    """``(id, writer, event_key)`` read TOLERANTLY from one event's frontmatter.

    Never raises and never validates: an unparseable file simply yields ``(None, None, None)`` and
    the resolver — the single authority on whether an event is addressable — supplies the verdict.
    This exists only so the report can LABEL an item by its id and name the tier-1 composite; it
    must never become a second opinion about where the event goes.
    """
    try:
        fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None, None
    values = tuple(fm.get(key) for key in ("id", "writer", "event_key"))
    event_id, writer, event_key = (value if isinstance(value, str) else None for value in values)
    return event_id, writer, event_key


def _occupied_detail(layout: RepoLayout, dest: Path | None) -> str:
    """The pinned ``destination-exists`` detail, naming the repo-relative slot that is taken."""
    return _DETAIL_OCCUPIED.format(dest=rel_to_repo(layout, dest) if dest else "the inbox slot")


def rel_to_repo(layout: RepoLayout, path: Path | None) -> str:
    """Repo-relative POSIX rendering — the ``worker._fail`` ``record_path`` convention.

    Host-free by construction (invariant 5), so requeue's output can be pasted into an issue and
    matched against ``agora status``'s ``failed_record:`` without editing. The ONE renderer for
    both the engine's ``archived:``/``kept:`` lines and the CLI's per-item lines, so a change to
    the convention cannot land on half the report. A path somehow outside the repo falls back to
    its own POSIX form rather than raising, and a missing address renders ``-``: a report must
    always be printable.
    """
    if path is None:
        return "-"
    try:
        return path.relative_to(layout.root).as_posix()
    except ValueError:
        return path.as_posix()
