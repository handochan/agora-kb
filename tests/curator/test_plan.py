"""Tests for the PASS-1 ``plan.json`` model + §4.1 PLAN validator (ADR-0011).

The INGEST core is "success = a pure function of (plan, diff, manifest, lint)" — so every plan here
is HAND-AUTHORED (zero model in the loop) and every §4.1 check has a plan that PASSES and one that
FAILS with the expected :class:`PlanError.check`.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agora_kb.curator.constants import is_allowlisted_path
from agora_kb.curator.plan import (
    Disposition,
    Plan,
    PlanError,
    PlanParseError,
    _implied_note_path,
    validate_plan,
)

RUN_ID = "2026-06-13T03-00-00.000Z--7f31ab"
RUN_DATE = RUN_ID[:10]  # the deterministic curator-owned fact schema-2 dailies are named by
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
    # only target a theme (apply._resolve_target_path sourced_only=True), so naming the MOC is a
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
    # Schema 2 basenames the journal with the bare run date (ADR-0041 D2.6) — and ADR-0011 §4.1
    # check 5's daily exemption must SURVIVE that change: one journal per run_date removes
    # cross-domain collisions but not the second `agora curate` of the same day, whose
    # APPEND_DAILY names a basename that is by then already on disk.
    plan = _plan(
        _disp(
            op="APPEND_DAILY",
            basename=RUN_DATE,
            tags=(),
            aliases=(),
            links=(),
        ),
        _disp(
            candidate_id="c2",
            event_ids=(E2,),
            op="APPEND_DAILY",
            domain="ai-tech",
            basename=RUN_DATE,  # same daily basename — exempt
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


# --- check 5 (schema 2): the mintable MAP basename reservation, ADR-0041 D1.3 -------------------


def test_basename_equal_to_a_declared_domain_is_reserved_for_its_map() -> None:
    """A concept may not take a name the lazily-minted ``wiki/maps/<subject>.md`` will need.

    D1.3 drops v1's ``-moc`` filename suffix, so the map's basename is the bare subject and shares
    ONE namespace with every concept. The map does not exist until the first concept of that
    subject is filed, so it can never be in ``live_basenames`` — the gate has to come from the
    declared domain set instead. Without it APPLY materializes an L1-1 duplicate basename out of a
    plan this gate called valid.
    """
    errors = _validate(_plan(_disp(basename="ai-tech", links=())))
    assert "BASENAME" in _checks(errors)
    assert any("wiki/maps/ai-tech.md" in e.message for e in errors)


def test_the_reservation_covers_a_domain_other_than_the_dispositions_own() -> None:
    """The CROSS-RUN wedge, which is the reachable shape.

    Run 1 files a concept named ``economy`` under subject ``ai-tech`` and publishes clean; from
    then on the FIRST run that classifies anything under subject ``economy`` mints
    ``wiki/maps/economy.md`` and every run touching that subject fails L1-1 forever — on an error
    naming neither the concept nor the disposition that triggered it. The reservation is over ALL
    declared domains, not just the disposition's own, so run 1 is rejected instead.
    """
    errors = _validate(_plan(_disp(domain="ai-tech", basename="economy", links=())))
    assert "BASENAME" in _checks(errors)


def test_the_reservation_reports_once_when_the_map_already_exists() -> None:
    """An already-materialized map is in ``live_basenames``; one collision, one error."""
    errors = validate_plan(
        _plan(_disp(basename="ai-tech-moc", links=())),
        manifest_event_ids={E1},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS | {"ai-tech-moc"},
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
    )
    basename_errors = [e for e in errors if e.check == "BASENAME"]
    assert len(basename_errors) == 1
    assert "already exists in the live worktree tree" in basename_errors[0].message


def test_a_basename_outside_the_domain_set_is_still_accepted() -> None:
    """The reservation is exactly the declared domains — it does not narrow ordinary naming."""
    assert _validate(_plan(_disp(basename="curator-concurrency"))) == []


# --- check 6 (schema 2): the kind-first path grammar, ADR-0041 D1 -------------------------------


def test_implied_path_create_theme_is_kind_first() -> None:
    """CREATE_THEME → ``wiki/concepts/<basename>.md``: the DIRECTORY is the kind (D1/D2.5).

    The subject has left the path entirely, which is the whole axis flip. Asserted on the composer
    rather than only through the validator because the shape is what APPLY has to agree with.
    """
    assert _implied_note_path(_disp()) == "wiki/concepts/curator-concurrency.md"


def test_implied_path_create_theme_needs_no_domain() -> None:
    """A subject is no longer a precondition for HAVING a path (ADR-0022 §A leg 1, retired).

    In v1 the note's path needed a domain, which is why the no-loss floor had to assert a possibly
    FALSE one. Schema 2 gives every concept a path regardless, so the floor's path leg is gone —
    the remaining ``domain`` requirement in check 5 is APPLY's, not the path's.
    """
    assert _implied_note_path(_disp(domain=None)) == "wiki/concepts/curator-concurrency.md"


def test_implied_path_append_daily_is_date_sharded_from_the_run_date() -> None:
    """APPEND_DAILY → ``wiki/notes/<yyyy>/<mm>/<run_date>.md`` (D2.6): shard AND basename from one
    curator-owned fact, never parsed back out of model output."""
    disp = _disp(op="APPEND_DAILY", basename=RUN_DATE)
    assert _implied_note_path(disp, run_date=RUN_DATE) == f"wiki/notes/2026/06/{RUN_DATE}.md"


def test_implied_path_daily_shard_ignores_a_model_supplied_basename() -> None:
    """The injected run_date WINS: a model basename can never move the note into another month."""
    disp = _disp(op="APPEND_DAILY", basename="1999-01-01")
    assert _implied_note_path(disp, run_date=RUN_DATE).startswith("wiki/notes/2026/06/")


def test_append_daily_basename_must_be_the_run_date_when_injected() -> None:
    """D2.6: one journal per run_date, repo-wide, BASENAMED by it — a mismatch is a PLAN reject."""
    plan = _plan(_disp(op="APPEND_DAILY", basename="ai-tech-2026-06-13", tags=(), links=()))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
        run_date=RUN_DATE,
    )
    assert "PATH-ALLOWLIST" in _checks(errors)
    assert any("is not the run date" in e.message for e in errors)


def test_append_daily_basename_equal_to_the_run_date_passes() -> None:
    plan = _plan(_disp(op="APPEND_DAILY", basename=RUN_DATE, tags=(), links=()))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=LIVE,
        theme_basenames=THEMES,
        gated_candidate_ids=set(),
        run_date=RUN_DATE,
    )
    assert errors == [], errors


# --- check 6 (schema 2): the pathsafe swap, ADR-0041 D4.4 ---------------------------------------
#
# The escape cases above (traversal, separator, leading dot) MUST stay red under the new rule —
# they are the reason the old regex existed. These add what the swap changes: what it now ADMITS
# (non-Latin scripts) and what it now REJECTS that the ASCII regex allowed (a leading underscore,
# a Windows device stem).


@pytest.mark.parametrize(
    "basename",
    [
        "한국어-메모",  # the widening the swap exists for (#57)
        "日本語",
        "Привет",
        "café",
        "my.note_v2-1",  # pathsafe's literal extras, unchanged from the ASCII regex
        "note-abc12345",  # the #57 hash floor's own output
    ],
)
def test_path_allowlist_admits_unicode_basenames(basename: str) -> None:
    """A Korean (or Japanese, or Cyrillic) note can now be NAMED, not just titled (#57/D4.4)."""
    assert _validate(_plan(_disp(basename=basename, links=()))) == []


@pytest.mark.parametrize(
    "basename",
    [
        "../../_kb/escape",
        "a/b",
        "a\\b",
        ".git",
        "..",
        ".",
        "a\x00b",
        "a\x1fb",
        "a\u202eb",  # bidi override
        "a\u200bb",  # zero-width space
        "a\uff0fb",  # fullwidth solidus
        'a"b',
        "a|b",
        "a?b",
        "a*b",
        "a:b",
        "",
        "   ",
        "!!!",
    ],
)
def test_path_allowlist_still_rejects_every_escape_shape(basename: str) -> None:
    """The closed-set property survives the widening: pathsafe is a CATEGORY allowlist, so every
    separator/control/invisible/Windows-hostile character is unreachable without being enumerated.
    """
    errors = _validate(_plan(_disp(basename=basename, links=())))
    assert "PATH-ALLOWLIST" in _checks(errors)


@pytest.mark.parametrize("basename", ["_blob", "_pages", "_kb", "_anything"])
def test_path_allowlist_rejects_a_leading_underscore_basename(basename: str) -> None:
    """ADR-0041 D4.4, NORMATIVE: the ONE character on which the swap is a LOOSENING.

    ``core.pathsafe`` puts ``_`` in its allowed extras and rejects only a leading ``.``, so
    ``is_safe_component('_blob')`` is True — while the deleted ASCII regex rejected it by its
    leading character class. That exclusion was the only thing stopping a plan token named
    ``_blob``, which shares a namespace with ``raw/_blob/``, so the rejection has to be explicit.
    """
    errors = _validate(_plan(_disp(basename=basename, links=())))
    assert "PATH-ALLOWLIST" in _checks(errors)
    assert any("_blob" in e.message for e in errors if e.check == "PATH-ALLOWLIST")


@pytest.mark.parametrize("basename", ["CON", "con.md", "COM1", "lpt9", "nul", "com¹"])
def test_path_allowlist_rejects_windows_reserved_device_stems(basename: str) -> None:
    """The tightening shipped WITH the widening: the ASCII regex admitted every one of these, and
    a repo cloned to Windows silently fails to check such a file out."""
    assert "PATH-ALLOWLIST" in _checks(_validate(_plan(_disp(basename=basename, links=()))))


def test_path_allowlist_rejects_an_over_long_basename() -> None:
    """The 180-UTF-8-byte cap (pathsafe's default) keeps a component inside POSIX NAME_MAX."""
    assert "PATH-ALLOWLIST" in _checks(_validate(_plan(_disp(basename="a" * 300, links=()))))


def test_path_allowlist_domain_is_graded_by_the_note_composers_rule() -> None:
    """The DOMAIN token is graded by ``_is_safe_basename``, the SAME rule as the basename.

    Under schema 2 the domain does not merely shard ``raw/``: ``apply._update_map`` composes
    ``wiki/maps/<subject>.md`` from it through ``RepoLayout.note_path_for`` (D1.3), so it names a
    NOTE. Grading it with a second, looser rule is how a plan this gate calls valid goes on to
    crash APPLY: the v1 ASCII regex admitted ``con`` (a Windows reserved device stem) and
    ``foo.md`` (an extension the composer appends itself), both of which the composer hard-rejects.
    """
    for domain in ("con", "foo.md", "a/b", ".."):
        errors = _validate(_plan(_disp(domain=domain, tags=())))
        assert "PATH-ALLOWLIST" in _checks(errors), domain


def test_path_allowlist_domain_admits_a_korean_token() -> None:
    """The other half of one-rule-one-spelling: the composer accepts ``wiki/maps/한국어.md``, so the
    PLAN gate must too. Rejecting it here (as the v1 ASCII rule did) wedges a Korean-domain repo —
    every CREATE_THEME/APPEND_DAILY in that domain fails forever — which is the exact shape
    #56/#57 exist to fix. TAXONOMY (check 4) still governs whether the domain is DECLARED.
    """
    errors = _validate(_plan(_disp(domain="한국어", tags=())))
    assert "PATH-ALLOWLIST" not in _checks(errors)
    assert "TAXONOMY" in _checks(errors), "undeclared is a taxonomy failure, not a path one"


def test_path_allowlist_grades_the_domain_on_a_merge_too() -> None:
    """MERGE/MARK_CONTESTED carry a domain and it composes ``raw/<domain>/<event_id>.md`` too.

    Grading only the two basename ops leaves the shard key of the two claim-bearing ops ungraded,
    which is how an escaping token still reaches a write.
    """
    merge = _disp(
        candidate_id="c1",
        op="MERGE_INTO_THEME",
        domain="../assets",
        basename=None,
        target_basename="cqrs",
        title=None,
        tags=(),
        aliases=(),
        links=(),
        needs_prose=False,
    )
    errors = _validate(_plan(merge))
    assert "PATH-ALLOWLIST" in _checks(errors)


def test_path_allowlist_domain_rejects_a_leading_underscore() -> None:
    """``_blob``/``_pages`` are RESERVED raw/ prefixes (D1.4). The taxonomy loader is the other
    layer over the other input; neither substitutes for the other."""
    assert "PATH-ALLOWLIST" in _checks(_validate(_plan(_disp(domain="_blob", tags=()))))


# --- check 6 (schema 2): the ONE allowlist constant, ADR-0041 D4.1 ------------------------------


def test_allowlist_constant_denies_the_people_carve_out() -> None:
    """D4.1/D3.3: ``wiki/people/**`` is human-owned — under ``wiki/`` and NOT allowlisted.

    Asserted on the shared predicate, not on a plan, because the point of D4.1 is that ONE
    constant serves both the PLAN check and the final-diff assertion: a test that only exercised
    the plan side would leave the worker side free to drift.
    """
    assert is_allowlisted_path("wiki/concepts/x.md") is True
    assert is_allowlisted_path("wiki/maps/x.md") is True
    assert is_allowlisted_path("index.md") is True
    assert is_allowlisted_path("log.md") is True
    assert is_allowlisted_path("assets/a.png") is True
    assert is_allowlisted_path("wiki/people/hando/x.md") is False
    assert is_allowlisted_path("wiki/people/x.md") is False
    # A FILE literally named `wiki/people` would occupy the directory's own name.
    assert is_allowlisted_path("wiki/people") is False


def test_allowlist_constant_rejects_a_traversal_segment() -> None:
    """A bare ``startswith`` allowlisted ``wiki/../_kb/index/x`` — a path with the right PREFIX
    naming a location outside the allowlist. Git's normalized output never spells it that way,
    which is exactly why it never fired; a gate that is safe only because its one caller happens
    to pass normalized input stops being a gate when a second caller appears.
    """
    assert is_allowlisted_path("wiki/../_kb/index/x") is False
    assert is_allowlisted_path("wiki/concepts/../../_meta/taxonomy.yaml") is False
    assert is_allowlisted_path("assets/../.git/config") is False
    # ".." INSIDE a component is a legal filename, not traversal.
    assert is_allowlisted_path("wiki/concepts/a..b.md") is True


def test_allowlist_constant_still_rejects_everything_outside_it() -> None:
    for path in (
        "_kb/index/notes.json",
        "_meta/taxonomy.yaml",
        "_templates/theme.md",
        "raw/ai-tech/e1.md",
        ".git/config",
        ".git/hooks/pre-commit",
        "AGENTS.md",
        "CLAUDE.md",
        "_agora_scratch/plan.json",
    ):
        assert is_allowlisted_path(path) is False, path


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
