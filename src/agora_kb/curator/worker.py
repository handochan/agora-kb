"""The capstone transactional curator run-loop + crash recovery (ADR-0008, ADR-0011, DESIGN §4).

This is the deterministic orchestrator that ties Stages A/B/the plan/apply gates into ONE
transaction. It owns everything around the two delegated cognitive acts (ADR-0011 §7); the backend
brain only decides (PASS 1 ``plan.json``) and authors prose (PASS 2). Success is a pure function of
``(plan.json, git_diff, manifest, bundle, lint)`` — the model is OUTSIDE the integrity boundary,
so this module is unit-testable with a :class:`FakeBackend` and ZERO real model in the loop.

The run transaction (:func:`run`) implements ADR-0008 steps 1-6 / DESIGN §4 / ADR-0011 §0:

1. acquire the per-repo :func:`~agora_kb.curator.claim.curator_lock` (held ⇒ ``noop``);
2. snapshot the inbox into ``processing/<run-id>/`` via the atomic FIFO
   :func:`~agora_kb.curator.claim.claim` (empty/all-deduped ⇒ ``noop``) and write the manifest
   (``phase=claimed``);
3. build the read-only :func:`~agora_kb.curator.bundle.build_bundle` (tier-2 dedup + ``related/``);
4. open a detached worktree at ``base_commit``; PASS 1 — :meth:`Backend.plan` → ``plan.json``
   → :func:`~agora_kb.curator.plan.validate_plan` (the §4.1 gate). On any PLAN/LINT/CAS failure
   the whole diff is discarded and events are returned to ``inbox/`` (retry the §5.1 budget) or
   moved to ``failed/`` with an error record — NOTHING partial is ever published;
5. APPLY the plan structurally (:func:`~agora_kb.curator.apply.apply_plan`), set ``phase=applied``;
   PASS 2 — :meth:`Backend.author` fills the candidate-id body sentinels, the §4.6 stray-wikilink
   strip repairs any stray ``[[X]]``, then :func:`~agora_kb.curator.apply.validate_author_diff`
   (degrade-or-fail per §4.2) and the deterministic :func:`~agora_kb.schema.lint.lint` (§4.4, must
   be ``ok``);
6. append ONE structured ``log.md`` entry (worker-only, AFTER validation, §4.3); assert the final
   ``git diff base_commit..HEAD`` touches ONLY canonical-ALLOWLIST paths with no introduced symlink/
   escape and ZERO ``_agora_scratch/`` tracked changes (§4.0/§4.3/§4.5 — the deterministic integrity
   boundary, ADR-0008 step 4); commit once; persist the candidate commit sha at ``phase=applied``
   BEFORE the CAS (so a crash in the CAS-success window recovers via the §9 git-ref row, never
   double-publishes); compare-and-swap the curated ref ``base_commit → new_commit`` (the durable
   publish point). Move events to ``processed/<date>/`` then record ``published_runs`` + counters +
   ``event_keys`` into ``state.json`` (processed-first so the keys are read from their final home),
   advance the manifest ``claimed → applied → published → finalized``.

Recovery (:func:`recover`) is the ADR-0011 §9 truth-table over
:func:`~agora_kb.curator.manifest.list_processing`: a ``published`` run (or one whose git ref
already points at its commit) is FINALIZED without any backend call (ADR-0008 step 6); a
``claimed`` or un-``prose_complete`` ``applied`` run returns its unchanged events to the inbox to
re-run from PASS 1 (the conservative default; the dropped worktree took ``plan.json`` with it); a
``finalized`` run is cleaned up.

Sandbox note (ADR-0013): the OS sandbox is NOT wired here. ``run`` invokes an INJECTED
:class:`Backend` abstraction; a sandbox wrapper around the *real* backend is a later layer. The
DETERMINISTIC validation chain (``validate_plan`` + ``validate_author_diff`` + ``lint`` + the
allowlisted final-diff assertion) is the integrity gate REGARDLESS of any sandbox (ADR-0008/0011
§7), which is exactly why the contract is gradable with a fake backend.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from ..core import frontmatter
from ..core.frontmatter import FrontmatterError
from ..core.ids import new_event_id
from ..core.inbox import Inbox, return_event_to_inbox
from ..core.layout import RepoLayout
from ..core.repo import GitError, Repo
from ..core.sentinel import BODY_RESET_PLACEHOLDER, UNAUTHORED_REGION_BODIES, has_unauthored_region
from ..core.sentinel import BODY_START_LINE_RE as _START_SENTINEL_RE
from ..core.state import CuratorState, LastBatch, LastFailure, StateStore
from ..schema.emit import Taxonomy
from ..schema.lint import lint
from ..schema.notes import Note, parse_all_notes
from .apply import (
    ApplyError,
    _contained,
    apply_plan,
    body_sentinels,
    region_sentinel_id,
    strip_stray_wikilinks,
    validate_author_diff,
)
from .bundle import BundleResult, build_bundle
from .claim import LockHeld, claim, curator_lock
from .constants import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_CANDIDATES_PER_RUN,
    DEFAULT_RELATED_K,
    SCHEMA_SYMLINKS,
    SCRATCH_DIRNAME,
    is_allowlisted_path,
)
from .manifest import Phase, RunManifest, list_processing, write_manifest
from .plan import Disposition, Plan, PlanParseError, validate_plan
from .subprocess_backend import BackendUnavailableError

if TYPE_CHECKING:
    from ..config import ConnectorSpec

__all__ = [
    "CURATOR_STAMP_KEYS",
    "AuthorRegion",
    "Backend",
    "FakeBackend",
    "HarvestCursorDelta",
    "LiveTree",
    "RunFailure",
    "RunReport",
    "compute_harvest_cursor_deltas",
    "is_curator_written",
    "iter_attempt_records",
    "run",
    "recover",
    "scan_live_tree",
]

# §4.2 AUTHOR-failure RESET placeholder (ADR-0011 §4.2): the blockquote derived from the plan
# summary. DISTINCT from APPLY's initial ``_summary pending_`` fill — this is the degrade-on-
# failure body the worker substitutes when a note's PASS-2 diff is rejected, so the run still
# publishes a structurally-valid (but prose-pending) note. The spelling itself now lives in
# :mod:`agora_kb.core.sentinel` (#119) so ``schema/lint.py`` grades "unauthored" with the SAME
# vocabulary without importing the curator; this is a local alias, not a second spelling.
_RESET_PLACEHOLDER = BODY_RESET_PLACEHOLDER

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthorRegion:
    """PASS-2 grounding for ONE run-scoped body-sentinel region (INGEST-CONTRACT §8.2).

    The worker substitutes these deterministically into the PASS-2 AUTHOR prompt so the backend
    grounds its prose in the candidate's verbatim captured ``source_text`` and is OP-AWARE (a MERGE
    region writes only the NEW claim, a CREATE writes the whole body, a daily writes the dated
    capture). Keyed by the run-scoped ``region_sentinel_id`` in the worker's context map; only THIS
    run's authored regions carry one (prior-run regions are not re-authored).
    """

    op: str
    title: str | None
    summary: str | None
    source_text: str  # the candidate's verbatim captured text (the "source facts", §8.2)


class Backend(Protocol):
    """The swappable INGEST brain — exactly the two delegated cognitive acts (ADR-0011 §7).

    The worker passes the backend ONLY read-only bundle + writable-worktree paths (argv/stdin in
    the real adapter, ADR-0008 §3); the backend is OUTSIDE the integrity boundary, so every output
    is graded deterministically by the worker (§4). A test :class:`FakeBackend` satisfies this
    Protocol with canned outputs and no model.
    """

    def plan(self, bundle_dir: Path) -> str:
        """PASS 1 — read the read-only ``bundle/``; return the ``plan.json`` text (§4.1 input).

        The backend reads ``bundle/candidates.json`` + ``bundle/related/*`` + ``bundle/schema.md`` +
        ``bundle/taxonomy.yaml`` and returns the closed-vocabulary plan as a JSON string. It writes
        no wiki files; the worker validates the returned text with :func:`Plan.from_json` +
        :func:`validate_plan`.
        """
        ...

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],
    ) -> list[str] | None:
        """PASS 2 — fill the candidate-id body sentinels in ``worktree`` (§3 / §4.2).

        ``needs_prose`` maps each note's repo-relative path to the run-scoped region ids whose
        ``agora:body:start id=<sid> … end`` sentinel regions the backend must author. ``context``
        maps each of those run-scoped ids to its :class:`AuthorRegion` grounding (op + title +
        summary + the candidate's verbatim source text, §8.2) so the backend can ground its prose
        and honor the op. The backend writes ONLY between those markers; the worker validates the
        diff with :func:`validate_author_diff` and strips/degrades anything out of bounds.

        MAY return one human-readable diagnostic per region it failed to author (a non-zero exit,
        an unreachable model), which the worker surfaces on the :class:`RunReport`. ``None`` is the
        no-diagnostics answer, so an implementation that simply writes prose (every test fake) is
        unaffected. The worker NEVER treats a returned diagnostic as authority on success: whether
        prose actually landed is decided by the §4.2 diff, never by the backend's own account.
        """
        ...


class FakeBackend:
    """A model-free :class:`Backend` for tests: a canned ``plan.json`` + a ``{cid: prose}`` map.

    :meth:`plan` returns the canned plan text verbatim. :meth:`author` writes each candidate's prose
    BETWEEN that candidate's body sentinels in every note that declares it (so it honors the §4.2
    "only sentinel regions change" contract), leaving frontmatter and out-of-region text untouched.
    A candidate id missing from ``prose`` is left at its APPLY placeholder.
    """

    def __init__(self, plan_text: str, prose: dict[str, str] | None = None) -> None:
        self._plan_text = plan_text
        self._prose = dict(prose or {})

    def plan(self, bundle_dir: Path) -> str:  # noqa: ARG002 — bundle is unused by the fake
        return self._plan_text

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],  # noqa: ARG002 — canned prose ignores grounding
    ) -> None:
        for rel_path, cids in needs_prose.items():
            path = worktree / rel_path
            text = path.read_text(encoding="utf-8")
            for cid in cids:
                prose = self._prose.get(cid)
                if prose is None:
                    continue
                text = _replace_sentinel_region(text, cid, prose)
            path.write_text(text, encoding="utf-8")


def _is_theme_note(note: Note) -> bool:
    """True iff ``note`` lives at ``wiki/<domain>/themes/<basename>.md`` (a THEME note).

    Path-based, mirroring :func:`apply._resolve_target_path`'s ``theme_only`` semantics so the §4.1
    BASENAME/PROVENANCE THEME-target check grades EXACTLY what APPLY will accept — robust to a wrong
    or missing ``type:`` frontmatter value (the live tree's directory is authoritative).
    MERGE/CONTEST may only target such a note; a MOC/index/daily basename must be rejected at the
    PLAN gate rather than crash APPLY.
    """
    parts = note.rel_path.split("/")
    # wiki / <domain> / themes / <basename>.md  (exactly four POSIX segments)
    return len(parts) == 4 and parts[0] == "wiki" and parts[2] == "themes"


# --- the producer/consumer boundary inside wiki/ (issue #152, ADR-0014 D1/D4 addendum) ----------
#
# A human is ALLOWED to write in `wiki/` — nothing forbids it, and a schema-valid hand-written note
# publishes untouched. What was NOT allowed was the consequence: the note was graded as CURATOR
# OUTPUT (strict producer) although the curator never wrote it, so one Obsidian save (Properties
# instead of the ADR-0010 §2 frontmatter, or no fence at all) hard-rejected EVERY subsequent run,
# forever, because the offending file sits in the base commit. The fix is a classification BEFORE
# the producer gate, not a softer gate: L1 severities stay frozen (kb_schema.md), the closed op
# vocabulary is untouched, and no rule changes. See the ADR-0014 addendum for the rejected
# alternatives (severity downgrade; git authorship).

# The frontmatter keys the ENGINE — never the model, never Obsidian — materializes on a note it
# wrote. `sources:` is written by APPLY's provenance union on every theme/daily (ADR-0011 §2: "the
# WORKER writes sources:, never the model"); `timestamp:` is the deterministic OKF
# `<updated>T00:00:00Z` APPLY stamps on every note type it creates or re-renders and `_set_updated`
# keeps in lock-step (ADR-0014 D2), including the seed `index.md` `repo init` writes. Presence of
# EITHER is the curator's stamp. The union (not the intersection) is what makes the discriminator
# total across the four note types and tolerant of a theme published before the OKF fields landed.
CURATOR_STAMP_KEYS: frozenset[str] = frozenset({"sources", "timestamp"})

# The same stamp, looked for TEXTUALLY, in a frontmatter block that does not parse. A malformed
# block hides its keys from `frontmatter.parse`, and "malformed" is precisely where the two
# populations must NOT be conflated: a broken CURATOR note is an integrity signal that still fails
# the run, a broken HUMAN note is somebody's draft.
_STAMP_LINE_RE = re.compile(r"^(?:sources|timestamp):")
# How far into an unclosed block the textual scan looks. The scan is deliberately confined to the
# FRONTMATTER REGION, never the body: a fenceless note whose PROSE happens to contain a line reading
# `sources: …` must not be mistaken for a damaged curator note — that misfiling would resurrect the
# permanent-failure bug this whole change exists to kill, for the one shape nobody would look at.
_MAX_STAMP_SCAN_LINES = 40


def _stamp_in_frontmatter_region(text: str) -> bool:
    """Best-effort: does an UNPARSEABLE frontmatter block still visibly carry a curator stamp?

    Only ever asked of a note whose frontmatter did NOT parse. Text that does not open with a
    ``---`` fence has no frontmatter block at all and therefore cannot be something APPLY rendered
    (:func:`agora_kb.core.frontmatter.render` always opens with one), so it is never stamped. For a
    block that opens but is malformed or unclosed, the scan stops at the closing fence or after
    :data:`_MAX_STAMP_SCAN_LINES` lines — bounding an unclosed fence to the region a real
    frontmatter block could occupy.

    The fence test tolerates a leading UTF-8 BOM and a stray CR (see below), and the discriminator
    is deliberately TEXTUAL: a human ``Properties`` block that is YAML-invalid AND happens to carry
    a line starting ``sources:`` / ``timestamp:`` is classified as a damaged PRODUCER artifact and
    fails the run, naming the file. That is the conservative direction on purpose (a genuinely
    damaged curator note must never be reclassified away as somebody's draft) and it is recorded as
    a known residual in the ADR-0014 addendum.
    """
    lines = text.split("\n")
    if not lines:
        return False
    # A leading UTF-8 BOM must not hide the fence. PowerShell `Set-Content`/`Out-File` and several
    # Windows editors write UTF-8-with-BOM by default (epic #85), and `frontmatter.parse` rejects
    # such a file — so WITHOUT this strip a BOM'd CURATOR note is silently demoted to "human",
    # which drops it out of the producer lint scope where L1-16 (the rule that exists to catch
    # exactly this damage) would never read it again. Stripping keeps it a damaged producer
    # artifact: the run fails loudly and names the file, which is what the ADR-0014 addendum
    # promises. A stray CR is stripped for the same "the fence is still a fence" reason.
    if lines[0].lstrip("\ufeff").rstrip(" \t\r") != "---":
        return False
    for line in lines[1 : 1 + _MAX_STAMP_SCAN_LINES]:
        if line.rstrip(" \t\r") == "---":
            return False  # a closed block: the stamp was not in it
        if _STAMP_LINE_RE.match(line):
            return True
    return False


def is_curator_written(note: Note) -> bool:
    """True iff ``note``'s frontmatter carries a curator stamp (:data:`CURATOR_STAMP_KEYS`).

    The discriminator between a PRODUCER artifact (graded by the L1 gate, a legal MERGE/CONTEST
    target) and a note a human wrote in ``wiki/`` (read, indexed, linkable — never graded, never
    written to). Frontmatter-based ON PURPOSE: git authorship (the rejected alternative (c)) is
    absent from a fresh clone and would misfile the legitimate case of a human hand-editing a
    curator note, while this predicate is a pure function of the bytes in the tree.

    Deliberately conservative in the one direction that matters: a human note that happens to carry
    ``sources:``/``timestamp:`` is graded as producer output, i.e. exactly today's behaviour.
    """
    return any(key in note.frontmatter for key in CURATOR_STAMP_KEYS)


def _is_structural_curator_path(rel_path: str) -> bool:
    """True iff APPLY rewrites this path BY CONSTRUCTION, whoever last saved it (#152 follow-up).

    The three structural notes the deterministic APPLY step opens without ever asking the plan gate
    for permission: the root ``index.md`` (:func:`apply._update_index`), a domain MOC
    ``wiki/<domain>/<domain>-moc.md`` (:func:`apply._update_moc`), and a per-domain daily
    ``wiki/<domain>/daily/<basename>.md`` (:func:`apply._apply_append_daily`). Each of those call
    sites does an UNGUARDED ``frontmatter.parse`` of the existing file.

    THEME notes are deliberately NOT here: a MERGE/CONTEST target must clear the PLAN gate's
    ``theme_basenames`` (built from :attr:`LiveTree.curator_paths`) before APPLY ever opens it, so a
    malformed human theme is already rejected as a clean plan error. These three have no such gate —
    the curator owns them by construction — so a malformed one is an integrity signal, not
    somebody's draft, and must FAIL the run (clean, named) instead of crashing APPLY mid-batch.
    """
    if rel_path == "index.md":
        return True
    parts = rel_path.split("/")
    if parts[0] != "wiki":
        return False
    # wiki / <domain> / <domain>-moc.md
    if len(parts) == 3 and parts[2] == f"{parts[1]}-moc.md":
        return True
    # wiki / <domain> / daily / <basename>.md
    return len(parts) == 4 and parts[2] == "daily"


def _malformed_frontmatter(path: Path) -> tuple[str, str] | None:
    """Return ``(raw_text, error message)`` if ``path``'s frontmatter does not parse, else ``None``.

    Read tolerantly (``errors="replace"``) for the same reason
    :func:`~agora_kb.schema.notes.parse_all_notes` is: a non-UTF-8 note in the live tree must not
    raise :class:`UnicodeDecodeError` out of the classification (ADR-0014 D4).
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        frontmatter.parse(text)
    except FrontmatterError as exc:
        return text, str(exc)
    return None


@dataclass(frozen=True)
class LiveTree:
    """The classified live-tree scan the run's registries and producer-lint scope are built from.

    ``notes`` is the TOLERANT parse of every note (a malformed one degrades to empty frontmatter +
    full body) — the read-path view, ADR-0014 D4. ``curator_paths`` are the notes carrying the
    engine's stamp; ``human_paths`` are the rest, in deterministic path order.
    ``malformed_curator`` is the first malformed note that still visibly carries the stamp, or sits
    at a structural path APPLY rewrites unconditionally (:func:`_is_structural_curator_path`) — a
    real integrity signal that must fail the run rather than be classified away.
    """

    notes: tuple[Note, ...]
    curator_paths: frozenset[str]
    human_paths: tuple[str, ...]
    malformed_curator: str | None


def scan_live_tree(layout: RepoLayout) -> LiveTree:
    """Parse the live tree once and split it into curator-produced vs human-written notes (#152).

    Replaces the strict :func:`parse_all_notes` call that used to abort the run on the FIRST
    unparseable note in the curated tree. The strictness is not dropped, it is SCOPED: a malformed
    note that still carries a curator stamp — or sits at one of the structural paths APPLY rewrites
    by construction (:func:`_is_structural_curator_path`) — is reported in ``malformed_curator``
    (the caller fails the run exactly as before); a malformed note without one is a human draft and
    joins ``human_paths``, which the caller warns about and otherwise leaves alone.

    Only notes whose PARSED frontmatter has no stamp are re-read, so the extra IO is bounded by the
    number of human notes (zero in a purely curated repo).
    """
    notes = parse_all_notes(layout)  # TOLERANT: never aborts on somebody's draft
    curator: list[str] = []
    human: list[str] = []
    malformed_curator: str | None = None
    for note in notes:
        if is_curator_written(note):
            curator.append(note.rel_path)
            continue
        # No stamp in the PARSED frontmatter — either the note genuinely has none, or its block did
        # not parse at all and the tolerant read handed us `{}`. Only the empty-frontmatter notes
        # can be the latter, so only those are re-read.
        if not note.frontmatter:
            malformed = _malformed_frontmatter(layout.root / note.rel_path)
            if malformed is not None:
                text, message = malformed
                # Stamped, OR at a path APPLY rewrites unconditionally. The second half is what
                # keeps a fenceless `index.md` / domain MOC / daily out of APPLY's UNGUARDED
                # `frontmatter.parse`: classified as a human draft it would sail past this gate and
                # crash `run()` with a traceback, stranding the claimed batch in `_kb/processing/`
                # while `agora status` reported `failed_events: 0` — strictly worse than the hard
                # rejection #152 set out to remove. The curator owns those three by construction, so
                # the clean named `_fail` IS the right answer for them.
                if _stamp_in_frontmatter_region(text) or _is_structural_curator_path(note.rel_path):
                    curator.append(note.rel_path)
                    if malformed_curator is None:  # first in sorted scan order (fail-fast shape)
                        malformed_curator = f"{note.rel_path}: {message}"
                    continue
        human.append(note.rel_path)
    return LiveTree(
        notes=tuple(notes),
        curator_paths=frozenset(curator),
        human_paths=tuple(human),
        malformed_curator=malformed_curator,
    )


# How many out-of-scope note paths one warning line names before it summarizes the rest. The
# warning is an operator signal, not an inventory: `agora doctor` prints the full count and the
# notes are right there in the tree.
_MAX_NAMED_HUMAN_NOTES = 5


def _human_notes_warning(human_paths: tuple[str, ...]) -> str:
    """Render the ONE operator-facing line naming the notes this run did not grade (#152)."""
    named = ", ".join(human_paths[:_MAX_NAMED_HUMAN_NOTES])
    if len(human_paths) > _MAX_NAMED_HUMAN_NOTES:
        named += f", … and {len(human_paths) - _MAX_NAMED_HUMAN_NOTES} more"
    return (
        f"LIVE-TREE: {len(human_paths)} note(s) in the curated tree carry no curator stamp — "
        f"left byte-identical, excluded from the plan registry and from the producer lint, still "
        f"readable and indexed: {named}"
    )


def _read_notes(worktree: Path, needs_prose: dict[str, list[str]]) -> dict[str, str]:
    """Read each ``needs_prose`` note's current text; a note PASS 2 DELETED reads as ``""``.

    PASS 2 is an untrusted backend with a writable worktree, so it can delete or rename a note it
    was asked to author. A bare ``read_text`` would then raise ``FileNotFoundError`` straight out of
    :func:`run`, which catches only ``LockHeld`` — an uncaught traceback where the contract promises
    a clean FAILED run. Reading the absent note as empty keeps the deterministic path: the §4.2
    validator sees a changed file whose sentinels are gone and rejects it, and
    :func:`_unauthored_regions` counts every missing region as pending.
    """
    state: dict[str, str] = {}
    for rel in needs_prose:
        path = worktree / rel
        state[rel] = path.read_text(encoding="utf-8") if path.is_file() else ""
    return state


def _region_body(text: str, sentinel_id: str) -> str | None:
    """Return the text BETWEEN ``sentinel_id``'s body markers, or ``None`` if the region is absent.

    The exact inverse of :func:`_replace_sentinel_region`'s surgery, used by
    :func:`_unauthored_regions` to grade PASS 2 per REGION rather than per FILE. Pure.
    """
    start, end = body_sentinels(sentinel_id)
    si = text.find(start)
    ei = text.find(end)
    if si == -1 or ei == -1 or ei < si:
        return None
    return text[si + len(start) : ei]


# A region body that still reads as one of these carries no prose, whatever the diff says: APPLY's
# initial fill, the §4.2 reset form, or nothing at all. Derived from the REAL constants rather than
# re-spelled, so a change to either placeholder cannot silently stop being detected — in the
# AUTHOR-diff-rejected path this set is the only thing standing between a reset note and a report
# claiming its prose landed. Compared after ``strip()`` so trailing-newline churn is not mistaken
# for authored content. Now a local alias of the :mod:`agora_kb.core.sentinel` set (#119) — the
# SAME frozenset the L2-6 lint rule grades with, so the run-scoped ``_unauthored_regions`` verdict
# and the note-local ``has_unauthored_region`` verdict can never disagree on the placeholder set.
_EMPTY_REGION_BODIES = UNAUTHORED_REGION_BODIES


def _unauthored_regions(
    needs_prose: dict[str, list[str]],
    per_file_old: dict[str, str],
    per_file_new: dict[str, str],
) -> list[tuple[str, str]]:
    """Return every ``(rel_path, sentinel_id)`` PASS 2 left WITHOUT prose (§4.2 gap, issue #115).

    A region counts as unauthored when its body came back byte-identical to the post-APPLY state, or
    when what came back is still a placeholder. This is deliberately REGION-granular: the per-FILE
    ``changed`` list the §4.2 diff validator consumes cannot tell "the brain authored 2 of this
    note's 3 regions" from "the brain authored all 3", and — the #115 failure — cannot tell "the
    brain wrote nothing anywhere" from "there was nothing to do". An empty diff produced no
    validation errors, so ``prose_complete`` stayed ``True`` and the run reported success while
    every body on disk was the placeholder.

    Pure + deterministic, and it grades the WORKTREE, never the backend's own account of itself —
    the model stays outside the integrity boundary (ADR-0011 §4).
    """
    unauthored: list[tuple[str, str]] = []
    for rel, sids in needs_prose.items():
        old_text = per_file_old.get(rel, "")
        new_text = per_file_new.get(rel, "")
        for sid in sids:
            new_body = _region_body(new_text, sid)
            if new_body is None:
                # The region vanished — a structural violation the §4.2 validator rejects on its
                # own; count it as unauthored so the report never claims prose that is not there.
                unauthored.append((rel, sid))
                continue
            if new_body.strip() in _EMPTY_REGION_BODIES or new_body == _region_body(old_text, sid):
                unauthored.append((rel, sid))
    return unauthored


def _replace_sentinel_region(text: str, candidate_id: str, prose: str) -> str:
    """Return ``text`` with the ``candidate_id`` body-sentinel region body replaced by ``prose``.

    Pure string surgery between the exact ``agora:body:start/end id=<cid>`` marker lines (so markers
    + frontmatter + out-of-region text are byte-preserved). Used only by :class:`FakeBackend`; the
    real backend writes the region itself inside the sandbox.
    """
    start, end = body_sentinels(candidate_id)
    si = text.find(start)
    ei = text.find(end)
    if si == -1 or ei == -1 or ei < si:
        return text
    region_start = si + len(start)
    return f"{text[:region_start]}\n{prose}\n{text[ei:]}"


@dataclass(frozen=True)
class RunFailure:
    """WHY a ``failed`` run failed — the fatal counterpart to :attr:`RunReport.warnings` (#96).

    Companion, not replacement: ``warnings`` (#115) carries NON-FATAL diagnostics — normally those
    of a run that STILL published, and since #96 also those of a failed run whose own observability
    write failed; this carries the FATAL cause of a run that published nothing. Both ride the
    SAME :class:`RunReport` and are rendered by the SAME single face-side printer
    (``cli._print_run_diagnostics``), so no face can render one and forget the other.

    ``record_path`` is the repo-RELATIVE, POSIX-separated path of the
    ``_kb/failed/<date>/<run-id>/error.json`` :func:`_fail` just wrote — the durable, LOSSLESS
    record (every ``failed_checks`` entry, the event ids, the base commit). Relative because the
    same string is persisted in ``state.json`` (:class:`~agora_kb.core.state.LastFailure`), which
    must survive a repo move and must not leak the host layout into a repo-scoped file
    (invariant 5). ``None`` on the ONE path that writes no record: a CAS conflict, where the events
    are valid, return to ``inbox/``, and never burn the §5.1 retry budget.

    ``reasons`` is a BOUNDED echo of that record's ``failed_checks`` (:func:`_bounded_reasons`),
    never the source of truth.
    """

    run_id: str
    phase: Phase
    reasons: tuple[str, ...] = ()
    record_path: str | None = None
    cas_conflict: bool = False

    def summary(self, *, limit: int = 3, width: int = 140) -> str:
        """One-line ``failed_checks`` rendering: first ``limit`` checks, then ``… +N more``.

        Lives HERE, not in a face, so ``agora curate``, the ``agora watch`` tick and any future
        surface emit the SAME bytes for the same failure — re-implementing the elision per face is
        precisely the scatter #115 removed. The full list is behind :attr:`record_path`.
        """
        if not self.reasons:
            return "-"
        shown = [r if len(r) <= width else r[:width].rstrip() + "…" for r in self.reasons[:limit]]
        extra = len(self.reasons) - len(shown)
        if extra > 0:
            shown.append(f"… +{extra} more")
        return " | ".join(shown)


# The RunReport's reasons echo is BOUNDED. A ``PLAN-BACKEND`` reason embeds the brain's raw stderr
# VERBATIM (SubprocessBackend.plan applies NO cap — unlike PASS-2's ``_FAILURE_DETAIL_CHARS``),
# which is routinely a multi-kilobyte traceback. Every consumer renders this list — an operator's
# terminal, an agent's kb_curate context window, and `_kb/state.json` via LastFailure — so the bound
# lives HERE, ONCE, at construction. The un-truncated text always stays in error.json. This is the
# ONLY truncator of the STORED/echoed reason text in the curator layer; `LastFailure` caps the LIST
# LENGTH but never re-clips a string (one rule, no double-truncation, and `reasons_total` therefore
# cannot lie). `RunFailure.summary` elides too, but at DISPLAY time only — it renders this already-
# bounded echo onto one terminal line and feeds no persisted field.
_REASON_CHARS = 400


def _bounded_reasons(reasons: list[str]) -> tuple[str, ...]:
    """Flatten each reason to ONE line and cap it at :data:`_REASON_CHARS` for the report echo.

    Whitespace-COLLAPSED, not first-line-truncated: for a brain traceback the first line
    (``Traceback (most recent call last):``) is the least informative one, so dropping the tail is
    strictly worse than flattening it. Entry COUNT is preserved (only each string is clipped), so
    ``len(result) == len(reasons)`` always.
    """
    out: list[str] = []
    for reason in reasons:
        flat = " ".join(str(reason).split())
        out.append(flat if len(flat) <= _REASON_CHARS else flat[:_REASON_CHARS].rstrip() + "…")
    return tuple(out)


@dataclass(frozen=True)
class RunReport:
    """The outcome of one :func:`run` (or a single :func:`recover` step).

    ``status`` is the terminal verdict: ``published`` (the CAS landed), ``failed`` (a PLAN/LINT/CAS
    rejection — nothing published, events moved to ``failed/`` or back to ``inbox/``), ``noop``
    (lock held or nothing to claim), or ``recovered`` (a crashed run finalized/returned on start).
    ``published_commit`` is the new curated sha iff ``published``. ``counts`` carries per-op
    disposition tallies (and recovery actions) for the dashboard / ``log.md``.
    """

    run_id: str
    status: Literal["published", "failed", "noop", "recovered"]
    published_commit: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    """Non-fatal operator-facing diagnostics for a run that STILL published (issue #115).

    A PASS-2 that authors nothing publishes structurally-valid notes with placeholder bodies by
    design (§4.2 degrade) — but reporting that as an unqualified success is how a Linux operator
    accumulated an entire KB of empty bodies without a single error line. The faces print these;
    they never change the terminal ``status``.

    Since #96 a ``failed`` report may carry these too — but only for the failure of an
    OBSERVABILITY write (:func:`_fail`'s ``state.json`` save), which must never turn a FAILED run
    into a CRASHED one. The invariant is unchanged: an entry here is always survivable."""
    failure: RunFailure | None = None
    """The fatal cause of a ``failed`` run — ``None`` for published/noop/recovered (#96).

    ``status`` says a run failed; this says WHY and WHERE the durable record is. Set ONLY by
    :func:`_fail`, so every ``status == "failed"`` report carries one and no other status does."""


def run(
    repo: Repo,
    *,
    backend: Backend,
    state_store: StateStore,
    now: datetime,
    taxonomy: Taxonomy,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    related_k: int = DEFAULT_RELATED_K,
    max_candidates: int = DEFAULT_MAX_CANDIDATES_PER_RUN,
    max_orphans: int | None = None,
) -> RunReport:
    """Execute ONE transactional curator run (ADR-0008 steps 1-6 / DESIGN §4 / ADR-0011 §0).

    Deterministic orchestration around the two delegated acts. ``now`` is the injected commit
    timestamp (the run reads no wall clock for the commit/dates — ADR-0010 D1); ``taxonomy``
    supplies
    the FIXED ``allowed_tags``/``domains`` the §4.1 / §4.4 gates enforce. ``max_attempts`` is the
    §5.1 per-event retry budget (``repo.yaml curator.max_attempts``, default
    :data:`~agora_kb.curator.constants.DEFAULT_MAX_ATTEMPTS`); keyword-only + additive so the frozen
    signature is preserved while an operator-configured budget is honored end-to-end.
    ``max_candidates`` is the §1.3 per-run candidate cap the FIFO claim enforces
    (``repo.yaml curator.limits.max_candidates_per_run``, default
    :data:`~agora_kb.curator.constants.DEFAULT_MAX_CANDIDATES_PER_RUN`; ADR-0024 OD-3a / #60) —
    the un-claimed remainder simply stays in the inbox for the next trigger.

    Returns a :class:`RunReport`. A held lock or empty/all-deduped inbox is a ``noop``; a PLAN,
    LINT,
    or CAS failure is a ``failed`` run that publishes NOTHING and discards the entire worktree diff
    (events return to ``inbox/`` within the §5.1 retry budget, else move to ``failed/``); a clean
    run
    is ``published`` with the new curated commit.
    """
    layout = repo.layout

    try:
        with curator_lock(layout):
            return _run_locked(
                repo,
                backend=backend,
                state_store=state_store,
                now=now,
                taxonomy=taxonomy,
                max_attempts=max_attempts,
                related_k=related_k,
                max_candidates=max_candidates,
                max_orphans=max_orphans,
            )
    except LockHeld:
        # A run is already in progress for this repo (ADR-0008 step 1 / DESIGN §4 step 1): exit
        # rather than block, preserving the single-writer invariant (ADR-0002).
        return RunReport(run_id="", status="noop", counts={"reason_lock_held": 1})


def _run_locked(
    repo: Repo,
    *,
    backend: Backend,
    state_store: StateStore,
    now: datetime,
    taxonomy: Taxonomy,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    related_k: int = DEFAULT_RELATED_K,
    max_candidates: int = DEFAULT_MAX_CANDIDATES_PER_RUN,
    max_orphans: int | None = None,
) -> RunReport:
    """The body of :func:`run`, executed UNDER the held curator lock (ADR-0011 §0)."""
    layout = repo.layout
    state = state_store.load()

    # ADR-0008 (read-after-publish, best-effort): a PRIOR run's post-publish sync may have failed
    # (owner working tree dirty/diverged), leaving HEAD behind the curated ref. Try a best-effort
    # fast-forward here so a transient dirty tree that has since been cleaned is reconciled before
    # we read the base. GUARDED by the same GitError swallow as the post-publish sync — a
    # still-dirty tree never aborts the run; we read the AUTHORITATIVE base from the ref below.
    _sync_owner_working_copy(repo, run_id="<pre-run>")

    # The CAS base is the curated branch ref (the authoritative published tip), NOT the owner's
    # working-copy HEAD: a stale HEAD left behind by a failed post-publish sync would otherwise
    # poison this run (plan against missing content + a CAS expected=stale that can never land —
    # a durable livelock). Reading the ref means a prior failed sync degrades the read path only,
    # never the next run's ability to make progress (ADR-0008 note: the read-after-publish sync is
    # best-effort and the CAS base is the curated ref, not the owner HEAD).
    base_commit = repo.branch_commit()
    run_id = _new_run_id(now)
    run_date = run_id[:10]

    # ADR-0008 step 1 / §0: atomic FIFO claim + tier-1 dedup (authoritative under the lock), capped
    # at max_candidates distinct tier-2 content groups (§1.3, ADR-0024 OD-3a / #60 — the remainder
    # stays in the inbox for the next trigger). A None result ⇒ the inbox was empty or every event
    # collapsed away — nothing to consolidate.
    manifest = claim(
        layout,
        base_commit=base_commit,
        run_id=run_id,
        started=_iso(now),
        state=state,
        max_candidates=max_candidates,
    )
    if manifest is None:
        return RunReport(run_id=run_id, status="noop", counts={"claimed": 0})

    # #96: this run has CLAIMED work, so the curator has ATTEMPTED a consolidation — record it NOW,
    # before the first thing that can fail. ONE stamp for all outcomes (publish, _fail, and an
    # UNCAUGHT crash such as the strict parse_all_notes below) rather than one per terminus, so
    # `last_attempt` can never be older than `last_run` and a crash-shaped run is still honest.
    # `state` is byte-equal to disk here (loaded above; `claim` is read-only w.r.t. it), and the
    # publish path re-loads before its own save, so this early save is neither premature nor
    # clobbered. NOT stamped on a noop — an idle `agora watch` tick must not rewrite state.json
    # 1440x/day, and "last time the curator polled" is not the fact an operator needs.
    #
    # DELIBERATELY UNGUARDED: `claim()` has already moved events into `processing/` and written a
    # manifest immediately before this, so the filesystem is proven writable at this instant; an
    # OSError here is the same class as one from `write_manifest`, which already propagates today.
    state.last_attempt = now
    state_store.save(state)

    # ADR-0011 §1 / §5 tier-2: build the read-only bundle (candidates + provenance + related/).
    #
    # `mergeable_paths` (#152): PASS 1 is instructed to pick its MERGE_INTO_THEME target out of the
    # §1.1 `related/` hits, but since #152 a human-written note is NOT a legal target — naming one
    # is a BASENAME plan error, and any plan error fails the WHOLE run. An unstamped note that
    # lexically overlaps a candidate would otherwise sit at the top of that view on every run, so
    # the engine would be failing runs over a menu it handed the model itself. Classified over the
    # LIVE tree, the same files `build_bundle`'s `Wiki` reads (the worktree scan below is at
    # `base_commit` and answers a different question: what this run is graded on).
    #
    # The set is the curator's own THEMES, not merely its own notes: the plan gate accepts a
    # MERGE/CONTEST target ONLY out of `theme_basenames` (plan.py checks 5 and 8), so a curator
    # daily, MOC or `index.md` in the view is just as run-killing a pick as a human note. This
    # mirrors the `theme_basenames` derivation below EXACTLY — same `_is_theme_note` over the same
    # stamped-paths set — so the menu and the gate cannot drift apart. kb_schema.md §7.1 states
    # this property to the model; keep the three in step.
    live_now = scan_live_tree(layout)
    bundle = build_bundle(
        layout,
        repo,
        manifest,
        related_k=related_k,
        mergeable_paths={
            n.rel_path
            for n in live_now.notes
            if _is_theme_note(n) and n.rel_path in live_now.curator_paths
        },
    )

    # #60 batch observability: the run's claim/bundle shape + the queue depth left after the claim
    # (one cheap dir count — the un-claimed FIFO remainder the next trigger will pick up). Captured
    # here while the bundle is in scope; logged now, folded into the RunReport counts + state.json
    # last_batch at finalize (the dashboard/metrics read those).
    claimed_n = len(manifest.event_ids)
    candidates_n = len(bundle.candidates)
    inbox_remaining = Inbox(layout).depth()
    _logger.info(
        "run %s: claimed %d event(s) -> %d candidate(s) (cap %d); %d event(s) left in inbox",
        run_id,
        claimed_n,
        candidates_n,
        max_candidates,
        inbox_remaining,
    )

    # ADR-0008 step 2: a detached worktree at base_commit is the ONLY writable mount; the model
    # never
    # touches the live tree or _kb/. live_basenames (the AUTHORITATIVE §1.2 registry) is read from
    # the committed worktree, so the §4.1 BASENAME/LINK checks grade against the real tree.
    with repo.worktree(at=base_commit) as wt:
        wt_layout = RepoLayout(wt)
        # Parse the live tree ONCE, CLASSIFY it (#152), and derive BOTH registries: the
        # all-basenames set (CREATE uniqueness + LINK resolvability grade against this) and the
        # THEME-only subset (MERGE/CONTEST targets grade against this, mirroring
        # apply._resolve_target_path theme_only).
        #
        # This used to be a strict parse_all_notes whose FrontmatterError failed the run. That was
        # already an improvement on the traceback it replaced (a claimed batch stranded in
        # `_kb/processing/` while `agora status` printed `failed_events: 0`), but it still had the
        # wrong subject: opening the repo in Obsidian and saving ONE fenceless note failed EVERY
        # subsequent run, forever, because the offending file sits in the base commit.
        # `scan_live_tree` keeps the strictness where it is an integrity signal — a malformed note
        # that still carries the curator's own stamp — and downgrades a human draft to a warning
        # (#152, ADR-0014 D1/D4).
        live_tree = scan_live_tree(wt_layout)
        if live_tree.malformed_curator is not None:
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=[
                    f"LIVE-TREE: unparseable note in the curated tree — "
                    f"{live_tree.malformed_curator}"
                ],
                max_attempts=max_attempts,
            )
        notes = list(live_tree.notes)
        # DELIBERATELY over ALL notes, human ones included. A human note is not a producer artifact,
        # but its basename is still TAKEN: dropping it here would let a CREATE_THEME reuse that
        # basename, and APPLY writes `wiki/<domain>/themes/<basename>.md` unconditionally — an
        # Obsidian note saved in a `themes/` folder would be silently overwritten. The registry that
        # must exclude human notes is the MERGE/CONTEST target set below, and it does.
        live_basenames = {n.basename for n in notes}
        theme_basenames = {
            n.basename for n in notes if _is_theme_note(n) and n.rel_path in live_tree.curator_paths
        }
        # Carried to the report (seeded into `prose_warnings` at PASS 2) so a published run says out
        # loud which notes it did not grade — the operator surface `agora doctor` mirrors.
        live_tree_warnings: list[str] = (
            [_human_notes_warning(live_tree.human_paths)] if live_tree.human_paths else []
        )

        # ADR-0011 §0 / §4.3: append `_agora_scratch/` to the WORKTREE's own .gitignore BEFORE
        # invoking the backend, so the backend's PASS-1 plan.json + any model scratch are writable
        # (the worktree is the only writable mount, ADR-0008) yet can NEVER appear in the curated
        # diff (`git add -A` skips it). The final-diff gate (below) additionally asserts it produced
        # zero tracked changes.
        _ignore_scratch(wt)

        # PASS 1 — DELEGATED: the backend reads the bundle and returns plan.json text. A bad-plan
        # TEXT is a PlanParseError; a backend that cannot run at all (missing/non-zero executable →
        # BackendUnavailableError, or a hung backend → subprocess.TimeoutExpired) is the OTHER
        # failure channel of the real SubprocessBackend seam. BOTH map to a deterministic FAILED run
        # here (lock released, worktree torn down, NOTHING published) so the "model outside the
        # integrity boundary → clean FAILED run" contract holds end-to-end — never an uncaught
        # traceback out of run() (ADR-0011 §4 / FOCUS/INGEST contract).
        try:
            plan = Plan.from_json(backend.plan(bundle.bundle_dir))
        except PlanParseError as exc:
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=[f"PLAN-PARSE: {exc}"],
                max_attempts=max_attempts,
            )
        except (BackendUnavailableError, subprocess.TimeoutExpired) as exc:
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=[f"PLAN-BACKEND: {exc}"],
                max_attempts=max_attempts,
            )

        # §4.1 PLAN validation (pure deterministic, model-independent).
        plan_errors = validate_plan(
            plan,
            manifest_event_ids=set(manifest.event_ids),
            allowed_tags=set(taxonomy.allowed_tags),
            domains=set(taxonomy.domains),
            live_basenames=live_basenames,
            theme_basenames=theme_basenames,
            gated_candidate_ids=bundle.gated_candidate_ids,
        )
        if plan_errors:
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=[f"{e.check}: {e.message}" for e in plan_errors],
                max_attempts=max_attempts,
            )

        # APPLY (deterministic, §3): the worker materializes ALL
        # structure/frontmatter/sources/links/
        # MOC/index/sentinels. confidence is MIRRORED from the candidate worst-case (never a plan
        # field) so the backend can never inflate it.
        confidence = {c.candidate_id: c.confidence for c in bundle.candidates if c.confidence}
        # raw_writes is the EXACT {raw_ref: bytes} set the engine materialized this run (ADR-0010
        # D3). The final-diff gate admits ONLY these exact paths-with-content under raw/; any other
        # raw/ change (a brain-planted file, or a PASS-2 overwrite of an engine-written source) is
        # then rejected off-allowlist, so the brain still never writes raw/.
        # A containment failure (`_contained` / `_resolve_target_path` raising ApplyError) is a
        # tamper signal, not a crash: it must produce the same clean FAILED run + error.json every
        # other integrity rejection does (ADR-0011 §4 "never an uncaught traceback out of run()"),
        # not an escaping traceback. This is latent while plan.py's ASCII PATH/ALLOWLIST regex
        # keeps every escaping token from ever reaching APPLY — pinned here regardless, before
        # that charset is ever widened, so a later widening cannot be blamed for this failure mode.
        try:
            raw_writes = apply_plan(
                plan,
                worktree=wt,
                run_date=run_date,
                provenance=bundle.provenance,
                confidence=confidence,
            )
        except ApplyError as exc:
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=[f"PATH/ALLOWLIST: {exc}"],
                max_attempts=max_attempts,
            )
        except FrontmatterError as exc:
            # DEFENCE IN DEPTH for the same contract (§4 "never an uncaught traceback out of
            # run()"). APPLY re-opens existing notes with an UNGUARDED `frontmatter.parse` in four
            # places — `_update_index`, `_update_moc`, `_apply_append_daily`, and the
            # `_apply_merge`/`_apply_contested` targets. `scan_live_tree`'s structural + stamp
            # classification is what SHOULD keep a malformed note from ever reaching them, and the
            # plan gate covers the theme targets; this catch is the belt to that pair of braces, so
            # no live-tree note can ever escape `run()` as a traceback that strands the claimed
            # batch in `_kb/processing/` with `agora status` reporting `failed_events: 0`.
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=[f"APPLY-PARSE: unparseable note reached APPLY — {exc}"],
                max_attempts=max_attempts,
            )

        # The post-APPLY tree is structurally complete with empty body regions (prose_complete still
        # false). Persist phase=applied BEFORE PASS 2 so a crash recovers at the right §9 entry.
        # BY DESIGN this `applied` manifest carries NO candidate commit sha (none exists yet) and
        # prose_complete=False, so recover() takes the conservative re-run-from-PASS-1 path (§9
        # rows applied|*|no): the dropped worktree took plan.json with it, so re-PLAN is the safe
        # default. Do NOT "optimize" this into a partial PASS-2 resume — that would risk a partial
        # publish. The candidate sha is recorded in a SECOND advance just before the CAS (below).
        # REBOUND (#96): _advance returns the advanced COPY (RunManifest is frozen), and discarding
        # it pinned the in-memory ``manifest.phase`` at the constant ``claimed`` for every
        # downstream reader — including _fail's ``error.json`` ``"phase"`` and RunFailure.phase, so
        # a LINT/AUTHOR/CAS failure has been mis-reporting ``claimed`` since that field was written.
        # Rebinding makes the phase truthful: ``claimed`` = failed BEFORE apply, ``applied`` =
        # failed after. Nothing else reads the rebound value (run_id/base_commit/event_ids are
        # preserved by model_copy), and the on-disk manifest is byte-identical either way.
        manifest = _advance(layout, manifest, phase="applied", prose_complete=False)

        # §4.2 SCOPE BASELINE: snapshot the post-APPLY tree so the AUTHOR diff below is graded on
        # what PASS 2 ACTUALLY touched, not merely on the notes it was ASKED to author. Deriving
        # the changed set from `needs_prose` alone made that set a SUBSET of `sentinels`, which
        # rendered validate_author_diff's "path not in sentinels" rejection unreachable in
        # production: a PASS-2 write to any OTHER `wiki/` note passed the §4.2 gate untested and
        # then passed §4.0 too, because the final-diff allowlist admits the whole `wiki/` prefix
        # (constants.ALLOWLIST_DIR_PREFIXES). Reproduced end-to-end: an adversarial backend rewrote
        # an unrelated theme's body AND flipped its frontmatter `status`, and the run PUBLISHED
        # with `failure=None`; deleting that note while scrubbing its MOC references published too,
        # losing the note. `_agora_scratch/` is already excluded per-worktree (above), so backend
        # scratch can never enter this baseline.
        _git(wt, "add", "-A")
        applied_tree = _git(wt, "write-tree").stdout.strip()

        # PASS 2 — DELEGATED: author prose between the candidate-id sentinels, then validate the
        # diff. The §8.2 context grounds each region in its candidate's verbatim source + op; it is
        # advisory backend input only — base_state/changed/sentinels (the §4.2 gate) are unchanged.
        candidate_texts = {c.candidate_id: c.text for c in bundle.candidates}
        needs_prose, sentinels, context = _needs_prose_map(plan, wt, run_date, candidate_texts)
        prose_complete = True
        # Seeded, not appended later: an APPEND_DAILY that never flagged needs_prose contributes NO
        # region, so a plan made only of those has an empty `needs_prose` map and skips the whole
        # block below — the one shape that most needs the diagnostic (#131).
        # `live_tree_warnings` FIRST: a note the run refused to grade is context for everything
        # below it, and it is the one warning a run with no prose at all still has to carry (#152).
        prose_warnings: list[str] = [*live_tree_warnings, *_plan_shape_warnings(plan)]
        prose_counts: dict[str, int] = {}
        if needs_prose:
            base_state = {rel: (wt / rel).read_text(encoding="utf-8") for rel in needs_prose}
            # A fatal backend-invocation failure on PASS 2 (missing executable / hung process) is
            # the same "model cannot run" channel as PASS 1: map it to a clean FAILED run rather
            # than let it escape run() as a traceback. A per-note NON-ZERO exit is NOT fatal — the
            # SubprocessBackend leaves it for the §4.2 AUTHOR-diff degrade path below.
            try:
                # A backend MAY report per-region failures (a non-zero exit, an unreachable model).
                # Advisory only: the §4.2 diff below decides what actually landed. They exist so the
                # operator sees the backend's own stderr next to a prose-pending result (#115).
                author_failures = backend.author(wt, needs_prose, context) or []
            except (BackendUnavailableError, subprocess.TimeoutExpired) as exc:
                return _fail(
                    layout,
                    manifest,
                    state_store,
                    now=now,
                    reasons=[f"AUTHOR-BACKEND: {exc}"],
                    max_attempts=max_attempts,
                )
            # §4.6 deterministic stray-wikilink strip BEFORE validation: a `[[X]]` PASS 2 emitted
            # that is not a plan link for that note has its delimiters removed (inner text kept), so
            # an otherwise-good prose pass is repaired byte-deterministically instead of degrading
            # the WHOLE note to the placeholder over one stray link. Links are structure (APPLY-
            # owned); the strip is iterated to a fixed point in strip_stray_wikilinks.
            _strip_stray_links(wt, needs_prose, plan, live_basenames, run_date)
            new_state = _read_notes(wt, needs_prose)
            # The REAL PASS-2 diff: every tracked add/modify/delete against the post-APPLY
            # baseline, so a write outside `needs_prose` reaches check 1 instead of being invisible.
            _git(wt, "add", "-A")
            pass2_diff = _parse_name_status_z(
                _git_bytes(wt, "diff", "--cached", "--name-status", "-z", applied_tree).stdout
            )
            changed = sorted({rel for _status, rel, _old in pass2_diff})
            # The validator reads BOTH sides for `log.md` (and for any needs_prose note); supply
            # every changed path so an out-of-scope edit is graded on real bytes, never on a
            # defaulted empty string that would silently compare equal.
            per_file_old = dict(base_state)
            per_file_new = dict(new_state)
            for rel in changed:
                if rel not in per_file_old:
                    per_file_old[rel] = _blob_text_at(wt, applied_tree, rel)
                if rel not in per_file_new:
                    per_file_new[rel] = _worktree_text(wt / rel)
            author_errors = validate_author_diff(
                changed_paths=changed,
                per_file_old=per_file_old,
                per_file_new=per_file_new,
                sentinels=sentinels,
            )
            if author_errors:
                # §4.2 degrade-or-fail: RESTORE every needs_prose note to its post-APPLY base state
                # (structurally valid, body already at the placeholder + body_status: pending),
                # discarding ALL of PASS 2's edits — including any out-of-region/frontmatter tamper.
                # Structure is APPLY-owned and intact, so the run still PUBLISHES a prose-pending
                # note rather than failing (only the prose pass is lost).
                _degrade_prose(wt, needs_prose, base_state)
                # _degrade_prose walks `needs_prose` ONLY, so a path outside it stays tampered and
                # would publish inside an otherwise-"rejected" run. Restore those from the same
                # post-APPLY baseline the diff was graded against — same all-or-nothing semantics.
                _restore_out_of_scope(
                    wt, applied_tree, [r for r in changed if r not in needs_prose]
                )
                prose_complete = False
                new_state = _read_notes(wt, needs_prose)
                prose_warnings.append(
                    f"AUTHOR-DIFF rejected: {len(author_errors)} violation(s); every prose region "
                    f"was reset to the placeholder — {author_errors[0]}"
                )

            # §4.2 gap-closure (#115): grade PASS 2 per REGION against the worktree. An EMPTY diff
            # is not evidence of success — it is exactly what a backend that could not start
            # produces, and it used to sail through the validator (no changed paths ⇒ no errors)
            # leaving prose_complete=True on a KB full of placeholders. Runs AFTER the degrade
            # above so a rejected pass is counted from its post-reset state, not double-counted.
            unauthored = _unauthored_regions(needs_prose, base_state, new_state)
            prose_counts = {
                "prose_regions": sum(len(sids) for sids in needs_prose.values()),
                "prose_pending": len(unauthored),
            }
            if unauthored:
                prose_complete = False
                pending_notes = sorted({rel for rel, _ in unauthored})
                prose_warnings.append(
                    f"PROSE PENDING: {len(unauthored)} of {prose_counts['prose_regions']} body "
                    f"region(s) were left unauthored; published with placeholder bodies in "
                    f"{', '.join(pending_notes)}"
                )
                prose_warnings.extend(author_failures)

            # #119: body_status: pending is APPLY's promise that a region is still empty — nothing
            # ever retracted it, so every published note carried a stale flag and the signal was
            # worthless to every reader (schema doc, dashboard, gold, agents). Retract it HERE:
            # AFTER _degrade_prose (a §4.2-rejected pass must KEEP its flag — that ordering is
            # belt-and-braces rather than load-bearing, since _degrade_prose re-stamps the flag
            # unconditionally, but it also avoids a pointless write) and BEFORE the §4.4 lint,
            # which reads the worktree from DISK — THAT ordering IS load-bearing and is locked by
            # test_the_clear_runs_before_the_lint_that_grades_it. PASS 2 cannot do this itself —
            # validate_author_diff requires frontmatter byte-identity and the model is outside the
            # integrity boundary (ADR-0011 §4). The predicate is WHOLE-NOTE, never this run's region
            # ids: a region an EARLIER run left at the placeholder keeps the flag alive even when
            # every region THIS run asked for landed. Consequence, deliberate and not a bug: a run
            # can report prose_pending: 0 while a note it touched still says body_status: pending.
            # prose_pending grades THIS run's PASS-2 (#115); body_status describes THE NOTE
            # (ADR-0010 §2.6). Do not "fix" the divergence.
            _clear_body_status(wt, needs_prose)
            # new_state is now STALE for any cleared note (its last reader was the grading above).
            # Re-read from disk rather than trusting it if you add a reader below.

        # §4.4 deterministic LINT — the SAME gate the dashboard runs, over the SAME worktree, with
        # every rule and severity unchanged. What #152 narrows is the SUBJECT: the gate grades what
        # the CURATOR PRODUCED, which is (a) every path THIS run added or modified — the run's own
        # diff against base_commit, the same name-status view the final-diff gate reads — plus (b)
        # every note carrying the curator's stamp from an earlier run. A note in neither set was
        # written by a human; it is read (it still resolves links and feeds the orphan derivation)
        # but never graded, so an Obsidian save can no longer hard-reject the run in perpetuity.
        # A DELETED path is excluded: there is nothing left on disk to lint.
        _git(wt, "add", "-A")
        run_diff = _parse_name_status_z(
            _git_bytes(wt, "diff", "--cached", "--name-status", "-z", base_commit).stdout
        )
        touched_this_run = {rel for status, rel, _old in run_diff if not status.startswith("D")}
        producer_scope = set(live_tree.curator_paths) | touched_this_run
        lint_result = lint(
            wt_layout,
            taxonomy=taxonomy,
            run_date=run_date,
            run_id=run_id,
            max_orphans=max_orphans,
            scope=producer_scope,
        )
        if not lint_result.ok:
            # ERRORS ONLY, matching the gate's own predicate (``LintResult.ok`` is False iff an
            # error-severity finding exists): a warning is by definition not a failed check. This
            # is load-bearing since #119 added L2-6, the first UNBOUNDED per-note warning — on a
            # repo published by a pre-#119 build every legacy note emits one, and `findings` is
            # sorted by (path, code), so passing warnings through here would bury the one line
            # that says why the run failed. Both operator surfaces truncate: ``summary(limit=3)``
            # is what `agora curate` / the `agora watch` tick print, and ``LastFailure`` keeps
            # only ``MAX_FAILURE_REASONS``. The lossless record stays in _kb/failed/**/error.json.
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=[
                    f"LINT {f.code} {f.path}: {f.message}"
                    for f in lint_result.findings
                    if f.severity == "error"
                ],
                max_attempts=max_attempts,
            )

        # §4.3 LOG (worker-only, AFTER validation): append ONE structured log.md entry. The model
        # never wrote log.md (asserted by the §4.2 diff scope above + the final-diff gate below).
        counts = _disposition_counts(plan)
        _append_log(wt, run_id=run_id, base_commit=base_commit, counts=counts, plan=plan)

        # ADR-0017 §7: compute the per-connector harvest cursor accepted/rejected deltas WHILE the
        # plan + the bundle provenance are both in scope (the bundle does not survive the worktree
        # block — only new_commit does). PURE + model-free; carried out and applied in the
        # happy-path finalize block below, mirroring _bump_counters. Loading the connectors here (no
        # adapters.yaml ⇒ empty) keeps the apply step a trivial cursor write.
        harvest_deltas = compute_harvest_cursor_deltas(plan, bundle, _load_connector_specs(layout))

        # §4.0/§4.3/§4.5 FINAL-DIFF ALLOWLIST GATE (ADR-0008 step 4): the deterministic integrity
        # boundary. Stage everything and assert the working tree touches ONLY canonical-ALLOWLIST
        # paths — no off-allowlist file the backend physically wrote (a new file the §4.2 scope
        # never surfaced, a planted symlink, an edit to _meta/_templates/raw/git internals), no
        # introduced/modified symlink or `..` escape, and ZERO tracked changes under
        # `_agora_scratch/`. A violation discards the whole worktree (no CAS) and fails the run.
        allowlist_errors = _assert_final_diff_allowlisted(
            wt, base_commit=base_commit, raw_writes=raw_writes
        )
        if allowlist_errors:
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=allowlist_errors,
                max_attempts=max_attempts,
            )

        # Commit once (advances only the detached worktree HEAD; the branch ref is unchanged until
        # the CAS below). `git add -A` is now safe — the final-diff gate already rejected anything
        # out of the allowlist.
        new_commit = repo.commit_worktree(wt, _commit_message(run_id, counts), when=now)

        # Persist the candidate commit sha DURABLY (phase=applied) BEFORE the CAS, so a crash in the
        # CAS-success window (ref advanced, state not yet saved) is recoverable: recover() sees the
        # recorded sha, checks repo.is_published, and FINALIZES instead of re-running PASS 1 and
        # double-publishing (ADR-0011 §9 "git ref already advanced" row). commit_worktree only moved
        # the detached HEAD, so recording the sha pre-CAS is safe even if the CAS then loses.
        # REBOUND for the same #96 reason as the first advance above: a CAS conflict must report
        # ``phase=applied``, not the claim-time constant.
        manifest = _advance(
            layout,
            manifest,
            phase="applied",
            prose_complete=prose_complete,
            published_commit=new_commit,
        )

        # compare-and-swap the curated ref base→new — the SINGLE durable publish point (§4.3).
        # A lost CAS means a concurrent writer advanced the branch: discard + retry NEXT run (a CAS
        # conflict never burns the §5.1 retry budget — the events are valid, just stale).
        if not repo.compare_and_swap_branch(expected=base_commit, new=new_commit):
            return _fail(
                layout,
                manifest,
                state_store,
                now=now,
                reasons=["CAS: curated ref moved since base_commit; conflict, discard + retry"],
                cas_conflict=True,
                max_attempts=max_attempts,
            )

    # --- published: the diff is durable in git. Record state + finalize (ADR-0008 step 5, §4.3).
    # ---
    _advance(
        layout,
        manifest,
        phase="published",
        prose_complete=prose_complete,
        published_commit=new_commit,
    )

    state = state_store.load()
    state.record_published_run(run_id, new_commit)
    state.mark_run(now, new_commit)
    # #60 / ADR-0024 §3: persist the run's batch shape so the dashboard/metrics can surface
    # batch-size-vs-cap pressure without re-reading run manifests (claimed==candidates==cap with a
    # big inbox_remaining ⇒ the backlog is draining in capped slices).
    state.last_batch = LastBatch(
        claimed=claimed_n,
        candidates=candidates_n,
        cap=max_candidates,
        inbox_remaining=inbox_remaining,
    )
    _bump_counters(state, counts)
    # ADR-0017 §7: bump the per-connector harvest cursors from THIS run's harvested-candidate
    # dispositions. HAPPY-PATH ONLY — mirrors _bump_counters exactly and is NEVER replayed in
    # _finalize_recovered, so it is exactly-once (or under-count + rebuildable on a rare crash)
    # without an is_published guard. The cursor write lands in git-ignored _kb/, OUTSIDE the CAS /
    # curated tree, so the integrity boundary is unchanged; it is best-effort + rebuildable, not
    # transactional with the publish. CRITICAL: unlike _bump_counters (pure in-memory mutation),
    # this does disk IO (CursorStore.load/save → atomic_write_text) which CAN raise OSError (ENOSPC,
    # EACCES, read-only FS). The run is ALREADY published-in-git at this point, so a cursor IO error
    # MUST NOT propagate and abort finalize — that would lose the published_runs entry, leave events
    # in processing/, and surface a traceback on a successful publish. Degrade to under-count
    # (rebuildable) by swallowing + logging, truly mirroring _bump_counters' inability to perturb
    # the publish.
    try:
        _apply_harvest_cursor_deltas(layout, harvest_deltas)
    except Exception as exc:  # noqa: BLE001 — best-effort; cursor is derived + rebuildable.
        _logger.warning(
            "harvest cursor update failed (rebuildable, run %s already published): %s",
            run_id,
            exc,
        )
    # Move events to processed/ BEFORE recording tier-1 event_keys: _record_event_keys reads each
    # event's frontmatter from processed/<date>/, so the move must happen first or every key is
    # silently skipped (cross-run delivery idempotency would be lost, ADR-0011 §5 tier-1).
    _move_events_to_processed(layout, manifest, run_date)
    _record_event_keys(state, layout, manifest)
    state_store.save(state)

    _advance(
        layout,
        manifest,
        phase="finalized",
        prose_complete=prose_complete,
        published_commit=new_commit,
    )

    # ADR-0008 (readers resolve a PUBLISHED commit): the CAS moved only the curated ref; the
    # repo-owner's MAIN working copy is still parked at base_commit, so core.Wiki/kb_query (which
    # read the on-disk tree) would not yet see the published theme. Fast-forward the working copy to
    # the new tip AFTER state is saved + the manifest is finalized, so the read-after-publish
    # contract holds. GUARDED: a sync failure (e.g. an owner left the working tree dirty) must NOT
    # undo a durable publish — the diff is already in git and state is finalized; we keep
    # status=published and only log the sync failure.
    synced = _sync_owner_working_copy(repo, run_id)
    if not synced:
        # Surface the stuck state as a LOUD, observable RunReport signal (not just a log line): the
        # publish is durable in git, but HEAD now diverges from the curated tip so the read path is
        # stale until the owner reconciles the working tree. The next run still reads the
        # authoritative base from the curated ref and keeps making progress, so this is purely an
        # operator/dashboard signal that the working copy needs manual attention.
        counts = {**counts, "owner_working_copy_unsynced": 1}

    # ADR-0012 §2 / #26: refresh the derived reader cache best-effort AFTER the working copy is
    # synced to the new curated tip (so the parsed on-disk tree == new_commit and the stamped commit
    # matches what the read path rglobs). Mirrors the ADR-0017 §7 cursor posture: the run is already
    # published, so a rebuild failure only DEGRADES the read path (silent full-scan fallback) and is
    # surfaced, never aborting finalize. When UNSYNCED we skip the build entirely (building from the
    # un-advanced base_commit tree would stamp new_commit onto stale content) and mark it unbuilt;
    # `and` short-circuits so rebuild_index_cache is not called in that case.
    if not synced or not rebuild_index_cache(repo):
        counts = {**counts, "index_cache_unbuilt": 1}

    # ADR-0027 §2a / issue #37: refresh the derived gold pack(s) best-effort AFTER the working copy
    # is synced to the new curated tip — the SAME posture as the index-cache rebuild above and the
    # ADR-0017 §7 harvest-cursor bump: the run is already published, so a build/IO failure only
    # DEGRADES the injection surface (a stale/absent pack) and is surfaced, never aborting finalize.
    # Skipped when UNSYNCED (building from the un-advanced base tree would stamp new_commit onto
    # stale content); `and` short-circuits so rebuild_gold_packs is not called in that case. This is
    # the freshness half of decision 6: a pack is never staler than silver.
    if not synced or not rebuild_gold_packs(repo, now=now):
        counts = {**counts, "gold_unbuilt": 1}

    # #60 batch observability on the report surface (CLI `counts:` line / MCP kb_curate): the
    # run's claim/bundle shape + the post-claim queue depth. Added HERE — after _bump_counters /
    # _append_log / _commit_message consumed the pure disposition tallies — so log.md and the
    # commit message stay byte-identical.
    counts = {
        **counts,
        "claimed": claimed_n,
        "candidates": candidates_n,
        "inbox_remaining": inbox_remaining,
        # #115 prose observability. Emitted HERE (with the other post-log report-only keys) so
        # log.md, the commit subject and state.counters stay byte-identical, and only when the run
        # had prose to author — a run with no needs_prose keeps its small report shape.
        **prose_counts,
        # #152: how many notes the run READ but did not GRADE (no curator stamp). Emitted ONLY when
        # there are any, so a purely curated repo's report shape is byte-unchanged. `agora doctor`
        # and the dashboard's health() derive the same number from the tree, outside a run.
        **({"unmanaged_notes": len(live_tree.human_paths)} if live_tree.human_paths else {}),
    }

    return RunReport(
        run_id=run_id,
        status="published",
        published_commit=new_commit,
        counts=counts,
        warnings=prose_warnings,
    )


