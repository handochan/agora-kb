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

# The canonical INGEST allowlist (ADR-0011 §4.0, matching ADR-0008 §4 verbatim):
#   { wiki/** , index.md , <domain>-moc.md , log.md , assets/** }
# A disposition's implied note path is always under ``wiki/<domain>/{themes,daily}/`` — i.e. under
# ``wiki/**`` — so PATH/ALLOWLIST (§4.1.6) reduces to a CHARACTER rule on the domain + basename
# tokens: each must be one safe path component (no separator, no ``..``, no leading dot), which is
# what stops a token from spelling its way out of ``wiki/`` into ``_kb/`` / ``_templates/`` /
# ``raw/`` / git internals / hooks. One safe-token regex enforces the whole character rule.
#
# What this regex canNOT do, stated so nobody re-derives a false guarantee from it:
#   * It cannot see the filesystem. A SYMLINK is an inode property, not a spelling — a perfectly
#     regex-clean name can BE a symlink pointing anywhere. Symlink prevention lives in the curator's
#     FINAL-DIFF gates (``curator/worker.py``: the ``_is_engine_written_raw`` ``is_symlink`` check
#     and the ``_assert_final_diff_allowlisted`` A/M/R symlink reject, §4.5), which grade the
#     committed tree.
#   * It cannot bind a caller. It is a gate in THIS module, one call up from the writes; anything
#     that reaches ``apply_plan`` by another route skips it entirely. Containment at the write site
#     is ``apply.py``'s ``_contained`` (``resolve()`` + ``is_relative_to(worktree)``), which holds
#     independently of this pattern — which is also why this pattern can later be widened for
#     non-ASCII without that widening becoming the escape.
_SAFE_TOKEN_RE_PATTERN = r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z"

SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


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


def _implied_note_path(disp: Disposition) -> str | None:
    """Return the wiki-relative path a content disposition implies, or ``None`` if it writes none.

    CREATE_THEME → ``wiki/<domain>/themes/<basename>.md``; APPEND_DAILY →
    ``wiki/<domain>/daily/<basename>.md``. MERGE/MARK_CONTESTED edit an EXISTING target (its path is
    derived at APPLY from the live tree, not from the plan), and DROP/NOOP write nothing — all three
    return ``None``. Used by the PATH/ALLOWLIST check, which only needs to reason about NEW paths.
    """
    if disp.op == "CREATE_THEME" and disp.domain and disp.basename:
        return f"wiki/{disp.domain}/themes/{disp.basename}.md"
    if disp.op == "APPEND_DAILY" and disp.domain and disp.basename:
        return f"wiki/{disp.domain}/daily/{disp.basename}.md"
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
) -> list[PlanError]:
    """Run ALL ten §4.1 PLAN checks against ``plan`` and return the failures (``[]`` iff valid).

    Pure + deterministic. The caller injects the run's deterministic facts:

    * ``manifest_event_ids`` — the run manifest's claimed ``event_ids`` (the SOLE coverage universe,
      §5). COVERAGE (check 2) is a strict multiset-vs-set equality against this.
    * ``allowed_tags`` / ``domains`` — the FIXED, read-only ``_meta/taxonomy.yaml`` vocabulary
      (ADR-0010 D6). TAXONOMY (check 4) rejects any tag ∉ ``allowed_tags`` or domain ∉ ``domains``.
    * ``live_basenames`` — every note basename present in the live worktree tree at ``base_commit``
      (the AUTHORITATIVE registry, §1.2). BASENAME (check 5) requires a new ``basename`` to be
      absent here (daily exempt, AND absent from ALL live basenames incl. MOC/index/daily so a new
      theme can never collide with a non-theme note); LINK resolvability (check 7) resolves links
      against this set ∪ same-plan new basenames.
    * ``theme_basenames`` — the SUBSET of ``live_basenames`` that are THEME notes
      (``wiki/<domain>/themes/<basename>.md``). MERGE_INTO_THEME / MARK_CONTESTED are theme-scoped
      ops (§2 op table) whose ``target_basename`` is resolved by ``apply._resolve_target_path`` with
      ``theme_only=True``: BASENAME (check 5) and PROVENANCE (check 8) therefore require their
      ``target_basename`` to be in ``theme_basenames`` (NOT merely in ``live_basenames``), so a
      validator-clean MERGE/CONTEST can never name a MOC/index/daily that APPLY then fails to
      materialize (the "validate-valid but apply-unmaterializable" class).
    * ``gated_candidate_ids`` — candidate ids the worker flagged ``is_gated`` (kind=candidate OR
      confidence=low, §6). CANDIDATE-GATE (check 10) restricts their op to
      {MERGE_INTO_THEME, MARK_CONTESTED, DROP}.

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
    #    §3.1); a target_basename (MERGE / MARK_CONTESTED) must EXIST as a THEME note. MERGE/CONTEST
    #    are theme-scoped ops (§2 op table; apply._resolve_target_path theme_only=True), so naming a
    #    MOC/index/daily is rejected HERE — otherwise a validator-clean plan would crash APPLY. The
    #    bundle registry is advisory; this re-checks against the authoritative live_basenames /
    #    theme_basenames (§1.2).
    within_plan_new: set[str] = set()
    for disp in plan.dispositions:
        if disp.op in _BASENAME_OPS:
            # A CREATE_THEME / APPEND_DAILY implies wiki/<domain>/{themes,daily}/<basename>.md
            # (ADR-0011 §2 table / §3): a missing domain has NO resolvable allowlist path and can
            # never be in the fixed taxonomy domains, so it must be rejected here (§4.1 checks 5/6)
            # rather than silently passing the gate and crashing APPLY (which would break "a valid
            # plan never raises" and the pure-function contract).
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

    # 6. PATH/ALLOWLIST — every NEW implied path resolves under the canonical ALLOWLIST (wiki/**)
    #    with no _kb/ / _templates/ / raw/ / git / hook / ".." escape. (NOT symlinks — see the
    #    header note above; a regex sees only a spelling, never an inode, so symlink rejection is
    #    worker.py's FINAL-DIFF gate, not this check.) Because a content disposition's path is
    #    always wiki/<domain>/{themes,daily}/<basename>.md, the check reduces to: domain + basename
    #    are SAFE path-component tokens (the regex forbids "/", "..", a leading dot).
    #    MERGE/MARK_CONTESTED/DROP/NOOP imply no NEW path (target paths come from the live tree at
    #    APPLY), so they are not path-checked here.
    safe_token = re.compile(_SAFE_TOKEN_RE_PATTERN)
    for disp in plan.dispositions:
        if disp.op not in _BASENAME_OPS:
            continue
        for label, token in (("domain", disp.domain), ("basename", disp.basename)):
            if token is not None and not safe_token.match(token):
                errors.append(
                    PlanError(
                        check="PATH-ALLOWLIST",
                        message=(
                            f"candidate {disp.candidate_id!r}: {label} {token!r} is not a safe "
                            f"path component (must match {_SAFE_TOKEN_RE_PATTERN}); a separator, "
                            f"'..', or leading dot could escape the wiki/ allowlist"
                        ),
                    )
                )
        # Belt-and-suspenders: confirm the assembled path is under wiki/ with no traversal segment.
        implied = _implied_note_path(disp)
        if implied is not None and (not implied.startswith("wiki/") or ".." in implied.split("/")):
            errors.append(
                PlanError(
                    check="PATH-ALLOWLIST",
                    message=(
                        f"candidate {disp.candidate_id!r}: implied path {implied!r} escapes the "
                        f"wiki/ allowlist"
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
