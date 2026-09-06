"""``agora repo upgrade --restamp`` — the ENGINE-ONLY curator maintenance run (#175 / #174 / #63).

A repo converted from a vault import carries notes the curator's APPLY step never wrote: their
``source_links:`` mirror was never minted (the mirror is stamped at CREATE / MERGE / CONTEST and
there is no backfill pass), and every ``tags:`` list was emptied because the importer strips a
source tag that is absent from the destination's ``allowed_tags`` — which for a fresh repo is ``{}``
(``ingest.vault_import._filter_tags``). This module is the backfill: a run with **no brain, no
bundle, no PLAN and no inbox claim** that re-derives those two frontmatter keys and publishes the
result through the ordinary curator publish path.

Three structural choices carry the safety, and each of them is a rejected alternative away from a
subtly wrong answer:

* **The mirror is stamped by APPLY's own function** (:func:`~agora_kb.curator.apply._stamp_source_
  links`), never by a re-implementation of the ADR-0010 §3.4 mirror rule. That is what makes "the
  bytes are what APPLY would have written" true BY DEFINITION rather than by two implementations
  agreeing today. The kind gate lives at the call site here exactly as it does at APPLY's three
  (``CLAIM_BEARING_KINDS``), so journals, maps, the root index and ``wiki/people/**`` are outside
  the run by construction.
* **The note population comes from** :func:`~agora_kb.curator.worker.scan_live_tree`, never from an
  ``rglob`` over ``*.md`` filtered on ``kind:``. A real converted repo's ``_templates/concept.md``
  DECLARES ``kind: concept`` in its frontmatter; an rglob-shaped selector pulls the template into
  the commit, and ``_templates/`` is outside the curator allowlist, so the run then fails its own
  final-diff gate. ``parse_all_notes`` scans ``index.md`` + ``wiki/**`` only, so going through the
  scan avoids that trap structurally. The scan's OTHER product is used too: only notes in
  ``curator_paths`` are written, so a human's draft under ``wiki/`` is reported and left alone
  (:data:`SKIP_HUMAN_AUTHORED`) rather than restamped, re-tagged, and dragged into the lint scope.
* **The body is proven unchanged three times** — see :func:`_plan_note` (layer 1, a per-note skip)
  and :func:`_assert_frontmatter_only` (layers 2 and 3, run-level refusals). The final-diff gate is
  PATH-level, not content-level: a mangled body under ``wiki/`` sails straight through it, so body
  invariance is this module's own obligation with no downstream backstop.

The commit is an ADMIN write, not an INGEST one. ``agora repo upgrade`` sits beside
:func:`~agora_kb.schema.emit.emit_schema` on the repo-init / admin path (emit's own docstring:
"Emit is the repo-init / admin path — it is NOT a curator INGEST write"), and the KB schema §5.2
names the taxonomy-evolution path a "separate human / admin path" on which "a new ``allowed_tags``
key and its first use land atomically in the same commit". That sentence is the ONLY warrant for
:func:`_assert_restamp_diff` admitting ``_meta/taxonomy.yaml`` alongside the §4.0 allowlist, and it
admits it only as the EXACT bytes this run computed — everything else (``raw/``, ``wiki/people/**``,
``_templates/``, ``_kb/``, the schema doc + its symlinks) stays rejected by the same checks the
INGEST gate makes.

Nothing is written to ``_kb/state.json`` (see :func:`run_restamp`): the receipts are ``log.md`` and
the git commit.
"""

from __future__ import annotations

import fcntl
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

import yaml

from ..core import frontmatter
from ..core.frontmatter import _CLOSING_FENCE as _FRONTMATTER_CLOSING_FENCE
from ..core.frontmatter import FrontmatterError
from ..core.ids import new_event_id
from ..core.layout import CLAIM_BEARING_KINDS, RepoLayout
from ..core.repo import Repo
from ..schema.emit import Taxonomy, merge_allowed_tags, taxonomy_document_text
from ..schema.lint import SOURCE_LINKS_KEY, LintResult, lint
from ..schema.notes import Note, is_people_path
from .apply import _stamp_source_links, _str_list
from .claim import curator_lock
from .constants import SCHEMA_SYMLINKS, SCRATCH_DIRNAME, is_allowlisted_path
from .worker import (
    LiveTree,
    _git,
    _git_bytes,
    _has_surrogate_escape,
    _parse_name_status_z,
    _sync_owner_working_copy,
    rebuild_gold_packs,
    rebuild_index_cache,
    scan_live_tree,
)

__all__ = [
    "CHANGEABLE_KEYS",
    "NoteChange",
    "RestampPlan",
    "RestampReport",
    "TagMatch",
    "TagSource",
    "plan_restamp",
    "run_restamp",
]

_logger = logging.getLogger(__name__)

#: The ONLY frontmatter keys this run may change. Asserted per note against the base blob, by VALUE
#: and not merely by key set (:func:`_assert_frontmatter_only`) — a run that touches anything else
#: is a bug in this module, so it refuses the whole run rather than skipping the note.
CHANGEABLE_KEYS: frozenset[str] = frozenset({SOURCE_LINKS_KEY, "tags"})

#: The one off-allowlist path an ``--tags-from-vault`` run may commit (ADR-0010 §5.2, admin path).
TAXONOMY_REL_PATH = "_meta/taxonomy.yaml"

#: The four top-level keys ``_meta/taxonomy.yaml`` is documented to hold (ADR-0010 §5). A document
#: carrying anything else is one this module does not fully understand, and re-rendering it would
#: silently DROP the stray key — so the tag leg refuses instead.
_TAXONOMY_KEYS = frozenset({"schema_version", "taxonomy_policy", "domains", "allowed_tags"})