# --- recovery (ADR-0011 §9 truth-table) --------------------------------------------------------


def recover(repo: Repo, *, state_store: StateStore) -> list[RunReport]:
    """Resolve every in-flight ``processing/<run-id>/`` run on start (ADR-0011 §9 / ADR-0008 §6).

    Deterministic, no backend call. For each manifest (chronological order) the action is a pure
    function of ``(phase, prose_complete, published_runs[run_id]?, git ref)``:

    * ``published`` (or ``state.published_runs`` has it, or the curated ref already points at the
      run's commit) ⇒ FINALIZE: ensure ``published_runs``/``state`` record the commit, move events
      to
      ``processed/``, set ``phase=finalized``. NO backend, NO re-commit (the CAS already landed).
    * ``claimed`` / ``applied`` with ``prose_complete=false`` ⇒ the run never published: return
      unchanged events to ``inbox/`` to re-run from PASS 1 next time, and clear the processing dir
      (the dropped worktree took ``plan.json`` with it — re-PLAN is the safe default).
    * ``finalized`` ⇒ nothing to do; clean up the (already empty) processing dir.

    Returns one :class:`RunReport` per processing run, each ``recovered``.
    """
    reports: list[RunReport] = []
    state = state_store.load()
    for manifest in list_processing(repo.layout):
        run_id = manifest.run_id
        # A run is "published" iff its commit is durable in git. The §9 "git ref already advanced"
        # row is the load-bearing one: the worker records `published_commit` at phase=applied
        # BEFORE the CAS, so a crash in the CAS-success window (ref advanced, manifest still
        # 'applied', state not yet saved) is detected here via repo.is_published — NOT re-run from
        # PASS 1 (which would double-publish). repo.is_published needs a recorded sha, so we keep
        # the None-guard; what makes this fire is the pre-CAS sha persistence, not a missing guard.
        published = (
            manifest.phase in ("published", "finalized")
            or state.is_published(run_id)
            or (
                manifest.published_commit is not None
                and repo.is_published(manifest.published_commit)
            )
        )
        if published:
            commit = (
                manifest.published_commit or state.published_commit(run_id) or repo.branch_commit()
            )
            reports.append(_finalize_recovered(repo, state_store, manifest, commit))
        else:
            reports.append(_return_to_inbox(repo, manifest))
        state = state_store.load()
    return reports


