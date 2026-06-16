"""Tests for the PASS-1 ``plan.json`` model + §4.1 PLAN validator (ADR-0011).

The INGEST core is "success = a pure function of (plan, diff, manifest, lint)" — so every plan here
is HAND-AUTHORED (zero model in the loop) and every §4.1 check has a plan that PASSES and one that
FAILS with the expected :class:`PlanError.check`.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agora_kb.curator.plan import (
    Disposition,
    Plan,
    PlanError,
    PlanParseError,
    validate_plan,
)

RUN_ID = "2026-06-13T03-00-00.000Z--7f31ab"
E1 = "2026-06-13T02-40-10.000Z--a1b2c3"
E2 = "2026-06-13T02-41-00.000Z--d4e5f6"
E3 = "2026-06-13T02-42-00.000Z--beef01"

# A taxonomy / live-tree fixture reused across the validator tests.
ALLOWED_TAGS = {"curator", "concurrency", "architecture"}
DOMAINS = {"ai-tech", "economy", "general"}
LIVE = {"cqrs", "single-writer-invariant", "ai-tech-moc"}
# The THEME subset of LIVE (wiki/<domain>/themes/<basename>.md): the MOC ``ai-tech-moc`` is NOT a
# theme, so MERGE/CONTEST may never target it (BASENAME/PROVENANCE grade against THEMES, not LIVE).
THEMES = {"cqrs", "single-writer-invariant"}


def _disp(**overrides: object) -> Disposition:
    """A valid CREATE_THEME disposition for candidate c1 over event E1, with field overrides."""
    base: dict[str, object] = {
        "candidate_id": "c1",
        "event_ids": (E1,),
        "op": "CREATE_THEME",
        "domain": "ai-tech",
        "basename": "curator-concurrency",
        "title": "Curator concurrency model",
        "summary": "One curator advances the curated branch under a per-repo lock.",
        "status": "active",
        "tags": ("curator", "concurrency"),
        "aliases": ("single-curator model",),
        "links": ("single-writer-invariant",),
        "needs_prose": True,
        "reason": "New concept; no related note above threshold.",
    }
    base.update(overrides)
    return Disposition(**base)


def _plan(*dispositions: Disposition, finished: bool = True) -> Plan:
    return Plan(
        schema_version=1,
        run_id=RUN_ID,
        finished=finished,
        dispositions=tuple(dispositions),
    )


def _checks(errors: list[PlanError]) -> set[str]:
    return {e.check for e in errors}


# --- from_json round-trip + malformed / unknown-version -----------------------------------------


def test_from_json_round_trip() -> None:
    plan = _plan(
        _disp(),
        _disp(
            candidate_id="c2",
            event_ids=(E2,),
            op="MERGE_INTO_THEME",
            basename=None,
            target_basename="cqrs",
            title=None,
            summary="Adds flock detail.",
            tags=(),
            aliases=(),
            links=(),
            reason="Overlaps related/c2 cqrs; union provenance.",
        ),
        _disp(
            candidate_id="c3",
            event_ids=(E3,),
            op="DROP",
            domain=None,
            basename=None,
            title=None,
            summary=None,
            status=None,
            tags=(),
            aliases=(),
            links=(),
            needs_prose=False,
            reason="Unsupported gated candidate; default drop.",
        ),
    )
    text = json.dumps(plan.model_dump(mode="json"))
    parsed = Plan.from_json(text)
    assert parsed == plan
    assert parsed.dispositions[1].op == "MERGE_INTO_THEME"
    assert parsed.dispositions[1].target_basename == "cqrs"
    assert parsed.dispositions[2].basename is None


def test_from_json_parses_spec_example() -> None:
    # The DATA-MODEL §11.1 / ADR-0011 §2 example shape parses (defaults fill omitted aliases/tags).
    text = json.dumps(
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": "c1",
                    "event_ids": [E1],
                    "op": "CREATE_THEME",
                    "domain": "ai-tech",
                    "basename": "curator-concurrency",
                    "title": "Curator concurrency model",
                    "summary": "One curator advances the branch under a lock.",
                    "status": "active",
                    "tags": ["curator", "concurrency"],
                    "links": ["single-writer-invariant"],
                    "needs_prose": True,
                    "reason": "New concept.",
                }
            ],
        }
    )
    plan = Plan.from_json(text)
    assert plan.dispositions[0].aliases == ()
    assert plan.dispositions[0].links == ("single-writer-invariant",)


def test_from_json_rejects_malformed_json() -> None:
    with pytest.raises(PlanParseError, match="not valid JSON"):
        Plan.from_json("{not json")


def test_from_json_rejects_non_object_top_level() -> None:
    with pytest.raises(PlanParseError, match="JSON object"):
        Plan.from_json("[]")


def test_from_json_rejects_unknown_schema_version() -> None:
    text = json.dumps(
        {"schema_version": 99, "run_id": RUN_ID, "finished": True, "dispositions": []}
    )
    with pytest.raises(PlanParseError, match="unknown plan schema_version 99"):
        Plan.from_json(text)


def test_from_json_rejects_missing_schema_version() -> None:
    text = json.dumps({"run_id": RUN_ID, "finished": True, "dispositions": []})
    with pytest.raises(PlanParseError, match="unknown plan schema_version"):
        Plan.from_json(text)


def test_from_json_rejects_unknown_op() -> None:
    text = json.dumps(
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": "c1",
                    "event_ids": [E1],
                    "op": "DELETE_THEME",
                    "reason": "bad op",
                }
            ],
        }
    )
    with pytest.raises(PlanParseError, match="does not match the plan schema"):
        Plan.from_json(text)


def test_from_json_rejects_stray_field() -> None:
    text = json.dumps(
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": "c1",
                    "event_ids": [E1],
                    "op": "DROP",
                    "reason": "x",
                    "bogus": 1,
                }
            ],
        }
    )
    with pytest.raises(PlanParseError):
        Plan.from_json(text)


def test_models_are_frozen() -> None:
    d = _disp()
    with pytest.raises(ValidationError):
        d.op = "DROP"  # type: ignore[misc]
    err = PlanError(check="COVERAGE", message="x")
    with pytest.raises(ValidationError):
        err.check = "STATUS"  # type: ignore[misc]


# --- the canonical happy path -------------------------------------------------------------------


def _validate(plan: Plan, *, gated: set[str] | None = None) -> list[PlanError]:
    return validate_plan(
        plan,
        manifest_event_ids=set(_manifest(plan)),
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=gated or set(),
    )


def _manifest(plan: Plan) -> list[str]:
    """The exact manifest set a coverage-clean plan implies (union of all event_ids)."""
    ids: list[str] = []
    for d in plan.dispositions:
        ids.extend(d.event_ids)
    return ids


def test_valid_plan_passes_all_checks() -> None:
    plan = _plan(
        _disp(),
        _disp(
            candidate_id="c2",
            event_ids=(E2,),
            op="MERGE_INTO_THEME",
            domain="ai-tech",
            basename=None,
            target_basename="cqrs",
            title=None,
            summary="Adds flock detail.",
            tags=(),
            aliases=(),
            links=(),
            reason="Overlaps cqrs.",
        ),
        _disp(
            candidate_id="c3",
            event_ids=(E3,),
            op="DROP",
            domain=None,
            basename=None,
            title=None,
            summary=None,
            status=None,
            tags=(),
            aliases=(),
            links=(),
            needs_prose=False,
            reason="Noise.",
        ),
    )
    assert _validate(plan) == []


# --- check 1: PARSE + FINISHED ------------------------------------------------------------------


def test_finished_true_passes() -> None:
    assert _validate(_plan(_disp(), finished=True)) == []


def test_finished_false_fails() -> None:
    # An otherwise-valid plan with finished=False fires EXACTLY the FINISHED check, nothing else.
    errors = _validate(_plan(_disp(), finished=False))
    assert [(e.check, e.message) for e in errors] == [
        (
            "FINISHED",
            "plan.finished is false; the backend did not signal a complete plan",
        )
    ]


# --- check 2: COVERAGE --------------------------------------------------------------------------


def test_coverage_exact_partition_passes() -> None:
    plan = _plan(
        _disp(event_ids=(E1, E2)),
        _disp(
            candidate_id="c3",
            event_ids=(E3,),
            op="DROP",
            domain=None,
            basename=None,
            title=None,
            summary=None,
            status=None,
            tags=(),
            aliases=(),
            links=(),
            needs_prose=False,
            reason="noise",
        ),
    )
    errors = validate_plan(
        plan,
        manifest_event_ids={E1, E2, E3},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    assert errors == []


def test_coverage_orphaned_manifest_event_fails() -> None:
    plan = _plan(_disp(event_ids=(E1,)))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1, E2},  # E2 never covered
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    assert "COVERAGE" in _checks(errors)
    assert any(E2 in e.message for e in errors)


def test_coverage_duplicate_emits_exact_ordered_errors() -> None:
    # A dup WITHIN one disposition (E1 twice) plus an orphaned manifest event (E3) must yield the
    # exact, deterministically-ordered COVERAGE findings: the duplicate (plan order) THEN the
    # orphan (sorted). This pins both the count and the message wording, not just check membership.
    plan = _plan(_disp(event_ids=(E1, E1)))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1, E3},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    coverage = [e for e in errors if e.check == "COVERAGE"]
    assert [(e.check, e.message) for e in coverage] == [
        (
            "COVERAGE",
            f"event_id {E1!r} appears more than once across dispositions "
            f"(candidate 'c1'); coverage must be a partition",
        ),
        ("COVERAGE", f"manifest event_id {E3!r} is not covered by any disposition"),
    ]


def test_coverage_duplicate_event_fails() -> None:
    plan = _plan(
        _disp(event_ids=(E1,)),
        _disp(
            candidate_id="c2",
            event_ids=(E1,),  # duplicate of c1
            op="DROP",
            domain=None,
            basename=None,
            title=None,
            summary=None,
            status=None,
            tags=(),
            aliases=(),
            links=(),
            needs_prose=False,
            reason="dup",
        ),
    )
    errors = validate_plan(
        plan,
        manifest_event_ids={E1},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    assert "COVERAGE" in _checks(errors)


def test_coverage_foreign_event_fails() -> None:
    plan = _plan(_disp(event_ids=(E1,)))
    errors = validate_plan(
        plan,
        manifest_event_ids=set(),  # E1 is NOT in the manifest universe
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    assert "COVERAGE" in _checks(errors)


# --- check 3: CLOSED VOCAB ----------------------------------------------------------------------


def test_closed_vocab_all_six_ops_pass() -> None:
    # Every op in the closed set validates against a tailored disposition; combined into one plan
    # the union of event_ids equals the manifest, so only CLOSED-VOCAB is under test here.
    plan = _plan(
        _disp(),  # CREATE_THEME
        _disp(
            candidate_id="c2",
            event_ids=(E2,),
            op="APPEND_DAILY",
            domain="ai-tech",
            basename="ai-tech-2026-06-13",
            title="Daily",
            summary="day",
            tags=(),
            aliases=(),
            links=(),
            reason="capture",
        ),
        _disp(
            candidate_id="c3",
            event_ids=(E3,),
            op="MERGE_INTO_THEME",
            domain=None,
            basename=None,
            target_basename="cqrs",
            title=None,
            summary="merge",
            tags=(),
            aliases=(),
            links=(),
            reason="overlap",
        ),
    )
    errors = validate_plan(
        plan,
        manifest_event_ids={E1, E2, E3},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    assert "CLOSED-VOCAB" not in _checks(errors)


def test_closed_vocab_unknown_op_fails_via_construct() -> None:
    # A Plan can be built bypassing from_json's Literal (model_construct), so validate_plan must
    # itself re-assert the closed vocabulary (defence in depth).
    bad = Disposition.model_construct(
        candidate_id="c1",
        event_ids=(E1,),
        op="DELETE_THEME",  # not in the closed set
        domain=None,
        basename=None,
        target_basename=None,
        title=None,
        summary=None,
        status=None,
        aliases=(),
        tags=(),
        links=(),
        needs_prose=False,
        reason="bad",
    )
    plan = Plan.model_construct(schema_version=1, run_id=RUN_ID, finished=True, dispositions=(bad,))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    assert "CLOSED-VOCAB" in _checks(errors)


# --- check 4: TAXONOMY --------------------------------------------------------------------------


def test_taxonomy_in_vocab_passes() -> None:
    assert _validate(_plan(_disp(tags=("curator", "architecture"), domain="ai-tech"))) == []


def test_taxonomy_unknown_tag_fails() -> None:
    errors = _validate(_plan(_disp(tags=("curator", "not-a-real-tag"))))
    assert "TAXONOMY" in _checks(errors)
    taxonomy = [e for e in errors if e.check == "TAXONOMY"]
    assert len(taxonomy) == 1
    assert "not-a-real-tag" in taxonomy[0].message


def test_taxonomy_unknown_domain_fails() -> None:
    # An unknown domain is a safe token (passes PATH) but absent from the fixed taxonomy domains.
    errors = _validate(_plan(_disp(domain="quantum", tags=())))
    assert "TAXONOMY" in _checks(errors)


# --- check 5: BASENAME --------------------------------------------------------------------------


def test_basename_new_and_target_existing_pass() -> None:
    plan = _plan(
        _disp(basename="brand-new-theme"),  # absent from LIVE
        _disp(
            candidate_id="c2",
            event_ids=(E2,),
            op="MERGE_INTO_THEME",
            domain=None,
            basename=None,
            target_basename="cqrs",  # present in LIVE
            title=None,
            summary="merge",
            tags=(),
            aliases=(),
            links=(),
            reason="overlap",
        ),
    )
    errors = validate_plan(
        plan,
        manifest_event_ids={E1, E2},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    # This plan is intended to be FULLY valid (not merely BASENAME-clean), so assert no findings.
    assert errors == []


def test_basename_collides_with_live_tree_fails() -> None:
    errors = _validate(_plan(_disp(basename="cqrs", links=())))  # already in LIVE
    assert "BASENAME" in _checks(errors)


def test_basename_create_theme_collides_with_moc_fails() -> None:
    # CREATE_THEME uniqueness must keep checking ALL live basenames (incl. the MOC), NOT just
    # themes: a new theme may not collide with a MOC/index/daily name even though MERGE/CONTEST
    # targets are now theme-only.
    errors = _validate(_plan(_disp(basename="ai-tech-moc", links=())))  # MOC name, in LIVE
    assert "BASENAME" in _checks(errors)


def test_basename_duplicate_within_plan_fails() -> None:
    plan = _plan(
        _disp(basename="dup-theme", links=()),
        _disp(
            candidate_id="c2",
            event_ids=(E2,),
            op="CREATE_THEME",
            domain="ai-tech",
            basename="dup-theme",  # same new basename twice
            title="Dup",
            summary="dup",
            tags=(),
            aliases=(),
            links=(),
            reason="dup",
        ),
    )
    errors = validate_plan(
        plan,
        manifest_event_ids={E1, E2},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    assert "BASENAME" in _checks(errors)


def test_basename_missing_target_fails() -> None:
    plan = _plan(
        _disp(
            op="MERGE_INTO_THEME",
            domain=None,
            basename=None,
            target_basename="no-such-note",  # not in LIVE
            title=None,
            summary="merge",
            tags=(),
            aliases=(),
            links=(),
        )
    )
    errors = _validate(plan)
    assert "BASENAME" in _checks(errors)


def test_basename_merge_target_non_theme_fails() -> None:
    # ``ai-tech-moc`` is in LIVE but is a MOC, NOT a theme (not in THEMES). MERGE_INTO_THEME may
    # only target a theme (apply._resolve_target_path theme_only=True), so naming the MOC is a
    # BASENAME rejection — otherwise validate_plan would accept a plan that crashes APPLY.
    plan = _plan(
        _disp(
            op="MERGE_INTO_THEME",
            domain=None,
            basename=None,
            target_basename="ai-tech-moc",  # in LIVE, NOT in THEMES
            title=None,
            summary="merge",
            tags=(),
            aliases=(),
            links=(),
        )
    )
    errors = _validate(plan)
    assert "BASENAME" in _checks(errors)
    basename_errors = [e for e in errors if e.check == "BASENAME"]
    assert any("is not an existing THEME note" in e.message for e in basename_errors)


def test_basename_contest_target_non_theme_fails() -> None:
    # Same for MARK_CONTESTED: the MOC is in LIVE but not a theme, so it is rejected.
    plan = _plan(
        _disp(
            op="MARK_CONTESTED",
            domain=None,
            basename=None,
            target_basename="ai-tech-moc",  # in LIVE, NOT in THEMES
            title=None,
            summary="contradicts",
            status="contested",
            tags=(),
            aliases=(),
            links=(),
            needs_prose=False,
        )
    )
    errors = _validate(plan)
    assert "BASENAME" in _checks(errors)
    assert any(
        "is not an existing THEME note" in e.message for e in errors if e.check == "BASENAME"
    )


def test_basename_merge_target_theme_passes() -> None:
    # The SAME shape but targeting ``cqrs`` (a real theme in THEMES) is fully valid.
    plan = _plan(
        _disp(
            op="MERGE_INTO_THEME",
            domain=None,
            basename=None,
            target_basename="cqrs",  # in THEMES
            title=None,
            summary="merge",
            tags=(),
            aliases=(),
            links=(),
        )
    )
    assert _validate(plan) == []


def test_basename_daily_exempt_from_uniqueness() -> None:
    # Two APPEND_DAILY dispositions sharing the same daily basename do NOT trip BASENAME (§3.1).
    plan = _plan(
        _disp(
            op="APPEND_DAILY",
            basename="ai-tech-2026-06-13",
            tags=(),
            aliases=(),
            links=(),
        ),
        _disp(
            candidate_id="c2",
            event_ids=(E2,),
            op="APPEND_DAILY",
            domain="ai-tech",
            basename="ai-tech-2026-06-13",  # same daily basename — exempt
            title="Daily",
            summary="day",
            tags=(),
            aliases=(),
            links=(),
            reason="capture",
        ),
    )
    errors = validate_plan(
        plan,
        manifest_event_ids={E1, E2},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    assert "BASENAME" not in _checks(errors)


def test_basename_create_theme_missing_domain_fails() -> None:
    # A CREATE_THEME with domain=None has NO resolvable allowlist path and cannot be a taxonomy
    # domain; §4.1 must reject it at PLAN time so APPLY never crashes on a validator-clean plan.
    errors = _validate(_plan(_disp(domain=None, tags=())))
    assert "BASENAME" in _checks(errors)
    assert any("requires a domain" in e.message for e in errors if e.check == "BASENAME")


def test_basename_append_daily_missing_domain_fails() -> None:
    plan = _plan(
        _disp(
            op="APPEND_DAILY",
            domain=None,
            basename="some-daily",
            tags=(),
            aliases=(),
            links=(),
        )
    )
    errors = _validate(plan)
    assert "BASENAME" in _checks(errors)
    assert any("requires a domain" in e.message for e in errors if e.check == "BASENAME")


# --- check 6: PATH / ALLOWLIST ------------------------------------------------------------------


def test_path_allowlist_safe_tokens_pass() -> None:
    assert _validate(_plan(_disp(basename="curator-concurrency", domain="ai-tech"))) == []


def test_path_allowlist_traversal_basename_fails() -> None:
    errors = _validate(_plan(_disp(basename="../../_kb/escape", links=())))
    assert "PATH-ALLOWLIST" in _checks(errors)


def test_path_allowlist_separator_in_domain_fails() -> None:
    # A "/" in the domain would let the implied path climb out of wiki/<domain>/.
    errors = _validate(_plan(_disp(domain="ai-tech/../_templates", tags=())))
    assert "PATH-ALLOWLIST" in _checks(errors)


def test_path_allowlist_leading_dot_basename_fails() -> None:
    errors = _validate(_plan(_disp(basename=".git", links=())))
    assert "PATH-ALLOWLIST" in _checks(errors)


# --- check 7: LINK RESOLVABILITY ----------------------------------------------------------------


def test_links_resolve_to_live_and_same_plan() -> None:
    # c1 creates "new-theme"; c2 links to it (same-plan) and to "cqrs" (live-tree).
    plan = _plan(
        _disp(basename="new-theme", links=()),
        _disp(
            candidate_id="c2",
            event_ids=(E2,),
            op="CREATE_THEME",
            domain="ai-tech",
            basename="linker",
            title="Linker",
            summary="links",
            tags=(),
            aliases=(),
            links=("new-theme", "cqrs"),
            reason="links to both",
        ),
    )
    errors = validate_plan(
        plan,
        manifest_event_ids={E1, E2},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    assert "LINK-RESOLVABILITY" not in _checks(errors)


def test_links_unresolvable_fails() -> None:
    errors = _validate(_plan(_disp(links=("ghost-note",))))
    assert "LINK-RESOLVABILITY" in _checks(errors)
    link_errors = [e for e in errors if e.check == "LINK-RESOLVABILITY"]
    assert len(link_errors) == 1
    assert "ghost-note" in link_errors[0].message


# --- check 8: PROVENANCE ------------------------------------------------------------------------


def test_provenance_present_passes() -> None:
    assert _validate(_plan(_disp(event_ids=(E1,)))) == []


def test_provenance_missing_event_ids_on_content_op_fails() -> None:
    # A CREATE_THEME with no event_ids loses provenance; also trips COVERAGE, but PROVENANCE must
    # specifically fire.
    plan = _plan(_disp(event_ids=()))
    errors = validate_plan(
        plan,
        manifest_event_ids=set(),
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    assert "PROVENANCE" in _checks(errors)


def test_provenance_missing_target_fails() -> None:
    # The PROVENANCE check restates the target-existence branch (plan.py check 8 half b): a
    # MERGE_INTO_THEME whose target is absent from the live tree fires BOTH BASENAME (check 5) and
    # PROVENANCE (check 8). Assert both are present, exercising the restated PROVENANCE branch.
    plan = _plan(
        _disp(
            op="MERGE_INTO_THEME",
            domain=None,
            basename=None,
            target_basename="no-such-note",  # not in LIVE
            title=None,
            summary="merge",
            tags=(),
            aliases=(),
            links=(),
        )
    )
    errors = _validate(plan)
    assert "PROVENANCE" in _checks(errors)
    assert "BASENAME" in _checks(errors)


def test_provenance_drop_needs_no_event_ids() -> None:
    # DROP/NOOP are non-content ops; an empty event_ids list is allowed (only COVERAGE governs them
    # — and here the manifest is empty so coverage is clean too).
    plan = _plan(
        _disp(
            op="DROP",
            event_ids=(),
            domain=None,
            basename=None,
            title=None,
            summary=None,
            status=None,
            tags=(),
            aliases=(),
            links=(),
            needs_prose=False,
        )
    )
    errors = validate_plan(
        plan,
        manifest_event_ids=set(),
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    assert "PROVENANCE" not in _checks(errors)


# --- check 9: STATUS ----------------------------------------------------------------------------


def test_status_active_passes() -> None:
    assert _validate(_plan(_disp(status="active"))) == []


def test_status_stub_passes() -> None:
    assert _validate(_plan(_disp(status="stub"))) == []


def test_status_deprecated_passes() -> None:
    # ``deprecated`` is a valid C1 status on a plain CREATE_THEME; the accept side must cover it so
    # a regression dropping it from _STATUS_VALUES is caught.
    assert _validate(_plan(_disp(status="deprecated"))) == []


def test_status_unknown_value_fails() -> None:
    errors = _validate(_plan(_disp(status="canonical")))
    assert "STATUS" in _checks(errors)
    status_errors = [e for e in errors if e.check == "STATUS"]
    assert len(status_errors) == 1
    assert "canonical" in status_errors[0].message


def test_status_contested_on_create_theme_fails() -> None:
    # ``contested`` is reserved for MARK_CONTESTED (which materializes the §2.1 shape).
    errors = _validate(_plan(_disp(status="contested")))
    assert "STATUS" in _checks(errors)


def test_status_contested_on_mark_contested_passes() -> None:
    plan = _plan(
        _disp(
            op="MARK_CONTESTED",
            domain=None,
            basename=None,
            target_basename="cqrs",
            title=None,
            summary="contradicts",
            status="contested",
            tags=(),
            aliases=(),
            links=(),
            needs_prose=False,
        )
    )
    errors = _validate(plan)
    # The MARK_CONTESTED contested-status plan is fully valid, so assert no findings at all.
    assert errors == []


# --- check 10: CANDIDATE GATE (§6) --------------------------------------------------------------


def test_gate_merge_for_gated_candidate_passes() -> None:
    plan = _plan(
        _disp(
            op="MERGE_INTO_THEME",
            domain=None,
            basename=None,
            target_basename="cqrs",
            title=None,
            summary="corroborate",
            tags=(),
            aliases=(),
            links=(),
        )
    )
    errors = _validate(plan, gated={"c1"})
    # A gated MERGE_INTO_THEME is fully valid, so assert no findings (not just gate-clean).
    assert errors == []


def test_gate_drop_for_gated_candidate_passes() -> None:
    plan = _plan(
        _disp(
            op="DROP",
            event_ids=(E1,),
            domain=None,
            basename=None,
            title=None,
            summary=None,
            status=None,
            tags=(),
            aliases=(),
            links=(),
            needs_prose=False,
        )
    )
    errors = _validate(plan, gated={"c1"})
    # A gated DROP is fully valid, so assert no findings at all.
    assert errors == []


def test_gate_create_theme_for_gated_candidate_fails() -> None:
    # A gated (harvested/low-confidence) candidate may NEVER originate a theme (ADR-0007 / §6).
    errors = _validate(_plan(_disp()), gated={"c1"})
    assert "CANDIDATE-GATE" in _checks(errors)


def test_gate_append_daily_for_gated_candidate_fails() -> None:
    plan = _plan(
        _disp(
            op="APPEND_DAILY",
            basename="ai-tech-2026-06-13",
            tags=(),
            aliases=(),
            links=(),
        )
    )
    errors = _validate(plan, gated={"c1"})
    assert "CANDIDATE-GATE" in _checks(errors)


def test_non_gated_candidate_may_create_theme() -> None:
    # Same plan, but the candidate is NOT gated — the gate does not fire.
    errors = _validate(_plan(_disp()), gated=set())
    assert "CANDIDATE-GATE" not in _checks(errors)