# --- per-note skip reasons (honest report lines, never failures) ---------------------------------
#: The note's bytes are not valid UTF-8. Re-writing it would have to decode with
#: ``errors="replace"`` and would therefore CHANGE bytes outside the frontmatter — the one thing
#: this run promises not to do. (``parse_all_notes`` reads tolerantly, which is right for a READER
#: and wrong for a writer.)
SKIP_NOT_UTF8 = "not-utf-8"
#: The frontmatter block does not parse. A stamped one already failed the run at
#: ``LiveTree.malformed_curator``; reaching here means an unstamped claim-bearing note.
SKIP_UNPARSEABLE = "unparseable-frontmatter"
#: ``render(*parse(text)) != text``: the note does not survive a frontmatter round trip
#: byte-for-byte (a hand-written flow-style ``tags: [a, b]``, a frontmatter comment, blank lines
#: after the fence). Re-rendering it would reformat data this run was never asked to touch, so
#: the note is left alone and reported. Not a failure — an honest "this note needs a human first".
SKIP_NOT_ROUND_TRIP = "not-round-trip-stable"
#: The note sits at a claim-bearing path but carries NO curator stamp
#: (:data:`~agora_kb.curator.worker.CURATOR_STAMP_KEYS`), so it is a note a HUMAN wrote in
#: ``wiki/``. :func:`~agora_kb.curator.worker.is_curator_written` states that contract normatively —
#: such a note is "read, indexed, linkable — never graded, never written to" — and this run honours
#: BOTH halves. Never written to: it neither re-seats a mirror the curator did not mint nor replaces
#: a hand-authored ``tags:`` list with a vault's. Never graded: an untouched note never enters
#: ``touched``, so its own lint findings stay out of this run's scope and one unfinished draft
#: cannot veto the whole backfill — the perpetual-rejection failure #152 removed.
SKIP_HUMAN_AUTHORED = "human-authored"


# --- the tag-recovery seam (the vault reader is injected; see D6 of the design brief) ------------
@dataclass(frozen=True)
class TagMatch:
    """One claim-bearing note's answer from a tag source, with the four outcomes kept DISTINCT.

    ``unmatched`` (the source has no note of this basename) and ``no-tags`` (it has exactly one and
    that note carries no usable ``tags:``) are deliberately not one bucket: collapsing them leaves
    an operator unable to tell "the matcher is broken" from "the answer is honestly empty".
    ``ambiguous`` (two or more candidates) never guesses — the note is left as it is and the
    candidates are named in ``source`` so the vault can be tidied and the run repeated.

    ``tags`` is the source's list VERBATIM (order preserved, no case folding, no kebab-casing): the
    importer that dropped these tags applied no normalisation either
    (``vault_import._filter_tags`` is a pure membership filter), so inventing one here would not be
    an inverse of the loss, it would be a new editorial decision. ``invalid_tags`` is the subset
    that is not kebab-case; a match carrying any is REPORTED and its tags are not applied, because
    lint L1-5 checks membership only and would happily pass a taxonomy that
    :class:`~agora_kb.core.models.InboxItem` and the web upload face then refuse.
    """

    status: Literal["matched", "no-tags", "unmatched", "ambiguous"]
    tags: tuple[str, ...] = ()
    source: str | None = None
    invalid_tags: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        """True iff these tags may be written onto a note and unioned into ``allowed_tags``."""
        return self.status == "matched" and not self.invalid_tags and bool(self.tags)


class TagSource(Protocol):
    """Anything that can answer "what tags did the source hold for this basename?".

    A PROTOCOL rather than a concrete reader because the engine must not depend on
    :mod:`agora_kb.ingest`: there is not one import between ``curator/`` and ``ingest/`` today, and
    a maintenance command is a poor reason to create that edge. The filesystem implementation lives
    in :mod:`agora_kb.ingest.vault_tags` and the face wires the two together, which also makes the
    engine fully testable against a fake with no vault on disk.
    """

    def lookup(self, basename: str) -> TagMatch:
        """Return the match for ``basename`` (never raises; ``unmatched`` when nothing is known)."""
        ...  # pragma: no cover - protocol


# --- plan model ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class NoteChange:
    """What this run does — or would do — to ONE claim-bearing note.

    ``base_text`` / ``new_text`` carry the pre-image and the rendered post-image and are excluded
    from ``repr`` and from EQUALITY: a ``--dry-run`` plans against the owner's live tree while the
    real run plans against a detached worktree at the curated tip, so comparing whole documents
    would make "the preview equals the result" flake on a working copy that is merely behind. The
    fields the operator is shown are the ones that must match, and those do compare.

    ``mirror_rewritten`` is the mirror leg's own BYTE predicate — true iff
    :func:`~agora_kb.curator.apply._stamp_source_links` altered the frontmatter mapping at all,
    INCLUDING the re-seat of a value-identical mirror that sat away from ``sources:``. It exists
    because ``source_links_before != source_links_after`` (a VALUE delta) misses that case, and a
    run whose receipts are counted by value can report ``restamped: 0`` for a commit that rewrote
    the note.
    """

    rel_path: str
    kind: str
    source_links_before: tuple[str, ...] = ()
    source_links_after: tuple[str, ...] = ()
    mirror_rewritten: bool = False
    tags_before: tuple[str, ...] = ()
    tags_after: tuple[str, ...] = ()
    tag_match: TagMatch | None = None
    skipped: str | None = None
    base_text: str = field(default="", repr=False, compare=False)
    new_text: str | None = field(default=None, repr=False, compare=False)

    @property
    def changed(self) -> bool:
        """True iff this note's BYTES change.

        Derived from ``new_text`` rather than from the before/after tuples so the predicate that
        decides whether to write cannot disagree with the file's actual content. The two would
        differ for a note whose mirror is unchanged in VALUE but sits away from ``sources:``:
        :func:`~agora_kb.curator.apply._stamp_source_links` re-orders it back into place, which is a
        byte change with no value change.
        """
        return self.new_text is not None


@dataclass(frozen=True)
class RestampPlan:
    """Everything one restamp run will do, computed by a PURE read of one tree.

    The same function produces this for ``--dry-run`` (over the live tree) and for the real run
    (over the worktree at ``base_commit``), which is what makes the preview a promise rather than a
    second implementation of the same intent.

    ``taxonomy_reformatted`` says the ``_meta/taxonomy.yaml`` this run will write differs from the
    file on disk by more than the added keys: that file is HUMAN-written (``config._checked_
    domains``) and is re-RENDERED here, never patched textually — one spelling of its bytes is the
    whole point of :func:`~agora_kb.schema.emit.taxonomy_document_text` — so comments and hand
    formatting do not survive. Detected by re-rendering the PRE-union document and comparing it to
    the bytes on disk, which catches comment loss, key re-ordering and flow style in one predicate
    instead of guessing at YAML syntax. Reported, not refused: the loss is cosmetic, the tag values
    and per-tag descriptors are preserved by :func:`~agora_kb.schema.emit.merge_allowed_tags`, and
    an operator who wants the comments back has the pre-image in git.
    """

    notes: tuple[NoteChange, ...] = ()
    taxonomy_before: tuple[str, ...] = ()
    taxonomy_after: tuple[str, ...] = ()
    taxonomy_policy: str = "open"
    taxonomy_reformatted: bool = False
    taxonomy_text: str | None = field(default=None, repr=False, compare=False)
    reasons: tuple[str, ...] = ()

    @property
    def taxonomy_added(self) -> tuple[str, ...]:
        """The ``allowed_tags`` keys this run adds, in the order they land in the file."""
        before = set(self.taxonomy_before)
        return tuple(t for t in self.taxonomy_after if t not in before)

    @property
    def changed(self) -> bool:
        """True iff this run has anything at all to commit."""
        return any(n.changed for n in self.notes) or self.taxonomy_text is not None

    @property
    def restamped(self) -> int:
        """Notes whose ``source_links:`` mirror this run WRITES.

        Counted off the same BYTE predicate that decides whether to write the file
        (:attr:`NoteChange.mirror_rewritten`), not off a value delta — so a mirror that was correct
        but sat away from ``sources:`` and got re-seated is counted, exactly as it is committed.
        ``log.md`` and the commit subject are this run's ONLY receipts, and a receipt that can be
        smaller than its own diff is not a receipt.
        """
        return sum(1 for n in self.notes if n.changed and n.mirror_rewritten)

    @property
    def tags_recovered(self) -> int:
        """Notes whose ``tags:`` this run recovers from the injected tag source."""
        return sum(1 for n in self.notes if n.changed and n.tags_before != n.tags_after)


