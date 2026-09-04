"""PASS-1 ``plan.json`` model + the §4.1 PLAN validator (ADR-0011, the INGEST contract).

This is the *first* deterministic gate of PLAN-APPLY-AUTHOR (ADR-0011 §4): the backend brain writes
ONLY a closed-vocabulary JSON plan in PASS 1, and the worker decides whether that plan is admissible
WITHOUT trusting the model. The whole point of the contract is that *success is a pure function* of
``(plan.json, git_diff, manifest, bundle, lint)`` — so this module is MODEL-FREE and fully
unit-testable against hand-authored plans.

Two pieces live here:

* :class:`Plan` / :class:`Disposition` — the typed mirror of the ``plan.json`` schema
  (DATA-MODEL §11.1). ``Plan.from_json`` parses + validates the JSON shape (raising a clear error on
  malformed JSON or an unknown ``schema_version``).
* :func:`validate_plan` — the ten §4.1 checks (PARSE+FINISHED, COVERAGE, CLOSED-VOCAB, TAXONOMY,
  BASENAME, PATH/ALLOWLIST, LINK-RESOLVABILITY, PROVENANCE, STATUS, CANDIDATE-GATE §6). It is a pure
  function of the plan plus the deterministic facts the worker computes about the run (the manifest
  event id set, the fixed taxonomy, the live basenames at ``base_commit``, and the gated-candidate
  set). It returns ``[]`` iff the plan is valid; otherwise a deterministically-ordered list of
  :class:`PlanError`.

The validator never touches disk or git: every external fact (``manifest_event_ids``,
``allowed_tags``, ``domains``, ``live_basenames``, ``gated_candidate_ids``) is injected by the
caller, so two independent implementations grade an identical plan identically.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ..core.layout import KIND_DIRECTORIES
from ..core.pathsafe import is_safe_component
from .constants import is_allowlisted_path

__all__ = [
    "Disposition",
    "Plan",
    "PlanError",
    "PlanParseError",
    "OPS",
    "CONTENT_OPS",
    "GATE_ALLOWED_OPS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "validate_plan",
]

# --- closed vocabulary (ADR-0011 §2) ----------------------------------------------------------

# The six allowed ops. Hard deletion of curated content does NOT exist in the vocabulary; link/MOC/
# index maintenance is a deterministic side-effect of CREATE/MERGE/CONTEST, never a standalone op.
Op = Literal[
    "CREATE_THEME",
    "APPEND_DAILY",
    "MERGE_INTO_THEME",
    "MARK_CONTESTED",
    "DROP",
    "NOOP",
]
OPS: frozenset[str] = frozenset(
    {"CREATE_THEME", "APPEND_DAILY", "MERGE_INTO_THEME", "MARK_CONTESTED", "DROP", "NOOP"}
)

# Ops that ORIGINATE or AUGMENT content and therefore must carry ≥1 event_id (PROVENANCE, §4.1.8).
CONTENT_OPS: frozenset[str] = frozenset(
    {"CREATE_THEME", "APPEND_DAILY", "MERGE_INTO_THEME", "MARK_CONTESTED"}
)

# Ops that carry a ``basename`` (a NEW note) vs a ``target_basename`` (an EXISTING note), §2 table.
_BASENAME_OPS: frozenset[str] = frozenset({"CREATE_THEME", "APPEND_DAILY"})
_TARGET_OPS: frozenset[str] = frozenset({"MERGE_INTO_THEME", "MARK_CONTESTED"})

# Daily ops are EXEMPT from global basename uniqueness (the daily basename is created-or-appended,
# DATA-MODEL §10 / ADR-0011 §3.1): a CREATE/APPEND collision check skips these.
_DAILY_OPS: frozenset[str] = frozenset({"APPEND_DAILY"})

# CANDIDATE GATE (§6 / §4.1.10): a gated candidate (kind=candidate OR confidence=low) may NEVER
# originate a theme or daily — only corroborate, contest, or drop.
GATE_ALLOWED_OPS: frozenset[str] = frozenset({"MERGE_INTO_THEME", "MARK_CONTESTED", "DROP"})

# STATUS (§4.1.9): the C1 enum (ADR-0010 D2). ``contested`` is emitted ONLY by MARK_CONTESTED (which
# materializes the §2.1 shape); a plain CREATE/MERGE disposition naming ``contested`` is rejected so
# the model cannot hand-roll a half-formed contested note. ``orphan``/``stale`` are never plan
# values.
_STATUS_VALUES: frozenset[str] = frozenset({"active", "stub", "contested", "deprecated"})

# The canonical INGEST allowlist (ADR-0011 §4.0, matching ADR-0008 §4 verbatim, minus the
# ADR-0041 D4.1 ``wiki/people/`` carve-out) is
# :func:`~agora_kb.curator.constants.is_allowlisted_path` — imported, never restated, so this
# check and the final-diff assertion cannot drift.
#
# A disposition's implied note path is always under ``wiki/`` (schema 2: ``wiki/concepts/`` or
# ``wiki/notes/<yyyy>/<mm>/``, ADR-0041 D1), so PATH/ALLOWLIST (§4.1.6) reduces to a CHARACTER rule
# on the tokens the path is composed from: each must be one safe path component (no separator, no
# ``..``, no leading dot), which is what stops a token from spelling its way out of ``wiki/`` into
# ``_kb/`` / ``_templates/`` / ``raw/`` / git internals / hooks.
#
# What a character rule canNOT do, stated so nobody re-derives a false guarantee from it:
#   * It cannot see the filesystem. A SYMLINK is an inode property, not a spelling — a perfectly
#     clean name can BE a symlink pointing anywhere. Symlink prevention lives in the curator's
#     FINAL-DIFF gates (``curator/worker.py``: the ``_is_engine_written_raw`` ``is_symlink`` check
#     and the ``_assert_final_diff_allowlisted`` A/M/R symlink reject, §4.5), which grade the
#     committed tree.
#   * It cannot bind a caller. It is a gate in THIS module, one call up from the writes; anything
#     that reaches ``apply_plan`` by another route skips it entirely. Containment at the write site
#     is ``apply.py``'s ``_contained`` (``resolve()`` + ``is_relative_to(worktree)``), which holds
#     independently of this rule — which is also why the rule can be widened for non-ASCII without
#     that widening becoming the escape.
#
# BOTH tokens — basename AND domain — are graded by :func:`_is_safe_basename` below (the ADR-0041
# D4.4 pathsafe swap). The domain used to keep a separate v1 ASCII rule
# (``\A[A-Za-z0-9][A-Za-z0-9._-]*\Z``) on the ground that it stays a ``raw/<domain>/`` shard
# segment; that rule is GONE, because under schema 2 the domain is no longer only a shard key:
# ``apply._update_map`` composes ``wiki/maps/<subject>.md`` from it through
# :meth:`~agora_kb.core.layout.RepoLayout.note_path_for` (D1.3), so the domain now names a NOTE and
# must be graded by the note composer's rule. Two graders over one token is how a plan the gate
# calls valid crashes APPLY: the ASCII regex admitted ``con`` (a Windows reserved device stem) and
# ``foo.md`` (an extension the composer appends itself), both of which
# ``_validate_note_basename`` hard-rejects; and it REJECTED the Korean domains #56/#57 exist for,
# which the composer accepts happily — a self-inflicted wedge where every disposition in such a
# domain fails forever. One rule, one spelling, both sides.
#
# The taxonomy loader enforces only the D1.4-layer-2 leading-underscore rejection
# (``config._checked_domains``), never a charset, so this stays the plan-side layer over a
# different input — as D1.4 requires the two layers to be.

# The ``YYYY-MM-DD`` run date. Schema 2 basenames the day's single journal with it and shards the
# path by its year and month (ADR-0041 D2.6), so the same literal is both the basename and the
# shard — never two independently-supplied facts that could disagree.
_RUN_DATE_RE = re.compile(r"\A(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\Z")

SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


def _is_safe_basename(token: str) -> bool:
    """True iff ``token`` is a safe, non-reserved single path component (ADR-0041 D4.4).

    Two controls, deliberately mirroring ``core.layout._validate_note_basename`` (the *composer*'s
    rule) rather than importing it: this module's contract is that it is a pure, self-contained
    grader — "two independent implementations grade an identical plan identically" — so the
    validator must not become a wrapper around the thing it validates. They share
    :func:`~agora_kb.core.pathsafe.is_safe_component`, which is where the character set actually
    lives, so the only duplicated line is the reservation.

    1. :func:`~agora_kb.core.pathsafe.is_safe_component` — the closed Unicode-CATEGORY allowlist
       that replaces v1's ASCII ``\\A[A-Za-z0-9][A-Za-z0-9._-]*\\Z``. Every property the regex
       bought is preserved *because the widening is an allowlist, not a denylist*: separators,
       ``..``, a leading dot, NUL, C0/C1 controls, bidi overrides, zero-width characters, the
       fullwidth solidus and the Windows-hostile ``<>:"|?*`` are unreachable without being
       enumerated, and a codepoint added by a future Unicode revision is excluded by default. It
       additionally rejects the Windows reserved device stems (``CON``, ``COM0``–``COM9``,
       ``LPT0``–``LPT9``, superscript forms included) that the ASCII regex admitted — a tightening
       shipped alongside the widening. What is new is that a Korean basename is now *admissible*,
       which is the whole point (#57).
    2. **No leading ``_``.** NORMATIVE (ADR-0041 D4.4): pathsafe puts ``_`` in its allowed extras
       and rejects only a leading ``.``, so ``is_safe_component('_blob')`` is ``True`` — while the
       ASCII regex's leading character class rejected it by construction. On that one character
       the swap is a LOOSENING, and it is the exact character the D1.4 ``raw/_blob`` /
       ``raw/_pages`` reservation depends on, so the rejection must exist *before* the regex is
       deleted. The rule is the PREFIX, not a list of the two reserved names: a name list would
       have to be widened by every future reservation, and the one that got forgotten would be the
       one that mattered. The taxonomy-side layer (L1-23 / ``config._checked_domains``) covers a
       different input and neither layer substitutes for the other.
    3. **No ``.md`` suffix.** The composer appends the extension itself and rejects a token that
       already carries one (``core.layout._validate_note_basename``), rather than stripping it —
       a silent strip would hide a caller passing a filename where a basename belongs. The rule has
       to be restated here for the same reason the reservation is: without it
       ``_is_safe_basename('foo.md')`` is ``True`` while ``note_path_for('concept', 'foo.md')``
       raises, so a plan the gate called valid crashes APPLY — and the reference brain's slugger
       genuinely produces ``foo.md`` from a title like "Foo.md".

    Grades the DOMAIN token too, not just the basename: under schema 2 ``apply._update_map``
    composes ``wiki/maps/<subject>.md`` from the domain, so it names a note and must clear the note
    composer's rule (see the module comment above :data:`_RUN_DATE_RE`).
    """
    return (
        bool(token)
        and not token.startswith("_")
        and not token.endswith(".md")
        and is_safe_component(token)
    )


class PlanParseError(ValueError):
    """Raised by :meth:`Plan.from_json` when ``plan.json`` is malformed or an unknown version.

    Distinct from a §4.1 :class:`PlanError`: this is a *parse-time* rejection (PASS-1 check 1's
    PARSE half), surfaced as an exception because there is no well-formed :class:`Plan` to grade.
    """


class Disposition(BaseModel):
    """One plan entry: the brain's decision for exactly ONE dedup'd candidate (ADR-0011 §2, §11.1).

    The model DECIDES these fields; deterministic APPLY (§3) MATERIALIZES them into files,
    frontmatter, links, MOC/index entries, and the ``sources:`` union. ``basename`` names a NEW note
    (CREATE_THEME / APPEND_DAILY); ``target_basename`` names an EXISTING note (MERGE_INTO_THEME /
    MARK_CONTESTED); both are ``None`` for DROP / NOOP. ``confidence`` is deliberately NOT a field —
    APPLY mirrors it from the candidate so the backend can never inflate it (§2).

    Frozen + ``extra='forbid'`` so a stray/renamed field in a hand-authored or model-emitted plan is
    a hard parse error, never a silently-ignored no-op.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    event_ids: tuple[str, ...]
    op: Op
    domain: str | None = None
    basename: str | None = None
    target_basename: str | None = None
    title: str | None = None
    summary: str | None = None
    status: str | None = None
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    needs_prose: bool = False
    reason: str


class Plan(BaseModel):
    """The PASS-1 ``plan.json`` (ADR-0011 §2 / DATA-MODEL §11.1) — the only thing the model writes.

    ``finished`` is the explicit done signal (§4.1 check 1): a plan with ``finished == false`` is
    treated as incomplete and rejected. ``dispositions`` holds EXACTLY one entry per candidate; the
    multiset union of every entry's ``event_ids`` must equal the manifest event set (§4.1 check 2,
    COVERAGE).

    Frozen + ``extra='forbid'`` so the on-disk shape cannot drift.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    run_id: str
    finished: bool
    dispositions: tuple[Disposition, ...]

    @classmethod
    def from_json(cls, text: str) -> Plan:
        """Parse + shape-validate ``plan.json`` text into a :class:`Plan`.

        Raises :class:`PlanParseError` (a ``ValueError``) on malformed JSON, a non-object top level,
        an unknown/unsupported ``schema_version``, or any pydantic shape violation (unknown op,
        missing required field, wrong type, stray key). This is the PARSE half of §4.1 check 1; the
        FINISHED half and checks 2-10 are graded by :func:`validate_plan` on the parsed object.
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlanParseError(f"plan.json is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise PlanParseError("plan.json must be a JSON object at the top level")

        version = data.get("schema_version")
        # Validate the version BEFORE full model construction so an unknown version yields a clear,
        # version-specific message rather than a generic pydantic error.
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise PlanParseError(
                f"unknown plan schema_version {version!r}; "
                f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise PlanParseError(f"plan.json does not match the plan schema: {exc}") from exc


class PlanError(BaseModel):
    """One §4.1 PLAN-validation failure: the failing ``check`` name + a human-readable ``message``.

    ``check`` is the stable, machine-comparable check identifier (e.g. ``'COVERAGE'``,
    ``'CANDIDATE-GATE'``) the worker logs / a test asserts on; ``message`` carries the offending
    detail. Frozen value object.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str
    message: str


def _implied_note_path(disp: Disposition, *, run_date: str | None = None) -> str | None:
    """Return the repo-relative path a content disposition implies, or ``None`` if it writes none.

    **Schema 2 (ADR-0041 D1/D2.5): the directory is the KIND, and the subject has left the path.**
    ``CREATE_THEME`` → ``wiki/concepts/<basename>.md``; ``APPEND_DAILY`` →
    ``wiki/notes/<yyyy>/<mm>/<run_date>.md``. The domain no longer appears in either — it survives
    on the wire only to seed a one-element ``subjects:`` at APPLY and as the ``raw/<domain>/`` shard
    key (D2.2 legs 1 and 3, OD-9) — which is why a ``CREATE_THEME`` now implies a path even when
    ``domain`` is ``None``.

    MERGE/MARK_CONTESTED edit an EXISTING target (its path is derived at APPLY from the live tree,
    not from the plan), and DROP/NOOP write nothing — all three return ``None``. Used by the
    PATH/ALLOWLIST check, which only needs to reason about NEW paths.

    ``run_date`` (``YYYY-MM-DD``) is the deterministic, curator-owned fact the daily shard is
    composed from (D2.6). It is a keyword because it is **transitional**: the production caller
    (``curator/worker.py``, which already holds ``run_date = run_id[:10]``) does not pass it yet,
    and until it does the shard falls back to the basename — which is safe but weaker, so the
    fallback is stated here rather than left to be discovered. When the date is unavailable AND the
    basename is not a date, the journal is placed flat under ``wiki/notes/``: still allowlisted,
    still not an escape, and the D1.1 shard rule is then graded by lint rather than here.
    """
    if disp.op == "CREATE_THEME" and disp.basename:
        return f"wiki/{KIND_DIRECTORIES['concept']}/{disp.basename}.md"
    if disp.op == "APPEND_DAILY":
        stem = run_date if run_date is not None else disp.basename
        if not stem:
            return None
        notes = KIND_DIRECTORIES["note"]
        match = _RUN_DATE_RE.match(stem)
        if match is None:
            return f"wiki/{notes}/{stem}.md"
        return f"wiki/{notes}/{match['y']}/{match['m']}/{stem}.md"
    return None


def validate_plan(
    plan: Plan,
    *,
    manifest_event_ids: set[str],
    allowed_tags: set[str],
    domains: set[str],
    live_basenames: set[str],
    theme_basenames: set[str],
    gated_candidate_ids: set[str],
    run_date: str | None = None,
) -> list[PlanError]:
    """Run ALL ten §4.1 PLAN checks against ``plan`` and return the failures (``[]`` iff valid).

    Pure + deterministic. The caller injects the run's deterministic facts:

    * ``manifest_event_ids`` — the run manifest's claimed ``event_ids`` (the SOLE coverage universe,
      §5). COVERAGE (check 2) is a strict multiset-vs-set equality against this.
    * ``allowed_tags`` / ``domains`` — the FIXED, read-only ``_meta/taxonomy.yaml`` vocabulary
      (ADR-0010 D6). TAXONOMY (check 4) rejects any tag ∉ ``allowed_tags`` or domain ∉ ``domains``.
      ``domains`` is ALSO the MINTABLE MAP-BASENAME set: BASENAME (check 5) reserves it, because
      APPLY mints ``wiki/maps/<subject>.md`` lazily and that file is invisible to ``live_basenames``
      until the run that creates it has already written it (ADR-0041 D1.3).
    * ``live_basenames`` — every note basename present in the live worktree tree at ``base_commit``
      (the AUTHORITATIVE registry, §1.2). BASENAME (check 5) requires a new ``basename`` to be
      absent here (daily exempt, AND absent from ALL live basenames incl. map/index/journal so a new
      concept can never collide with a non-concept note); LINK resolvability (check 7) resolves
      links against this set ∪ same-plan new basenames.
    * ``theme_basenames`` — the SUBSET of ``live_basenames`` that are SOURCED notes
      (``wiki/concepts/**`` / ``wiki/summaries/**``, ADR-0041 D1.1). MERGE_INTO_THEME /
      MARK_CONTESTED are sourced-kind ops (§2 op table) whose ``target_basename`` is resolved by
      ``apply._resolve_target_path`` with ``sourced_only=True``: BASENAME (check 5) and PROVENANCE
      (check 8) therefore require their ``target_basename`` to be in ``theme_basenames`` (NOT merely
      in ``live_basenames``), so a validator-clean MERGE/CONTEST can never name a map/index/journal
      that APPLY then fails to materialize (the "validate-valid but apply-unmaterializable" class).
    * ``gated_candidate_ids`` — candidate ids the worker flagged ``is_gated`` (kind=candidate OR
      confidence=low, §6). CANDIDATE-GATE (check 10) restricts their op to
      {MERGE_INTO_THEME, MARK_CONTESTED, DROP}.
    * ``run_date`` — the run's ``YYYY-MM-DD`` date (``run_id[:10]``), OPTIONAL. When supplied,
      PATH/ALLOWLIST additionally asserts that every ``APPEND_DAILY`` basename IS that date
      (ADR-0041 D2.6: one journal per run date, repo-wide, basenamed by it) and composes the
      ``wiki/notes/<yyyy>/<mm>/`` shard from it, so a curator-owned path segment is never a
      function of model output. It is injected rather than read from ``plan.run_id`` **because
      ``plan.run_id`` is model-supplied**: deriving the shard from it would hand the model the
      segment this rule exists to keep. Omitting it grades the daily basename as a plain path
      component only (the v1 behaviour) — the transitional default while the worker call site is
      wired.

    Findings are appended check-by-check (1→10) and, within a check, in plan-disposition order, so
    the returned order is a deterministic function of the plan. Each finding's ``check`` is the
    canonical name from §4.1 so tests / logs can assert on it.
    """
    errors: list[PlanError] = []

    # 1. FINISHED — the parse half is Plan.from_json; here we grade the explicit done signal.
    if not plan.finished:
        errors.append(
            PlanError(
                check="FINISHED",
                message="plan.finished is false; the backend did not signal a complete plan",
            )
        )

    # 2. COVERAGE — the multiset of all event_ids must equal the manifest set EXACTLY: each manifest
    #    event appears once, none duplicated, none orphaned, none from outside the manifest. We walk
    #    deterministically so duplicates/foreign ids are reported in plan order.
    seen_counts: dict[str, int] = {}
    for disp in plan.dispositions:
        for event_id in disp.event_ids:
            if event_id in seen_counts:
                errors.append(
                    PlanError(
                        check="COVERAGE",
                        message=(
                            f"event_id {event_id!r} appears more than once across dispositions "
                            f"(candidate {disp.candidate_id!r}); coverage must be a partition"
                        ),
                    )
                )
            seen_counts[event_id] = seen_counts.get(event_id, 0) + 1
            if event_id not in manifest_event_ids:
                errors.append(
                    PlanError(
                        check="COVERAGE",
                        message=(
                            f"event_id {event_id!r} (candidate {disp.candidate_id!r}) is not in "
                            f"the run manifest; the manifest is the sole coverage universe"
                        ),
                    )
                )
    for event_id in sorted(manifest_event_ids - set(seen_counts)):
        errors.append(
            PlanError(
                check="COVERAGE",
                message=f"manifest event_id {event_id!r} is not covered by any disposition",
            )
        )

    # 3. CLOSED VOCAB — every op ∈ §2 set. (Plan.from_json's Literal already rejects unknown ops at
    #    parse time, but we re-assert here so validate_plan is a complete §4.1 gate on its own and a
    #    hand-built Plan object — bypassing from_json — is still graded.)
    for disp in plan.dispositions:
        if disp.op not in OPS:
            errors.append(
                PlanError(
                    check="CLOSED-VOCAB",
                    message=f"candidate {disp.candidate_id!r}: op {disp.op!r} is not in the "
                    f"closed vocabulary {sorted(OPS)}",
                )
            )

    # 4. TAXONOMY — tags ⊆ allowed_tags; a target domain ∈ domains. The taxonomy is FIXED +
    #    read-only (ADR-0010 D6): a CREATE_THEME naming a non-taxonomy domain is rejected here, no
    #    backdoor that lets the model widen the vocabulary (§6.1). DROP/NOOP carry no domain.
    for disp in plan.dispositions:
        for tag in disp.tags:
            if tag not in allowed_tags:
                errors.append(
                    PlanError(
                        check="TAXONOMY",
                        message=(
                            f"candidate {disp.candidate_id!r}: tag {tag!r} is not in the fixed "
                            f"taxonomy allowed_tags"
                        ),
                    )
                )
        if disp.domain is not None and disp.domain not in domains:
            errors.append(
                PlanError(
                    check="TAXONOMY",
                    message=(
                        f"candidate {disp.candidate_id!r}: domain {disp.domain!r} is not in the "
                        f"fixed taxonomy domains"
                    ),
                )
            )

    # 5. BASENAME — a NEW basename (CREATE_THEME / APPEND_DAILY) must be absent from the live tree
    #    (ALL basenames, incl. MOC/index/daily) AND unique within the plan (daily EXEMPT from both,
    #    §3.1); a target_basename (MERGE / MARK_CONTESTED) must EXIST as a SOURCED note. MERGE/
    #    CONTEST are sourced-kind ops (§2 op table; apply._resolve_target_path sourced_only=True),
    #    so naming a map/index/journal is rejected HERE — otherwise a validator-clean plan would
    #    crash APPLY. The bundle registry is advisory; this re-checks against the authoritative
    #    live_basenames / theme_basenames (§1.2).
    #
    #    A new basename must ALSO be absent from the MINTABLE MAP basenames — the declared taxonomy
    #    `domains`. Under schema 2 `apply._update_map` mints `wiki/maps/<subject>.md` LAZILY, at the
    #    first concept of that subject (ADR-0041 D1.3), so that map is by construction invisible to
    #    `live_basenames` (built from the tree at base_commit) until after the run that creates it.
    #    Without this reservation a CREATE_THEME whose basename equals a domain passes PLAN and then
    #    materializes an L1-1 duplicate basename at APPLY — and the CROSS-RUN shape is worse: run 1
    #    publishes `wiki/concepts/<domain>.md` cleanly, and from then on EVERY run that files
    #    anything under that subject fails the §4.4 lint gate on a collision neither its plan nor
    #    its error message names. v1 could not reach this state because the MOC carried a `-moc`
    #    filename suffix; D1.3 drops the suffix, so the gate has to exist instead. The domain set is
    #    exactly the mintable set: check 4 (TAXONOMY) already refuses a domain outside it.
    within_plan_new: set[str] = set()
    for disp in plan.dispositions:
        if disp.op in _BASENAME_OPS:
            # A CREATE_THEME / APPEND_DAILY still REQUIRES a domain, and under schema 2 that is no
            # longer a PATH fact — the path is kind-first (ADR-0041 D1) and needs no domain at all.
            # APPLY has been relaxed accordingly (`_apply_create_concept` files a domain-less
            # disposition with `subjects: []`), so this check is no longer the "would crash APPLY"
            # precondition it once was. It is KEPT, deliberately, on two live grounds:
            #   * `raw/` did NOT move (D1.4/D2.2 leg 3): with no domain `_sources_union` composes a
            #     ROOT-level `raw/<event_id>.md`, a `raw/` layout no ADR names.
            #   * the reference brain still substitutes ADR-0022's catch-all domain
            #     (`ollama_brain.normalize_plan`), so the plan wire never carries `None` anyway.
            # Net: D2.2 leg 2 (`subjects: []` reachable END TO END) is DEFERRED, not shipped — the
            # honest statement, in place of the stale "a domain-less disposition would crash APPLY".
            # Recorded as ADR-0041 OD-10, which names the three coupled changes that close it.
            if disp.domain is None:
                errors.append(
                    PlanError(
                        check="BASENAME",
                        message=f"candidate {disp.candidate_id!r}: {disp.op} requires a domain",
                    )
                )
            base = disp.basename
            if base is None:
                errors.append(
                    PlanError(
                        check="BASENAME",
                        message=f"candidate {disp.candidate_id!r}: {disp.op} requires a basename",
                    )
                )
                continue
            if disp.op in _DAILY_OPS:
                continue  # daily basenames are exempt from global + within-plan uniqueness
            if base in live_basenames:
                errors.append(
                    PlanError(
                        check="BASENAME",
                        message=(
                            f"candidate {disp.candidate_id!r}: new basename {base!r} already "
                            f"exists in the live worktree tree"
                        ),
                    )
                )
            elif base in domains:
                # RESERVED: `wiki/maps/<base>.md` is the map APPLY mints lazily for that subject
                # (D1.3), so the name is taken even though no file carries it yet. Reported only
                # when the live check did not already fire — an existing map IS in live_basenames,
                # and one collision deserves one error.
                errors.append(
                    PlanError(
                        check="BASENAME",
                        message=(
                            f"candidate {disp.candidate_id!r}: new basename {base!r} is a declared "
                            f"taxonomy domain, which RESERVES it for the wiki/maps/{base}.md map "
                            f"APPLY mints lazily for that subject (ADR-0041 D1.3) — publishing the "
                            f"concept would arm a permanent L1-1 duplicate-basename failure"
                        ),
                    )
                )
            if base in within_plan_new:
                errors.append(
                    PlanError(
                        check="BASENAME",
                        message=(
                            f"candidate {disp.candidate_id!r}: basename {base!r} is proposed by "
                            f"more than one disposition in this plan"
                        ),
                    )
                )
            within_plan_new.add(base)
        elif disp.op in _TARGET_OPS:
            target = disp.target_basename
            if target is None:
                errors.append(
                    PlanError(
                        check="BASENAME",
                        message=(
                            f"candidate {disp.candidate_id!r}: {disp.op} requires a target_basename"
                        ),
                    )
                )
            elif target not in theme_basenames:
                errors.append(
                    PlanError(
                        check="BASENAME",
                        message=(
                            f"candidate {disp.candidate_id!r}: target_basename {target!r} is not "
                            f"an existing THEME note (merge/contest may only target a theme)"
                        ),
                    )
                )

    # 6. PATH/ALLOWLIST — every NEW implied path resolves under the canonical ALLOWLIST (wiki/**,
    #    minus the ADR-0041 D4.1 wiki/people/ carve-out) with no _kb/ / _templates/ / raw/ / git /
    #    hook / ".." escape. (NOT symlinks — see the header note above; a spelling rule never sees
    #    an inode, so symlink rejection is worker.py's FINAL-DIFF gate, not this check.) A content
    #    disposition's schema-2 path is wiki/concepts/<basename>.md or
    #    wiki/notes/<yyyy>/<mm>/<run_date>.md (D1), so the check reduces to: the tokens the path is
    #    composed from are SAFE path components, plus (for a daily) the basename IS the run date.
    #    MERGE/MARK_CONTESTED/DROP/NOOP imply no NEW WIKI path (target paths come from the live
    #    tree at APPLY), so only their DOMAIN is graded — see below.
    for disp in plan.dispositions:
        # The DOMAIN is graded on EVERY op that carries one, not only the two basename ops, because
        # it composes a path on every one of them: MERGE/MARK_CONTESTED pass it to `_sources_union`
        # as the `raw/<domain>/<event_id>.md` shard key (D2.2 leg 3) exactly as CREATE_THEME does.
        # Leaving those two ungraded is how an escaping token still reaches a write.
        #
        # It composes TWO paths, not one: that shard, and — new in schema 2 — the
        # `wiki/maps/<subject>.md` map APPLY mints lazily for it (D1.3). So it is graded by the NOTE
        # composer's rule rather than a token rule of its own: two graders over one token is exactly
        # how a plan this gate calls valid goes on to crash APPLY inside `_update_map`.
        if disp.domain is not None and not _is_safe_basename(disp.domain):
            errors.append(
                PlanError(
                    check="PATH-ALLOWLIST",
                    message=(
                        f"candidate {disp.candidate_id!r}: domain {disp.domain!r} is not a safe "
                        f"path component (letters, numbers or combining marks plus '-', '_' and "
                        f"'.', no separator, no '..', no leading '.' or '_', no '.md' suffix, no "
                        f"Windows reserved device stem, at most 180 UTF-8 bytes); the domain is "
                        f"both the raw/<domain>/ shard key and the wiki/maps/<subject>.md basename "
                        f"APPLY composes from it (ADR-0041 D1.3/D2.2/D4.4)"
                    ),
                )
            )
        if disp.op not in _BASENAME_OPS:
            continue
        if disp.basename is not None and not _is_safe_basename(disp.basename):
            errors.append(
                PlanError(
                    check="PATH-ALLOWLIST",
                    message=(
                        f"candidate {disp.candidate_id!r}: basename {disp.basename!r} is not a "
                        f"safe path component (letters, numbers or combining marks plus '-', '_' "
                        f"and '.', no separator, no '..', no leading '.' or '_', no Windows "
                        f"reserved device stem, at most 180 UTF-8 bytes); a separator, '..', or "
                        f"leading dot could escape the wiki/ allowlist, and a leading '_' collides "
                        f"with the reserved raw/_blob and raw/_pages namespaces (ADR-0041 D4.4)"
                    ),
                )
            )
        # D2.6: the day's single journal is BASENAMED by the run date, so basename and shard are
        # two views of one curator-owned fact. Asserted only when the caller injected the date —
        # the fact is deterministic and external, never parsed back out of the plan.
        if disp.op == "APPEND_DAILY" and run_date is not None and disp.basename != run_date:
            errors.append(
                PlanError(
                    check="PATH-ALLOWLIST",
                    message=(
                        f"candidate {disp.candidate_id!r}: APPEND_DAILY basename "
                        f"{disp.basename!r} is not the run date {run_date!r}; schema 2 writes ONE "
                        f"journal per run_date, basenamed by it (ADR-0041 D2.6)"
                    ),
                )
            )
        # Belt-and-suspenders: confirm the assembled path is inside the ONE canonical allowlist.
        implied = _implied_note_path(disp, run_date=run_date)
        if implied is not None and not is_allowlisted_path(implied):
            errors.append(
                PlanError(
                    check="PATH-ALLOWLIST",
                    message=(
                        f"candidate {disp.candidate_id!r}: implied path {implied!r} escapes the "
                        f"curator allowlist"
                    ),
                )
            )

    # 7. LINK RESOLVABILITY — every links[] target resolves to an existing (live-tree) basename or
    #    a same-plan NEW basename. ``within_plan_new`` is the set of CREATE_THEME basenames computed
    #    in check 5; daily basenames are not link targets so they are not in the resolvable set.
    resolvable = live_basenames | within_plan_new
    for disp in plan.dispositions:
        for link in disp.links:
            if link not in resolvable:
                errors.append(
                    PlanError(
                        check="LINK-RESOLVABILITY",
                        message=(
                            f"candidate {disp.candidate_id!r}: link {link!r} resolves to neither a "
                            f"live-tree basename nor a same-plan basename"
                        ),
                    )
                )

    # 8. PROVENANCE — every content op (CREATE_THEME / APPEND_DAILY / MERGE_INTO_THEME /
    #    MARK_CONTESTED) lists ≥1 event_id; the target-bearing ops' target_basename must exist AS A
    #    THEME (the existence half overlaps BASENAME check 5 but is restated per §4.1.8 so it is a
    #    complete check — and like check 5 it grades against theme_basenames, since MERGE/CONTEST
    #    only resolve to a theme at APPLY). DROP/NOOP need no provenance.
    for disp in plan.dispositions:
        if disp.op in CONTENT_OPS and not disp.event_ids:
            errors.append(
                PlanError(
                    check="PROVENANCE",
                    message=(
                        f"candidate {disp.candidate_id!r}: {disp.op} must carry at least one "
                        f"event_id (provenance can never be lost)"
                    ),
                )
            )
        if disp.op in _TARGET_OPS:
            target = disp.target_basename
            if target is not None and target not in theme_basenames:
                errors.append(
                    PlanError(
                        check="PROVENANCE",
                        message=(
                            f"candidate {disp.candidate_id!r}: {disp.op} target_basename "
                            f"{target!r} is not an existing THEME note in the live worktree tree"
                        ),
                    )
                )

    # 9. STATUS — when a CREATE_THEME / MERGE_INTO_THEME disposition declares ``status``, it must
    #    be in the C1 enum, and ``contested`` is reserved for MARK_CONTESTED (which alone
    #    materializes the §2.1 shape). ``orphan``/``stale`` are never plan values (derived at read).
    for disp in plan.dispositions:
        if disp.status is None:
            continue
        if disp.status not in _STATUS_VALUES:
            errors.append(
                PlanError(
                    check="STATUS",
                    message=(
                        f"candidate {disp.candidate_id!r}: status {disp.status!r} is not in the "
                        f"enum {sorted(_STATUS_VALUES)}"
                    ),
                )
            )
        elif disp.status == "contested" and disp.op != "MARK_CONTESTED":
            errors.append(
                PlanError(
                    check="STATUS",
                    message=(
                        f"candidate {disp.candidate_id!r}: status 'contested' may only be set by "
                        f"MARK_CONTESTED, not {disp.op}"
                    ),
                )
            )

    # 10. CANDIDATE GATE (§6) — a gated candidate (is_gated: kind=candidate OR confidence=low) may
    #     only MERGE_INTO_THEME / MARK_CONTESTED / DROP; it may NEVER originate (CREATE_THEME /
    #     APPEND_DAILY). Structural enforcement, not trust (harvester safety, ADR-0007).
    for disp in plan.dispositions:
        if disp.candidate_id in gated_candidate_ids and disp.op not in GATE_ALLOWED_OPS:
            errors.append(
                PlanError(
                    check="CANDIDATE-GATE",
                    message=(
                        f"candidate {disp.candidate_id!r} is gated (harvested/low-confidence) and "
                        f"may not {disp.op}; allowed: {sorted(GATE_ALLOWED_OPS)}"
                    ),
                )
            )

    return errors
