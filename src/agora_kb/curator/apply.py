"""Deterministic §3 APPLY + §4.2 AUTHOR-diff validation + §4.6 stray-link strip (ADR-0011).

This is the *second* deterministic stage of PLAN-APPLY-AUTHOR (ADR-0011). After PASS-1's
``plan.json`` clears the §4.1 PLAN gate (:mod:`agora_kb.curator.plan`), the WORKER — not the model
— materializes EVERYTHING that bears integrity: files, the full ADR-0010 C2 frontmatter, the
``sources:`` provenance union, wikilinks, MOC/index entries, the contested callout, daily sections,
and the candidate-id-keyed body sentinels. The model later authors ONLY prose between those
sentinels (PASS 2), graded by :func:`validate_author_diff`.

Three pure pieces live here:

* :func:`apply_plan` — the §3 deterministic APPLY. Given a validated
  :class:`~agora_kb.curator.plan.Plan`,
  a worktree path, the injected ``run_date`` (so APPLY reads NO wall clock — ADR-0010 D1), and the
  per-candidate ``provenance`` (so the WORKER writes ``sources:``, never the model — ADR-0011 §2),
  it performs each disposition's op and updates the touched ``wiki/maps/<subject>.md`` files and
  root ``index.md``. It writes under the §4.0 curated allowlist (``wiki/**``, ``index.md``,
  ``log.md``, ``assets/**``) PLUS the immutable canonical sources under
  ``raw/<domain>/<event_id>.md`` for every cited free-text capture, and RETURNS the exact
  ``{raw_ref: content}`` set it materialized so the worker's final-diff gate admits ONLY those exact
  engine-written sources (a brain-planted or brain-overwritten ``raw/`` file is rejected).

  **APPLY writes KB WIKI SCHEMA 2 (ADR-0041), and only schema 2.** The wiki axis is flipped: the
  first path segment under ``wiki/`` IS the note's KIND (``concepts/``, ``notes/``, ``maps/``) and
  the subject has left the path entirely for the ``subjects:`` frontmatter list (D1/D2.2/D3.2).
  Every path is composed by :meth:`~agora_kb.core.layout.RepoLayout.note_path_for` so the composer
  has exactly one home, every note carries the D2 common base (``kind``/``kb``/``subjects``/
  ``derived``/``provenance``) plus a derived ``type:`` OKF mirror of ``kind`` (OD-3), and a repo
  with no ``_meta/kb.yaml`` is refused rather than written half-identified (D1.5). ``raw/`` is
  UNMOVED and byte-identical to schema 1 (D1.4/D3.4) — that is the whole reason the conversion is
  cheap. A schema-1 repo never reaches here: the write refusal is the writable-schema predicate in
  :mod:`agora_kb.config`, one layer up at the ADR-0041 D6 call sites.

  ADR cross-reference (design divergence, ADR-0010 D3): the ADR's literal wording names
  ``core.ingest`` (the WRITE path) as the ONLY writer of ``raw/``, persisting each capture at
  capture time. Until a dedicated ``core.ingest`` ``raw/`` persister exists, this deterministic
  APPLY engine materializes free-text ``raw/<domain>/<event_id>.md`` sources from the immutable
  claimed-event body instead. The relocation is sound (APPLY is also deterministic engine code,
  never the sandboxed brain, and copies the body verbatim from the immutable event, so the baseline
  is faithful); a future ``core.ingest`` persister and this engine must agree on the exact path or
  the new final-diff exact-set gate would reject one of the two producers.
* :func:`validate_author_diff` — the §4.2 frontmatter-aware AUTHOR diff check: accept ONLY edits
  inside the candidate-id-keyed body-sentinel regions of ``needs_prose`` notes; reject any
  frontmatter change, any other file, any line outside a sentinel pair, sentinel tampering, a
  ``log.md`` byte change, an over-bound body, or a NEW ``[[wikilink]]`` beyond the plan's links.
* :func:`strip_stray_wikilinks` — the §4.6 byte-deterministic stray-link strip: any ``[[X]]`` whose
  key is not in the allowed set is replaced by its inner text (delimiters removed, meaning kept).

Determinism is the contract: ``apply_plan`` is a pure function of ``(plan, run_date, provenance)``
over the worktree at ``base_commit``, and the two validators are pure functions of their arguments —
so the same inputs always produce the same bytes / the same verdict, with ZERO model in the loop.
"""

from __future__ import annotations

import datetime
import posixpath
import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from agora_kb.core import frontmatter
from agora_kb.core.frontmatter import FrontmatterError
from agora_kb.core.inbox import (
    AttachmentError,
    StagedAttachment,
    attachment_sha256,
    parse_attachments,
    read_attachment,
)
from agora_kb.core.layout import (
    KIND_DIRECTORIES,
    SIDECAR_SUFFIX,
    RepoLayout,
    attachment_basename,
    blob_ref,
)
from agora_kb.core.models import Attachment
from agora_kb.core.pathsafe import is_safe_component

# The strict PRODUCER body-region grammar + the initial-fill placeholder. These live in
# :mod:`agora_kb.core.sentinel` (#119) rather than here because ``schema/lint.py`` must grade "is
# this region unauthored?" with the SAME vocabulary and may not import the curator (core <- curator,
# ADR-0001/0003) — so the one shared home has to sit BELOW both packages. The two matchers are the
# AUTHOR-diff parser's (§4.2): they capture the candidate id so the validator can pair start/end and
# confirm only declared needs_prose regions were touched. The §4.4 L1-20 gate and the L2-6
# stale-flag check import the very same two patterns, so the three graders provably cannot drift.
from agora_kb.core.sentinel import BODY_END_LINE_RE as _SENTINEL_END_RE
from agora_kb.core.sentinel import BODY_PLACEHOLDER
from agora_kb.core.sentinel import BODY_START_LINE_RE as _SENTINEL_START_RE
from agora_kb.curator.plan import Disposition, Plan
from agora_kb.schema.notes import is_people_path, path_kind, wikilinks

__all__ = [
    "apply_plan",
    "validate_author_diff",
    "strip_stray_wikilinks",
    "ApplyError",
    "body_sentinels",
    "region_sentinel_id",
    "DEFAULT_MAX_BODY_BYTES",
]

# --- sentinels (ADR-0011 §3 / §3.1) -----------------------------------------------------------

# The PERSISTED body sentinel id is RUN-SCOPED — ``{run_id}--{candidate_id}`` (see
# :func:`region_sentinel_id`) — NOT the bare candidate_id. The bare candidate_id ("c1","c2",…) is
# reassigned per RUN by :func:`agora_kb.curator.bundle._dedup_tier2`, so it is NOT unique across
# runs: a MERGE_INTO_THEME / cross-run APPEND_DAILY appends a NEW region to a note that may ALREADY
# hold a region with the same bare candidate_id from a PRIOR run, producing two identical
# ``id=c1`` markers in one note. ``run_id`` is globally unique per run (and regex-safe for the
# sentinel grammar), so prefixing with it makes every persisted region id globally unique while
# multiple APPEND_DAILY dispositions in ONE run still get distinct ids (their candidate_ids differ).
# PASS 2 writes ONLY between the start/end markers; APPLY places them empty (CREATE_THEME wraps the
# whole body; MERGE_INTO_THEME wraps a NEW augmentation sub-region appended below prior prose).
_SENTINEL_START = "<!-- agora:body:start id={cid} -->"
_SENTINEL_END = "<!-- agora:body:end id={cid} -->"

# The INITIAL-fill placeholder line the worker writes inside a fresh body region (PASS 2 replaces
# it). Kept on its own line so a clean sentinel region is never byte-empty. This is DISTINCT from
# the §4.2 AUTHOR-failure RESET placeholder, which ADR-0011 §4.2 pins as the blockquote ``>
# _summary pending_`` derived from the plan summary; the reset path lives in the worker, not here,
# so the two must not be conflated. Both spellings now live in :mod:`agora_kb.core.sentinel` (#119)
# so ``schema/lint.py`` can grade "is this region unauthored?" with the SAME vocabulary without
# importing the curator; re-exported here because APPLY is the historical home every caller imports
# (worker.py, subprocess_backend.py, adapters/ollama_brain.py, curator/__init__.py, the tests).
# Historical private alias, kept so in-module references and any external reader stay valid.
_BODY_PLACEHOLDER = BODY_PLACEHOLDER

# §4.2 default per-region body byte bound (tunable via repo.yaml curator.limits, §1.3).
DEFAULT_MAX_BODY_BYTES = 8 * 1024

# The confidence APPLY mirrors when a candidate's worst-case value is not supplied. ``confidence``
# is the candidate's worst-case value (ADR-0011 §2 / DATA-MODEL §1) the WORKER passes in; ``high``
# is the conservative non-gated default for a candidate omitted from the per-candidate map.
_DEFAULT_CONFIDENCE = "high"

# --- schema-2 vocabulary (ADR-0041 D1/D2.5) ----------------------------------------------------

# The repo-relative POSIX directories of the two kinds APPLY composes into. Derived from
# `core.layout.KIND_DIRECTORIES` rather than re-typed, so a link target computed here can never
# disagree with the path `RepoLayout.note_path_for` actually writes to.
_CONCEPTS_DIR = f"wiki/{KIND_DIRECTORIES['concept']}"
_MAPS_DIR = f"wiki/{KIND_DIRECTORIES['map']}"

# The kinds a MERGE_INTO_THEME / MARK_CONTESTED target may resolve to. Both ops are claim-bearing
# (they union `sources:` and append prose/a callout to a note that makes assertions), so they are
# scoped to the two SOURCED kinds — `summary` is included because ADR-0041 D2 gives it the same
# `sources:`/`related:`/`confidence:` shape as `concept`, so a merge into one is well-defined the
# day OD-7's producer lands. A `note` (dated journal), a `map`/`index` (navigation) and a `person`
# (human-owned, D3.3) are all refused, exactly as v1 refused everything but a theme.
_MERGE_TARGET_KINDS = frozenset({"concept", "summary"})

# The §2.1 contested callout first-line detector (mirrors lint's _CONTESTED_CALLOUT_RE) — used only
# to keep the rendered template consistent; APPLY renders the block, the lint/dashboard parse it.
_CONTESTED_CALLOUT_PREFIX = "> [!contested]"

# §4.6 stray-wikilink regex: a [[...]] token with no nested brackets / newlines, inner captured.
_WIKILINK_TOKEN_RE = re.compile(r"\[\[([^\[\]\r\n]*)\]\]")


class ApplyError(ValueError):
    """Raised by :func:`apply_plan` when a disposition cannot be materialized deterministically.

    This is a worker-side precondition failure (e.g. a CREATE_THEME missing the domain/basename the
    §4.1 PLAN gate is supposed to have guaranteed), NOT a model verdict — a valid plan never raises.
    """


def _contained(worktree: Path, candidate: Path) -> Path:
    """Return ``candidate`` unchanged, having PROVED it lands inside ``worktree``; else raise.

    Every filesystem path in this module is composed from caller-supplied tokens — ``disp.domain``,
    ``disp.basename``, ``disp.target_basename``, a provenance ``event_id`` — and is then handed
    straight to ``mkdir(parents=True)`` / ``write_text`` / ``write_bytes``. Until this helper
    existed the ONLY thing between a token and a write outside the repo was the §4.1 PATH/ALLOWLIST
    safe-token regex in :mod:`agora_kb.curator.plan`, one gate one caller up: a ``basename`` of
    ``"../../../../tmp/x"`` or ``"/etc/x"`` composes an escaping path here, and nothing downstream
    re-checks it. A gate that lives in a DIFFERENT module than the write is a gate that a refactor,
    a new caller, or a widened charset can silently remove; this is the same check restated AT the
    write, where it cannot be bypassed by reaching :func:`apply_plan` some other way.

    The check is ``resolve(strict=False)`` on both sides plus
    :meth:`~pathlib.Path.is_relative_to`. Resolving is what makes it total rather than textual: it
    normalizes ``..`` (a token like ``a/../../b``), it absolutizes ``/etc/passwd`` (which
    ``worktree / "/etc/passwd"`` yields as an ABSOLUTE path — pathlib discards the left operand),
    and it follows symlinks, so a component that is a symlink pointing OUT of the worktree is
    rejected on the target it actually names rather than on its innocent-looking name. ``strict``
    stays ``False`` because the common case is a path that does not exist yet — that is the whole
    point of the write.

    Returns the ORIGINAL ``candidate``, not the resolved twin, deliberately: callers keep using the
    exact path they composed, so a worktree reached through a symlinked ancestor (``/tmp`` →
    ``/private/tmp`` on macOS) still satisfies the ``path.relative_to(worktree)`` derivations in
    :func:`_apply_merge` / :func:`_apply_contested`, and the bytes this module writes stay
    byte-identical to what it wrote before the check existed. Containment is *asserted*, never
    *repaired*: the resolved form is used only to decide, because silently rewriting an escaping
    path to some in-tree path would turn an integrity failure into a wrong-file write.

    This does NOT subsume the ``curator/worker.py`` FINAL-DIFF symlink gates and is not subsumed by
    them: those grade the committed tree, this refuses the write in the first place.

    Applied at every site that turns a token into a path. The handful of paths built from LITERALS
    alone — root ``index.md``, the ``wiki/`` root in :func:`_update_index` — are deliberately left
    bare, since there is no token there to escape with; a literal site that later gains a token
    must be routed through here at the same time.

    :param worktree: the repo worktree every curated write must stay inside.
    :param candidate: the composed path to check.
    :returns: ``candidate``, unmodified.
    :raises ApplyError: when ``candidate`` resolves outside ``worktree``.
    """
    root = worktree.resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ApplyError(
            f"PATH/ALLOWLIST: {str(candidate)!r} resolves to {str(resolved)!r}, outside the "
            f"worktree {str(root)!r} — refusing to touch it"
        )
    # Return the ORIGINAL candidate, not `resolved`: `worktree` itself is generally UNRESOLVED (the
    # caller's own composed path — e.g. a tempdir under macOS's /var -> /private/var symlink), so a
    # RESOLVED return value would make `path.relative_to(worktree)` at call sites such as
    # `_apply_merge` raise ValueError even on a fully legitimate, contained path. `resolved` is used
    # only to DECIDE containment; the value callers keep composing with must match the unresolved
    # `worktree` they already hold.
    return candidate