@dataclass(frozen=True)
class RestampReport:
    """The outcome of one :func:`run_restamp` invocation. The face renders it; the engine never
    prints.

    ``status`` is ``dry-run`` for a preview, ``noop`` when there was nothing to change (no commit,
    ``log.md`` untouched), ``published`` after a successful CAS, ``conflict`` when the curated ref
    moved under us, and ``refused`` for every gate in the design brief's D15 table.
    """

    status: Literal["published", "noop", "dry-run", "conflict", "refused"]
    run_id: str
    plan: RestampPlan
    published_commit: str | None = None
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    lint: LintResult | None = None


# --- the pure planner ----------------------------------------------------------------------------
def plan_restamp(
    layout: RepoLayout,
    *,
    schema_version: int,
    tag_source: TagSource | None = None,
    live: LiveTree | None = None,
) -> RestampPlan:
    """Plan the restamp over the tree at ``layout``. PURE: reads files, writes nothing.

    ``live`` lets the caller hand in a scan it already made (the real run classifies the worktree
    before planning it, and re-scanning would parse every note twice); omitted, one is taken here,
    which is what ``--dry-run`` does against the live tree.

    Population and ordering come from the scan, so this function never decides which notes exist —
    only what happens to them. The scan's OTHER half is load-bearing too: a claim-bearing note that
    is not in ``curator_paths`` is a human draft, and this run reports it and leaves it alone
    (:data:`SKIP_HUMAN_AUTHORED`).
    """
    tree = live if live is not None else scan_live_tree(layout, schema_version=schema_version)

    changes: list[NoteChange] = []
    recovered: set[str] = set()
    for note in tree.notes:
        if note.kind not in CLAIM_BEARING_KINDS:
            continue
        if is_people_path(note.rel_path):
            # Unreachable: a people note's kind is `person`, which is not claim-bearing. Stated as a
            # hard refusal rather than an `assert` (stripped under -O) or a silent `continue`,
            # because ADR-0041 D3.3 is the one boundary where "we quietly did not write it" and "we
            # proved we cannot write it" are different guarantees.
            raise RuntimeError(
                f"restamp refuses to plan {note.rel_path!r}: wiki/people/** is human-owned "
                f"(ADR-0041 D3.3) and can never be a curator write"
            )
        if note.rel_path not in tree.curator_paths:
            # The #152 split, applied. `scan_live_tree` exists to separate the curator's own
            # artifacts from notes a human wrote in `wiki/`, and a maintenance pass that writes the
            # second class would silently reformat someone's draft — the tag leg REPLACES `tags:`,
            # so a hand-authored vocabulary would vanish with no line of output naming it. Keeping
            # the note out of the population also keeps it out of `touched`, and therefore out of
            # the lint scope: one unfinished draft can no longer hard-refuse the whole backfill.
            changes.append(
                NoteChange(
                    rel_path=note.rel_path, kind=note.kind or "", skipped=SKIP_HUMAN_AUTHORED
                )
            )
            continue
        change = _plan_note(layout, note, tag_source=tag_source)
        changes.append(change)
        # Union the tags this run will LEAVE ON the note, not only the ones it newly wrote: a
        # recovered tag the note already carried still has to be declared, or L1-5 rejects a repo
        # this run had the pair in hand to fix. A SKIPPED note contributes nothing — its tags are
        # not being written, so widening the vocabulary for them would declare a tag nothing uses.
        if change.skipped is None and change.tag_match is not None and change.tag_match.usable:
            recovered.update(change.tags_after)

    doc, doc_present, doc_text = _read_taxonomy_document(layout)
    raw_allowed = doc.get("allowed_tags")
    # Narrowed at the CALL SITE rather than by widening `merge_allowed_tags` to `object`: the merge
    # rule's contract is "a mapping, a list, or nothing", and a taxonomy whose `allowed_tags:` is a
    # scalar is exactly the "or nothing" case its docstring degenerates to.
    before_raw = raw_allowed if isinstance(raw_allowed, dict | list) else None
    before = tuple(_allowed_tag_names(before_raw))
    merged = merge_allowed_tags(before_raw, recovered)
    after = tuple(merged)
    policy = doc.get("taxonomy_policy")
    policy_str = policy if isinstance(policy, str) else "open"

    reasons: list[str] = []
    taxonomy_text: str | None = None
    reformatted = False
    if after != before:
        if not doc_present:
            reasons.append(
                f"TAXONOMY: {TAXONOMY_REL_PATH} is missing, so the recovered tags "
                f"({', '.join(sorted(set(after) - set(before)))}) have nowhere to be declared; "
                f"run `agora repo init` on this repo or declare them by hand, then re-run"
            )
        elif not set(doc) <= _TAXONOMY_KEYS:
            reasons.append(
                f"TAXONOMY: {TAXONOMY_REL_PATH} carries unrecognized top-level key(s) "
                f"{', '.join(sorted(set(doc) - _TAXONOMY_KEYS))} — refusing to re-render a "
                f"document this build would silently drop them from"
            )
        else:
            # D8's `taxonomy_policy` gate lives HERE, in the pure planner, and not in the publish
            # path: `--dry-run` renders `plan.reasons` as "would refuse — …", so a gate evaluated
            # only after the lock would let a preview print a clean plan and exit 0 over a run that
            # cannot publish. Sharing the planner is what makes preview == result true; a refusal
            # the preview cannot see is a hole in exactly that promise.
            added = tuple(t for t in after if t not in set(before))
            policy_refusal = _check_taxonomy_policy(policy_str, added)
            if policy_refusal is not None:
                reasons.append(policy_refusal)
            else:
                taxonomy_text = _render_taxonomy(
                    doc, schema_version=schema_version, policy=policy_str, allowed_tags=merged
                )
                # Comment/format loss is detected by rendering the PRE-union document and comparing
                # it to the bytes on disk: everything this build does not reproduce (comments, key
                # order, flow style) shows up in one comparison, with no YAML-syntax guessing.
                reformatted = doc_text != _render_taxonomy(
                    doc,
                    schema_version=schema_version,
                    policy=policy_str,
                    allowed_tags=merge_allowed_tags(before_raw, ()),
                )

    return RestampPlan(
        notes=tuple(changes),
        taxonomy_before=before,
        taxonomy_after=after,
        taxonomy_policy=policy_str,
        taxonomy_reformatted=reformatted,
        taxonomy_text=taxonomy_text,
        reasons=tuple(reasons),
    )