def _finalize_recovered(
    repo: Repo, state_store: StateStore, manifest: RunManifest, commit: str
) -> RunReport:
    """Finalize a published-but-unfinalized run WITHOUT any backend call (ADR-0008 step 6)."""
    run_id = manifest.run_id
    run_date = run_id[:10]
    layout = repo.layout

    state = state_store.load()
    if not state.is_published(run_id):
        state.record_published_run(run_id, commit)
        # #60: the happy-path finalize records published_runs AND last_batch in ONE atomic state
        # save, so an unrecorded run means that save never landed — whatever last_batch holds
        # describes an OLDER published run. Its true shape is unknowable post-crash (candidates/
        # cap went down with the crashed process), so CLEAR it rather than let kb_status /
        # /metrics label the previous run's shape as this run's; the gauges then omit, exactly
        # like never-run. Same best-effort posture as the un-replayed counters below. A re-walked
        # already-finalized manifest keeps its recorded last_batch (is_published skips this).
        state.last_batch = None
    if state.last_commit != commit:
        # last_run timestamp is unknown post-crash; the published commit is the durable fact.
        state.last_commit = commit

    # Move events to processed/ FIRST, then record tier-1 event_keys (reads from processed/<date>/),
    # mirroring the happy-path order. A crash-at-published must NOT lose delivery idempotency: a
    # later same-key retry has to be dropped at claim time, so the keys for this run's events are
    # persisted here too (ADR-0011 §5 tier-1 / §9). Counters are NOT replayed — the plan went with
    # the dropped worktree, and counters are a best-effort dashboard signal rebuildable from git
    # (DATA-MODEL §4); event_keys are the correctness-bearing fact we must not lose.
    _move_events_to_processed(layout, manifest, run_date)
    _record_event_keys(state, layout, manifest)
    state_store.save(state)

    _advance(
        layout,
        manifest,
        phase="finalized",
        prose_complete=manifest.prose_complete,
        published_commit=commit,
    )

    # Same read-after-publish sync as the happy path (ADR-0008): a run finalized on restart already
    # advanced the curated ref, but the owner's working copy may still be behind, so fast-forward it
    # to the published commit. GUARDED: a sync failure never un-finalizes the recovered run.
    _sync_owner_working_copy(repo, run_id)

    return RunReport(
        run_id=run_id, status="recovered", published_commit=commit, counts={"finalized": 1}
    )