def _note_path(worktree: Path, kind: str, basename: str, *, run_date: str | None = None) -> Path:
    """Compose the schema-2 path of one note and PROVE it lands inside ``worktree`` (ADR-0041 D1).

    The one composer, reached through :meth:`~agora_kb.core.layout.RepoLayout.note_path_for` so
    APPLY cannot grow a second spelling of the kind-first layout: ``concept`` lands flat under
    ``wiki/concepts/``, ``note`` under its ``wiki/notes/<yyyy>/<mm>/`` date shard, ``map`` under
    ``wiki/maps/`` and ``index`` at the repo root (D1.1/D1.2/D2.6).

    Two failure modes are TRANSLATED rather than propagated. ``note_path_for`` raises
    :class:`~agora_kb.core.layout.InvalidNoteBasenameError` (a ``ValueError``) for a basename that
    is not a safe path component or that begins with the reserved ``_`` (D4.4), and a plain
    ``ValueError`` for an unknown kind or a missing/mismatched ``run_date``. Both are re-raised as
    :class:`ApplyError` because that is the exception class
    :func:`agora_kb.curator.worker.run` catches to turn an integrity refusal into a clean FAILED
    run with an ``error.json`` — a raw ``ValueError`` would escape as the uncaught traceback
    ADR-0011 §4 forbids.
    """
    try:
        path = RepoLayout(worktree).note_path_for(kind, basename, run_date=run_date)
    except ValueError as exc:  # InvalidNoteBasenameError is a ValueError subclass
        raise ApplyError(f"PATH/ALLOWLIST: {exc}") from exc
    return _contained(worktree, path)


def _kb_id(worktree: Path) -> str:
    """Return the ``_meta/kb.yaml`` ``kb_id`` every schema-2 note mirrors into ``kb:`` (D1.5/D2).

    FAIL LOUD when the file is absent. ``load_kb_identity`` deliberately returns ``None`` for a
    missing file because it cannot know which schema its caller is on; APPLY *is* the schema-2
    write path, so for it ``None`` is a broken repo. Writing notes anyway would produce a tree that
    L1-4 rejects on every note (``kb:`` is REQUIRED) — i.e. a whole run's work discarded at the
    lint gate — or, worse, notes that name no origin once copied out of the repo, which is the one
    thing the field exists to prevent.

    The import is function-local ON PURPOSE: :mod:`agora_kb.config` imports
    :mod:`agora_kb.curator` at module scope (for ``BackendRegistry``/``TriggerConfig``), so a
    module-level import here would close a cycle through ``curator/__init__``. Deferring it to call
    time is the same idiom :func:`agora_kb.schema.notes.resolve_schema_version` uses.
    """
    from agora_kb.config import ConfigError, load_kb_identity

    layout = RepoLayout(worktree)
    try:
        identity = load_kb_identity(layout)
    except ConfigError as exc:
        raise ApplyError(f"KB identity: {exc}") from exc
    if identity is None:
        raise ApplyError(
            f"KB identity: {layout.kb_meta_file} is missing — a KB wiki schema 2 repo mints its "
            f"kb_id once at `agora repo init` and every curated note mirrors it into `kb:` "
            f"(ADR-0041 D1.5 / D2). Refusing to write notes that name no knowledge base."
        )
    return identity.kb_id


def region_sentinel_id(run_id: str, candidate_id: str) -> str:
    """Return the globally-unique PERSISTED body-sentinel id for a region (ADR-0011 §3 / §3.1).

    The id is ``{run_id}--{candidate_id}``. ``candidate_id`` ("c1","c2",…) is reassigned per RUN by
    :func:`agora_kb.curator.bundle._dedup_tier2`, so it is NOT unique across runs; ``run_id`` is
    globally unique per run, so the composite is globally unique across runs while two regions
    placed in ONE run still differ (their candidate_ids differ). This is the SINGLE SOURCE OF TRUTH
    for the persisted id — APPLY (placement) and the worker's ``_needs_prose_map`` (the §4.2
    ``sentinels`` set) BOTH call it, so they can never drift. ``run_id`` is regex-safe for the
    sentinel grammar (no ``" -->"`` substring, no newline) so the composite parses unambiguously.
    """
    return f"{run_id}--{candidate_id}"


def body_sentinels(sentinel_id: str) -> tuple[str, str]:
    """Return the ``(start, end)`` body-sentinel marker lines for ``sentinel_id`` (§3 / §3.1).

    ``sentinel_id`` is the FINAL persisted region id — for placed regions the run-scoped
    :func:`region_sentinel_id` value, never the bare per-run candidate_id. This formatter is
    generic: it wraps whatever id string it is given, so it is reused by tests/validators too.
    """
    return (
        _SENTINEL_START.format(cid=sentinel_id),
        _SENTINEL_END.format(cid=sentinel_id),
    )


def _str_list(value: object) -> list[str]:
    """Return the string elements of a raw frontmatter value as a ``list[str]`` (else ``[]``).

    Frontmatter values are typed ``object`` (a parsed YAML mapping); a ``sources``/``related``/
    ``contested_by`` entry is a list of strings. This narrows a present-but-untyped value to
    ``list[str]`` so the set-union edits below stay typed; a missing/non-list value yields ``[]``,
    matching :func:`agora_kb.schema.lint._str_items`.
    """
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


# --- provenance -> sources union (ADR-0011 §2 / ADR-0010 D3, L1-7/L1-8) ------------------------


def _sources_union(
    domain: str | None,
    provenance: list[dict[str, object]],
    *,
    worktree: Path,
    raw_writes: dict[str, bytes],
    attachments_dir: Path | None = None,
) -> list[str]:
    """Return the ordered, de-duplicated ``sources:`` list, MATERIALIZING each cited ``raw/`` (§2).

    The WORKER — never the model — writes ``sources:`` so provenance can never be lost (§2, "the
    worker writes/extends this, NOT the model"). Each provenance tuple cites a ``raw/`` artifact:

    * a tuple WITH an explicit ``raw_ref`` (an uploaded file) cites that ref and is NOT (re)written
      here — ``core.ingest`` already persisted the upload at capture time (ADR-0010 D3);
    * a tuple WITHOUT a ``raw_ref`` (a free-text ``kb_remember`` capture) cites
      ``raw/<domain>/<event_id>.md`` (basename == inbox event id, ADR-0010 D3) AND the engine
      materializes that file in the worktree from the tuple's immutable ``body`` so the curator's
      commit contains ``raw/`` + ``wiki/`` consistently and lint L1-8 passes. The write is the
      DETERMINISTIC engine's job (never the sandboxed brain, which can never touch ``raw/``); the
      file is immutable — written once, NEVER overwritten if it already exists. A tuple with neither
      a ``raw_ref`` nor a ``body`` (e.g. a hand-authored unit-test provenance fixture) keeps citing
      the path but skips the file write, preserving today's behavior.

    A tuple may ALSO carry ``attachments`` — the captured artefact's original bytes, staged beside
    the event in ``attachments_dir`` (ADR-0041 D4.2). Each one is materialized into
    ``raw/_blob/<ab>/<sha256>.<ext>`` with its sidecar (:func:`_materialize_attachments`) and cited
    BESIDE the text evidence, in that order: the extracted text and the bytes it was extracted from
    are two artefacts of one capture, and a note that cites only one of them loses half its
    provenance. The SIDECAR is never cited (lint L1-8b).

    Every engine-WRITTEN ``raw/`` ref (and its exact on-disk content) is recorded in ``raw_writes``
    so the worker's final-diff gate can admit ONLY these exact paths-with-content — a brain that
    overwrites or plants a ``raw/`` file during PASS 2 is then NOT in ``raw_writes`` (a new path) or
    has mismatched content (an overwrite), so it falls through to the off-allowlist rejection.

    Order is provenance order; duplicates collapse while preserving first-seen order so the rendered
    list is a deterministic function of the provenance input.
    """
    sources: list[str] = []
    seen: set[str] = set()
    for tup in provenance:
        ref: str | None
        raw_ref = tup.get("raw_ref")
        if isinstance(raw_ref, str) and raw_ref:
            ref = raw_ref
        else:
            event_id = tup.get("event_id")
            if isinstance(event_id, str) and event_id:
                ref = f"raw/{domain}/{event_id}.md" if domain else f"raw/{event_id}.md"
                _materialize_raw_source(worktree, ref, tup.get("body"), raw_writes=raw_writes)
            else:
                # A hand-authored provenance fixture with neither a raw_ref nor an event_id cites
                # no text artefact. Its ATTACHMENTS are still materialized below: dropping bytes
                # because the TEXT half of the same tuple is unaddressable would lose the artefact
                # silently, which is the one outcome the capture channel exists to prevent.
                ref = None
        if ref is not None and ref not in seen:
            seen.add(ref)
            sources.append(ref)
        # Named ``captured_ref``, not ``blob_ref``: the composer of that name is now imported from
        # ``core.layout`` and a loop variable would shadow it.
        for captured_ref in _materialize_attachments(
            tup, worktree=worktree, attachments_dir=attachments_dir, raw_writes=raw_writes
        ):
            if captured_ref not in seen:
                seen.add(captured_ref)
                sources.append(captured_ref)
    return sources


def _materialize_raw_source(
    worktree: Path, ref: str, body: object, *, raw_writes: dict[str, bytes]
) -> None:
    """Write the cited ``raw/`` free-text capture into the worktree, immutably (ADR-0010 D3).

    The DETERMINISTIC engine (this APPLY pass, NOT the sandboxed brain) persists each free-text
    capture as an immutable ``raw/<domain>/<event_id>.md`` so the cited source EXISTS in the
    curator's commit and lint L1-8 ("sources path does not exist") passes. ``body`` is the immutable
    claimed-event body threaded through the provenance tuple by
    :func:`agora_kb.curator.bundle.build_bundle`. Skips the write when ``body`` is absent (a
    hand-authored provenance fixture has no body — keep today's cite-only behavior) or when the file
    already exists (immutable: written once, never overwritten — so a re-run / cross-domain merge
    citing the same ref never clobbers it).

    Records ``raw_writes[ref] = <exact bytes written>`` for every ref the engine OWNS in this run —
    whether it wrote the file now or found it already materialized (an immutable re-cite). This is
    the EXACT-PATH-AND-CONTENT allowlist the final-diff gate enforces against: a PASS-2 overwrite of
    one of these files (same path, different content) and any brain-planted ``raw/`` file (a path
    absent from ``raw_writes``) are both rejected, so the brain still never writes ``raw/``.
    """
    if not isinstance(body, str):
        return
    # ``ref`` is the one path token here the §4.1 PATH/ALLOWLIST regex never graded: it is composed
    # as ``raw/<domain>/<event_id>.md`` from the PROVENANCE tuple, and ``event_id`` comes from the
    # manifest, not the plan. Checked HERE and before the ``exists()`` probe — a containment failure
    # must not even disclose whether an out-of-tree file exists. (An EXPLICIT ``raw_ref`` on the
    # tuple never reaches this function at all: _sources_union cites it and materializes nothing.)
    # ALLOWLIST, distinct from containment and not substitutable for it. `_contained` proves the
    # write lands inside the WORKTREE; it says nothing about WHERE inside, so `raw/../wiki/…`
    # resolves in-tree and passes it. This asserts the OTHER half — the ref really is the
    # `raw/<domain>/<event_id>.md` this function claims to write — so a redirect out of `raw/`
    # (which would also slip past the ADR-0010 D3 authorship channel, since git reports the
    # NORMALIZED path and the `raw_writes` key would keep the un-normalized one) is refused at the
    # write rather than admitted by whatever prefix it landed under.
    if posixpath.normpath(ref) != ref or not ref.startswith("raw/"):
        raise ApplyError(
            f"PATH/ALLOWLIST: canonical source ref {ref!r} is not a normalized path under raw/ — "
            f"refusing to materialize it (ADR-0010 D3 / ADR-0041 D1.4)"
        )
    path = _contained(worktree, worktree / ref)
    if path.exists():
        # Immutable: never overwrite. The engine still OWNS this ref this run (an immutable
        # re-cite), so record its current bytes — a later PASS-2 overwrite changes them, fails gate.
        raw_writes[ref] = path.read_bytes()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # write_BYTES, never write_text: text mode translates "\n" to os.linesep, so on Windows the
    # file on disk would NOT equal the bytes recorded here and the §4.0 byte-equality admit would
    # reject the engine's OWN write (#85). Byte equality is the whole contract — write the exact
    # bytes recorded, record the exact bytes written.
    data = body.encode("utf-8")
    path.write_bytes(data)
    raw_writes[ref] = data


# --- raw/_blob/: the ORIGINAL BYTES of a captured artefact (ADR-0041 D1.4 / D4.2) ---------------
#
# The prefix, the sidecar suffix and the ``<ab>``-sharded composer used to be private to this
# module. They now live in :mod:`agora_kb.core.layout` (``BLOB_PREFIX`` / ``SIDECAR_SUFFIX`` /
# :func:`~agora_kb.core.layout.blob_ref`) because the read face resolves a ``sources:`` citation
# back to the same path (#169, DRILLDOWN-169 D2) and ``core`` may not import ``curator``. The
# composed bytes are unchanged: ``blob_ref`` validates the basename first and slices the shard off
# it, which is the same string this module built. ONE spelling for writer and readers.