def _render_taxonomy(
    doc: dict[str, object],
    *,
    schema_version: int,
    policy: str,
    allowed_tags: dict[str, object],
) -> str:
    """Render ``doc`` with ``allowed_tags`` swapped in, through the ONE emitter (``#174`` D7).

    Both renders this planner makes go through here — the post-union document it will write, and
    the pre-union one it only compares against — so the reformat predicate can never be a second
    spelling of the same file.
    """
    return taxonomy_document_text(
        schema_version=_int_or(doc.get("schema_version"), schema_version),
        taxonomy_policy=policy,
        domains=_str_list(doc.get("domains")),
        allowed_tags=allowed_tags,
    )


def _plan_note(layout: RepoLayout, note: Note, *, tag_source: TagSource | None) -> NoteChange:
    """Plan ONE note: the round-trip pre-condition, the mirror re-derivation, the tag recovery.

    LAYER 1 of the body-invariance defence lives here, and it is a per-note SKIP rather than a run
    failure. :func:`~agora_kb.core.frontmatter.render` re-dumps YAML with ``sort_keys=False,
    allow_unicode=True, default_flow_style=False`` and :func:`~agora_kb.core.frontmatter.parse`
    strips leading/trailing blank lines off the body — so a hand-edited note can be semantically
    identical and textually different after a round trip. Refusing to touch such a note is how this
    command stays safe on a KB it has never seen; reformatting one silently is not.
    """
    kind = note.kind or ""
    path = layout.root / note.rel_path
    try:
        base_text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return NoteChange(rel_path=note.rel_path, kind=kind, skipped=SKIP_NOT_UTF8)

    try:
        fm, body = frontmatter.parse(base_text)
    except FrontmatterError:
        return NoteChange(
            rel_path=note.rel_path, kind=kind, skipped=SKIP_UNPARSEABLE, base_text=base_text
        )

    if frontmatter.render(fm, body) != base_text:
        return NoteChange(
            rel_path=note.rel_path, kind=kind, skipped=SKIP_NOT_ROUND_TRIP, base_text=base_text
        )

    new_fm = dict(fm)
    # THE re-derivation: APPLY's own mutator, called at a CLAIM-BEARING call site exactly as APPLY's
    # three call sites do. It pops an empty mirror (a note whose `sources:` holds no `raw/` entry
    # legitimately gets no chip) and re-seats the key immediately after `sources:`.
    _stamp_source_links(new_fm)
    # ITEMS, not values: `==` on two dicts ignores order, and the re-seat of a value-identical
    # mirror is precisely an ORDER change. This is the mirror leg's write predicate and the thing
    # `restamped` counts, so it has to see everything `render` will.
    mirror_rewritten = list(new_fm.items()) != list(fm.items())

    match = tag_source.lookup(note.basename) if tag_source is not None else None
    if match is not None and match.usable:
        new_fm["tags"] = list(match.tags)

    new_text = frontmatter.render(new_fm, body)
    return NoteChange(
        rel_path=note.rel_path,
        kind=kind,
        source_links_before=tuple(_str_list(fm.get(SOURCE_LINKS_KEY))),
        source_links_after=tuple(_str_list(new_fm.get(SOURCE_LINKS_KEY))),
        mirror_rewritten=mirror_rewritten,
        tags_before=tuple(_str_list(fm.get("tags"))),
        tags_after=tuple(_str_list(new_fm.get("tags"))),
        tag_match=match,
        base_text=base_text,
        new_text=new_text if new_text != base_text else None,
    )


def _read_taxonomy_document(layout: RepoLayout) -> tuple[dict[str, object], bool, str | None]:
    """Return ``(_meta/taxonomy.yaml as a raw mapping, does the file exist, its raw TEXT)``.

    RAW on purpose: :class:`~agora_kb.schema.emit.Taxonomy` models ``allowed_tags`` as a tuple of
    NAMES, so a round trip through it would flatten the per-tag descriptor mapping §5 documents
    (``allowed_tags: {architecture: {desc: "…"}}``) into ``{}`` for every existing key. The merge
    rule is "widen, preserving what is there", which needs the values.

    The TEXT comes back too because ``yaml.safe_load`` is where comments and hand formatting die:
    the parsed mapping cannot answer "did the file say anything this build does not re-render?",
    and :attr:`RestampPlan.taxonomy_reformatted` has to.
    """
    path = layout.root / "_meta" / "taxonomy.yaml"
    if not path.is_file():
        return {}, False, None
    try:
        text = path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(text)
    except (OSError, yaml.YAMLError):
        return {}, False, None
    return (loaded, True, text) if isinstance(loaded, dict) else ({}, True, text)


def _allowed_tag_names(raw: object) -> list[str]:
    """The ``allowed_tags`` KEYS, from either documented shape (mapping or list)."""
    if isinstance(raw, dict):
        return [str(t) for t in raw]
    return _str_list(raw)