def _return_to_inbox(repo: Repo, manifest: RunManifest) -> RunReport:
    """Return an unpublished run's unchanged events to ``inbox/`` + clear its processing dir (§9).

    Events are byte-for-byte the original inbox files (claim moved them by rename, DATA-MODEL §1),
    so
    a recovered run re-claims them next pass and re-runs from PASS 1. The destination writer
    namespace
    is recovered from each event's frontmatter; the run directory (manifest + bundle + worktree
    scratch) is removed entirely.
    """
    layout = repo.layout
    run_id = manifest.run_id
    run_dir = layout.processing_dir / run_id
    returned, preserved = _return_events_to_inbox(
        layout, run_dir / "events", preserve_dir=layout.failed_dir / run_id[:10] / run_id
    )
    shutil.rmtree(run_dir, ignore_errors=True)
    return RunReport(
        run_id=run_id,
        status="recovered",
        # `preserved` only when non-zero (issue #124) — a clean recovery's counts are unchanged.
        counts={"returned": returned} | ({"preserved": preserved} if preserved else {}),
    )


# --- failure handling (ADR-0011 §4.3 / §5.1) ---------------------------------------------------


def _fail(
    layout: RepoLayout,
    manifest: RunManifest,
    state_store: StateStore,
    *,
    now: datetime,
    reasons: list[str],
    cas_conflict: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> RunReport:
    """Discard the run (publish NOTHING) and dispose of its events per the §5.1 retry budget.

    The entire worktree diff is dropped by the caller (the ``with repo.worktree`` block exits
    without a successful CAS, so the detached commit — if any — is unreachable). NOTHING is ever
    published on this path; only the events' LOCATION changes (DATA-MODEL §1, immutable events).

    Per-event disposition (ADR-0011 §4.3 / §5.1):

    * ``cas_conflict`` — a concurrent writer advanced the curated ref since ``base_commit``. The
      events are valid; only the base is stale. Return them ALL to ``inbox/`` to re-claim next run;
      a CAS conflict NEVER burns the retry budget.
    * otherwise (a PLAN/LINT/APPLY/diff validation failure) — for EACH event derive its retry count
      (the number of distinct ``processing/<run-id>/`` manifests + ``failed/**/error.json`` records
      that already reference its event_id, §5.1, this run inclusive). If it has NOT reached
      ``max_attempts`` (``repo.yaml curator.max_attempts``, default 3) return it to ``inbox/`` for a
      retry; only at the budget limit does it go terminal to ``failed/`` with an error record. The
      counter is DERIVED, never stored in the immutable event, so it is rebuildable from retained
      manifests/error records.
    """
    run_id = manifest.run_id
    run_date = run_id[:10]
    events_dir = layout.processing_dir / run_id / "events"

    if cas_conflict:
        returned, preserved = _return_events_to_inbox(
            layout, events_dir, preserve_dir=layout.failed_dir / run_date / run_id
        )
        shutil.rmtree(layout.processing_dir / run_id, ignore_errors=True)
        # BYTE-IDENTICAL to pre-#96 apart from the additive `failure`: a CAS conflict writes NO
        # error record and NO `last_failure` (no budget burned, nothing an operator must fix), and
        # `last_attempt` was already stamped at claim time.
        return RunReport(
            run_id=run_id,
            status="failed",
            # `preserved` is emitted ONLY when non-zero (issue #124), so the ordinary CAS conflict
            # keeps its exact pre-#124 counts mapping and the strict-equality assertions on it hold.
            counts={"retried": returned} | ({"preserved": preserved} if preserved else {}),
            failure=RunFailure(
                run_id=run_id,
                phase=manifest.phase,
                reasons=_bounded_reasons(reasons),
                record_path=None,  # this path writes NO error record — see the docstring
                cas_conflict=True,
            ),
        )

    # Prior attempt count per event = number of retained failed/ error records already referencing
    # it (§5.1). THIS run is one more attempt, so attempt N = prior + 1. We ALWAYS write this run's
    # error record (the durable retry counter + audit trail); the EVENT then either returns to
    # inbox/ (attempt < max_attempts) or stays terminal in failed/ (attempt >= max_attempts).
    prior = _event_attempt_counts(layout)

    failed_dir = layout.failed_dir / run_date / run_id
    failed_dir.mkdir(parents=True, exist_ok=True)
    error_path = failed_dir / "error.json"
    # #96: repo-RELATIVE, POSIX-separated. THE canonical string — the same value goes to
    # RunFailure.record_path AND CuratorState.last_failure.record_path, so they cannot drift.
    # Derived from layout, never hardcoded; layout.root is `.absolute()` so relative_to() is total.
    record_path = error_path.relative_to(layout.root).as_posix()
    # The record receives the RAW, un-truncated reasons: error.json is the LOSSLESS source of truth
    # the bounded report echo points AT (the `…` clipping happens only in _bounded_reasons below).
    error_record = {
        "run_id": run_id,
        "base_commit": manifest.base_commit,
        "event_ids": list(manifest.event_ids),
        "phase": manifest.phase,
        "failed_checks": reasons,
    }
    error_path.write_text(
        json.dumps(error_record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    retried = 0
    failed = 0
    if events_dir.is_dir():
        for event_path in sorted(events_dir.glob("*.md")):
            event_id = event_path.stem
            attempt = prior.get(event_id, 0) + 1  # this run is attempt N
            if attempt < max_attempts and return_event_to_inbox(layout, event_path).ok:
                # Within budget: return the unchanged event to inbox/ (re-claimed next run, §5.1).
                # The error.json above stays as the durable retry counter even though the event
                # leaves failed/.
                retried += 1
            else:
                # Budget exhausted OR the return was REFUSED (issue #124's no-loss floor — this
                # branch used to be reachable only by the budget, so a refusal fell through to the
                # rmtree below and destroyed the event). Either way the event stays TERMINAL in
                # failed/ beside the error record. A preserved event IS a terminal disposition, so
                # it counts as one: `counts["failed"]` and `counters.failed` stay honest, and no
                # counts KEY changes (the #123 strict-equality assertions hold).
                os.replace(event_path, failed_dir / event_path.name)
                failed += 1

    bounded = _bounded_reasons(reasons)

    # #96: record the failure in ONE atomic state save, ALWAYS — not only at budget exhaustion.
    # Non-terminal failures are the blind spot: the event returns to inbox/ (depth unchanged),
    # mark_run never fires (last_run stays `never`, and it MUST — that is "last successful publish",
    # explicitly out of scope), and counters.failed is untouched. `last_failure` is the only
    # surviving signal. ONE save, so counters.failed and last_failure can never disagree about the
    # same run. The load cannot raise on a corrupt state.json: _run_locked already loaded it before
    # the claim, so a corrupt file fails the run before any _fail site is reachable.
    #
    # OSError-CONTAINED on purpose: before #96 _fail touched state only when `failed > 0`, so an
    # unconditional save would turn a clean `status: failed` RunReport into an UNCAUGHT traceback
    # out of run() (for `agora curate` AND MCP `kb_curate`) on a full/read-only disk. An
    # observability write must never convert a FAILED run into a CRASHED one — so the write's own
    # failure rides the #115 warnings channel instead.
    state_warnings: list[str] = []
    try:
        state = state_store.load()
        state.last_failure = LastFailure.from_run_failure(
            when=now,
            run_id=run_id,
            phase=manifest.phase,
            reasons=bounded,
            record_path=record_path,
        )
        if failed:
            # Terminal failures only (retries are not failures yet, §5.1) — semantics UNCHANGED.
            state.counters.failed += failed
        state_store.save(state)
    except OSError as exc:
        state_warnings.append(f"could not record this failure in _kb/state.json: {exc}")

    # Remove the run's processing dir (manifest + bundle); events now live under inbox/ or failed/.
    shutil.rmtree(layout.processing_dir / run_id, ignore_errors=True)
    return RunReport(
        run_id=run_id,
        status="failed",
        counts={"retried": retried, "failed": failed},
        warnings=state_warnings,
        failure=RunFailure(
            run_id=run_id,
            phase=manifest.phase,
            reasons=bounded,
            record_path=record_path,
        ),
    )


def _event_attempt_counts(layout: RepoLayout) -> dict[str, int]:
    """Derive each event_id's PRIOR attempt count (ADR-0011 §5.1): retained ``failed/`` records.

    The retry count is NOT stored in the immutable event; it equals the number of distinct
    ``failed/**/error.json`` records that reference the event_id (one is written by :func:`_fail`
    per non-CAS attempt, so the count is durable even when the event itself is returned to inbox/
    for a retry). Rebuildable by scanning retained records, so a lost counter is recoverable
    (DATA-MODEL §1 / §4). Returns ``{event_id: prior_attempts}`` (THIS run not yet counted).
    """
    counts: dict[str, int] = {}
    for _record_path, event_ids in iter_attempt_records(layout):
        # One attempt per distinct error record, regardless of how many event_ids it lists.
        for event_id in event_ids:
            counts[event_id] = counts.get(event_id, 0) + 1
    return counts


def iter_attempt_records(layout: RepoLayout) -> Iterator[tuple[Path, list[str]]]:
    """Every readable ``failed/**/error.json`` with the event ids it governs (ADR-0011 §5.1).

    THE single derivation of the retry budget. :func:`_event_attempt_counts` tallies it, and
    ``agora requeue --reset-attempts`` (#99) walks the SAME enumeration to decide which records it
    may archive — two readers of one enumeration, so the budget a requeue releases can never
    disagree with the budget the next run charges. Sorted by path so both readers walk them in one
    deterministic order.

    Tolerance is WIDER than the code this was extracted from, deliberately — ``_kb/failed/`` is
    operator-editable, and the previous ``record.get("event_ids", [])`` raised an UNCAUGHT
    ``AttributeError`` out of :func:`_fail` on any valid-JSON record whose top level was not an
    object (only ``OSError``/``ValueError`` were caught). A hand-edited audit record could therefore
    crash a curator run. Anything whose SHAPE cannot be read is now skipped exactly like a record
    whose bytes cannot be read, so a malformed record costs a run nothing and requeue reports it
    honestly as unreadable rather than as "governs no events". Skipping under-counts attempts (more
    retries, never fewer) — the safe direction, since the alternative is dropping an event early.
    """
    failed_root = layout.failed_dir
    if not failed_root.exists():
        return
    for error_path in sorted(failed_root.rglob("error.json")):
        try:
            record = json.loads(error_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            # Valid JSON, wrong shape. NOT yielded (rather than yielded with no ids) so that
            # `agora requeue` calls it an unreadable record instead of one that governs nothing —
            # the two are different facts and only the first should hold a budget open.
            continue
        raw = record.get("event_ids", [])
        # A bare string is tolerated as a one-element list: this is an on-disk audit record an
        # operator can hand-edit, and treating "abc" as ['a','b','c'] would silently inflate the
        # budget of three events that do not exist.
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        yield error_path, [e for e in raw if isinstance(e, str)]


def _return_events_to_inbox(
    layout: RepoLayout, events_dir: Path, *, preserve_dir: Path
) -> tuple[int, int]:
    """Return every event under ``events_dir`` to its writer namespace; PRESERVE any refusal.

    Returns ``(returned, preserved)``.

    THE NO-LOSS FLOOR (issue #124). Every caller of this function clears the processing dir with
    ``shutil.rmtree`` immediately afterwards, so an event that is neither returned nor moved
    elsewhere is **destroyed** — silently, and counted by nothing: not ``RunReport.counts``, not
    ``counters.failed``, not ``failed_event_count``. That contradicts invariants 1 and 3 (markdown
    is the source of truth; events are immutable) and :func:`_fail`'s own contract that "only the
    events' LOCATION changes". A refusal is not exotic: an occupied destination
    (``dest.exists()`` — an idempotent duplicate) reaches it with no attacker involved.

    So a refused event is moved to ``preserve_dir`` instead — ``_kb/failed/<date>/<run-id>/``, where
    ``failed_event_count`` sees it, ``agora status`` reports it, and (once #99 lands) ``agora
    requeue`` can retrieve it. Preservation is best-effort in that it never raises: if even THAT
    rename fails, the event is left in ``processing/`` where ``rmtree(ignore_errors=True)`` may
    or may not remove it — but the far more likely outcomes (occupied inbox slot, unaddressable
    frontmatter) are now lossless.
    """
    returned = 0
    preserved = 0
    if events_dir.is_dir():
        for event_path in sorted(events_dir.glob("*.md")):
            if return_event_to_inbox(layout, event_path).ok:
                returned += 1
            elif _preserve_one_event(event_path, preserve_dir):
                preserved += 1
    return returned, preserved


def _preserve_one_event(event_path: Path, preserve_dir: Path) -> bool:
    """Move ONE unreturnable event under ``preserve_dir``, byte-for-byte (issue #124).

    Rename-only, like every other event disposition (DATA-MODEL §1): the bytes are never rewritten,
    so a preserved event is indistinguishable from the original an operator captured. Never raises —
    it runs on the failure/recovery paths, where an exception would strand the events after it in
    the disposal loop, turning a one-event problem into a whole-run one.
    """
    try:
        preserve_dir.mkdir(parents=True, exist_ok=True)
        dest = preserve_dir / event_path.name
        if dest.exists():
            # Already preserved under this run (a re-walked directory) — nothing to do, and
            # clobbering would overwrite an immutable event.
            return False
        os.replace(event_path, dest)
    except OSError:
        return False
    return True


# --- helpers -----------------------------------------------------------------------------------


def _sync_owner_working_copy(repo: Repo, run_id: str) -> bool:
    """Fast-forward the repo-owner's MAIN working copy to the published curated tip (ADR-0008).

    Called after a run is durably published + finalized (happy path or recovery), and best-effort at
    the START of a run to reconcile a working tree left behind by a prior failed sync. The CAS moves
    the curated ref but leaves the owner's on-disk working copy at base_commit, so the read path
    (``core.Wiki`` / ``kb_query``, which read the on-disk tree) would not yet resolve the published
    content. :meth:`Repo.sync_to_branch` does a safe ``--ff-only`` advance. GUARDED: the publish is
    already durable in git and state is finalized, so a sync failure (e.g. a dirty/diverged owner
    working tree) must NOT undo it — log a warning and return ``False``, keeping status=published.
    Returns ``True`` iff the working copy is now at the curated tip.
    """
    try:
        repo.sync_to_branch()
    except GitError as exc:
        _logger.warning(
            "curator run %s published but the owner working copy could not be fast-forwarded "
            "to the curated tip (%s); the publish is durable in git — resolve the working tree "
            "manually so the read path reflects it",
            run_id,
            exc,
        )
        return False
    return True


def rebuild_index_cache(repo: Repo) -> bool:
    """Best-effort rebuild of the ADR-0012 §2 derived reader cache after a publish (issue #26).

    Deterministic, non-sandboxed reader/worker code — NEVER the sandboxed curator backend, NEVER in
    the ADR-0008 INGEST allowlist (invariant #2 / contract C10). Honors the ``index.enabled``
    kill-switch. NEVER raises: the run is ALREADY published in git, so a cache parse/IO failure must
    only DEGRADE (the read path silently full-scans) and be surfaced as a signal, exactly the
    ADR-0017 §7 harvest-cursor swallow+log posture — it must not perturb a durable publish. Returns
    ``True`` when the cache was rebuilt OR intentionally disabled (nothing to flag); ``False`` only
    on a genuine failure, so the caller can raise an observable ``index_cache_unbuilt`` signal.
    """
    from ..config import load_index_policy
    from ..core.wiki import build_cache

    try:
        if not load_index_policy(repo.layout).enabled:
            return True  # intentionally off — not a failure, nothing to surface.
        build_cache(repo)
        return True
    except Exception as exc:  # noqa: BLE001 — derived + rebuildable; must not abort finalize.
        _logger.warning("index cache rebuild failed (rebuildable, run already published): %s", exc)
        return False


def rebuild_gold_packs(repo: Repo, *, now: datetime) -> bool:
    """Best-effort rebuild of the ADR-0027 gold pack(s) after a publish (issue #37).

    Deterministic, non-sandboxed reader/worker code — NEVER the sandboxed curator backend, NEVER in
    the ADR-0008 INGEST allowlist (invariant #2). NEVER raises: the run is ALREADY published in git,
    so a build/IO failure must only DEGRADE (a stale/absent pack → no fresh injection) and be
    surfaced as a signal, exactly the ADR-0017 §7 harvest-cursor + ADR-0012 §2 index-cache
    swallow+log posture — it must not perturb a durable publish. Returns ``True`` on success, and
    ``False`` on a genuine failure so the caller can raise an observable ``gold_unbuilt`` signal.
    ``now`` is the run's wall clock, recorded ONLY in the pack meta sidecar (the body stays stable).
    v1 ships one implicit zero-config ``default`` pack (ADR-0027 §S3); a ``_meta/gold.yaml`` policy
    file (per-audience packs) is the deferred future home.
    """
    from ..core.gold import build_gold

    try:
        build_gold(repo, generated_at=now)
        return True
    except Exception as exc:  # noqa: BLE001 — derived + rebuildable; must not abort finalize.
        _logger.warning("gold pack rebuild failed (rebuildable, run already published): %s", exc)
        return False


def _new_run_id(now: datetime) -> str:
    return new_event_id(now=now)


def _iso(now: datetime) -> str:
    """The DATA-MODEL §5 ``started`` form: ``2026-06-13T03:00:00Z`` (second precision)."""
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- §3 / §4.3 worktree gitignore + final-diff allowlist gate ----------------------------------


def _ignore_scratch(worktree: Path) -> None:
    """Ignore ``_agora_scratch/`` in the worktree via its PER-WORKTREE git exclude (ADR-0011 §4.3).

    The detached worktree at ``base_commit`` carries the repo's committed ``.gitignore`` (``_kb/`` +
    ``.DS_Store`` only, core/repo.py), which does NOT ignore the backend scratch dir — so without
    this the backend's ``plan.json`` + model scratch would be staged by ``git add -A`` and leak into
    the curated tree. We write the ignore rule to the worktree's UNTRACKED ``$GIT_DIR/info/exclude``
    (``git rev-parse --git-path info/exclude``) rather than the tracked ``.gitignore``, so the
    scratch is writable inside the only writable mount (ADR-0008) AND ignored, while producing ZERO
    tracked change — the §4.3 ``.gitignore`` intent without a spurious curated diff entry that the
    §4.0 final-diff gate would (correctly) reject. Idempotent: the line is added once.
    """
    exclude_rel = _git(worktree, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    exclude = Path(exclude_rel)
    if not exclude.is_absolute():
        exclude = worktree / exclude
    line = f"{SCRATCH_DIRNAME}/"
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if line in existing.split("\n"):
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = existing if existing == "" or existing.endswith("\n") else existing + "\n"
    exclude.write_text(f"{prefix}{line}\n", encoding="utf-8")


def _strip_stray_links(
    worktree: Path,
    needs_prose: dict[str, list[str]],
    plan: Plan,
    live_basenames: set[str],
    run_date: str,
) -> None:
    """Strip §4.6 stray wikilinks from each authored sentinel region IN PLACE before validation.

    For each needs_prose note, the allowed link set is the union of every disposition's plan
    ``links`` targeting that note plus the live-tree basenames (resolvable links APPLY may have
    placed). Any ``[[X]]`` PASS 2 introduced whose key is NOT allowed has its delimiters removed
    (inner text kept) by :func:`strip_stray_wikilinks` (byte-deterministic, iterated to a fixed
    point), so an otherwise-good prose pass is REPAIRED rather than degraded over a single stray
    link (§4.6). Only the region BODY is rewritten; frontmatter, markers, and out-of-region text are
    byte-preserved, so a clean pass produces no spurious diff.
    """
    plan_links_by_note: dict[str, set[str]] = {}
    for disp in plan.dispositions:
        if not disp.needs_prose:
            continue
        rel = _disposition_note_rel_path(disp, worktree, run_date)
        if rel is not None:
            plan_links_by_note.setdefault(rel, set()).update(disp.links)

    for rel, cids in needs_prose.items():
        path = worktree / rel
        if not path.is_file():
            continue
        allowed = live_basenames | plan_links_by_note.get(rel, set())
        text = path.read_text(encoding="utf-8")
        new_text = text
        for cid in cids:
            new_text = _strip_region_stray_links(new_text, cid, allowed)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")


def _strip_region_stray_links(text: str, candidate_id: str, allowed: set[str]) -> str:
    """Return ``text`` with stray wikilinks stripped only inside ``candidate_id``'s body region.

    Pure string surgery between the exact ``agora:body:start/end id=<cid>`` markers (markers,
    frontmatter, and out-of-region text are byte-preserved); the region body is passed through
    :func:`strip_stray_wikilinks` so the §4.6 strip is scoped to the authored region exactly.
    """
    start, end = body_sentinels(candidate_id)
    si = text.find(start)
    ei = text.find(end)
    if si == -1 or ei == -1 or ei < si:
        return text
    region_start = si + len(start)
    region = text[region_start:ei]
    return f"{text[:region_start]}{strip_stray_wikilinks(region, allowed)}{text[ei:]}"


def _is_engine_written_raw(
    path: str, status: str, worktree: Path, raw_writes: dict[str, bytes]
) -> bool:
    """True iff ``path`` is EXACTLY an engine-written canonical ``raw/`` source (ADR-0010 D3).

    Admits an added/modified ``raw/`` entry into the final diff ONLY when BOTH hold:

    * ``path`` is in ``raw_writes`` — the exact set of refs the deterministic APPLY pass
      materialized this run (a brain-PLANTED ``raw/`` file is absent from the set → rejected); and
    * the file's current bytes equal what the engine wrote (``raw_writes[path]``) — a PASS-2
      OVERWRITE of an engine source (same path, forged content; appears as ``A``/``M`` in the cached
      diff vs ``base_commit``) has mismatched bytes → rejected.

    The two are NOT interchangeable and the second NEVER subsumes the first. The membership test is
    the AUTHORSHIP check ("the engine wrote this path this run"); the byte comparison is only an
    ADDITIONAL self-check that the engine's own write survived PASS 2 intact (Stratum note §5). No
    property computable from a file's own content — a content-addressed ``raw/_blob/<ab>/<sha256>``
    basename that correctly hashes its own bytes, most of all — may ever be substituted for
    membership: a brain can always compute that hash for bytes it invents, so "self-consistent" is
    an integrity claim, never an authorship one. Hash-named plants stay rejected because they are
    absent from ``raw_writes``, and that is the only reason they are rejected.

    Comparison is on RAW BYTES (``read_bytes()``), not decoded text: ``raw/`` captures may be binary
    or non-UTF-8, and decoding them as text raised :class:`UnicodeDecodeError` here (#85). Comparing
    bytes obliges the writer to write bytes too — hence ``write_bytes`` in
    ``_materialize_raw_source`` — which is a NEW obligation, not a fix for a prior false reject: the
    old text/text pair's ``write_text`` newline translation was cancelled by ``read_text``'s
    universal-newline decode on the read side, so it did not previously mismatch on LF-only bodies.

    This replaces the prior blanket ``raw/``-prefix admit, which waved through ANY ``raw/`` add or
    modify and so let a brain forge the immutable verification baseline during PASS 2 (the §4.2
    AUTHOR-diff check never sees ``raw/`` — it grades only needs_prose notes' sentinel regions). A
    ``D`` (delete) of an engine source is NEVER admitted here: deleting a cited ``raw/`` source must
    fall through to the off-allowlist rejection. ``_kb/``, ``_meta/``, ``_templates/``, git
    internals, and hooks stay rejected by :func:`agora_kb.curator.constants.is_allowlisted_path`.
    """
    if status[:1] not in ("A", "M"):
        return False
    if path not in raw_writes:
        return False
    full = worktree / path
    if not full.is_file() or full.is_symlink():
        return False
    return full.read_bytes() == raw_writes[path]


def _assert_final_diff_allowlisted(
    worktree: Path, *, base_commit: str, raw_writes: dict[str, bytes]
) -> list[str]:
    """Assert the worktree's pending diff touches ONLY canonical-ALLOWLIST paths (ADR-0011 §4.0).

    The deterministic integrity boundary (ADR-0008 step 4): stage everything (``git add -A``) and
    read ``git diff --cached --name-status``. The run FAILS (returns non-empty reasons) unless every
    added/modified/renamed/deleted path is allowlisted (§4.0) OR is an EXACT engine-written
    canonical source under ``raw/`` (ADR-0010 D3 — membership in ``raw_writes`` with matching
    content, the precise set :func:`agora_kb.curator.apply.apply_plan` materialized this run). The
    brain-only restriction on ``raw/`` is enforced HERE, by that exact-set check: a brain-planted
    ``raw/`` file (a path not in ``raw_writes``) or a PASS-2 overwrite of an engine source (same
    path, different bytes) both fall through to the off-allowlist rejection — the §4.2 AUTHOR-diff
    check cannot see ``raw/`` (it grades only needs_prose notes' sentinel body regions), so this is
    the only gate that protects the immutable verification baseline against a brain write. No entry
    introduces or modifies a symlink or a ``..`` escape (§4.5, diff-scoped — pre-existing schema
    symlinks are whitelisted as unchanged), and ``_agora_scratch/`` produced ZERO tracked changes.
    Reasons are
    sorted for deterministic error records. This catches any out-of-allowlist file the backend
    physically wrote that the §4.1 PLAN gate (which only constrains plan-IMPLIED paths) cannot see.
    ``_kb/``, ``_meta/``, ``_templates/``, git internals, and hooks stay REJECTED.

    A rename/copy's SOURCE path is graded too, not just its destination: ``git`` reports only the
    NEW path in ``--name-status`` by default, so a PASS-2 rename of a protected file (a ``_kb/``
    inbox entry, an engine ``raw/`` source, a schema symlink) INTO the allowlist would otherwise
    read as an ordinary in-allowlist add with the deletion of the protected original invisible —
    silently exfiltrating it into the curated tree. A rename out of a non-allowlisted prefix is
    graded as though it were a delete of that protected path.
    """
    _git(worktree, "add", "-A")
    out = _git_bytes(worktree, "diff", "--cached", "--name-status", "-z").stdout

    reasons: list[str] = []
    for status, path, old_path in _parse_name_status_z(out):
        old_has_surrogate = old_path is not None and _has_surrogate_escape(old_path)
        if _has_surrogate_escape(path) or old_has_surrogate:
            reasons.append(
                f"FINAL-DIFF: {path!r} ({status}) is not valid UTF-8 — refusing to grade an "
                f"unnameable path"
            )
            continue
        # The rename/copy SOURCE: graded exactly as the destination would be, BEFORE the
        # destination checks below, so an off-allowlist source is rejected even when the
        # destination alone would look like an ordinary in-allowlist add.
        if old_path is not None:
            if old_path in SCHEMA_SYMLINKS:
                reasons.append(
                    f"FINAL-DIFF: schema symlink {old_path!r} was modified ({status}, rename "
                    f"source) — immutable"
                )
                continue
            if not is_allowlisted_path(old_path):
                reasons.append(
                    f"FINAL-DIFF: rename/copy source {old_path!r} ({status}) is outside the "
                    f"canonical ALLOWLIST — treated as a delete of a protected path"
                )
                continue
        # §4.5: a NEW/MODIFIED schema symlink (or any change touching one) fails the run; an
        # unchanged pre-existing symlink never appears in the diff, so any appearance here is an
        # illegal mutation.
        if path in SCHEMA_SYMLINKS:
            reasons.append(
                f"FINAL-DIFF: schema symlink {path!r} was modified ({status}) — immutable"
            )
            continue
        if SCRATCH_DIRNAME in Path(path).parts:
            reasons.append(
                f"FINAL-DIFF: {path!r} under {SCRATCH_DIRNAME}/ produced a tracked change "
                f"(scratch must be git-ignored)"
            )
            continue
        # ADR-0010 D3: the engine (deterministic APPLY) legitimately commits canonical ``raw/``
        # free-text sources alongside ``wiki/``; admit ONLY the EXACT paths-with-content it wrote
        # (``raw_writes``), in addition to the §4.0 curated allowlist. A brain-planted ``raw/`` file
        # or a PASS-2 overwrite of an engine source is NOT in that exact set → falls through to the
        # rejection below, so the brain still never writes ``raw/``. The symlink/``..``-escape check
        # below still applies to admitted raw/ adds.
        if not (
            is_allowlisted_path(path) or _is_engine_written_raw(path, status, worktree, raw_writes)
        ):
            reasons.append(f"FINAL-DIFF: {path!r} ({status}) is outside the canonical ALLOWLIST")
            continue
        # §4.5: reject any added/modified entry that is a symlink or a path-escape, scoped to the
        # diff. Deleted (D) entries cannot introduce a symlink, so only A/M/R are checked.
        if status[:1] in ("A", "M", "R"):
            full = worktree / path
            if full.is_symlink():
                reasons.append(f"FINAL-DIFF: {path!r} ({status}) introduced/modified a symlink")
            if ".." in Path(path).parts:
                reasons.append(f"FINAL-DIFF: {path!r} ({status}) contains a '..' path escape")

    return sorted(reasons)


def _worktree_text(path: Path) -> str:
    """The note's current text, or ``""`` when PASS 2 deleted it (mirrors :func:`_read_notes`)."""
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _blob_at(worktree: Path, tree: str, rel: str) -> bytes:
    """``rel``'s RAW BYTES in ``tree``, or ``b""`` when the path did not exist there (a PASS-2 ADD).

    Bytes, not text, because ``rel`` ranges over the FULL PASS-2 changed set — which includes
    ``raw/``, where a capture may legitimately be binary or non-UTF-8. Decoding here made
    ``git show`` raise :class:`UnicodeDecodeError` out of the PASS-2 collection loop, so a forged
    BINARY ``raw/`` file crashed the run with a traceback instead of producing the ordinary #135
    FINAL-DIFF TAMPER rejection. Callers that genuinely need text go through :func:`_blob_text_at`.
    """
    try:
        return _git_bytes(worktree, "show", f"{tree}:{rel}").stdout
    except RuntimeError:
        return b""


def _blob_text_at(worktree: Path, tree: str, rel: str) -> str:
    """``rel``'s text in ``tree`` (lossy-decoded), or ``""`` when it did not exist there.

    Used to give :func:`agora_kb.curator.apply.validate_author_diff` the real base content for a
    path outside ``needs_prose`` (whose text ``base_state`` never captured), so the ``log.md``
    equality check grades actual content instead of two defaulted empty strings.
    ``errors="replace"`` MIRRORS :func:`_worktree_text`, the NEW side of that comparison, so both
    are mangled identically and an UNCHANGED undecodable file still compares equal. The residual —
    two DIFFERENT undecodable blobs collapsing to the same replaced string — can only make §4.2
    under-report, and §4.2 merely degrades the prose pass; the enforcing gate for ``raw/`` is
    :func:`_is_engine_written_raw`, which compares RAW BYTES and is unaffected.
    """
    return _blob_at(worktree, tree, rel).decode("utf-8", errors="replace")


def _restore_out_of_scope(worktree: Path, tree: str, paths: list[str]) -> None:
    """Undo PASS-2 writes to paths outside ``needs_prose`` by restoring them from ``tree``.

    A path present in ``tree`` is checked back out (undoing a modify or a delete); a path ABSENT
    from it was created by PASS 2 and is removed. Both leave the worktree byte-identical to the
    post-APPLY baseline, so the §4.0 final-diff gate then sees only APPLY's own changes.

    ONLY §4.0-allowlisted paths are restored. An OFF-allowlist write (``_templates/``, ``_kb/``, a
    planted or forged ``raw/`` source, a touched schema symlink) is deliberately left on disk so
    :func:`_assert_final_diff_allowlisted` still FAILS the whole run on it — §4.2 degrades, §4.0
    fails, and sanitizing here must not quietly convert the second into the first.
    """
    for rel in paths:
        if not is_allowlisted_path(rel):
            continue
        try:
            _git(worktree, "checkout", tree, "--", rel)
        except RuntimeError:
            (worktree / rel).unlink(missing_ok=True)


def _parse_name_status_z(out: bytes) -> list[tuple[str, str, str | None]]:
    """Parse ``git diff --name-status -z`` output into ``[(status, path, old_path), ...]``.

    ``-z`` separates every field by NUL: ``A\\0path\\0`` for simple statuses, and for
    renames/copies ``R<score>\\0old\\0new\\0`` (three NUL fields). We report the NEW path for a
    rename/copy (the one that lands in the curated tree) as ``path``, and the OLD path as the third
    element (``None`` for a non-rename/copy status) so a caller can grade the SOURCE side too — a
    rename out of a protected prefix is a delete of a protected file, which the new-path-only view
    cannot see. Empty trailing tokens are ignored.

    Takes RAW BYTES, split on the NUL byte BEFORE any decoding, and each field is then decoded with
    ``errors="surrogateescape"`` (never raises, #85 / #135): a text-mode read here would (a) let a
    non-UTF-8 byte raise :class:`UnicodeDecodeError` out of the integrity gate as an uncaught
    traceback instead of a rejection, and (b) run ``-z``'s NUL-framed, otherwise-untouched byte
    stream through universal-newline translation, silently rewriting an embedded ``\\r`` in a path
    to ``\\n`` before the gate ever compares it. Splitting bytes on ``\\0`` first sidesteps both.
    """
    tokens = [t for t in out.split(b"\0") if t != b""]
    decoded = [t.decode("utf-8", errors="surrogateescape") for t in tokens]
    pairs: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(decoded):
        status = decoded[i]
        if status[:1] in ("R", "C") and i + 2 < len(decoded):
            pairs.append((status, decoded[i + 2], decoded[i + 1]))  # new path, old path
            i += 3
        elif i + 1 < len(decoded):
            pairs.append((status, decoded[i + 1], None))
            i += 2
        else:  # pragma: no cover — malformed/truncated git output
            break
    return pairs


def _has_surrogate_escape(text: str) -> bool:
    """True if ``text`` contains a lone surrogate — i.e. it round-tripped through
    ``errors="surrogateescape"`` from bytes that are not valid UTF-8.

    A path a legitimate git operation can ever produce is always valid UTF-8 (git stores/emits path
    bytes verbatim and this codebase only ever writes UTF-8 paths); a surrogate here means the byte
    stream held un-decodable bytes, which :func:`_parse_name_status_z` no longer raises on. Grading
    it explicitly turns an unnameable path into a named FINAL-DIFF rejection rather than either a
    traceback (the pre-surrogateescape behaviour) or a silent pass-through.
    """
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _git_bytes(worktree: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run a hermetic, no-shell ``git`` command in ``worktree``, capturing stdout as RAW BYTES.

    The bytes-mode twin of :func:`_git`, for the one thing git can legitimately print that is not
    text: the CONTENT of a blob. ``git show <tree>:<path>`` on a binary or non-UTF-8 ``raw/``
    capture has no valid text decoding, and decoding it would raise
    :class:`UnicodeDecodeError` out of the caller instead of letting the integrity gate reject the
    file (#135). stderr is still decoded (UTF-8, replacing) purely to build the error message.
    """
    cmd = ["git", "-c", f"core.hooksPath={os.devnull}", *args]
    cp = subprocess.run(  # noqa: S603 (argv list, no shell)
        cmd, cwd=str(worktree), capture_output=True
    )
    if cp.returncode != 0:
        stderr = cp.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed (rc={cp.returncode}): {stderr}")
    return cp


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a hermetic, no-shell ``git`` command in ``worktree`` (mirrors core.repo._git flags).

    ``core.hooksPath=<devnull>`` neutralizes any host/repo hook so the deterministic gate can never
    execute planted code (ADR-0008 step 3). argv list, never a shell string. Raises on failure so a
    git error in the integrity gate surfaces rather than being silently treated as a clean diff.

    stdout is decoded as UTF-8 EXPLICITLY, never with the host locale: git emits UTF-8 path bytes,
    so a cp949 / latin-1 console would otherwise mis-decode the non-ASCII paths in
    ``diff --cached --name-status -z`` and turn a legitimate engine write into a spurious
    off-allowlist FINAL-DIFF rejection (#85). The ``-z`` name-status callers now go through
    :func:`_git_bytes` + :func:`_parse_name_status_z` directly (bytes end-to-end, never raising on
    an invalid path), so this function's remaining callers only ever see git's own ASCII output
    (shas, ref names, ``add``/``checkout`` silence) — but ``errors="surrogateescape"`` (never
    ``strict``) is still used rather than assumed, so a surprising non-UTF-8 byte in stderr (e.g. a
    server-hook message) degrades to an unprintable-but-decoded string instead of raising
    UnicodeDecodeError out of the integrity gate. Blob CONTENT never comes through here — see
    :func:`_git_bytes`.
    """
    cmd = ["git", "-c", f"core.hooksPath={os.devnull}", *args]
    cp = subprocess.run(  # noqa: S603 (argv list, no shell)
        cmd,
        cwd=str(worktree),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    if cp.returncode != 0:
        # ``cp.stderr`` was decoded with ``errors="surrogateescape"`` above, so a non-UTF-8 byte in
        # git's stderr (e.g. a server-hook message this process does not control) can be present as
        # a lone surrogate code point. Embedding it directly (rather than via ``!r``) would raise
        # ``UnicodeEncodeError`` the moment an operator-facing print/log tries to encode this
        # message under a strict stream — re-encode/decode through ``replace`` (U+FFFD) so the
        # message is always printable (mirrors ``core.repo._printable``).
        stderr = (
            cp.stderr.strip()
            .encode("utf-8", errors="surrogateescape")
            .decode("utf-8", errors="replace")
        )
        raise RuntimeError(f"git {' '.join(args)} failed (rc={cp.returncode}): {stderr}")
    return cp


def _advance(
    layout: RepoLayout,
    manifest: RunManifest,
    *,
    phase: Literal["claimed", "applied", "published", "finalized"],
    prose_complete: bool,
    published_commit: str | None = None,
) -> RunManifest:
    """Atomically rewrite the manifest at a new phase (DATA-MODEL §5; write_manifest is
    in-place)."""
    advanced = manifest.model_copy(
        update={
            "phase": phase,
            "prose_complete": prose_complete,
            "published_commit": published_commit
            if published_commit is not None
            else manifest.published_commit,
        }
    )
    write_manifest(layout, advanced)
    return advanced


def _needs_prose_map(
    plan: Plan, worktree: Path, run_date: str, candidate_texts: dict[str, str]
) -> tuple[dict[str, list[str]], dict[str, set[str]], dict[str, AuthorRegion]]:
    """Map ``needs_prose`` notes to RUN-SCOPED region ids + the §4.2 set + the §8.2 context.

    Returns ``(needs_prose, sentinels, context)`` where ``needs_prose`` is
    ``{rel_path: [region_id, ...]}`` (the PASS-2 instruction the backend receives), ``sentinels`` is
    ``{rel_path: {region_id}}`` — the COMPLETE set of body-sentinel regions
    :func:`validate_author_diff` expects in each note at post-APPLY state — and ``context`` is
    ``{region_id: AuthorRegion}`` carrying the op + title + summary + candidate ``source_text`` so
    the backend can ground its prose (INGEST-CONTRACT §8.2). ``candidate_texts`` is
    ``{candidate_id: text}`` (the bundle's verbatim captures); only THIS run's authored regions get
    a context entry — the prior-run ``present`` ids unioned into ``sentinels`` are NOT re-authored.
    Each ``region_id`` is the run-scoped ``region_sentinel_id`` (``{run_id}--{candidate_id}``),
    computed via the SAME helper APPLY uses to PLACE the region so the two can never drift (the bare
    candidate_id is per-run and collides across runs). CREATE_THEME →
    ``wiki/<domain>/themes/<basename>.md``; APPEND_DAILY →
    ``wiki/<domain>/daily/<domain>-<run_date>.md`` (multiple dispositions can share one daily file,
    so ids accumulate); MERGE_INTO_THEME → the target theme path resolved in the live tree.
    """
    needs: dict[str, list[str]] = {}
    sentinels: dict[str, set[str]] = {}
    context: dict[str, AuthorRegion] = {}
    for disp in plan.dispositions:
        if not disp.needs_prose:
            continue
        rel = _disposition_note_rel_path(disp, worktree, run_date)
        if rel is None:
            continue
        # The PERSISTED region id is RUN-SCOPED (region_sentinel_id), computed via the SAME shared
        # helper APPLY uses to PLACE the region, so the §4.2 sentinels set can never drift from the
        # ids on disk. The bare candidate_id is per-run and would collide across runs (a MERGE /
        # cross-run daily append into a note holding a prior-run region).
        sentinel_id = region_sentinel_id(plan.run_id, disp.candidate_id)
        needs.setdefault(rel, []).append(sentinel_id)
        sentinels.setdefault(rel, set()).add(sentinel_id)
        # §8.2 grounding for THIS region only (keyed by the run-scoped id the backend will fill).
        context[sentinel_id] = AuthorRegion(
            op=disp.op,
            title=disp.title,
            summary=disp.summary,
            source_text=candidate_texts.get(disp.candidate_id, ""),
        )
    # A note that already carried prior-run sentinel regions must list ALL live regions in
    # `sentinels` (validate_author_diff enforces exact set equality). Re-scan each touched note for
    # every present start-sentinel id so a prior CREATE_THEME body under a now-MERGED theme is kept.
    for rel in list(sentinels):
        present = _present_sentinel_ids(worktree / rel)
        sentinels[rel] |= present
    return needs, sentinels, context


def _disposition_note_rel_path(disp: Disposition, worktree: Path, run_date: str) -> str | None:
    """Return the repo-relative path the disposition authors prose into, or ``None`` if none.

    For APPEND_DAILY the daily basename defaults to ``<domain>-<run_date>`` when the plan omits it —
    MIRRORING :func:`agora_kb.curator.apply._apply_append_daily`, which derives daily basenames
    worker-side (ADR-0011 §2/§3.1, daily basenames are not a model field). Without this default a
    §4.1-valid APPEND_DAILY that omits ``basename`` (the normal case) would build no sentinel entry,
    leaving APPLY's authored region unfilled and failing the §4.4 lint.

    This is the SINGLE funnel every downstream ``needs_prose``/``sentinels`` write and read site
    goes through (:func:`_needs_prose_map`, :func:`_strip_stray_links`, :func:`_degrade_prose`,
    :func:`_clear_body_status`), so the containment check
    (:func:`agora_kb.curator.apply._contained`, the same helper APPLY's own write sites use) is
    applied HERE, once, rather than re-derived at each call site — matching
    :func:`agora_kb.curator.apply._resolve_target_path`'s exact-name (not glob-pattern) lookup so a
    ``target_basename`` containing a glob metacharacter cannot widen the search onto another note.
    """
    candidate: Path
    if disp.op == "CREATE_THEME" and disp.domain and disp.basename:
        candidate = worktree / "wiki" / disp.domain / "themes" / f"{disp.basename}.md"
    elif disp.op == "APPEND_DAILY" and disp.domain:
        basename = disp.basename or f"{disp.domain}-{run_date}"
        candidate = worktree / "wiki" / disp.domain / "daily" / f"{basename}.md"
    elif disp.op == "MERGE_INTO_THEME" and disp.target_basename:
        wanted = f"{disp.target_basename}.md"
        matches = sorted(
            p
            for p in (worktree / "wiki").rglob("*.md")
            if p.is_file() and p.name == wanted and p.parent.name == "themes"
        )
        if not matches:
            return None
        candidate = matches[0]
    else:
        return None
    try:
        contained = _contained(worktree, candidate)
    except ApplyError:
        # Unreachable while plan.py's ASCII PATH/ALLOWLIST regex still gates every domain/basename
        # token (UNIT 1 is layout-invariant); kept fail-CLOSED (treated as "no note") rather than
        # letting a future charset widening turn this funnel into an escape.
        return None
    return contained.relative_to(worktree).as_posix()


def _present_sentinel_ids(path: Path) -> set[str]:
    """Return every ``agora:body:start id=<cid>`` candidate id present in ``path`` (or ``set()``).

    Uses the SAME start-marker grammar as :func:`agora_kb.curator.apply.body_sentinels` /
    ``validate_author_diff`` so the §4.2 ``sentinels`` set the worker builds matches exactly what
    the
    AUTHOR-diff validator extracts.
    """
    if not path.is_file():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").split("\n"):
        m = _START_SENTINEL_RE.match(line)
        if m is not None:
            ids.add(m.group("cid"))
    return ids


def _degrade_prose(
    worktree: Path, needs_prose: dict[str, list[str]], base_state: dict[str, str]
) -> None:
    """Degrade every needs_prose note to a prose-pending state on AUTHOR-diff rejection (§4.2).

    The note is RESTORED to its post-APPLY ``base_state`` text (structurally valid, with the
    candidate-id body regions still at APPLY's initial fill and ``body_status: pending`` already
    set), which discards EVERY PASS-2 edit — including any out-of-region or frontmatter tamper the
    model wrote, since frontmatter/structure are APPLY-owned and the model is outside the integrity
    boundary (§4.2 / §7). Each candidate-id region body is then normalized to the §4.2 RESET
    placeholder ``> _summary pending_`` (derived from the plan summary). The structure is intact, so
    the run still PUBLISHES; only the prose pass is lost (``prose_complete=False``). Falls back to
    the on-disk note if (defensively) the base state is missing a path.
    """
    for rel, cids in needs_prose.items():
        path = worktree / rel
        base_text = base_state.get(rel)
        if base_text is None:
            if not path.is_file():
                continue
            base_text = path.read_text(encoding="utf-8")
        fm, body = frontmatter.parse(base_text)
        for cid in cids:
            body = _replace_sentinel_region(body, cid, _RESET_PLACEHOLDER)
        fm["body_status"] = "pending"
        path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def _clear_body_status(worktree: Path, needs_prose: dict[str, list[str]]) -> list[str]:
    """Drop the stale ``body_status: pending`` from every needs_prose note that owes no prose.

    The exact inverse of :func:`_degrade_prose`, and the EXACT INVERSE of the L2-6 lint predicate
    (both call :func:`agora_kb.core.sentinel.has_unauthored_region`), so a tree this step just
    wrote can never trip the §4.4 gate's own new rule. Returns the rel_paths actually rewritten
    (unused by :func:`run` today — it exists so a test can call this directly and so a future
    counter is a one-line change).

    CLEAR-ONLY: this never ADDS the key. APPLY owns placement, and an unauthored region with no
    flag remains a legitimate state AT REST even though the curator no longer mints one: #131 made
    region placement and the flag one decision in :func:`agora_kb.curator.apply._apply_append_daily`
    (before it, a ``needs_prose=False`` APPEND_DAILY placed an empty region that nothing would ever
    author). The shape survives on disk in any daily whose APPEND_DAILY dispositions never flagged
    ``needs_prose`` — which the reference brain cannot emit, since ``ollama_brain.normalize_plan``
    forces the flag for this op — and in any hand-edited or imported note. Stamping a flag there
    would pin a ``pending`` this clear-only step could never retract: the flag would outlive every
    run, on a note nobody owes prose for.

    Scope is deliberately the run's OWN needs_prose notes: those are the notes APPLY already
    rewrote this run, so ``parse -> pop -> render`` round-trips the bytes APPLY just produced and
    the run's diff gains no file its ``log.md`` does not explain. A note whose flag is stale from
    an EARLIER build is left to L2-6 (warning) and to the ``agora repo upgrade`` migration (#63) —
    healing the whole worktree inside a curate run would rewrite notes the plan never named.
    """
    cleared: list[str] = []
    for rel in sorted(needs_prose):  # sorted so the write order is deterministic
        path = worktree / rel
        if not path.is_file():
            continue  # defensive: PASS-2 deleted it, §4.2 rejected, _degrade_prose restored it
        try:
            fm, body = frontmatter.parse(path.read_text(encoding="utf-8"))
        except FrontmatterError:
            continue  # a malformed note is L1-4's finding at §4.4; never fabricate a fence here
        if fm.get("body_status") is None:
            continue  # nothing to clear -> NO WRITE, so an already-correct note never churns
        if has_unauthored_region(body):
            continue  # still owes prose (this run's, OR an earlier run's still-empty region)
        fm.pop("body_status")
        path.write_text(frontmatter.render(fm, body), encoding="utf-8")
        cleared.append(rel)
    return cleared


def _plan_shape_warnings(plan: Plan) -> list[str]:
    """Non-fatal §7.1 shape diagnostics for an otherwise-VALID plan (#131, on the #115 channel).

    APPEND_DAILY's body deliverable IS its dated section (schema §7.1: "yes (that section)"), and
    since #131 APPLY places that section only when the disposition flags ``needs_prose``. So a plan
    that leaves the flag off publishes a daily recording the day's ``sources:`` and nothing
    readable. That is UNDER-DELIVERY by the planner, not corruption by the engine — hence a warning
    and neither of the two louder options:

    * NOT a §4.1 rejection. ``needs_prose`` is ``bool = False`` by default, so "false" is
      indistinguishable from "omitted"; failing the run would burn the §5.1 retry budget and push
      real captures to ``_kb/failed/`` over a field whose default value the contract itself made
      legal.
    * NOT a normalization to ``True``. ``needs_prose`` is also the §4.2 PASS-2 write allowlist
      (:func:`agora_kb.curator.apply.validate_author_diff`) and the §4.6 stray-link-strip scope
      (:func:`_strip_stray_links`), so coercing it would have the ENGINE GRANT the model a writable
      region the plan never asked for — the wrong direction for every other engine/model asymmetry
      here, all of which NARROW model authority. It would also make the persisted ``plan.json``
      disagree with what the run actually did, which is the audit record's whole job.

    One line per run listing every under-specified candidate, in plan order, so a chatty
    third-party brain cannot flood the channel. The shipped brains never trip it:
    ``ollama_brain.normalize_plan`` forces ``needs_prose = op in _PROSE_OPS``.
    """
    ids = [
        d.candidate_id for d in plan.dispositions if d.op == "APPEND_DAILY" and not d.needs_prose
    ]
    if not ids:
        return []
    return [
        f"PLAN SHAPE: {len(ids)} APPEND_DAILY disposition(s) did not flag needs_prose, so no dated "
        f"section was written for them — the day's sources: and raw/ captures are recorded, the "
        f"prose is not ({', '.join(ids)}); schema §7.1 lists this op as needing prose (#131)"
    ]


def _disposition_counts(plan: Plan) -> dict[str, int]:
    """Per-op disposition tallies for the run (log.md + state.counters / dashboard signal)."""
    counts: dict[str, int] = {}
    for disp in plan.dispositions:
        counts[disp.op] = counts.get(disp.op, 0) + 1
    return counts


@dataclass(frozen=True)
class HarvestCursorDelta:
    """Per-connector ``accepted``/``rejected`` increments for ONE finalized run (ADR-0017 §7).

    A pure, derived tally the worker computes from the validated :class:`Plan` dispositions over the
    bundle's HARVESTED candidate provenance, attributed to the configured connector that produced
    each tuple. Carried out of the worktree run block (where the bundle provenance is in scope) and
    applied to the §6 cursor in the happy-path finalize block ONLY (mirrors :func:`_bump_counters`).
    """

    accepted: int = 0
    rejected: int = 0


# Disposition ops that COUNT a harvested tuple as REJECTED (discarded as noise) vs the accepted
# catch-all (kept/corroborated/contested), per ADR-0017 §7 / INGEST-CONTRACT §6. A gated candidate
# may ride ONLY MERGE_INTO_THEME / MARK_CONTESTED / DROP (the §4.1 check-10 GATE_ALLOWED_OPS set —
# NOOP, CREATE_THEME and APPEND_DAILY are all rejected at validation for a gated candidate, so they
# never occur for harvested provenance). NOOP and CREATE_THEME/APPEND_DAILY handling below is
# therefore purely DEFENSIVE — harmless if a non-gated candidate ever carries harvested provenance.
_HARVEST_REJECTED_OPS: frozenset[str] = frozenset({"DROP"})


def compute_harvest_cursor_deltas(
    plan: Plan,
    bundle: BundleResult,
    connector_specs: list[ConnectorSpec],
) -> dict[str, HarvestCursorDelta]:
    """Per-connector cursor ``accepted``/``rejected`` deltas from this run's plan (ADR-0017 §7).

    PURE + deterministic + MODEL-FREE — a function of the validated :class:`Plan`, the read-only
    :class:`BundleResult` provenance, and the configured connector list. Unit-testable with
    hand-built plans + candidates and ZERO backend in the loop (the model is outside the integrity
    boundary, ADR-0011 §7).

    Granularity is per HARVESTED EVENT (per provenance tuple), matching how the harvester counts
    ``proposed`` (one per written fact): a disposition over a candidate whose provenance carries K
    harvested tuples from connector C contributes K to C. A MIXED-provenance candidate (some
    harvested tuples, some local captures) counts ONLY its harvested tuples, each attributed to its
    own connector, so ``proposed`` and ``accepted + rejected`` reconcile at the same granularity.

    Mapping: a harvested tuple's ``source`` is ``"harvest:<agent>"`` (harvester.py); the cursor is
    keyed by CONNECTOR NAME, so the agent is mapped to its connector via the CONFIGURED
    ``connector_specs`` (``{agent -> name}``). Only connectors STILL PRESENT in config are counted —
    a harvested tuple whose connector was removed from ``adapters.yaml`` is skipped (no stray cursor
    is ever created). No connectors configured (no ``adapters.yaml``) ⇒ ``{}``; no harvested
    provenance ⇒ ``{}``.

    Counting (ADR-0017 §7 / INGEST-CONTRACT §6): ``rejected`` += op == DROP (discarded as noise);
    ``accepted`` += every other non-NOOP op (MERGE_INTO_THEME / MARK_CONTESTED — kept / corroborated
    / contested). NOOP = SKIP (neither). DEFENSIVE only: NOOP, CREATE_THEME and APPEND_DAILY cannot
    occur for a GATED (harvested) candidate — the §4.1 check-10 gate (``GATE_ALLOWED_OPS`` =
    {MERGE_INTO_THEME, MARK_CONTESTED, DROP}) rejects them at validation — so these branches only
    ever fire for a (hypothetical) non-gated candidate that happens to carry harvested provenance.
    """
    agent_to_connector: dict[str, str] = {}
    for spec in connector_specs:
        # The connector agent is the part after the "<type>:" prefix of the spec name (the same form
        # the harvester uses for source=f"harvest:{agent}", writer=f"harvest-{agent}"). A spec name
        # without a ':' has no agent and cannot match a harvest source, so it is skipped.
        _, _, agent = spec.name.partition(":")
        if agent:
            agent_to_connector[agent] = spec.name
    if not agent_to_connector:
        return {}

    accepted: dict[str, int] = {}
    rejected: dict[str, int] = {}
    for disp in plan.dispositions:
        if disp.op == "NOOP":
            # DEFENSIVE: NOOP is NOT in GATE_ALLOWED_OPS (§4.1 check 10), so a gated candidate can
            # never reach here via NOOP; skip it if a non-gated candidate ever carries harvested
            # provenance — an exact duplicate is neither accepted nor rejected.
            continue
        # DROP is the only rejected op; everything else that is not NOOP is accepted. CREATE_THEME /
        # APPEND_DAILY also cannot carry harvested provenance under the §4.1 gate; if one somehow
        # does (non-gated path), it counts accepted (kept) — defensive, never expected.
        is_rejected = disp.op in _HARVEST_REJECTED_OPS
        bucket = rejected if is_rejected else accepted
        for tup in bundle.provenance.get(disp.candidate_id, []):
            source = tup.get("source")
            if not isinstance(source, str) or not source.startswith("harvest:"):
                continue  # a local capture tuple in a mixed-provenance candidate — not counted.
            agent = source[len("harvest:") :]
            name = agent_to_connector.get(agent)
            if name is None:
                continue  # this agent's connector was removed from config — skip (no stray cursor).
            bucket[name] = bucket.get(name, 0) + 1

    deltas: dict[str, HarvestCursorDelta] = {}
    for name in accepted.keys() | rejected.keys():
        deltas[name] = HarvestCursorDelta(
            accepted=accepted.get(name, 0), rejected=rejected.get(name, 0)
        )
    return deltas


def _load_connector_specs(layout: RepoLayout) -> list[ConnectorSpec]:
    """Load the configured ``adapters.yaml`` connector specs, or ``[]`` when none (ADR-0017 §7).

    Lazy import (``config`` imports back into the curator package, so importing it at module load
    would risk a cycle). An absent ``adapters.yaml`` / ``connectors:`` block returns ``[]`` so the
    harvest-cursor wiring is a clean no-op on a repo that never configured a connector — the common
    case (harvesting is opt-in, ADR-0007). A malformed connectors block is a loud operator error
    from :func:`agora_kb.config.load_connector_specs`; it must surface, not be swallowed here.
    """
    from ..config import ConfigError, load_connector_specs

    try:
        specs = load_connector_specs(layout.root / "adapters.yaml")
    except ConfigError as exc:
        # A malformed harvest-config block must NOT block an otherwise publish-ready curate run:
        # the harvest cursor is best-effort + rebuildable (DATA-MODEL §6 / ADR-0017 §7), and curate
        # would previously never parse the connectors block at all (backend loading ignores it).
        # Degrade to "no connectors counted this run" with a loud log instead of aborting publish.
        _logger.warning(
            "adapters.yaml connectors block is malformed; skipping harvest-cursor accounting this "
            "run (cursor is rebuildable): %s",
            exc,
        )
        return []
    return specs or []


def _apply_harvest_cursor_deltas(layout: RepoLayout, deltas: dict[str, HarvestCursorDelta]) -> None:
    """Add this run's harvested-candidate dispositions to the §6 cursors (ADR-0017 §7, finalize).

    The HAPPY-PATH-ONLY mirror of :func:`_bump_counters`: applied next to it in the finalize block
    and NEVER replayed in :func:`_finalize_recovered`, so the increment is exactly-once (or
    under-count + rebuildable on a rare crash) without an ``is_published`` guard. The cursor is a
    derived, git-ignored, rebuildable value (DATA-MODEL §6 / ADR-0017), so this is BEST-EFFORT,
    NOT transactional with the CAS — the additive write lands OUTSIDE the curated git tree (``_kb/``
    is git-ignored), so the ADR-0008 integrity boundary is byte-for-byte unchanged. Reuses the
    harvester's atomic :class:`CursorStore` IO; preserves every other §6 field (the harvester owns
    ``proposed`` / ``last_scan`` / etc.). Empty ``deltas`` is a no-op.
    """
    if not deltas:
        return
    # Lazy import: the harvester does not import the curator, so importing CursorStore here keeps
    # the cursor IO single-sourced without creating a package import cycle at module load.
    from ..harvester.harvester import CursorStore

    store = CursorStore(layout)
    for name, delta in deltas.items():
        cursor = store.load(name)
        cursor.accepted += delta.accepted
        cursor.rejected += delta.rejected
        store.save(cursor)


def _bump_counters(state: CuratorState, counts: dict[str, int]) -> None:
    """Update ``state.counters`` from the run dispositions (ADR-0011 §4.3 step 4 / DATA-MODEL §4).

    ``ingested`` += every originated/augmented content op
    (CREATE_THEME/APPEND_DAILY/MERGE/CONTESTED);
    ``merged`` += MERGE_INTO_THEME; ``dropped`` += DROP + NOOP. ``failed`` is bumped on the failure
    path (:func:`_fail`), never here.
    """
    ingested = sum(
        counts.get(op, 0)
        for op in ("CREATE_THEME", "APPEND_DAILY", "MERGE_INTO_THEME", "MARK_CONTESTED")
    )
    state.counters.ingested += ingested
    state.counters.merged += counts.get("MERGE_INTO_THEME", 0)
    state.counters.dropped += counts.get("DROP", 0) + counts.get("NOOP", 0)


def _record_event_keys(state: CuratorState, layout: RepoLayout, manifest: RunManifest) -> None:
    """Record each claimed event's ``writer:event_key`` into ``state.event_keys`` (ADR-0011 §4.3).

    Tier-1 delivery idempotency persists at finalization (NOT at claim time, §5 tier-1): a future
    run's claim drops a same-key retry even after this run's events have left the inbox. Each
    event's
    writer + event_key is read from its (now ``processed/``-bound) immutable frontmatter; an event
    with no event_key is skipped (un-keyed events are never deduped).
    """
    run_date = manifest.run_id[:10]
    processed_dir = layout.processed_dir / run_date
    for event_id in manifest.event_ids:
        path = processed_dir / f"{event_id}.md"
        if not path.is_file():
            continue
        try:
            fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        writer = fm.get("writer")
        event_key = fm.get("event_key")
        if isinstance(writer, str) and isinstance(event_key, str) and event_key:
            state.record_event_key(writer, event_key, event_id)


def _move_events_to_processed(layout: RepoLayout, manifest: RunManifest, run_date: str) -> None:
    """Move the claimed events ``processing/<run-id>/events/`` → ``processed/<date>/`` (§4.3 step
    4).

    A same-filesystem rename (``_kb/`` is one tree), so each event stays byte-for-byte immutable
    (DATA-MODEL §1). Idempotent: an already-moved event (a re-finalize after a crash) is skipped.
    """
    events_dir = layout.processing_dir / manifest.run_id / "events"
    if not events_dir.is_dir():
        return
    dest_dir = layout.processed_dir / run_date
    dest_dir.mkdir(parents=True, exist_ok=True)
    for event_path in sorted(events_dir.glob("*.md")):
        dest = dest_dir / event_path.name
        if dest.exists():
            continue
        os.replace(event_path, dest)


def _commit_message(run_id: str, counts: dict[str, int]) -> str:
    """One-line conventional commit subject for a curator run (one commit per run, §4.3)."""
    summary = ", ".join(f"{op}={n}" for op, n in sorted(counts.items()))
    return f"curate: run {run_id} ({summary})" if summary else f"curate: run {run_id}"


def _append_log(
    worktree: Path,
    *,
    run_id: str,
    base_commit: str,
    counts: dict[str, int],
    plan: Plan,
) -> None:
    """Append ONE structured entry to ``log.md`` (worker-only, AFTER validation; ADR-0011 §4.3).

    ``log.md`` MUST have been byte-identical to ``base_commit`` throughout PASS 1/PASS 2 (the model
    never touches it — enforced by the §4.2 diff scope), so this is the SINGLE in-run mutation of
    it,
    written by deterministic code only (append-only + single-writer, ADR-0001/0002). The entry
    records the run id, the per-op disposition counts, and the contested/dropped/pending lists.
    """
    log_path = worktree / "log.md"
    existing = log_path.read_text(encoding="utf-8") if log_path.is_file() else "# Curator log\n"

    contested = [d.target_basename for d in plan.dispositions if d.op == "MARK_CONTESTED"]
    dropped = [d.candidate_id for d in plan.dispositions if d.op in ("DROP", "NOOP")]
    pending = [
        d.candidate_id
        for d in plan.dispositions
        if d.needs_prose and d.op in ("CREATE_THEME", "APPEND_DAILY", "MERGE_INTO_THEME")
    ]
    counts_line = ", ".join(f"{op}={n}" for op, n in sorted(counts.items())) or "no-op"

    entry_lines = [
        f"## {run_id}",
        f"- base: `{base_commit}`",
        f"- dispositions: {counts_line}",
    ]
    if contested:
        entry_lines.append(f"- contested: {', '.join(str(c) for c in contested)}")
    if dropped:
        entry_lines.append(f"- dropped: {', '.join(dropped)}")
    if pending:
        entry_lines.append(f"- pending-body: {', '.join(pending)}")
    entry = "\n".join(entry_lines) + "\n"

    body = existing if existing.endswith("\n") else existing + "\n"
    log_path.write_text(f"{body}\n{entry}", encoding="utf-8")