def _materialize_attachments(
    tup: Mapping[str, object],
    *,
    worktree: Path,
    attachments_dir: Path | None,
    raw_writes: dict[str, bytes],
) -> list[str]:
    """Materialize one provenance tuple's attachments into ``raw/_blob/``; return their refs.

    APPLY — the deterministic engine, and the sole writer of ``raw/`` (ADR-0020 decision 3) — is
    the ONLY place bytes cross from the per-writer staging area into the canonical tree. Fail-loud
    throughout: a malformed record, a missing staged file, or bytes that do not hash to the digest
    naming them all raise :class:`ApplyError`, which the worker turns into a clean FAILED run
    (the event returns to the inbox with its bytes). The alternative — publishing a note that cites
    a blob nobody wrote — would fail lint L1-8 at best and silently drop the artefact at worst.
    """
    try:
        records = parse_attachments(tup)
    except AttachmentError as exc:
        raise ApplyError(f"BLOB: unreadable attachment record on provenance tuple — {exc}") from exc
    return [
        _materialize_one_blob(
            record,
            tup,
            worktree=worktree,
            attachments_dir=attachments_dir,
            raw_writes=raw_writes,
        )
        for record in records
    ]


def _materialize_one_blob(
    record: Attachment,
    tup: Mapping[str, object],
    *,
    worktree: Path,
    attachments_dir: Path | None,
    raw_writes: dict[str, bytes],
) -> str:
    """Write ONE attachment's bytes + sidecar into the worktree and return the blob's ``sources:``
    ref.

    **Immutable and content-addressed** (ADR-0041 D1.4). A blob already present at this content
    address is RE-CITED and never rewritten: identical bytes are identical bytes, so a second event
    carrying the same artefact adds a citation and no write. Its digest is re-checked all the same —
    the note is about to cite it as evidence, and a file whose name does not hash its own content is
    not evidence.

    Both the blob AND its sidecar are recorded in ``raw_writes``, because both are engine-written
    files under ``raw/`` and the final-diff gate admits nothing else there. Recording the bytes of a
    file this run merely RE-cited follows :func:`_materialize_raw_source`'s rule, for the same
    reason: a PASS-2 overwrite then changes them and fails the gate.

    Those recorded bytes stay resident for the whole run (the gate compares them after PASS 2), so
    a run's peak is bounded by ``max_candidates_per_run`` x the per-attachment cap. That is the
    price of the gate's byte-equality contract, and it is paid deliberately: a digest-based
    comparison for ``raw/_blob/`` paths alone would make the ONE check that distinguishes an
    engine-written file from a planted one depend on a property a planter can also satisfy.

    The gate's AUTHORSHIP check is untouched by any of this. ``hash(bytes) == basename`` is an
    INTEGRITY property a brain can compute for bytes it invents; membership in ``raw_writes`` is
    the statement that the ENGINE wrote the path this run. A planted ``raw/_blob/`` file with a
    correct self-hash is still rejected, and content-addressing must never be offered as a reason
    to relax that (D1.4, normative).
    """
    ref = blob_ref(record.sha256, record.ext)
    sidecar_ref = f"{ref}{SIDECAR_SUFFIX}"
    path = _contained(worktree, worktree / ref)
    sidecar = _contained(worktree, worktree / sidecar_ref)

    if path.exists():
        data = path.read_bytes()
        actual = attachment_sha256(data)
        if actual != record.sha256:
            raise ApplyError(
                f"BLOB: {ref!r} already exists but its bytes hash to {actual} — a "
                f"content-addressed artefact whose content is not what its name says cannot be "
                f"cited as evidence"
            )
        raw_writes[ref] = data
    else:
        data = _read_staged_attachment(record, attachments_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        # write_BYTES: the artefact is opaque (a PDF, an image, non-UTF-8 with NULs in it) and the
        # final-diff gate compares RAW bytes, so text mode's newline translation would both corrupt
        # the file and make the engine's own write fail its own admission check (#85).
        path.write_bytes(data)
        raw_writes[ref] = data

    if sidecar.exists():
        # Never rewritten, for the same immutability reason as the blob: the sidecar records the
        # FIRST capture of these bytes, and a later event carrying the same artefact does not make
        # that record wrong.
        raw_writes[sidecar_ref] = sidecar.read_bytes()
    else:
        sidecar_bytes = _render_blob_sidecar(record, tup, size=len(data)).encode("utf-8")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_bytes(sidecar_bytes)
        raw_writes[sidecar_ref] = sidecar_bytes
    return ref


def _read_staged_attachment(record: Attachment, attachments_dir: Path | None) -> bytes:
    """Read one staged attachment's bytes, verified against the digest that names them.

    ``attachments_dir`` is ``<processing>/<run-id>/events/_attach/`` — where the bytes travelled to
    with their event. ``None`` means the caller supplied no staging area at all (a unit-test APPLY
    invoked directly), which for a tuple that DOES declare attachments is a caller bug, not a
    tolerable absence: cite-without-materialize would publish a dangling citation.
    """
    if attachments_dir is None:
        raise ApplyError(
            f"BLOB: attachment {record.sha256}.{record.ext} was declared but APPLY was given no "
            f"staging directory to read its bytes from"
        )
    staged = StagedAttachment(
        record=record, path=attachments_dir / attachment_basename(record.sha256, record.ext)
    )
    try:
        return read_attachment(staged)
    except (OSError, AttachmentError) as exc:
        raise ApplyError(f"BLOB: cannot read staged attachment {staged.path} — {exc}") from exc


def _render_blob_sidecar(record: Attachment, tup: Mapping[str, object], *, size: int) -> str:
    """Render the ``<blob>.meta.yaml`` capture sidecar — a CLOSED key set (DATA-MODEL §2).

    ``sha256``, ``ext``, ``media_type``, ``bytes``, ``filename``, ``captured_at``, ``writer``,
    ``source``, ``event_id`` — the capture FACTS, and never the extracted text: that lives in the
    event body and, after curation, in the note. Duplicating it here would create a second copy of
    the knowledge that nothing keeps in step with the first.

    ``bytes`` is the length of the bytes actually written, not the record's declared size, so the
    sidecar can never disagree with the file beside it. An absent optional (``media_type``,
    ``filename``) and an absent provenance field are OMITTED rather than emitted empty — the key set
    is closed against ADDITIONS; it does not oblige every key to be present.
    """
    doc: dict[str, object] = {"sha256": record.sha256, "ext": record.ext}
    if record.media_type is not None:
        doc["media_type"] = record.media_type
    doc["bytes"] = size
    if record.filename is not None:
        doc["filename"] = record.filename
    captured_at = _blob_timestamp(tup.get("created"))
    if captured_at is not None:
        doc["captured_at"] = captured_at
    for key in ("writer", "source", "event_id"):
        value = tup.get(key)
        if isinstance(value, str) and value:
            doc[key] = value
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _blob_timestamp(value: object) -> str | None:
    """The event's ``created`` as the sidecar's ``captured_at`` string, or ``None``.

    ``created`` is written as a quoted ``YYYY-MM-DDTHH:MM:SSZ`` string (DATA-MODEL §1) and normally
    round-trips as one, but a hand-edited event can leave it UNQUOTED, in which case
    ``yaml.safe_load`` hands back a :class:`datetime.datetime`. Both are accepted and normalized to
    the same spelling, so the sidecar's shape never depends on how the event happened to be quoted.
    """
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def _is_harvest_origin(provenance: list[dict[str, object]]) -> str | None:
    """Return the ``harvest:<agent>`` origin to stamp, or ``None`` (ADR-0011 §2 / ADR-0010 D4).

    A kept region whose provenance includes ANY ``source == harvest:<agent>`` is tagged ``origin:
    harvest:<agent>`` by the worker for loop-prevention (DATA-MODEL §7) — the model never writes
    this tag. The FIRST harvest source in provenance order wins (deterministic).
    """
    for tup in provenance:
        source = tup.get("source")
        if (
            isinstance(source, str)
            and source.startswith("harvest:")
            and len(source) > len("harvest:")
        ):
            return source
    return None


def _canonicalize_dates(fm: dict[str, object]) -> None:
    """Coerce ``date``-typed frontmatter values to canonical ``YYYY-MM-DD`` strings in place.

    A foreign/externally-authored note may carry an UNQUOTED ``created: 2026-06-13``, which
    ``yaml.safe_load`` returns as a :class:`datetime.date` and ``frontmatter.render`` re-emits
    unquoted — while the worker-written ``updated`` is a quoted string, leaving the SAME note with
    mixed YAML quoting on re-render. Coercing every ``date``/``datetime`` value to its ISO string
    makes the rendered YAML quoting uniform regardless of how the note was originally authored, so
    MERGE/MARK_CONTESTED output stays uniform without depending on the source note's quoting style.
    Notes CREATED by APPLY already pass string dates, so this only normalizes foreign notes.
    """
    for key, value in list(fm.items()):
        if isinstance(value, datetime.date):  # datetime.datetime is a subclass — both handled
            fm[key] = value.isoformat()


def _stamp_harvest_origin(fm: dict[str, object], provenance: list[dict[str, object]]) -> None:
    """Set ``fm['origin'] = harvest:<agent>`` on a re-rendered note whose provenance is harvested.

    Used by MERGE/MARK_CONTESTED, which fold a candidate into an EXISTING note (ADR-0011 §2 / §6 /
    DATA-MODEL §7 loop-prevention). Set-union semantics: only ADD a harvest origin when the note has
    no ``origin`` yet, so a pre-existing origin is never overwritten and a non-harvest provenance
    leaves ``origin`` untouched. The model never writes this tag.
    """
    origin = _is_harvest_origin(provenance)
    if origin is not None and not fm.get("origin"):
        fm["origin"] = origin


# --- OKF v0.1 producer fields (ADR-0014 D2) ----------------------------------------------------

# The OKF v0.1 bundle-root version string, emitted on the root ``index.md`` ONLY (per the OKF spec;
# ADR-0014 D2 / D6). It is the EXTERNAL conformance axis, orthogonal to the internal
# ``schema_version`` (ADR-0010 D6 / L1-17) — the two evolve independently. Never on theme/daily/moc.
_OKF_VERSION = "0.1"


def _okf_timestamp(updated: str) -> str:
    """Return OKF's ``timestamp`` for a note, derived DETERMINISTICALLY from ``updated`` (D2).

    OKF v0.1 expects an ISO-8601 datetime for "last meaningful change"; Agora's canonical clock is a
    DATE (``updated`` == ``run_date``, ADR-0010 D1) read from the injected run manifest, never the
    wall clock. So the OKF datetime is the run date pinned to midnight UTC — ``<updated>T00:00:00Z``
    — which satisfies OKF WITHOUT reintroducing a system clock (ADR-0014 ratified decision #5 / D1
    replay determinism). ``updated`` is the same ``YYYY-MM-DD`` string APPLY already materializes,
    so ``timestamp`` is a pure function of it and the run stays byte-reproducible.
    """
    return f"{updated}T00:00:00Z"


def _set_updated(fm: dict[str, object], run_date: str) -> None:
    """Bump ``updated`` AND re-derive the OKF ``timestamp`` together, in lock-step (D2).

    The OKF invariant ``timestamp == <updated>T00:00:00Z`` must hold on EVERY note after EVERY
    curator edit (ADR-0014 D2 / kb_schema.md §2.7), not just at CREATE. Every UPDATE branch that
    advances ``updated`` to ``run_date`` (append-daily re-touch, MERGE, MARK_CONTESTED, MOC /
    index re-render) MUST go through this helper so ``timestamp`` can never drift stale behind
    ``updated``. The CREATE branches pair the two keys inline at note-build time (key order matters
    there); this helper is the single update-path equivalent.
    """
    fm["updated"] = run_date
    fm["timestamp"] = _okf_timestamp(run_date)


# --- schema-2 frontmatter (ADR-0041 D2, the OKF superset of ADR-0010 C2) ----------------------


def _provenance_block(provenance: list[dict[str, object]]) -> dict[str, object]:
    """Build the ADR-0041 D2.3 ``provenance:`` block for the candidates folded into one note.

    Two lists, deliberately unequal. ``writers`` holds AUTHENTICATED principals and is TRUSTED; it
    is written EMPTY here and that is the honest value, not a stub — Agora has no authn plane until
    Phase 4, so there is no principal this worktree can authenticate. ``agents`` holds agent
    SELF-DECLARATIONS and is RECORDED, NEVER TRUSTED: it is the distinct ``source`` of the inbox
    events merged into this note (``claude-code``, ``agent:<name>``, ``harvest:<agent>``, …), in
    first-seen provenance order so the block is a deterministic function of its input.

    Without the split the custody claim Agora makes — *"a system of record for what your agents
    learned"* — would be false, because an unauthenticated self-declared agent name would be
    indistinguishable from an authenticated principal.
    """
    agents: list[str] = []
    for tup in provenance:
        source = tup.get("source")
        if isinstance(source, str) and source and source not in agents:
            agents.append(source)
    return {"writers": [], "agents": agents}


def _merge_agents(fm: dict[str, object], provenance: list[dict[str, object]]) -> None:
    """Union this run's self-declared agents into an EXISTING note's ``provenance:`` block (D2.3).

    Set-union, never replacement, and the same posture as ``sources:``/``related:``: a note the
    curator re-renders keeps every agent that ever contributed to it. A malformed or absent block
    is REBUILT rather than repaired in place, so a note arriving from an importer or a human editor
    leaves this function in the D2.3 shape; ``writers`` is preserved when it is already a list of
    strings, because the auth plane that will one day populate it is not this function's to erase.
    """
    block = fm.get("provenance")
    writers: list[str] = []
    agents: list[str] = []
    if isinstance(block, dict):
        writers = _str_list(block.get("writers"))
        agents = _str_list(block.get("agents"))
    for tup in provenance:
        source = tup.get("source")
        if isinstance(source, str) and source and source not in agents:
            agents.append(source)
    fm["provenance"] = {"writers": writers, "agents": agents}


def _common_frontmatter(
    *,
    title: str,
    kind: str,
    kb: str,
    subjects: list[str],
    aliases: list[str],
    tags: list[str],
    run_date: str,
    status: str,
    summary: str,
    provenance: list[dict[str, object]],
    okf_version: str | None = None,
) -> dict[str, object]:
    """Build the ADR-0041 D2 COMMON BASE every curator-written note carries, in key order.

    The order is D2's own block, with the ADR-0014 D2 OKF mirrors interleaved at the positions the
    v1 shape already put them: ``type`` right after ``kind`` (the DERIVED mirror, OD-3),
    ``okf_version`` on the bundle-root ``index.md`` ONLY, ``timestamp`` right after ``updated``,
    and ``description`` right after ``summary``. It is byte-identical in ORDER to the
    ``agora repo init --schema 2`` seed index, so a note APPLY re-renders and a note the CLI seeded
    read the same way in a diff.

    Three fields are new in schema 2 and each is materialized by the WORKER, never the model:
    ``kind`` (the mirror of the DIRECTORY, which is authoritative where the two disagree — D2.1),
    ``kb`` (the ``_meta/kb.yaml`` ULID, D1.5) and ``subjects`` (the successor to the v1 path domain,
    D2.2 — ``[]`` is a legal, honest value that asserts nothing and loses nothing). ``derived`` is
    ``False``: APPLY is the CURATED plane, and D2.4 reserves ``true`` for a proposal/derivation
    plane that has no day-1 producer.
    """
    fm: dict[str, object] = {
        "title": title,
        "kind": kind,
        "type": kind,
        "kb": kb,
    }
    if okf_version is not None:
        fm["okf_version"] = okf_version
    fm["subjects"] = list(subjects)
    fm["aliases"] = list(aliases)
    fm["tags"] = list(tags)
    fm["created"] = run_date
    fm["updated"] = run_date
    fm["timestamp"] = _okf_timestamp(run_date)
    fm["status"] = status
    fm["summary"] = summary
    fm["description"] = summary
    fm["derived"] = False
    fm["provenance"] = _provenance_block(provenance)
    return fm


def _concept_frontmatter(
    disp: Disposition,
    *,
    kb: str,
    run_date: str,
    subjects: list[str],
    sources: list[str],
    origin: str | None,
    confidence: str,
    provenance: list[dict[str, object]],
) -> dict[str, object]:
    """Build the FULL schema-2 ``kind: concept`` frontmatter (ADR-0041 D2; v1's ``type: theme``).

    The model DECIDES ``title``/``summary``/``status``/``tags``/``aliases``/``links``; the WORKER
    MATERIALIZES ``kind``/``kb``/``subjects`` (D2/D2.2), ``created``/``updated`` (== ``run_date``,
    ADR-0010 D1), ``sources`` (the provenance union, never the model), ``related`` (the plan
    ``links`` as ``"[[basename]]"`` tokens), ``origin`` (harvest only), ``confidence`` (MIRRORED
    from the candidate's worst-case value — NEVER decided by the model, ADR-0011 §2, so the backend
    can never inflate it) and ``body_status: pending`` when the note needs prose.

    ``subjects`` is seeded from the disposition's singular ``domain`` (OD-9: the plan wire keeps one
    subject; 0..n is an APPLY-and-human capability) and is ``[]`` when the disposition names none —
    which is what retires ADR-0022's ``domains[0]`` catch-all on the CLASSIFICATION leg: a concept
    lands at ``wiki/concepts/<slug>.md`` whether or not its subject is known, so nothing is ever
    dropped for lack of a domain and nothing has to ASSERT a possibly-false one (D2.2 legs 1 + 2).

    The per-kind additions carry over from v1 unchanged in shape and order.
    """
    status = disp.status or "active"
    summary = disp.summary or ""
    fm = _common_frontmatter(
        title=disp.title or disp.basename or disp.candidate_id,
        kind="concept",
        kb=kb,
        subjects=subjects,
        aliases=list(disp.aliases),
        tags=list(disp.tags),
        run_date=run_date,
        status=status,
        summary=summary,
        provenance=provenance,
    )
    fm["sources"] = list(sources)
    fm["related"] = [f"[[{link}]]" for link in disp.links]
    if origin is not None:
        fm["origin"] = origin
    fm["confidence"] = confidence
    if disp.needs_prose:
        fm["body_status"] = "pending"
    return fm


def _journal_frontmatter(
    disp: Disposition,
    *,
    kb: str,
    run_id: str,
    run_date: str,
    subjects: list[str],
    sources: list[str],
    provenance: list[dict[str, object]],
) -> dict[str, object]:
    """Build the schema-2 ``kind: note`` journal frontmatter (ADR-0041 D2/D2.6; v1's ``daily``).

    ONE journal per ``run_date``, repo-wide (D2.6), so ``date``/``run_id`` come from the injected
    run and the note<->``run_id`` relation is 1:1. ``subjects`` is the UNION of the contributing
    dispositions' domains, accumulated across the appends this run makes into the same file — the
    journal is the one note schema 2 genuinely makes multi-subject, because the domain that used to
    fan it out into one file per domain is gone from the path (D2.6 / D6 step 4).
    """
    summary = disp.summary or ""
    fm = _common_frontmatter(
        title=disp.title or f"Daily {run_date}",
        kind="note",
        kb=kb,
        subjects=subjects,
        aliases=list(disp.aliases),
        tags=list(disp.tags),
        run_date=run_date,
        status=disp.status or "active",
        summary=summary,
        provenance=provenance,
    )
    fm["date"] = run_date
    fm["run_id"] = run_id
    fm["sources"] = list(sources)
    if disp.needs_prose:
        fm["body_status"] = "pending"
    return fm


# --- region rendering -------------------------------------------------------------------------


def _empty_body_region(sentinel_id: str) -> str:
    """Render a fresh, sentinel-wrapped body region with a placeholder line (PASS 2 fills it).

    ``sentinel_id`` is the FINAL persisted region id (the run-scoped :func:`region_sentinel_id`
    value at every placement site), never the bare per-run candidate_id — so a MERGE / cross-run
    APPEND_DAILY into a note already holding a prior-run region never collides on the bare id.
    """
    start, end = body_sentinels(sentinel_id)
    return f"{start}\n{_BODY_PLACEHOLDER}\n{end}"


def _contested_callout(disp: Disposition, *, run_date: str, sources: list[str]) -> str:
    """Render the §2.1 ``> [!contested]`` callout block byte-for-byte.

    One block per contesting claim. ``competing-basename`` is the FIRST plan link (a DIFFERENT note
    whose claim disagrees, §2.1); ``sources`` lists the new claim's event refs. The summary supplies
    the verbatim competing claim text. The first line matches the lint/dashboard detector
    ``^> \\[!contested\\]`` exactly. The caller (:func:`_apply_contested`) guarantees ``links`` is
    non-empty, so the competing basename is never a self-referential fallback.
    """
    competing = disp.links[0]
    claim_text = disp.summary or ""
    sources_line = ", ".join(sources)
    return (
        f"{_CONTESTED_CALLOUT_PREFIX} Competing claim (recorded {run_date})\n"
        f"> {claim_text}\n"
        f"> — see [[{competing}]] · sources: {sources_line}"
    )


# --- map / index maintenance (ADR-0041 D1.2/D1.3 children + the FROZEN §3.2 bullet grammar) ----
#
# The MOC's kind marker left the FILENAME for the DIRECTORY (`wiki/<domain>/<domain>-moc.md` ->
# `wiki/maps/<subject>.md`), which is the whole point of the flip (ADR-0041 D5) — so there is no
# `_moc_basename` any more: a map IS basenamed by its subject. The CHILD-BULLET GRAMMAR itself is
# retained BYTE-FOR-BYTE (D1.3): `- [Title](relative/path.md)` at indent 0, frontmatter `children:`
# staying `[[basename]]` and required to equal the child-bullet set exactly (L1-6). What changed is
# only the ADMITTED child set and the relative paths the bullets carry.


def _note_subjects(path: Path) -> list[str]:
    """Return a note's ``subjects:`` list, TOLERANTLY (ADR-0041 D2.2/D3.2).

    This is the one place APPLY asks "which subject is this note filed under?", and after the flip
    the answer comes from FRONTMATTER and nothing else — no code derives a subject from a path
    (D3.2), which is exactly what lets a concept sit in a free sub-folder (D1.1) without changing
    what it is about.

    Unreadable / unparseable / non-list values degrade to ``[]`` rather than raising, matching
    :func:`_note_title`'s posture at the sibling read: one malformed foreign note in
    ``wiki/concepts/`` must not fail a whole curator run over a map it is not even a member of.
    Membership is a pure function of the worktree at APPLY time, so the map stays deterministic.
    """
    try:
        fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
    except (FrontmatterError, OSError, ValueError):
        return []
    return _str_list(fm.get("subjects"))


def _note_is_derived(path: Path) -> bool:
    """Return a note's ``derived:`` flag, TOLERANTLY (ADR-0041 D2.4).

    ``derived: true`` marks the PROPOSAL/derivation plane: an artifact something computed, not a
    curated claim. D2.4's day-1 semantics are that such a note *"is never a ``MERGE_INTO_THEME``
    target"* — merging would union ``sources:`` and append prose into the proposal plane, which is
    exactly the boundary the flag draws. No day-1 producer sets it, so this is unreachable through
    the curator today; it IS reachable by the human frontmatter edit D2.2 explicitly contemplates,
    which is why the rule lives at the write site rather than in a comment.

    Same posture as :func:`_note_subjects`: unreadable / unparseable / non-bool degrades to
    ``False`` (a note nobody can prove is derived is treated as an ordinary one), so a single
    malformed foreign note never fails a whole run.
    """
    try:
        fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
    except (FrontmatterError, OSError, ValueError):
        return False
    return fm.get("derived") is True


def _rel_posix(worktree: Path, path: Path) -> str:
    """Return ``path`` as a repo-relative POSIX string — the form link targets are built from."""
    return path.relative_to(worktree).as_posix()


def _notes_under(worktree: Path, directory: str) -> dict[str, str]:
    """Return ``{basename: repo-relative POSIX path}`` for every ``.md`` under ``wiki/<directory>``.

    ``rglob`` rather than ``glob`` because ADR-0041 D1.1 permits free sub-folders under a kind and
    *"no code reads the intermediate segments"* — a concept a human filed at
    ``wiki/concepts/engineering/team/foo.md`` is still a concept, still basename-identified, and
    still a legitimate map child. Sorted so a later basename collision resolves deterministically
    (the first path in sort order wins) rather than by filesystem iteration order.
    """
    root = _contained(worktree, worktree / "wiki" / directory)
    found: dict[str, str] = {}
    if not root.is_dir():
        return found
    for child in sorted(root.rglob("*.md")):
        if child.is_file():
            found.setdefault(child.stem, _rel_posix(worktree, child))
    return found


# --- ADR-0014 D3: standard-markdown BODY graph links -------------------------------------------


def _note_title(path: Path, fallback: str) -> str:
    """Return a note's frontmatter ``title`` for use as markdown-link TEXT, else ``fallback``.

    ADR-0014 D3 emits the MOC/index BODY child bullets as standard markdown links
    ``[Title](relative.md)`` — the link TEXT is the CHILD note's ``title`` (read from its
    frontmatter), so the rendered link is human/Obsidian/git-friendly while the basename remains the
    internal identity (parsed back from the link path, ADR-0010 D5). DEFENSIVELY falls back to
    ``fallback`` (the child basename) when the note is absent, unreadable, has no frontmatter, or
    its ``title`` is missing/non-string — the link still resolves by basename, only the display text
    degrades. This read is a pure function of the worktree at APPLY time (deterministic).
    """
    if not path.is_file():
        return fallback
    try:
        fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
    except (FrontmatterError, OSError):
        return fallback
    title = fm.get("title")
    return title if isinstance(title, str) and title else fallback


def _link_text(title: str, *, fallback: str) -> str:
    """Sanitize a note ``title`` for safe use as markdown-link TEXT (ADR-0014 D3 / ADR-0010 D5).

    The MOC/index body child bullet is emitted as ``[<text>](<path>.md)`` and MUST round-trip
    through the FROZEN ``_CHILD_BULLET_RE`` (``schema/notes.py``) whose link-text class
    ``[^\\]\\r\\n]*`` forbids ``]`` and any newline. A note ``title`` is model-decided
    (``disp.title``) and so may legally contain ``]``, ``[``, ``\\r`` or ``\\n`` (all valid YAML
    scalars) — interpolating it raw would emit a bullet the curator's own L1-6/L1-2 lint can no
    longer parse, dropping the child from ``child_bullets`` / ``body_link_basenames`` and the
    read-path graph seed (ADR-0012). We therefore DROP the bracket characters that would terminate
    the text group early and COLLAPSE any ``\\r``/``\\n`` to a single space, falling back to the
    child basename if nothing survives. Only the human-readable TEXT is touched; the basename in the
    PATH (slug-constrained) is never at risk, so emit->parse still recovers the same basename and
    the round-trip identity is preserved.
    """
    text = title.replace("]", "").replace("[", "").replace("\r", " ").replace("\n", " ").strip()
    return text if text else fallback


def _child_link(base: str, note_rel: str, *, from_dir: str, worktree: Path) -> str:
    """Render one map/index child bullet as ``- [Title](<relative path>)`` (ADR-0014 D3 / D1.3).

    ``note_rel`` is the CHILD's repo-relative POSIX path and ``from_dir`` the LINKING note's
    repo-relative directory (``""`` for the root ``index.md``, ``wiki/maps`` for a map), so the
    emitted target is the POSIX-relative path between them — ``../concepts/x.md`` from a map,
    ``wiki/maps/x.md`` from the index. No leading ``/`` or ``./``: that is the form that resolves in
    BOTH Obsidian and an OKF bundle. The link TEXT is the child's ``title`` (basename fallback), and
    the child BASENAME is recoverable from the path regardless of how deep the child sits
    (``_basename_from_link_path``), which is what keeps L1-6 / L1-2 / the read-path graph seed total
    under D1.1's free sub-folders.

    ``_contained`` here is defence-in-depth, not the primary gate: ``note_rel`` is always either a
    live on-disk path from :func:`_notes_under`\'s walk or a path this run just composed through
    :func:`_note_path`, so an escaping token cannot reach this READ. Kept anyway so a future caller
    does not have to re-derive that reasoning to stay safe.
    """
    title = _link_text(_note_title(_contained(worktree, worktree / note_rel), base), fallback=base)
    target = posixpath.relpath(note_rel, from_dir) if from_dir else note_rel
    return f"- [{title}]({target})"


# --- the deterministic APPLY (ADR-0011 §3) -----------------------------------------------------


def apply_plan(
    plan: Plan,
    *,
    worktree: Path,
    run_date: str,
    provenance: dict[str, list[dict[str, object]]],
    confidence: dict[str, str] | None = None,
    attachments_dir: Path | None = None,
) -> dict[str, bytes]:
    """Materialize a validated ``plan`` into the ``worktree`` deterministically (ADR-0011 §3).

    Returns ``{raw_ref: exact_bytes}`` for every engine-WRITTEN canonical ``raw/`` source this run
    materialized (ADR-0010 D3) — the EXACT path-and-content set the worker's final-diff gate admits
    into the curated diff. Any OTHER ``raw/`` change in the committed tree (a brain-planted file, or
    a PASS-2 overwrite of one of these — same path, different content) is therefore rejected, so the
    brain still never writes ``raw/``.

    ``attachments_dir`` is where the claimed events' staged bytes live — ``<processing>/<run-id>/
    events/_attach/`` (ADR-0041 D4.2). Every attachment a provenance tuple declares is materialized
    into ``raw/_blob/<ab>/<sha256>.<ext>`` with its ``.meta.yaml`` sidecar, BOTH recorded in the
    returned set and the BLOB (never the sidecar) cited beside the text evidence in ``sources:``.
    ``None`` is for callers with no staging area — a direct unit-test APPLY — and is safe exactly
    while no tuple declares an attachment; one that does raises :class:`ApplyError` rather than
    publishing a citation to bytes that were never written.

    ``confidence`` maps ``candidate_id -> worst-case confidence`` (``high|medium|low``, ADR-0011
    §2 / DATA-MODEL §1). APPLY MIRRORS it onto the materialized note (it is NOT a plan field, so the
    backend can never inflate it); a candidate omitted from the map falls back to the conservative
    :data:`_DEFAULT_CONFIDENCE`. The WORKER computes this worst-case value across merged events —
    the model never supplies it.

    The WORKER performs ALL structural mutation so correctness is by construction, not by post-hoc
    rejection. For each disposition, in the KB WIKI SCHEMA 2 layout (ADR-0041 D1):

    * ``CREATE_THEME`` — create ``wiki/concepts/<basename>.md`` (the kind is the DIRECTORY; the
      subject has left the path for ``subjects: [<domain>]``, D2.2) with the FULL D2 frontmatter +
      an ``agora:body`` sentinel pair keyed by ``candidate_id`` when ``needs_prose``; add the
      concept to its subject's map and the map to the root index.
    * ``APPEND_DAILY`` — create-or-append ``wiki/notes/<yyyy>/<mm>/<run_date>.md``: ONE journal per
      ``run_date``, REPO-WIDE (D2.6), so dispositions from several domains append ``## <run_date>``
      sections to the SAME file in domain order, unioning ``sources:`` and ``subjects:``. A
      disposition that does NOT flag ``needs_prose`` contributes its provenance only — the ``raw/``
      capture and the day's ``sources:`` union — and no body section (#131); the worker reports that
      under-delivery on :attr:`~agora_kb.curator.worker.RunReport.warnings` rather than inventing
      prose.
    * ``MERGE_INTO_THEME`` — union this run's provenance into the target concept's ``sources:``
      (never drop prior), bump ``updated``, insert ``links``, and append a NEW sentinel augmentation
      sub-region below existing prose (never rewrite prior prose).
    * ``MARK_CONTESTED`` — set ``status: contested`` + ``contested_by`` + ``contested_at`` ==
      ``run_date`` and render the ``> [!contested]`` callout (§2.1).
    * ``DROP`` / ``NOOP`` — no wiki edit.

    Reads NO wall clock (``run_date`` is injected). Writes under the §4.0 curated allowlist PLUS the
    immutable canonical ``raw/<domain>/<event_id>.md`` source for every cited free-text capture —
    UNMOVED and byte-identical to schema 1, because ADR-0041 D1.4/D3.4 never re-paths ``raw/``.
    ``plan`` is assumed §4.1-valid; a precondition violation raises :class:`ApplyError`, including
    a schema-2 repo with no ``_meta/kb.yaml`` to stamp into ``kb:``.
    """
    run_id = plan.run_id
    conf_map = confidence or {}

    # The KB identity every note mirrors into `kb:` (ADR-0041 D1.5/D2). Resolved ONCE, and only
    # when this plan actually materializes something: a DROP/NOOP-only plan writes no note, so it
    # has no `kb:` to stamp and must not be failed over a file it never needed. Resolving it once
    # (rather than per note) also makes the identity a single fact about the run — two notes from
    # one run can never name two knowledge bases.
    kb = _kb_id(worktree) if any(d.op not in ("DROP", "NOOP") for d in plan.dispositions) else ""

    # The EXACT set of engine-written canonical raw/ sources (ref -> exact BYTES) materialized
    # this run, accumulated across every disposition's _sources_union. Returned so the worker's
    # final-diff gate admits ONLY these exact paths-with-content (ADR-0010 D3); anything else under
    # raw/ in the committed tree is a brain write and is rejected off-allowlist.
    raw_writes: dict[str, bytes] = {}

    # Track subject -> concept basenames added this run, so map/index maintenance is a single pass
    # at the end (idempotent set-union with whatever concepts of that subject already live in the
    # tree). A concept with NO subject (`subjects: []`, D2.2) joins no map: it is filed, searchable
    # and linkable — it simply has no map to be a child of, which is an L2 orphan health signal and
    # never a lost fact.
    created_concepts_by_subject: dict[str, set[str]] = {}

    # Group APPEND_DAILY dispositions so their sections land in a stable order in the ONE journal.
    daily_dispositions: list[Disposition] = []

    for disp in plan.dispositions:
        prov = provenance.get(disp.candidate_id, [])
        conf = conf_map.get(disp.candidate_id, _DEFAULT_CONFIDENCE)
        if disp.op == "CREATE_THEME":
            _apply_create_concept(
                disp,
                worktree=worktree,
                kb=kb,
                run_id=run_id,
                run_date=run_date,
                provenance=prov,
                confidence=conf,
                raw_writes=raw_writes,
                attachments_dir=attachments_dir,
            )
            if disp.domain and disp.basename:
                created_concepts_by_subject.setdefault(disp.domain, set()).add(disp.basename)
        elif disp.op == "APPEND_DAILY":
            daily_dispositions.append(disp)
        elif disp.op == "MERGE_INTO_THEME":
            _apply_merge(
                disp,
                worktree=worktree,
                kb=kb,
                run_id=run_id,
                run_date=run_date,
                provenance=prov,
                raw_writes=raw_writes,
                attachments_dir=attachments_dir,
            )
        elif disp.op == "MARK_CONTESTED":
            _apply_contested(
                disp,
                worktree=worktree,
                kb=kb,
                run_date=run_date,
                provenance=prov,
                raw_writes=raw_writes,
                attachments_dir=attachments_dir,
            )
        elif disp.op in ("DROP", "NOOP"):
            # No note, therefore no `sources:`, therefore no provenance materialisation at all —
            # neither the free-text `raw/<domain>/<event_id>.md` nor, since ADR-0041 D4.2, an
            # attachment's `raw/_blob/` bytes. That parity is deliberate: `raw/` holds the evidence
            # a published note CITES, and an uncited blob would be a file the final diff admits and
            # lint L1-8 can never account for. The artefact is not destroyed — it drains to
            # `_kb/processed/<date>/_attach/` with its event and is never pruned — but that spool is
            # git-ignored, so a DROPped capture's bytes never enter the committed tree. Documented
            # in DATA-MODEL §1/§2 and INGEST-CONTRACT §0.1 rule 2 rather than left to be discovered.
            continue
        else:  # pragma: no cover — §4.1 CLOSED-VOCAB makes this unreachable for a valid plan
            raise ApplyError(f"candidate {disp.candidate_id!r}: unknown op {disp.op!r}")

    # APPEND_DAILY section order: DOMAIN-major (ADR-0041 D6 step 4's merge shape — the one journal
    # now collects several domains, so the domain is the outer, human-legible ordering), then §3.1's
    # stable manifest-event order WITHIN a domain, which is byte-identical to v1 for the
    # single-domain case. Both keys are curator-owned facts, so the order is deterministic.
    daily_dispositions.sort(
        key=lambda d: (d.domain or "", d.event_ids[0] if d.event_ids else "", d.candidate_id)
    )
    for disp in daily_dispositions:
        _apply_append_journal(
            disp,
            worktree=worktree,
            kb=kb,
            run_id=run_id,
            run_date=run_date,
            provenance=provenance.get(disp.candidate_id, []),
            raw_writes=raw_writes,
            attachments_dir=attachments_dir,
        )

    # Map + root-index maintenance for every subject that gained a concept this run. The map is
    # created LAZILY, at the first concept of its subject (D1.3): a subject with no concepts has no
    # map, so `wiki/maps/` never accrues empty navigation.
    for subject, basenames in sorted(created_concepts_by_subject.items()):
        _update_map(subject, basenames, worktree=worktree, kb=kb, run_date=run_date)
    if created_concepts_by_subject:
        _update_index(set(created_concepts_by_subject), worktree=worktree, kb=kb, run_date=run_date)

    return raw_writes


def _apply_create_concept(
    disp: Disposition,
    *,
    worktree: Path,
    kb: str,
    run_id: str,
    run_date: str,
    provenance: list[dict[str, object]],
    confidence: str,
    raw_writes: dict[str, bytes],
    attachments_dir: Path | None,
) -> None:
    """Create ``wiki/concepts/<basename>.md`` with full D2 frontmatter + a body sentinel (D1/D2.2).

    Only ``basename`` is a precondition. The v1 requirement that a CREATE_THEME also carry a
    ``domain`` was a PATH requirement — ``wiki/<domain>/themes/`` had nowhere to put a note whose
    domain was unknown — and ADR-0041 D2.2 leg 1 retires exactly that: a concept lands at
    ``wiki/concepts/<slug>.md`` regardless of subject, so nothing can be dropped for lack of a
    domain because nothing needs a domain to have a path. A disposition with no domain is filed
    with ``subjects: []``, which is strictly more honest than v1's ``domains[0]`` floor (it asserts
    nothing rather than possibly-falsely asserting a subject) and strictly no-loss.

    The domain is still read for the ``raw/`` SHARD KEY (D2.2 leg 3): ``raw/`` does not move, so
    ``raw/<domain>/<event_id>.md`` needs a directory. That is the one place ``domains[0]`` survives,
    and it survives in the PLAN, not here.
    """
    if not disp.basename:
        raise ApplyError(f"candidate {disp.candidate_id!r}: CREATE_THEME requires a basename")
    sources = _sources_union(
        disp.domain,
        provenance,
        worktree=worktree,
        raw_writes=raw_writes,
        attachments_dir=attachments_dir,
    )
    origin = _is_harvest_origin(provenance)
    fm = _concept_frontmatter(
        disp,
        kb=kb,
        run_date=run_date,
        subjects=[disp.domain] if disp.domain else [],
        sources=sources,
        origin=origin,
        confidence=confidence,
        provenance=provenance,
    )
    if disp.needs_prose:
        body = _empty_body_region(region_sentinel_id(run_id, disp.candidate_id))
    else:
        body = ""
    path = _note_path(worktree, "concept", disp.basename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def _apply_append_journal(
    disp: Disposition,
    *,
    worktree: Path,
    kb: str,
    run_id: str,
    run_date: str,
    provenance: list[dict[str, object]],
    raw_writes: dict[str, bytes],
    attachments_dir: Path | None,
) -> None:
    """Create-or-append the ONE journal of this ``run_date``, repo-wide (ADR-0041 D2.6).

    The path is ``wiki/notes/<yyyy>/<mm>/<run_date>.md`` and BOTH the shard and the basename are
    composed from the injected ``run_date`` — never parsed back out of ``disp.basename``. That
    inversion is what D2.6 forbids: it would make a curator-owned, deterministic path segment a
    function of model output. The PLAN gate separately asserts ``disp.basename == run_date``, so a
    model that names the journal something else is rejected there rather than silently overridden
    here.

    v1 wrote one daily PER DOMAIN and namespaced the basename ``<domain>-YYYY-MM-DD`` because bare
    dates would collide across domains; with the domain out of the path that reason is gone, so
    dispositions from several domains now append ``## <run_date>`` sections to the same file and
    the note<->``run_id`` relation becomes 1:1.
    """
    path = _note_path(worktree, "note", run_date, run_date=run_date)
    # A body region is placed ONLY for a disposition flagged `needs_prose` — the placement rule
    # ADR-0011 §3 and INGEST-CONTRACT §3 already state ("place body sentinels ... for notes flagged
    # needs_prose"), and the SAME gate `_apply_create_concept` and `_apply_merge` apply. This
    # function placed one UNCONDITIONALLY while the worker's `_needs_prose_map` skips
    # non-`needs_prose` dispositions, so the region was built and then never handed to PASS 2
    # (#131). Two reachable damages, both reproduced live: alone it left a permanent
    # `_summary pending_` placeholder, and a run mixing a `needs_prose` True and False APPEND_DAILY
    # into ONE journal pinned `body_status: pending` FOREVER — `has_unauthored_region` stayed True
    # over the False region, so the #119 retraction could never fire on a note whose prose HAD all
    # landed.
    #
    # The dated heading goes with it: `## <run_date>` is the section's first line, not a separate
    # artifact (§7.1 calls this op's body deliverable "that section"), so with no prose there is no
    # section and no heading — otherwise every no-prose disposition accrues one empty heading.
    # `_sources_union` stays OUTSIDE the gate: the PROVENANCE half of the op is unconditional, so
    # the capture is still written to `raw/` and unioned into the day's `sources:`. This is a note
    # with no readable section, NOT a silently-dropped capture.
    #
    # The heading NAMES ITS CONTRIBUTOR — `## <run_date> · <domain>` — because D2.6 made the journal
    # multi-domain: several dispositions now append sections to the SAME note, and a bare
    # `## <run_date>` repeated N times is N byte-identical headings with nothing saying which
    # subject each one came from. The information exists at write time (the sections are already
    # ordered domain-major) and was simply not rendered. It also disambiguates the anchor:
    # `schema.notes.heading_slug` otherwise has to de-duplicate identical slugs positionally, so
    # every heading-keyed read into a journal is ambiguous. D2.6 leaves the heading TEXT unspecified
    # (it pins only the §3.1 "one `## ` section per needs_prose disposition" COUNT rule, which is
    # unchanged); the shape mirrors D6 step 4, where the importer's same-date daily merge heads each
    # merged section by its contributor. A domain-less disposition keeps the bare date.
    if disp.needs_prose:
        region = _empty_body_region(region_sentinel_id(run_id, disp.candidate_id))
        heading = f"## {run_date} · {disp.domain}" if disp.domain else f"## {run_date}"
        section = f"{heading}\n\n{region}"
    else:
        section = ""
    new_sources = _sources_union(
        disp.domain,
        provenance,
        worktree=worktree,
        raw_writes=raw_writes,
        attachments_dir=attachments_dir,
    )

    if path.is_file():
        # Append a new dated section, unioning the day's sources into frontmatter (keep prior).
        existing = path.read_text(encoding="utf-8")
        fm, prior_body = frontmatter.parse(existing)
        _canonicalize_dates(fm)
        merged_sources = _str_list(fm.get("sources"))
        for s in new_sources:
            if s not in merged_sources:
                merged_sources.append(s)
        fm["sources"] = merged_sources
        # The journal is the ONE note schema 2 genuinely makes multi-subject: several domains now
        # share one file, so `subjects:` unions rather than being set (D2.6 / D6 step 4).
        _stamp_schema2_base(fm, kb=kb, kind="note", subject=disp.domain, widen_subjects=True)
        # RE-STAMP `run_id`, and `date` with it. Under D2.6 there is ONE journal per run_date and a
        # second `agora curate` the same day APPENDS to it — an entry the ADR sanctions explicitly
        # (it is why ADR-0011 §4.1 check 5's "(daily exempt)" clause survives). Leaving the FIRST
        # run's `run_id` in place makes that second run fail its own §4.4 lint gate on L1-14
        # (`run_id … != injected run_id …`) over the very file it just appended to — i.e. the note
        # cannot satisfy the ruleset it is graded against. The only coherent reading of `run_id:`
        # on a note several runs may touch is "the last run that touched this journal", which keeps
        # D2.6's 1:1 note<->run relation true PER RUN. `date` is re-set for the same reason it is
        # the basename: the file is the journal OF this run_date, so an inherited stale `date` from
        # a mis-shaped prior note would fail the L1-14 structural half.
        fm["date"] = run_date
        fm["run_id"] = run_id
        _merge_agents(fm, provenance)
        _set_updated(fm, run_date)
        if disp.needs_prose:
            fm["body_status"] = "pending"
        # A provenance-only append (no section, #131) byte-preserves the body. `frontmatter.render`
        # rstrips trailing newlines, so the unguarded expression renders identically today — this is
        # explicit intent plus insurance against a renderer that stops rstripping.
        if not section:
            body = prior_body
        else:
            body = f"{prior_body}\n\n{section}" if prior_body else section
        path.write_text(frontmatter.render(fm, body), encoding="utf-8")
    else:
        fm = _journal_frontmatter(
            disp,
            kb=kb,
            run_id=run_id,
            run_date=run_date,
            subjects=[disp.domain] if disp.domain else [],
            sources=new_sources,
            provenance=provenance,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frontmatter.render(fm, section), encoding="utf-8")


def _stamp_schema2_base(
    fm: dict[str, object],
    *,
    kb: str,
    kind: str,
    subject: str | None = None,
    widen_subjects: bool = False,
) -> None:
    """Backfill the ADR-0041 D2 common base onto a note APPLY RE-RENDERS rather than creates.

    Every note APPLY touches must leave the touch carrying schema 2, or a MERGE into a note written
    by an importer (or edited by a human) would produce a note L1-4 rejects for a missing ``kind:``
    / ``kb:``. The posture is BACKFILL, never overwrite, and each choice is deliberate:

    * ``kind`` — and its derived ``type:`` mirror (OD-3) — is set to the kind of the DIRECTORY the
      note lives in, because the directory is authoritative (D2.1). Re-stamping rather than
      defaulting is deliberate: it repairs a note whose mirror had drifted or was a v1 ``type:``,
      which is the whole reason the mirror is safe to keep at all.
    * ``kb`` is written only when ABSENT. A note that already names a knowledge base names the one
      it came FROM (D1.5: "a note that is copied out still names its origin"), and silently
      re-identifying it would erase exactly the fact the field exists to carry.
    * ``subjects`` is seeded only when absent/empty, and widened only when ``widen_subjects`` is set
      (the journal case). D2.2 is explicit that *"a curator run writes at most one subject"*, so a
      MERGE from domain X into a concept already filed under Y does NOT quietly re-file it — the
      0..n capability is APPLY-and-human, not a per-merge side effect.
    * ``derived`` defaults to ``False`` and is never flipped: D2.4's ``true`` marks a proposal-plane
      artifact, and a curated note is by definition not one.
    * ``provenance`` is SEEDED (never replaced) through :func:`_merge_agents` with no new
      contributors, so the block exists in its D2.3 shape on every note APPLY re-renders. Without
      it the root ``index.md`` — re-rendered by ``_update_index``, which has no provenance of its
      own to merge — was the ONE curator-written note missing the D2 common base, and silently:
      lint's ``_check_provenance`` grades the block only when present. Set-union semantics make the
      call harmless everywhere else, including the sites that immediately merge real provenance.
    """
    fm["kind"] = kind
    fm["type"] = kind
    if not isinstance(fm.get("kb"), str) or not fm.get("kb"):
        fm["kb"] = kb
    subjects = _str_list(fm.get("subjects"))
    if subject:
        if not subjects:
            subjects = [subject]
        elif widen_subjects and subject not in subjects:
            subjects.append(subject)
    fm["subjects"] = subjects
    if not isinstance(fm.get("derived"), bool):
        fm["derived"] = False
    _merge_agents(fm, [])


def _shard_key(fm: dict[str, object], fallback: str | None) -> str | None:
    """Return the ``raw/<domain>/`` shard key for a note APPLY is editing (ADR-0041 D2.2 leg 3).

    ``raw/`` does NOT move (D1.4/D3.4), so ``raw/<domain>/<event_id>.md`` still needs a directory —
    the ONE place the v1 domain survives. For an existing note that directory is the note's own
    FIRST subject: the schema-1 code read the same fact out of the target's path (``wiki/<domain>/
    …``), and D3.2's rule for every such site is that the replacement reads ``subjects:``. Falling
    back to the disposition's ``domain`` keeps a subject-less target (``subjects: []``) behaving
    exactly as it does on the CREATE path, and ``None`` from both is handled by
    :func:`_sources_union` itself.

    **The subject is GRADED before it composes a path, and that is the substitution's load-bearing
    half.** The v1 source was charset-constrained BY CONSTRUCTION — a real directory component of
    the target's own path, which cannot contain a separator and cannot be ``..``. Frontmatter is
    not: ``subjects:`` is an arbitrary string list that a human edit, an import, or a MERGE into a
    foreign note can put anything into, and ``_sources_union`` turns it straight into
    ``raw/<subject>/<event_id>.md``. Without this grade a ``subjects: ['../wiki/concepts']`` target
    makes APPLY write an engine file OUTSIDE ``raw/`` — inside the worktree, so ``_contained``
    passes, and under a path that never matches the ``raw_writes`` key the final-diff authorship
    channel greps for. The rule is the plan's own closed rule for the same token
    (:func:`~agora_kb.core.pathsafe.is_safe_component` plus the D1.4 leading-``_`` reservation, so a
    subject named ``_blob``/``_pages`` cannot reach the content-addressed namespace either), and an
    ungradeable subject DEGRADES to ``fallback`` — the plan-graded domain — rather than raising: a
    note somebody hand-edited must not fail a whole run, it must simply not steer the write.
    """
    subjects = _str_list(fm.get("subjects"))
    first = subjects[0] if subjects else None
    if first is not None and _is_safe_shard_key(first):
        return first
    return fallback


def _is_safe_shard_key(token: str) -> bool:
    """True iff ``token`` is safe as the ``raw/<domain>/`` directory component (ADR-0041 D1.4).

    The same closed rule :func:`agora_kb.curator.plan._is_safe_basename` applies to a plan domain,
    restated here rather than imported because this grades a DIFFERENT input (frontmatter written
    by anyone) at the write site, where a gate cannot be bypassed by reaching :func:`apply_plan`
    some other way. Two controls: the closed Unicode-category allowlist (no separator, no ``..``,
    no leading dot, no controls/bidi/zero-width, no Windows-hostile characters or reserved device
    stems) and the leading-``_`` rejection that keeps ``raw/_blob`` / ``raw/_pages`` reserved.
    """
    return bool(token) and not token.startswith("_") and is_safe_component(token)


def _target_kind(worktree: Path, path: Path) -> str:
    """Return the DIRECTORY-derived kind of a MERGE/CONTEST target (ADR-0041 D2.1).

    ``_resolve_target_path`` has already restricted the match to :data:`_MERGE_TARGET_KINDS`, so
    :func:`~agora_kb.schema.notes.path_kind` is non-``None`` here; the fallback exists only so a
    future caller that skips that filter degrades to the commonest kind instead of crashing.
    """
    return path_kind(_rel_posix(worktree, path)) or "concept"


def _resolve_target_path(
    target_basename: str, worktree: Path, *, sourced_only: bool = True
) -> Path:
    """Return the live-tree path of an existing note basename, else raise.

    MERGE/MARK_CONTESTED edit an EXISTING note whose path is derived from the live tree at APPLY
    (not the plan, §4.1 / _implied_note_path). We search ``wiki/**`` for ``<basename>.md`` — the
    §4.1 BASENAME/PROVENANCE checks already guaranteed it exists and is unique.

    ``sourced_only`` restricts the match to the two SOURCED kinds — a note whose repo-relative path
    puts it under ``wiki/concepts/`` or ``wiki/summaries/`` (:data:`_MERGE_TARGET_KINDS`). Both ops
    are claim-bearing, so resolving to a journal, a map/index or a human-owned ``wiki/people/`` note
    must be rejected here as a precondition violation rather than mutating the wrong kind of note
    (the §4.1 BASENAME check only verifies existence, not kind). The kind comes from
    :func:`agora_kb.schema.notes.path_kind` — the DIRECTORY, which is authoritative (D2.1) — rather
    than from the target's own ``kind:``, which is a mirror a brain could have written a lie into.
    It additionally drops any candidate flagged ``derived: true`` — D2.4's day-1 semantics are that
    a proposal-plane artifact is never a ``MERGE_INTO_THEME`` target; see :func:`_note_is_derived`.

    The lookup compares ``p.name`` EXACTLY instead of interpolating the basename into an
    ``rglob`` PATTERN. A pattern would read the token as a glob: ``"*"`` matches every note in the
    tree and ``"[a-z]"`` matches a whole class, so a single crafted ``target_basename`` would WIDEN
    the search and — since the first sorted match wins — silently retarget the merge/contest write
    onto an unrelated note. Exact-name matching cannot widen, cannot traverse (``p.name`` never
    contains a separator, so ``"../x"`` matches nothing), and needs no ``glob.escape`` reasoning
    about which metacharacters today's charset happens to exclude. Cost is unchanged: ``rglob``
    walks the same directories either way.

    Not reachable today through the normal call path: PASS-1's §4.1 BASENAME check
    (:mod:`agora_kb.curator.plan`) already requires ``target_basename`` to be an existing concept
    basename in the live tree before :func:`agora_kb.curator.worker.run` ever calls
    :func:`apply_plan`, so a crafted glob token is rejected before reaching this lookup. The fix
    still holds independently of that gate (a caller that skips ``validate_plan``, or a repo that
    genuinely contains a note whose basename happens to be a glob-metacharacter string) — this is
    defence-in-depth at the write site, not a closure of a live hole.
    """
    wiki = _contained(worktree, worktree / "wiki")
    wanted = f"{target_basename}.md"
    matches = sorted(p for p in wiki.rglob("*.md") if p.name == wanted and p.is_file())
    if sourced_only:
        matches = [p for p in matches if path_kind(_rel_posix(worktree, p)) in _MERGE_TARGET_KINDS]
        # D2.4: a `derived: true` note is NEVER a MERGE_INTO_THEME target. The kind filter above is
        # DIRECTORY-derived and cannot see this — `derived:` is a plane marker, not a kind — so it
        # is read from the candidate's own frontmatter, tolerantly. Dropped from the match set (not
        # raised on) so the failure is the ordinary "not found as a concept/summary" precondition
        # the caller already handles, and so a second, non-derived note of the same basename still
        # resolves.
        matches = [p for p in matches if not _note_is_derived(p)]
    if not matches:
        raise ApplyError(
            f"target basename {target_basename!r} not found as a concept/summary in the live "
            f"worktree tree"
            if sourced_only
            else f"target basename {target_basename!r} not found in the live worktree tree"
        )
    # The match came from walking the tree, so its NAME is trusted — but a matched file reached
    # through a symlinked directory still resolves outside the worktree, and this path is about to
    # be read AND written by _apply_merge / _apply_contested. Prove containment before returning it.
    return _contained(worktree, matches[0])


def _apply_merge(
    disp: Disposition,
    *,
    worktree: Path,
    kb: str,
    run_id: str,
    run_date: str,
    provenance: list[dict[str, object]],
    raw_writes: dict[str, bytes],
    attachments_dir: Path | None,
) -> None:
    """Union provenance into the target's ``sources:`` + append an augmentation sub-region."""
    if not disp.target_basename:
        raise ApplyError(f"candidate {disp.candidate_id!r}: MERGE_INTO_THEME requires target")
    path = _resolve_target_path(disp.target_basename, worktree)
    fm, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    _canonicalize_dates(fm)

    # The raw/ SHARD KEY, re-expressed for schema 2. Under v1 it was read out of the target's live
    # PATH (`wiki/<domain>/…`); ADR-0041 D3.2 abolishes path-derived subjects and the path no
    # longer carries one — so the same fact is read from the target's own `subjects:`, which is
    # exactly the "replacements read `subjects:`" substitution D3.2 prescribes for its three
    # sibling call sites. `raw/<domain>/<event_id>.md` is UNMOVED (D1.4/D3.4), so this keeps every
    # `sources:` string derivable and lint L1-8 satisfiable.
    target_domain = _shard_key(fm, disp.domain)
    new_sources = _sources_union(
        target_domain,
        provenance,
        worktree=worktree,
        raw_writes=raw_writes,
        attachments_dir=attachments_dir,
    )
    merged = _str_list(fm.get("sources"))
    for s in new_sources:
        if s not in merged:
            merged.append(s)
    fm["sources"] = merged
    # The kind comes from the resolved DIRECTORY, never a literal: `_resolve_target_path` admits
    # BOTH sourced kinds, so hard-coding `concept` would stamp `kind: concept` onto a note in
    # `wiki/summaries/` the day OD-7's producer lands — an L1-11 hard reject the curator wrote
    # itself. The directory is authoritative (D2.1), so it is also the right thing to mirror.
    _stamp_schema2_base(fm, kb=kb, kind=_target_kind(worktree, path), subject=disp.domain)
    _merge_agents(fm, provenance)
    _set_updated(fm, run_date)

    # MERGE_INTO_THEME is the ONLY op a gated/harvested candidate may use to ADD content (§6), so it
    # is the primary loop-prevention site: a kept-via-merge harvested region tags the target
    # ``origin: harvest:<agent>`` (§2 / §6 / DATA-MODEL §7). Set-union: only ADD a harvest origin
    # when the note has none yet, never overwrite a pre-existing origin (the model never writes it).
    _stamp_harvest_origin(fm, provenance)

    # Insert plan links into related (set-union, materialized as "[[basename]]"), never dropping.
    if disp.links:
        related = _str_list(fm.get("related"))
        for link in disp.links:
            token = f"[[{link}]]"
            if token not in related:
                related.append(token)
        fm["related"] = related

    if disp.needs_prose:
        fm["body_status"] = "pending"
        augmentation = _empty_body_region(region_sentinel_id(run_id, disp.candidate_id))
        body = f"{body}\n\n{augmentation}" if body else augmentation
    path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def _apply_contested(
    disp: Disposition,
    *,
    worktree: Path,
    kb: str,
    run_date: str,
    provenance: list[dict[str, object]],
    raw_writes: dict[str, bytes],
    attachments_dir: Path | None,
) -> None:
    """Set the §2.1 contested frontmatter + render the ``> [!contested]`` callout on the target."""
    if not disp.target_basename:
        raise ApplyError(f"candidate {disp.candidate_id!r}: MARK_CONTESTED requires target")
    # The §2.1 contested shape requires ≥1 competing basename: ``contested_by`` must be non-empty
    # (ADR-0010 L1-10) and the callout cites a DIFFERENT note. A §4.1-valid MARK_CONTESTED carries
    # it in ``links``; an empty ``links`` is a precondition violation (it would produce a self-link
    # and an empty ``contested_by``), so surface it at APPLY rather than fabricating a self-link.
    if not disp.links:
        raise ApplyError(
            f"candidate {disp.candidate_id!r}: MARK_CONTESTED requires at least one competing "
            f"basename in links (the contested shape needs a non-empty contested_by)"
        )
    path = _resolve_target_path(disp.target_basename, worktree)
    fm, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    _canonicalize_dates(fm)

    # Same schema-2 shard-key derivation as MERGE: the target's own `subjects:`, never its path.
    new_sources = _sources_union(
        _shard_key(fm, disp.domain),
        provenance,
        worktree=worktree,
        raw_writes=raw_writes,
        attachments_dir=attachments_dir,
    )

    # Union the new claim's provenance into sources (keep BOTH; contested needs >=2 sources, §2.1).
    merged = _str_list(fm.get("sources"))
    for s in new_sources:
        if s not in merged:
            merged.append(s)
    fm["sources"] = merged
    # The kind comes from the resolved DIRECTORY, never a literal: `_resolve_target_path` admits
    # BOTH sourced kinds, so hard-coding `concept` would stamp `kind: concept` onto a note in
    # `wiki/summaries/` the day OD-7's producer lands — an L1-11 hard reject the curator wrote
    # itself. The directory is authoritative (D2.1), so it is also the right thing to mirror.
    _stamp_schema2_base(fm, kb=kb, kind=_target_kind(worktree, path), subject=disp.domain)
    _merge_agents(fm, provenance)

    # status: contested + contested_by (set-union, never replaced) + contested_at == run_date.
    fm["status"] = "contested"
    # contested_by is the set-union of any prior value with this run's competing basenames (the
    # plan links); never replaced (§2.1). An empty list would later fail lint L1-10 (the full
    # contested-shape conjunction) rather than be silently accepted here.
    contested_by = _str_list(fm.get("contested_by"))
    for link in disp.links:
        if link not in contested_by:
            contested_by.append(link)
    fm["contested_by"] = contested_by
    fm["contested_at"] = run_date
    _set_updated(fm, run_date)

    # A harvested contradicting claim folded into the target tags it origin: harvest:<agent> for
    # loop-prevention (same rule as MERGE; §2 / §6 / DATA-MODEL §7). Set-union; never overwrite.
    _stamp_harvest_origin(fm, provenance)

    callout = _contested_callout(disp, run_date=run_date, sources=new_sources)
    body = f"{body}\n\n{callout}" if body else callout
    path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def _assert_map_basename_free(subject: str, *, worktree: Path) -> None:
    """Refuse to MINT ``wiki/maps/<subject>.md`` when another note already carries that basename.

    The APPLY-side half of the collision class ADR-0041 D1.3 creates by dropping v1's ``-moc``
    filename suffix: the map's basename is now the bare subject, so it shares one namespace with
    every concept — and L1-1 (global basename uniqueness, minus the D3.3 ``wiki/people/`` carve-out)
    turns the overlap into a hard lint failure of the WHOLE run.

    The PLAN gate (``plan.validate_plan`` check 5) reserves the declared domains against new
    basenames, which is where the class is closed for anything the curator itself proposes. This is
    the belt to that brace, and it covers the inputs the plan gate structurally cannot see: a
    concept named after a domain that a HUMAN, an importer, or a pre-reservation build already put
    in the tree. Without it the first concept filed under that subject mints the collision and the
    run dies at the §4.4 lint gate with two symmetric ``L1-1 duplicate basename`` findings that name
    neither the disposition that caused it nor the map that appeared — and, because the tree keeps
    the concept, EVERY later run touching that subject fails identically until a human renames it.
    Failing HERE instead names the cause once, before the write, exactly as ADR-0041 D6 step 7 makes
    a conversion-introduced basename collision a hard failure with a named list rather than a
    silent rename.

    ``wiki/people/**`` is excluded because people basenames are outside the global ``[[basename]]``
    identity space altogether (D3.3) — L1-1 excludes them, so they cannot collide.
    """
    wanted = f"{subject}.md"
    taken: list[str] = []
    # The root `index.md` is the one note that lives outside `wiki/` (D1.2), so the tree walk below
    # cannot see it; a subject literally named `index` would mint `wiki/maps/index.md` beside it.
    if subject == "index" and (worktree / "index.md").is_file():
        taken.append("index.md")
    wiki = _contained(worktree, worktree / "wiki")
    if wiki.is_dir():
        for candidate in sorted(wiki.rglob("*.md")):
            if candidate.name != wanted or not candidate.is_file():
                continue
            rel = _rel_posix(worktree, candidate)
            if is_people_path(rel):
                continue
            taken.append(rel)
    if taken:
        raise ApplyError(
            f"map basename collision: wiki/maps/{subject}.md cannot be created because "
            f"{', '.join(taken)} already carries the basename {subject!r} — one basename, one note "
            f"(L1-1). Rename that note (or the subject) before filing concepts under it "
            f"(ADR-0041 D1.3)"
        )


def _update_map(
    subject: str,
    new_basenames: set[str],
    *,
    worktree: Path,
    kb: str,
    run_date: str,
) -> None:
    """Create-or-update ``wiki/maps/<subject>.md`` so children == the concepts of that subject.

    The map is created LAZILY, at the first concept of its subject (ADR-0041 D1.3), and its
    children are the UNION of every concept in the tree whose ``subjects:`` contains ``subject``
    and the ones created this run. Membership is read from FRONTMATTER, never from a path — the
    subject left the path entirely (D3.2), which is precisely what lets the same concept be a child
    here while sitting in a free sub-folder a human chose (D1.1).

    ``children:`` frontmatter is kept exactly equal to the body child-bullet BASENAME set (L1-6)
    and both are sorted, so the map is a deterministic function of its children. The child-bullet
    grammar is FROZEN (D1.3): ``- [Title](../concepts/<base>.md)`` at indent 0, the frontmatter
    ``children:`` staying ``"[[basename]]"`` wikilink strings (Obsidian-Properties-native; OKF
    preserves them). Both sides derive from the same sorted basename set.

    **Only concepts are listed**, and that is the day-1 population rather than the rule: D1.3 admits
    ``concept``/``summary``/``map`` as children and forbids ``note``/``person``, but
    ``wiki/summaries/`` ships EMPTY (OD-7 has no producer) and APPLY composes no nested map — so
    every child APPLY can author today is a concept, and every one of them is admitted by L1-24 by
    construction. Widening to summaries the day OD-7 lands is additive.
    """
    members = {
        base: rel
        for base, rel in _notes_under(worktree, KIND_DIRECTORIES["concept"]).items()
        if subject in _note_subjects(_contained(worktree, worktree / rel))
    }
    for base in new_basenames:
        members.setdefault(base, f"{_CONCEPTS_DIR}/{base}.md")
    children = sorted(members)

    map_path = _note_path(worktree, "map", subject)
    body = "\n".join(
        _child_link(b, members[b], from_dir=_MAPS_DIR, worktree=worktree) for b in children
    )
    if not map_path.is_file():
        _assert_map_basename_free(subject, worktree=worktree)
    if map_path.is_file():
        fm, _ = frontmatter.parse(map_path.read_text(encoding="utf-8"))
        _canonicalize_dates(fm)
        # A map's own `subjects:` is what ADR-0041 D5 reads for the ranking domain filter (the
        # successor to v1's "seed `<domain>-moc.md` if `<domain>` is declared"), so it is stamped,
        # not merely tolerated.
        _stamp_schema2_base(fm, kb=kb, kind="map", subject=subject)
        fm["children"] = [f"[[{b}]]" for b in children]
        _set_updated(fm, run_date)
        map_path.write_text(frontmatter.render(fm, body), encoding="utf-8")
    else:
        summary = f"Map of content for the {subject} subject."
        fm = _common_frontmatter(
            title=f"{subject} map",
            kind="map",
            kb=kb,
            subjects=[subject],
            aliases=[],
            tags=[],
            run_date=run_date,
            status="active",
            summary=summary,
            provenance=[],
        )
        fm["children"] = [f"[[{b}]]" for b in children]
        map_path.parent.mkdir(parents=True, exist_ok=True)
        map_path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def _update_index(
    subjects: set[str],
    *,
    worktree: Path,
    kb: str,
    run_date: str,
) -> None:
    """Create-or-update root ``index.md`` so its children list every map (ADR-0041 D1.2).

    ``index.md`` sits at the repo ROOT, not under ``wiki/maps/``, so the directory rule cannot name
    it: it carries ``kind: index``, is the only note basenamed ``index`` (L1-13) and has cardinality
    exactly one. It is the ROOT OF the map tier, which is why ``maps/`` hangs off it; it is not a
    member of it. Its children are the map basenames — the UNION of every map already present in
    the worktree and the ones touched this run (the ``-moc`` filename suffix is gone: the kind
    marker moved into the directory, D5).

    ADR-0014 D3 — the BODY child bullets are STANDARD MARKDOWN LINKS ``- [Title](wiki/maps/<b>.md)``
    (git + Obsidian + OKF native), and the ``children:`` FRONTMATTER stays ``"[[basename]]"``
    wikilink strings. Both derive from the same sorted map-basename set, so L1-6 holds by
    construction, and every child is a ``map`` — admitted by D1.3 / L1-24.
    """
    maps = _notes_under(worktree, KIND_DIRECTORIES["map"])
    for subject in subjects:
        maps.setdefault(subject, f"{_MAPS_DIR}/{subject}.md")
    children = sorted(maps)

    index_path = _note_path(worktree, "index", "index")
    body = "\n".join(_child_link(b, maps[b], from_dir="", worktree=worktree) for b in children)
    if index_path.is_file():
        fm, _ = frontmatter.parse(index_path.read_text(encoding="utf-8"))
        _canonicalize_dates(fm)
        # The root map is filed under NO subject: `subjects: []` asserts nothing and loses nothing
        # (D2.2), so no subject is passed and an existing list is left exactly as it is.
        _stamp_schema2_base(fm, kb=kb, kind="index")
        fm["children"] = [f"[[{b}]]" for b in children]
        _set_updated(fm, run_date)
        index_path.write_text(frontmatter.render(fm, body), encoding="utf-8")
    else:
        # The root index.md is the OKF BUNDLE ROOT (ADR-0014 D2): it ALONE carries ``okf_version``
        # (never on a concept/journal/map, per the OKF spec).
        summary = "Top map of content; links every map."
        fm = _common_frontmatter(
            title="Knowledge base index",
            kind="index",
            kb=kb,
            subjects=[],
            aliases=[],
            tags=[],
            run_date=run_date,
            status="active",
            summary=summary,
            provenance=[],
            okf_version=_OKF_VERSION,
        )
        fm["children"] = [f"[[{b}]]" for b in children]
        index_path.write_text(frontmatter.render(fm, body), encoding="utf-8")


# --- §4.6 stray-wikilink stripping -------------------------------------------------------------


def strip_stray_wikilinks(text: str, allowed: set[str]) -> str:
    """Strip every ``[[X]]`` whose key is not in ``allowed``, keeping the inner text (§4.6).

    Byte-deterministic: each ``[[X]]`` token (no nested brackets / newlines) whose RESOLVED key —
    the substring left of any ``|``, ASCII-stripped, matching
    :func:`agora_kb.schema.notes.wikilinks` normalization — is absent from ``allowed`` is replaced
    by its inner text (delimiters removed, meaning preserved), so PASS 2 can never introduce a
    dangling link (links are structure, owned by APPLY). A token whose key IS allowed is kept.

    The substitution is iterated to a FIXED POINT because nested/doubled delimiters can SYNTHESIZE a
    surviving link from a single pass: stripping the inner token of ``[[[[victim]]]]`` would leave
    ``[[victim]]`` — a brand-new stray link a single non-recursive ``re.sub`` never re-scans.
    Looping until the text stops changing guarantees no stray ``[[X]]`` survives. As a final
    invariant we then assert (via :func:`agora_kb.schema.notes.wikilinks`, the frozen grammar) that
    every surviving link key is in ``allowed``; this is the §4.6 "no dangling link is created"
    guarantee, and it uses the SAME matcher as the §4.2 detector so the two provably agree.

    The token grammar here intentionally matches :func:`agora_kb.schema.notes.wikilinks` (no nested
    brackets / newlines) rather than the looser ``\\[\\[([^\\]]*)\\]\\]`` literal once written in
    ADR-0011 §4.6, so stripping and detection share one definition (a divergence would let a stray
    link past one but not the other).
    """
    prev = None
    out = text
    while prev != out:
        prev = out
        out = _WIKILINK_TOKEN_RE.sub(lambda m: _strip_one(m, allowed), out)
    # Invariant: after reaching the fixed point, no surviving link key is outside ``allowed`` (the
    # §4.6 guarantee). A stray that somehow survives is re-stripped rather than smuggled through.
    while set(wikilinks(out)) - allowed:
        prev = out
        out = _WIKILINK_TOKEN_RE.sub(lambda m: _strip_one(m, allowed), out)
        if out == prev:  # pragma: no cover — the fixed-point loop already removes every stray token
            break
    return out


def _strip_one(m: re.Match[str], allowed: set[str]) -> str:
    """Replace ONE ``[[X]]`` token: keep it if its key is allowed, else drop the delimiters.

    The resolved key matches :func:`agora_kb.schema.notes.wikilinks` normalization (the substring
    left of any ``|``, ASCII-stripped) so the strip grammar and the §4.2 detector share one rule.
    """
    inner = m.group(1)
    key = inner.split("|", 1)[0].strip(" \t\r\n\f\v")
    if key in allowed:
        return m.group(0)  # a planned link — keep delimiters intact
    return inner  # stray — drop the [[ ]] delimiters, keep the inner text verbatim


# --- §4.2 AUTHOR-diff validation ---------------------------------------------------------------


def _extract_sentinel_regions(text: str) -> dict[str, str] | None:
    """Return ``{candidate_id: region_body}`` for the matched sentinel pairs in ``text``, or None.

    The region body is the text BETWEEN a ``start``/``end`` pair (exclusive of the marker lines).
    Returns ``None`` on any sentinel tampering: an unmatched start, an unmatched end, a duplicated
    id, or a nested/overlapping pair — so :func:`validate_author_diff` rejects the note. Lines are
    matched against the exact ``agora:body:start/end id=<cid>`` grammar; a malformed marker line is
    treated as ordinary content (and will surface as an out-of-sentinel edit if it differs).
    """
    regions: dict[str, str] = {}
    open_cid: str | None = None
    open_lines: list[str] = []
    for line in text.split("\n"):
        start = _SENTINEL_START_RE.match(line)
        end = _SENTINEL_END_RE.match(line)
        if start is not None:
            if open_cid is not None:
                return None  # nested/overlapping start before the prior end — tampering
            open_cid = start.group("cid")
            open_lines = []
            continue
        if end is not None:
            cid = end.group("cid")
            if open_cid is None or cid != open_cid:
                return None  # end with no matching open, or mismatched id — tampering
            if cid in regions:
                return None  # duplicated candidate-id region — tampering
            regions[cid] = "\n".join(open_lines)
            open_cid = None
            open_lines = []
            continue
        if open_cid is not None:
            open_lines.append(line)
    if open_cid is not None:
        return None  # an unmatched start — tampering
    return regions


def _split_frontmatter_and_body(text: str) -> tuple[str, str] | None:
    """Split a note into ``(frontmatter_block_including_fences, body)``, or None if no frontmatter.

    The frontmatter block is everything up to and including the closing ``---`` fence (so a §4.2
    frontmatter-change check is a byte comparison of that exact slice); the body is the remainder.
    Mirrors :func:`agora_kb.core.frontmatter.parse`'s fence rules without coercing the YAML, so the
    comparison is purely textual.
    """
    nl = text.find("\n")
    first = text if nl == -1 else text[:nl]
    if first.strip() != "---":
        return None
    rest = text[nl + 1 :] if nl != -1 else ""
    closing = re.search(r"^---[ \t]*$", rest, re.MULTILINE)
    if closing is None:
        return None
    fm_block = text[: nl + 1 + closing.end()]
    body = rest[closing.end() :]
    return fm_block, body


def validate_author_diff(
    *,
    changed_paths: list[str],
    per_file_old: dict[str, str],
    per_file_new: dict[str, str],
    sentinels: dict[str, set[str]],
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> list[str]:
    """Validate the PASS-2 AUTHOR diff (ADR-0011 §4.2); return failure messages (``[]`` iff clean).

    Accept ONLY edits inside the candidate-id-keyed body-sentinel regions of ``needs_prose`` notes
    and reject everything else. Pure + deterministic.

    Parameters
    ----------
    changed_paths:
        POSIX repo-relative paths the PASS-2 diff touched (git status ``A``/``M``/``D``). Any path
        not in ``sentinels`` is rejected (the model may only edit declared needs_prose notes).
    per_file_old / per_file_new:
        Pre-/post-PASS-2 full text of each changed file (``base``-state vs worktree-state).
    sentinels:
        ``{rel_path: {candidate_id, ...}}`` — the COMPLETE set of candidate-id body-sentinel regions
        currently present in each needs_prose note at base-state, NOT just this run's new regions.
        The validator enforces EXACT set equality (``set(regions) == sentinels[rel_path]``), so a
        multi-region note (e.g. a CREATE_THEME body from a prior run plus a MERGE augmentation
        appended this run) must list ALL live candidate ids here. Prior-run regions are expected to
        retain their sentinels (ADR-0011 §3) so re-authoring cannot drift them; a note may only
        change WITHIN these regions, and only these candidate ids may exist.
    max_body_bytes:
        Per-region UTF-8 byte bound (default :data:`DEFAULT_MAX_BODY_BYTES`).

    Checks (failures reported in ``changed_paths`` order, then a stable per-file order):

    1. only declared needs_prose notes changed; NO other file (incl. ``log.md`` — byte-identical to
       base, asserted explicitly);
    2. NO frontmatter change (byte-identical frontmatter block);
    3. ONLY sentinel-region bodies changed — the body OUTSIDE every sentinel region is
       byte-identical to base, no sentinel tampering, and only the DECLARED candidate-id regions
       exist;
    4. each region body within ``max_body_bytes``; valid UTF-8;
    5. NO new ``[[wikilink]]`` introduced beyond what the base region already contained (links are
       structure, owned by APPLY; stray links are stripped by :func:`strip_stray_wikilinks`).
    """
    errors: list[str] = []

    # log.md must be byte-identical to base throughout PASS 2 (§4.2 check 2 / §4.3 ordering).
    if "log.md" in changed_paths:
        old = per_file_old.get("log.md", "")
        new = per_file_new.get("log.md", "")
        if old != new:
            errors.append("log.md changed during PASS 2 (must be byte-identical to base_commit)")

    for path in changed_paths:
        if path == "log.md":
            continue  # handled above
        if path not in sentinels:
            errors.append(
                f"{path}: file changed during PASS 2 but is not a declared needs_prose note "
                f"(only sentinel body regions may change)"
            )
            continue

        old_text = per_file_old.get(path, "")
        new_text = per_file_new.get(path, "")

        # check 2 — frontmatter byte-identical.
        old_split = _split_frontmatter_and_body(old_text)
        new_split = _split_frontmatter_and_body(new_text)
        if old_split is None or new_split is None:
            errors.append(f"{path}: missing/malformed frontmatter block in the PASS-2 diff")
            continue
        old_fm, old_body = old_split
        new_fm, new_body = new_split
        if old_fm != new_fm:
            errors.append(
                f"{path}: frontmatter changed during PASS 2 (frontmatter is owned by APPLY)"
            )

        # check 3 — sentinel structure intact + only declared regions; out-of-region body unchanged.
        expected_cids = sentinels[path]
        old_regions = _extract_sentinel_regions(old_body)
        new_regions = _extract_sentinel_regions(new_body)
        if old_regions is None or new_regions is None:
            errors.append(f"{path}: sentinel tampering (unmatched/duplicated agora:body markers)")
            continue
        if set(new_regions) != set(old_regions) or set(new_regions) != expected_cids:
            errors.append(
                f"{path}: sentinel region set {sorted(new_regions)} != "
                f"expected {sorted(expected_cids)}"
            )
            continue

        # The body OUTSIDE every sentinel region must be byte-identical (replace each region body
        # with a fixed token so only out-of-region text is compared).
        if _strip_region_bodies(old_body) != _strip_region_bodies(new_body):
            errors.append(
                f"{path}: content outside the sentinel body regions changed during PASS 2"
            )

        # checks 4 + 5 — per region: byte bound, UTF-8, no NEW wikilinks beyond the base region.
        for cid in sorted(expected_cids):
            new_region = new_regions[cid]
            old_region = old_regions[cid]
            if len(new_region.encode("utf-8")) > max_body_bytes:
                errors.append(f"{path}: body region id={cid} exceeds {max_body_bytes} bytes")
            try:
                new_region.encode("utf-8").decode("utf-8")
            except UnicodeDecodeError:  # pragma: no cover — a Python str is always valid UTF-8
                errors.append(f"{path}: body region id={cid} is not valid UTF-8")
            old_links = set(wikilinks(old_region))
            new_links = set(wikilinks(new_region))
            stray = sorted(new_links - old_links)
            if stray:
                errors.append(
                    f"{path}: body region id={cid} introduced new wikilink(s) {stray} "
                    f"(links are owned by APPLY; strip stray links via strip_stray_wikilinks)"
                )

    return errors


# A token that cannot appear in a sentinel region body, used to blank out region bodies so the
# out-of-region byte comparison in check 3 ignores intentional in-region prose edits.
_REGION_BLANK = "\x00AGORA_REGION\x00"


def _strip_region_bodies(body: str) -> str:
    """Replace each sentinel region BODY with a fixed token; keep markers + out-of-region text.

    So two notes with identical structure but different in-region prose compare EQUAL outside the
    regions — the §4.2 check that "content outside the sentinel body regions is unchanged" reduces
    to a byte comparison of this normalized form.
    """
    out: list[str] = []
    inside = False
    for line in body.split("\n"):
        if _SENTINEL_START_RE.match(line):
            out.append(line)
            out.append(_REGION_BLANK)
            inside = True
            continue
        if _SENTINEL_END_RE.match(line):
            inside = False
            out.append(line)
            continue
        if not inside:
            out.append(line)
    return "\n".join(out)