def _int_or(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


# --- the taxonomy_policy gate (ADR-0010 §5.2 / L1-18's first enforcement point) ------------------
def _check_taxonomy_policy(policy: str, added: tuple[str, ...]) -> str | None:
    """Return a refusal reason if adding ``added`` violates ``policy``, else ``None``.

    L1-18 is the one L1 rule ``lint()`` documents itself as unable to evaluate: it needs a
    (before, after) taxonomy pair and a single-worktree read has none. This run holds exactly that
    pair, which makes it the natural — and, in this build, the first — enforcement point.

    Called from :func:`plan_restamp`, so the verdict lands in ``RestampPlan.reasons`` and reaches
    the PREVIEW as well as the publish path. A gate evaluated after the lock would be invisible to
    ``--dry-run``, which would then print a clean plan for a run that cannot publish.

    An empty ``added`` passes under every policy: no evolution happened, so there is nothing for an
    anti-sprawl gate to grade. An unparseable policy string fails CLOSED, because a repo that
    declares a rule this build cannot read is not a repo to widen on a guess.
    """
    if not added:
        return None
    if policy == "open":
        return None
    escape = (
        f"declare the tag(s) by hand in {TAXONOMY_REL_PATH} allowed_tags and re-run, or relax "
        f"taxonomy_policy"
    )
    if policy == "review-only":
        return (
            f"TAXONOMY-POLICY: taxonomy_policy is 'review-only' and this run would add "
            f"{len(added)} new allowed_tags key(s) ({', '.join(added)}) in direct-commit mode; "
            f"{escape}"
        )
    if policy.startswith("capped:"):
        raw = policy[len("capped:") :]
        try:
            cap = int(raw)
        except ValueError:
            cap = -1
        if cap < 0:
            return (
                f"TAXONOMY-POLICY: taxonomy_policy {policy!r} is not a readable 'capped:<N>'; "
                f"refusing to widen the taxonomy under a rule this build cannot evaluate"
            )
        if len(added) > cap:
            return (
                f"TAXONOMY-POLICY: taxonomy_policy is {policy!r} and this run would add "
                f"{len(added)} new allowed_tags key(s) ({', '.join(added)}); {escape}"
            )
        return None
    return (
        f"TAXONOMY-POLICY: taxonomy_policy {policy!r} is not one of open / review-only / "
        f"capped:<N>; refusing to widen the taxonomy under a rule this build cannot evaluate"
    )


# --- the body-invariance assertions (layers 2 and 3; run-level REFUSALS) -------------------------
def _body_region(text: str) -> str:
    """The RAW substring after the closing ``---`` fence — no normalisation whatsoever.

    Deliberately not "the ``body`` half of :func:`~agora_kb.core.frontmatter.parse`": that reader
    strips leading and trailing newlines, which is exactly the difference a re-render could
    introduce, so comparing parsed bodies would hide the change this assertion exists to catch. A
    document with no parseable fence yields itself, so the comparison degenerates to whole-file
    equality rather than to a vacuous pass.
    """
    nl = text.find("\n")
    if nl == -1 or text[:nl].strip() != "---":
        return text
    rest = text[nl + 1 :]
    closing = _FRONTMATTER_CLOSING_FENCE.search(rest)
    if closing is None:
        return text
    return rest[closing.end() :]


def _assert_frontmatter_only(change: NoteChange) -> list[str]:
    """LAYERS 2 and 3: prove ``change`` edits the frontmatter and NOTHING else. Run-level.

    Layer 2 compares the raw body REGION of the post-image against the base blob's. Layer 3
    compares the frontmatter mappings key by key — by VALUE, not merely by key set, so a re-ordered
    list or a coerced scalar is caught as surely as a new key — and requires the differing set to be
    a subset of :data:`CHANGEABLE_KEYS`.

    Both are self-checks on code that constructs the post-image from the base's own ``body`` object,
    so a finding here is a defect in this module, not bad input: the caller refuses the whole run
    and publishes nothing rather than skipping the note.
    """
    if change.new_text is None:
        return []
    reasons: list[str] = []
    if _body_region(change.new_text) != _body_region(change.base_text):
        reasons.append(
            f"BODY-INVARIANT: {change.rel_path} — the body region changed; refusing to publish a "
            f"restamp that is not frontmatter-only"
        )
    try:
        before, _ = frontmatter.parse(change.base_text)
        after, _ = frontmatter.parse(change.new_text)
    except FrontmatterError as exc:  # pragma: no cover - the pre-image already parsed
        return [*reasons, f"BODY-INVARIANT: {change.rel_path} — re-parse failed: {exc}"]
    differing = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    if not differing <= CHANGEABLE_KEYS:
        reasons.append(
            f"KEY-DELTA: {change.rel_path} — this run changed "
            f"{', '.join(sorted(differing - CHANGEABLE_KEYS))}; only "
            f"{', '.join(sorted(CHANGEABLE_KEYS))} may change"
        )
    return reasons


# --- the ADMIN final-diff gate -------------------------------------------------------------------
def _is_restamp_taxonomy_write(
    path: str, status: str, worktree: Path, taxonomy_write: bytes | None
) -> bool:
    """True iff ``path`` is EXACTLY the ``_meta/taxonomy.yaml`` THIS run computed.

    The authorship-then-bytes pattern of :func:`~agora_kb.curator.worker._is_engine_written_raw`,
    for the one off-allowlist path the §5.2 admin path admits. ``taxonomy_write is None`` — a run
    without ``--tags-from-vault`` — admits nothing at all, so the widened gate exists only for the
    run that needs it, and even then only for bytes this module produced.
    """
    if taxonomy_write is None:
        return False
    if path != TAXONOMY_REL_PATH:
        return False
    if status[:1] not in ("A", "M"):
        return False
    full = worktree / path
    if not full.is_file() or full.is_symlink():
        return False
    return full.read_bytes() == taxonomy_write


def _assert_restamp_diff(
    worktree: Path, *, base_commit: str, taxonomy_write: bytes | None
) -> list[str]:
    """Assert the pending diff touches ONLY the §4.0 allowlist plus this run's taxonomy write.

    Every check :func:`~agora_kb.curator.worker._assert_final_diff_allowlisted` makes is made here:
    surrogate-escaped (un-decodable) paths are refused rather than graded, a rename/copy's SOURCE
    path is graded as well as its destination, any change touching a schema symlink fails, any
    tracked change under ``_agora_scratch/`` fails, and an added/modified/renamed entry may be
    neither a symlink nor a ``..`` escape. The INGEST gate is not simply reused because it would
    reject ``_meta/taxonomy.yaml`` — a ``--tags-from-vault`` run would refuse ITSELF.

    What the shared :func:`~agora_kb.curator.constants.is_allowlisted_path` keeps out for free, and
    the reason no extra check is written for any of them: ``raw/`` (there is no engine-written-raw
    concept in a restamp, so every ``raw/`` path falls off the allowlist), ``wiki/people/**`` (the
    D3.3 deny-prefix is tested BEFORE the allow test), ``_templates/``, ``_kb/``, ``AGENTS.md`` and
    git internals.
    """
    _git(worktree, "add", "-A")
    out = _git_bytes(worktree, "diff", "--cached", "--name-status", "-z", base_commit).stdout

    reasons: list[str] = []
    for status, path, old_path in _parse_name_status_z(out):
        old_has_surrogate = old_path is not None and _has_surrogate_escape(old_path)
        if _has_surrogate_escape(path) or old_has_surrogate:
            reasons.append(
                f"RESTAMP-DIFF: {path!r} ({status}) is not valid UTF-8 — refusing to grade an "
                f"unnameable path"
            )
            continue
        if old_path is not None:
            if old_path in SCHEMA_SYMLINKS:
                reasons.append(
                    f"RESTAMP-DIFF: schema symlink {old_path!r} was modified ({status}, rename "
                    f"source) — immutable"
                )
                continue
            if not is_allowlisted_path(old_path):
                reasons.append(
                    f"RESTAMP-DIFF: rename/copy source {old_path!r} ({status}) is outside the "
                    f"canonical ALLOWLIST — treated as a delete of a protected path"
                )
                continue
        if path in SCHEMA_SYMLINKS:
            reasons.append(
                f"RESTAMP-DIFF: schema symlink {path!r} was modified ({status}) — immutable"
            )
            continue
        if SCRATCH_DIRNAME in Path(path).parts:
            reasons.append(
                f"RESTAMP-DIFF: {path!r} under {SCRATCH_DIRNAME}/ produced a tracked change"
            )
            continue
        if not (
            is_allowlisted_path(path)
            or _is_restamp_taxonomy_write(path, status, worktree, taxonomy_write)
        ):
            reasons.append(f"RESTAMP-DIFF: {path!r} ({status}) is outside the canonical ALLOWLIST")
            continue
        if status[:1] in ("A", "M", "R"):
            full = worktree / path
            if full.is_symlink():
                reasons.append(f"RESTAMP-DIFF: {path!r} ({status}) introduced/modified a symlink")
            if ".." in Path(path).parts:
                reasons.append(f"RESTAMP-DIFF: {path!r} ({status}) contains a '..' path escape")

    return sorted(reasons)


# --- log.md + the commit subject -----------------------------------------------------------------
def _append_upgrade_log(
    worktree: Path, *, run_id: str, base_commit: str, plan: RestampPlan
) -> None:
    """Append ONE ``log.md`` entry in ``worker._append_log``'s grammar, with maintenance bullets.

    The header (``## <run_id>``), the ``- base: `<sha>` `` line, the ``# Curator log`` seed for a
    missing file and the one blank line before the entry are byte-identical to what the INGEST
    writer produces, so the file stays one parseable format. :func:`~agora_kb.curator.worker.
    _append_log` itself cannot be called — it requires a :class:`~agora_kb.curator.plan.Plan`, and
    manufacturing a fake one would write a fiction into the run log.

    ``- dispositions:`` is deliberately NOT written. The dashboard's ``_recent_log`` parses that
    bullet into its ops timeline, so putting maintenance counts there would inject names that are
    not in the closed ADR-0011 op vocabulary. That parser ignores bullets it does not recognise and
    leaves ``ops`` empty when the bullet is absent, so ``- upgrade: restamp`` reads as an entry with
    no dispositions — which is exactly what this run is.
    """
    log_path = worktree / "log.md"
    existing = log_path.read_text(encoding="utf-8") if log_path.is_file() else "# Curator log\n"

    entry_lines = [
        f"## {run_id}",
        f"- base: `{base_commit}`",
        "- upgrade: restamp",
        f"- restamped: {plan.restamped}",
        f"- tags-recovered: {plan.tags_recovered}",
    ]
    added = plan.taxonomy_added
    if added:
        entry_lines.append(f"- taxonomy-added: {', '.join(added)}")
    entry = "\n".join(entry_lines) + "\n"

    body = existing if existing.endswith("\n") else existing + "\n"
    log_path.write_text(f"{body}\n{entry}", encoding="utf-8")


def _restamp_commit_message(plan: RestampPlan) -> str:
    """The one-line commit subject.

    ``chore(kb):`` rather than ``curate:``: the INGEST verb names a consolidation run, and a git
    history in which a maintenance pass is indistinguishable from a curation is a history that
    cannot answer "when did the wiki last change because the curator thought something".
    """
    parts = [f"source_links={plan.restamped}", f"tags={plan.tags_recovered}"]
    if plan.taxonomy_added:
        parts.append(f"taxonomy=+{len(plan.taxonomy_added)}")
    return f"chore(kb): upgrade --restamp ({', '.join(parts)})"


# --- orchestration --------------------------------------------------------------------------------
def run_restamp(
    repo: Repo,
    *,
    taxonomy: Taxonomy,
    now: datetime,
    tag_source: TagSource | None = None,
    dry_run: bool = False,
    max_orphans: int | None = None,
) -> RestampReport:
    """Plan → (lock → worktree → write → lint → log → gate → commit → CAS) → sync. Never prints.

    Shaped after :func:`~agora_kb.curator.requeue.run_requeue`, the existing precedent for an
    engine-only maintenance operation that takes the curator lock itself and returns a typed report.
    It is NOT a mode of :func:`~agora_kb.curator.worker.run`: every early return there passes
    through ``_fail``, which requires a manifest and an event spool, and a restamp has neither —
    with an empty inbox ``claim()`` returns ``None`` and the run ends before any of the machinery
    this operation needs.

    **``_kb/state.json`` is deliberately left untouched.** Each field a published INGEST run writes
    would be a lie here: ``published_runs`` is a manifest-keyed crash-recovery ledger and this run
    writes no manifest; ``last_batch`` describes a claim that never happened; ``counters`` are
    derived from plan op names; ``last_attempt`` means "the curator claimed work"; and ``last_run``
    is an INPUT to the trigger evaluator and ``is_cron_due``, so stamping it here would silently
    postpone the next scheduled consolidation. The receipts are ``log.md`` and the commit. The
    visible consequence — ``agora status`` reporting a ``last_commit`` behind the branch tip — is
    honest: that IS the last INGEST publish.

    Raises :class:`~agora_kb.curator.claim.LockHeld` when a curator run is in progress; the caller
    reports it and exits non-zero with nothing changed.
    """
    # Imported here, not at module scope: `config` imports `curator`, so a module-level import
    # would close a cycle (the same reason `worker.rebuild_index_cache` defers its config import).
    from ..config import MAX_SUPPORTED_KB_SCHEMA_VERSION

    layout = repo.layout
    run_id = new_event_id(now=now)
    schema_version = taxonomy.schema_version

    if schema_version != MAX_SUPPORTED_KB_SCHEMA_VERSION:
        # Belt-and-braces behind the face's own read-only refusal (ADR-0041 D6): this engine
        # publishes a curated tree, and it will not do so into a KB wiki schema this build does not
        # write. Stated as an equality on the version the caller resolved rather than by calling the
        # D6 write-boundary wrapper, which is exhaustively wired at the faces and must stay there.
        return RestampReport(
            status="refused",
            run_id=run_id,
            plan=RestampPlan(),
            reasons=(
                f"SCHEMA: this repo declares KB wiki schema {schema_version}; this build writes "
                f"schema {MAX_SUPPORTED_KB_SCHEMA_VERSION} only (ADR-0041 D6) — the one crossing "
                f"is `agora import --from-kb`",
            ),
        )

    if dry_run:
        return _preview(repo, schema_version=schema_version, tag_source=tag_source, run_id=run_id)

    with curator_lock(layout):
        base_commit = repo.branch_commit()
        with repo.worktree(at=base_commit) as wt:
            wt_layout = RepoLayout(wt)
            live = scan_live_tree(wt_layout, schema_version=schema_version)
            if live.malformed_curator is not None:
                return RestampReport(
                    status="refused",
                    run_id=run_id,
                    plan=RestampPlan(),
                    reasons=(
                        f"LIVE-TREE: unparseable note in the curated tree — "
                        f"{live.malformed_curator}",
                    ),
                )

            # The AUTHORITATIVE plan: the same pure function `--dry-run` uses, mounted on the
            # worktree at base_commit rather than on the owner's (possibly stale) working copy.
            plan = plan_restamp(
                wt_layout, schema_version=schema_version, tag_source=tag_source, live=live
            )
            if plan.reasons:
                return RestampReport(
                    status="refused", run_id=run_id, plan=plan, reasons=plan.reasons
                )
            if not plan.changed:
                # BEFORE any write, and in particular before `log.md`: appending an entry first
                # would make the diff non-empty on every run, so a second run could never be the
                # honest no-op this command promises.
                return RestampReport(status="noop", run_id=run_id, plan=plan)

            # `taxonomy_policy` (D8) is NOT re-checked here: `plan_restamp` evaluates it and the
            # `plan.reasons` refusal above carries it, which is what lets `--dry-run` show the same
            # verdict. A second call here would be a second implementation of the same gate.
            reasons: list[str] = []
            for change in plan.notes:
                if not change.changed:
                    continue
                reasons.extend(_assert_frontmatter_only(change))
            if reasons:
                return RestampReport(
                    status="refused", run_id=run_id, plan=plan, reasons=tuple(sorted(reasons))
                )
            for change in plan.notes:
                if change.changed and change.new_text is not None:
                    # write_text with an explicit encoding, exactly as APPLY writes a note, so the
                    # bytes this run produces are the bytes APPLY would have produced.
                    (wt / change.rel_path).write_text(change.new_text, encoding="utf-8")

            taxonomy_write: bytes | None = None
            if plan.taxonomy_text is not None:
                # write_BYTES: `_is_restamp_taxonomy_write` re-reads the file and compares raw bytes
                # to admit it into the diff, and text-mode newline translation would break that
                # comparison on a platform whose os.linesep is not "\n" (epic #85).
                taxonomy_write = plan.taxonomy_text.encode("utf-8")
                taxonomy_path = wt / TAXONOMY_REL_PATH
                taxonomy_path.parent.mkdir(parents=True, exist_ok=True)
                taxonomy_path.write_bytes(taxonomy_write)

            _git(wt, "add", "-A")
            staged = _parse_name_status_z(
                _git_bytes(wt, "diff", "--cached", "--name-status", "-z", base_commit).stdout
            )
            touched = {rel for status, rel, _old in staged if not status.startswith("D")}
            if taxonomy_write is not None and TAXONOMY_REL_PATH not in touched:
                return RestampReport(
                    status="refused",
                    run_id=run_id,
                    plan=plan,
                    reasons=(
                        f"TAXONOMY: {TAXONOMY_REL_PATH} did not stage — it is git-ignored in this "
                        f"repo, so the new allowed_tags key(s) could not land in the same commit "
                        f"as their first use, which §5.2 requires",
                    ),
                )

            lint_result = lint(
                wt_layout,
                # The UNIONED taxonomy, never the value the caller loaded before this run: every
                # recovered tag is new to `allowed_tags`, so linting against the pre-union value
                # would fail L1-5 on every note this run just fixed. The file and this object are
                # built from ONE merged mapping so they cannot disagree.
                taxonomy=_unioned_taxonomy(taxonomy, plan),
                run_date=run_id[:10],
                # `run_id` is deliberately NOT injected. L1-14's full-equality half asserts a
                # journal's `run_id:` equals the RUNNING run's — true for the INGEST run that wrote
                # the journal, and false for a maintenance run on the same calendar day, which
                # would then refuse itself over a note it never touched. Without it the date-portion
                # half still grades today's journal.
                max_orphans=max_orphans,
                scope=set(live.curator_paths) | touched,
                schema_version=schema_version,
            )
            if not lint_result.ok:
                return RestampReport(
                    status="refused",
                    run_id=run_id,
                    plan=plan,
                    lint=lint_result,
                    reasons=tuple(
                        f"LINT {f.code} {f.path}: {f.message}"
                        for f in lint_result.findings
                        if f.severity == "error"
                    ),
                )

            _append_upgrade_log(wt, run_id=run_id, base_commit=base_commit, plan=plan)

            gate = _assert_restamp_diff(wt, base_commit=base_commit, taxonomy_write=taxonomy_write)
            if gate:
                return RestampReport(
                    status="refused",
                    run_id=run_id,
                    plan=plan,
                    lint=lint_result,
                    reasons=tuple(gate),
                )

            # Defensive: `Repo.commit_worktree` raises GitError on an empty diff, and catching that
            # would also swallow a real commit failure. The `plan.changed` short-circuit above
            # should make this unreachable — the `log.md` append alone guarantees a diff. (Read as
            # a name-status list rather than `git diff --cached --quiet`, whose exit code 1 for
            # "there are differences" is a raise, not a result, through `_git`.)
            if not _parse_name_status_z(
                _git_bytes(wt, "diff", "--cached", "--name-status", "-z", base_commit).stdout
            ):  # pragma: no cover - unreachable while the log append is unconditional
                return RestampReport(status="noop", run_id=run_id, plan=plan, lint=lint_result)

            new_commit = repo.commit_worktree(wt, _restamp_commit_message(plan), when=now)

        if not repo.compare_and_swap_branch(expected=base_commit, new=new_commit):
            return RestampReport(
                status="conflict",
                run_id=run_id,
                plan=plan,
                lint=lint_result,
                reasons=(
                    "CAS: the curated ref moved since base_commit; nothing was published — re-run",
                ),
            )

        warnings = _refresh_derived(repo, run_id=run_id, now=now)

    return RestampReport(
        status="published",
        run_id=run_id,
        plan=plan,
        published_commit=new_commit,
        lint=lint_result,
        warnings=tuple(warnings),
    )


def _preview(
    repo: Repo, *, schema_version: int, tag_source: TagSource | None, run_id: str
) -> RestampReport:
    """``--dry-run``: a pure read of the LIVE tree. Not one byte is written, including ``_kb/``.

    The lock is deliberately NOT taken, and that is a correctness requirement rather than a
    shortcut: :func:`~agora_kb.curator.claim.curator_lock` creates ``_kb/`` and a 0-byte
    ``_kb/curator.lock`` merely by being entered, and ``git worktree add`` writes into
    ``.git/worktrees/`` — so a naive preview would visibly mutate a repo it promised to leave
    alone. A concurrent run is reported as a staleness WARNING instead, and so is a working copy
    that lags the curated ref, since the real run plans against the ref rather than against HEAD.
    """
    layout = repo.layout
    plan = plan_restamp(layout, schema_version=schema_version, tag_source=tag_source)
    warnings: list[str] = []
    if _lock_is_held(layout.lock_file):
        warnings.append(
            "a curator run is in progress (_kb/curator.lock is held); this preview may be stale"
        )
    try:
        if repo.branch_commit() != repo.head_commit():
            warnings.append(
                "the working copy is not at the curated tip; the real run plans against the "
                "curated ref, so this preview may differ from its result"
            )
        # A working copy AT the curated tip can still be dirty, and a dirty tree is invisible to the
        # commit comparison above: an uncommitted edit or an untracked `wiki/**.md` is planned by
        # this preview and cannot be planned by the real run, which mounts the ref. Named here so
        # "the preview equals the result" is a promise about a clean tree rather than an ambush.
        dirty = _dirty_curated_paths(repo)
        if dirty:
            shown = ", ".join(dirty[:3])
            more = f" (+{len(dirty) - 3} more)" if len(dirty) > 3 else ""
            warnings.append(
                f"the working copy has uncommitted or untracked changes under the curated tree "
                f"({shown}{more}); the real run plans against the curated ref, so this preview may "
                f"differ from its result"
            )
    except Exception as exc:  # noqa: BLE001 - a preview must never fail on a git read
        warnings.append(f"could not compare the working copy to the curated ref: {exc}")
    return RestampReport(
        status="dry-run",
        run_id=run_id,
        plan=plan,
        reasons=plan.reasons,
        warnings=tuple(warnings),
    )


def _dirty_curated_paths(repo: Repo) -> list[str]:
    """Paths under the curated tree that differ from HEAD in the owner's working copy, sorted.

    Only the tiers a restamp plans (``wiki/``, ``_meta/`` and the root ``index.md``) count: a
    modified ``README.md`` or a stray note under ``_kb/`` changes nothing about what this run would
    do, and warning about it would train an operator to ignore the line.
    """
    out = _git(repo.layout.root, "status", "--porcelain", "--untracked-files=all").stdout
    dirty: set[str] = set()
    for line in out.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        # A rename prints "old -> new"; the destination is the one that is in the tree now.
        rel = entry.split(" -> ")[-1].strip().strip('"')
        if rel.startswith(("wiki/", "_meta/")) or rel == "index.md":
            dirty.add(rel)
    return sorted(dirty)


def _lock_is_held(lock_file: Path) -> bool:
    """True iff ``lock_file`` EXISTS and is currently flocked. Never creates it.

    ``O_CREAT`` is absent on purpose — the whole reason ``--dry-run`` does not take the lock is that
    taking it would materialise ``_kb/`` in a repo that has none.
    """
    if not lock_file.is_file():
        return False
    try:
        fd = os.open(lock_file, os.O_RDONLY)
    except OSError:  # pragma: no cover - unreadable lock file
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _unioned_taxonomy(taxonomy: Taxonomy, plan: RestampPlan) -> Taxonomy:
    """The taxonomy the lint gate must grade against: the one the COMMIT will leave behind.

    When this run writes no ``_meta/taxonomy.yaml`` the file is unchanged and the caller's value
    already describes it, so it is returned untouched — narrowing it to the keys this planner
    happened to read would let a maintenance run second-guess the value the caller resolved. When
    the run DOES widen the file, the merged key set is the only correct input: every recovered tag
    is new to ``allowed_tags``, so grading against the pre-union value fails L1-5 on precisely the
    notes this run just fixed. The file and this object come from ONE merged mapping.
    """
    if plan.taxonomy_text is None:
        return taxonomy
    return taxonomy.model_copy(update={"allowed_tags": plan.taxonomy_after})


def _refresh_derived(repo: Repo, *, run_id: str, now: datetime) -> list[str]:
    """Post-publish: fast-forward the owner's working copy, then rebuild the derived tiers.

    Reported LOUDLY on failure rather than logged and forgotten. If the sync fails the CAS has
    landed but the on-disk tree still holds the pre-restamp notes, so ``kb_query``, the web face and
    the reader cache keep serving them — an operator who sees only ``published`` concludes the
    command did nothing. Tag recovery in particular changes cached note records and BM25F ranking
    (``tags`` is a weighted field), so a stale cache is a visible wrong answer, not a slow one.
    """
    warnings: list[str] = []
    if not _sync_owner_working_copy(repo, run_id=run_id):
        warnings.append(
            "owner working copy not synced (dirty or diverged) — the publish is durable in git, "
            "but the on-disk tree still shows the pre-restamp notes"
        )
        return warnings
    if not rebuild_index_cache(repo):
        warnings.append("index cache not rebuilt (derived + rebuildable; `agora index` re-runs it)")
    if not rebuild_gold_packs(repo, now=now):
        warnings.append("gold packs not rebuilt (derived + rebuildable; `agora gold` re-runs it)")
    return warnings
