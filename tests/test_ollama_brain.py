"""Unit tests for the Ollama curator-brain WRITE-adapter shim (NO network).

The shim is OUTSIDE the curator integrity boundary, so the crux test asserts the contract the worker
re-grades: a model raw plan, once :func:`normalize_plan`-ed, PASSES the real §4.1 PLAN validator
(:func:`agora_kb.curator.plan.validate_plan`) by construction. Every other test pins a pure-function
helper; :func:`call_ollama` / :func:`list_ollama_models` are monkeypatched so no HTTP ever happens.
"""

from __future__ import annotations

import json

import pytest

from agora_kb.adapters import ollama_brain as ob
from agora_kb.core.hashing import content_sha256
from agora_kb.curator.plan import Plan, validate_plan

RUN_ID = "2026-06-13T03-00-00.000Z--7f31ab"
E1 = "2026-06-13T02-40-10.000Z--a1b2c3"
E2 = "2026-06-13T02-41-00.000Z--d4e5f6"

ALLOWED_TAGS = {"curator", "concurrency", "architecture"}
DOMAINS = {"ai-tech", "economy", "general"}


# --- extract_json_object ----------------------------------------------------------------------


def test_extract_json_object_bare() -> None:
    assert ob.extract_json_object('{"a": 1}') == '{"a": 1}'


def test_extract_json_object_fenced() -> None:
    text = '```json\n{"a": 1, "b": [2, 3]}\n```'
    assert json.loads(ob.extract_json_object(text)) == {"a": 1, "b": [2, 3]}


def test_extract_json_object_prose_then_object() -> None:
    text = 'Sure, here is the plan you asked for:\n{"op": "DROP"}\ntrailing words'
    assert ob.extract_json_object(text) == '{"op": "DROP"}'


def test_extract_json_object_braces_inside_string() -> None:
    text = '{"reason": "use {curly} and \\"quotes\\" {nested}"}'
    extracted = ob.extract_json_object(text)
    assert json.loads(extracted)["reason"] == 'use {curly} and "quotes" {nested}'


def test_extract_json_object_none_raises() -> None:
    with pytest.raises(ob.BrainError):
        ob.extract_json_object("no object here at all")


def test_extract_json_object_unbalanced_raises() -> None:
    with pytest.raises(ob.BrainError):
        ob.extract_json_object('{"a": 1')


# --- select_model -----------------------------------------------------------------------------


def test_select_model_flag_wins() -> None:
    assert ob.select_model("llama3", "qwen3", ["zephyr", "qwen3.6"]) == "llama3"


def test_select_model_env_next() -> None:
    assert ob.select_model(None, "qwen-env", ["zephyr"]) == "qwen-env"


def test_select_model_qwen_preference() -> None:
    assert ob.select_model(None, None, ["zephyr", "qwen3.6:35b", "llama3"]) == "qwen3.6:35b"


def test_select_model_first_sorted_fallback() -> None:
    assert ob.select_model(None, None, ["zephyr", "llama3", "mistral"]) == "llama3"


def test_select_model_empty_raises() -> None:
    with pytest.raises(ob.BrainError):
        ob.select_model(None, None, [])


# --- parse_taxonomy ---------------------------------------------------------------------------


def test_parse_taxonomy_dict_form() -> None:
    doc = {"domains": ["ai-tech", "economy"], "allowed_tags": {"curator": {}, "ollama": {}}}
    tags, domains = ob.parse_taxonomy(doc)
    assert tags == {"curator", "ollama"}
    assert domains == {"ai-tech", "economy"}


def test_parse_taxonomy_list_form() -> None:
    doc = {"domains": ["general"], "allowed_tags": ["a", "b"]}
    tags, domains = ob.parse_taxonomy(doc)
    assert tags == {"a", "b"}
    assert domains == {"general"}


def test_parse_taxonomy_missing_keys() -> None:
    assert ob.parse_taxonomy({}) == (set(), set())
    assert ob.parse_taxonomy("not a dict") == (set(), set())


# --- related_basenames ------------------------------------------------------------------------


def test_related_basenames_union() -> None:
    docs = [
        {"hits": [{"path": "wiki/ai-tech/themes/foo.md"}, {"path": "wiki/economy/themes/bar.md"}]},
        {"hits": [{"path": "wiki/general/themes/baz.md"}]},
        {"hits": "malformed"},
        "not a dict",
    ]
    assert ob.related_basenames(docs) == {"foo", "bar", "baz"}


def test_related_theme_basenames_only_theme_paths() -> None:
    # Only hits whose path contains "/themes/" are themes; a MOC/index/daily path is excluded.
    docs = [
        {
            "hits": [
                {"path": "wiki/ai-tech/themes/foo.md"},  # theme -> kept
                {"path": "wiki/ai-tech/ai-tech-moc.md"},  # MOC -> excluded
                {"path": "wiki/ai-tech/daily/ai-tech-2026-06-13.md"},  # daily -> excluded
            ]
        },
        {"hits": [{"path": "wiki/economy/themes/bar.md"}, {"path": "index.md"}]},  # theme + index
        {"hits": "malformed"},
        "not a dict",
    ]
    assert ob.related_theme_basenames(docs) == {"foo", "bar"}
    # It is a subset of the all-stems set.
    assert ob.related_theme_basenames(docs) <= ob.related_basenames(docs)


# --- normalize_plan (the crux) ----------------------------------------------------------------


def _candidate(cid: str, *, text: str, event_id: str, gated: bool, domain: str | None) -> dict:
    return {
        "candidate_id": cid,
        "text": text,
        "is_gated": gated,
        "domain": domain,
        "provenance": [{"event_id": event_id}],
    }


def test_normalize_plan_passes_validator() -> None:
    """The full crux: normalize a messy model plan and assert it passes the real §4.1 validator."""
    candidates = [
        _candidate(
            "c1", text="Mutexes guard shared state", event_id=E1, gated=False, domain="ai-tech"
        ),
        _candidate(
            "c2", text="A harvested low-conf claim", event_id=E2, gated=True, domain="ai-tech"
        ),
    ]
    live_basenames = {"existing-note"}
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "create_theme",  # lowercase on purpose
                "domain": "ai-tech",
                "title": "Mutexes",
                "status": "active",
                "summary": "About mutexes.",
                "tags": ["concurrency", "NOT_A_TAG"],  # out-of-taxonomy tag must be dropped
                "links": ["existing-note", "phantom"],  # phantom must be dropped
                "reason": "new theme",
            },
            {
                "candidate_id": "c2",
                "op": "MERGE_INTO_THEME",
                "target_basename": "existing-note",
                "reason": "corroborates",
            },
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=live_basenames,
        live_theme_basenames=live_basenames,  # "existing-note" is a theme (c2 merges into it)
        run_id=RUN_ID,
    )
    plan = Plan.from_json(json.dumps(plan_dict))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1, E2},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=live_basenames,
        theme_basenames=live_basenames,
        gated_candidate_ids={"c2"},
    )
    assert errors == [], errors

    # event_ids exactly partition the manifest.
    all_events = [e for d in plan.dispositions for e in d.event_ids]
    assert sorted(all_events) == sorted([E1, E2])
    assert len(all_events) == len(set(all_events))

    by_id = {d.candidate_id: d for d in plan.dispositions}
    # needs_prose forced by op.
    assert by_id["c1"].needs_prose is True  # CREATE_THEME
    assert by_id["c2"].needs_prose is True  # MERGE_INTO_THEME
    # out-of-taxonomy tag dropped, phantom link dropped.
    assert by_id["c1"].tags == ("concurrency",)
    assert by_id["c1"].links == ("existing-note",)
    # gated candidate never CREATE_THEME.
    assert by_id["c2"].op == "MERGE_INTO_THEME"


def test_normalize_plan_gated_create_downgraded_to_drop() -> None:
    candidates = [
        _candidate("c1", text="harvested", event_id=E1, gated=True, domain="ai-tech"),
    ]
    raw = {
        "dispositions": [
            {"candidate_id": "c1", "op": "CREATE_THEME", "domain": "ai-tech", "reason": "x"}
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=set(),
        live_theme_basenames=set(),
        run_id=RUN_ID,
    )
    disp = plan_dict["dispositions"][0]
    assert disp["op"] == "DROP"
    assert disp["basename"] is None
    assert disp["target_basename"] is None
    assert disp["domain"] is None
    assert disp["status"] is None
    assert disp["tags"] == []
    assert disp["links"] == []
    assert disp["needs_prose"] is False
    assert disp["event_ids"] == [E1]  # provenance kept even on DROP


# --- catch_all_domain helper + the ADR-0022 §A no-loss floor -----------------------------------


def test_catch_all_domain() -> None:
    # first DECLARED domain (list order), read before parse_taxonomy's set-collapse.
    assert ob.catch_all_domain({"domains": ["general", "ai-tech"]}) == "general"
    assert ob.catch_all_domain({"domains": ("foo", "bar")}) == "foo"
    assert ob.catch_all_domain({"domains": [1, "two"]}) == "1"  # int coerced to str
    # empty / missing / mapping (ADR-0022 §B, deferred) / non-dict → None (floor is a safe no-op).
    assert ob.catch_all_domain({"domains": []}) is None
    assert ob.catch_all_domain({}) is None
    assert ob.catch_all_domain({"domains": {"general": {}}}) is None
    assert ob.catch_all_domain(None) is None
    assert ob.catch_all_domain("nope") is None


def test_load_taxonomy_arity(tmp_path) -> None:
    """Both return paths of _load_taxonomy are 3-tuples (locks the early-return arity the run_plan
    unpack depends on — a bundle with no taxonomy.yaml must not crash the PLAN pass)."""
    assert ob._load_taxonomy(tmp_path) == (set(), set(), None)  # no taxonomy.yaml (pre-emit repo)
    (tmp_path / "taxonomy.yaml").write_text(
        "domains:\n  - general\n  - ai-tech\nallowed_tags:\n  - curator\n", encoding="utf-8"
    )
    allowed_tags, domains, catch_all = ob._load_taxonomy(tmp_path)
    assert catch_all == "general"  # first declared domain
    assert domains == {"general", "ai-tech"}
    assert allowed_tags == {"curator"}


def test_normalize_plan_unresolvable_domain_routes_to_catch_all() -> None:
    """ADR-0022 §A: a NON-gated basename op whose domain is unresolvable floors to the catch-all
    (the first declared domain) instead of DROP — and the result still passes the §4.1 validator."""
    candidates = [
        _candidate(
            "c1", text="An orphaned durable fact", event_id=E1, gated=False, domain="no-such-domain"
        ),
    ]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "CREATE_THEME",
                "domain": "also-missing",  # neither model nor candidate domain ∈ DOMAINS
                "title": "Orphaned Fact",
                "status": "active",
                "summary": "A fact with no home domain.",
                "reason": "no matching domain",
            }
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=set(),
        live_theme_basenames=set(),
        run_id=RUN_ID,
        catch_all="general",
    )
    disp = plan_dict["dispositions"][0]
    assert disp["op"] == "CREATE_THEME"  # NOT dropped
    assert disp["domain"] == "general"  # floored to the catch-all
    assert disp["event_ids"] == [E1]
    # and it validates by construction (check-4 TAXONOMY passes: general ∈ DOMAINS).
    plan = Plan.from_json(json.dumps(plan_dict))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=set(),
        theme_basenames=set(),
        gated_candidate_ids=set(),
    )
    assert errors == [], errors


def test_normalize_plan_unresolvable_domain_no_catch_all_drops() -> None:
    """catch_all=None (empty taxonomy) keeps pre-floor behavior: unresolvable → DROP, no crash."""
    candidates = [
        _candidate("c1", text="x", event_id=E1, gated=False, domain="no-such-domain"),
    ]
    raw = {
        "dispositions": [
            {"candidate_id": "c1", "op": "CREATE_THEME", "domain": "bad", "reason": "x"}
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=set(),
        live_basenames=set(),
        live_theme_basenames=set(),
        run_id=RUN_ID,
        catch_all=None,
    )
    disp = plan_dict["dispositions"][0]
    assert disp["op"] == "DROP"
    assert disp["domain"] is None
    assert disp["event_ids"] == [E1]


def test_normalize_plan_gated_never_routes_to_catch_all() -> None:
    """A gated candidate is DROPped by the step-2 gate BEFORE step 3, so the floor can never
    originate a catch-all domain for harvested/low-confidence memory (ADR-0007)."""
    candidates = [
        _candidate("c1", text="harvested", event_id=E1, gated=True, domain="no-such-domain"),
    ]
    raw = {
        "dispositions": [
            {"candidate_id": "c1", "op": "CREATE_THEME", "domain": "bad", "reason": "x"}
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=set(),
        live_theme_basenames=set(),
        run_id=RUN_ID,
        catch_all="general",
    )
    disp = plan_dict["dispositions"][0]
    assert disp["op"] == "DROP"  # gate wins; floor never reached
    assert disp["domain"] is None


def test_normalize_plan_model_drop_stays_drop() -> None:
    """The floor rescues only originate-intent (CREATE_THEME/APPEND_DAILY). A model that chose DROP
    for genuine noise is not resurrected (DROP ∉ _BASENAME_OPS → step 3 skipped)."""
    candidates = [
        _candidate("c1", text="noise", event_id=E1, gated=False, domain="general"),
    ]
    raw = {"dispositions": [{"candidate_id": "c1", "op": "DROP", "reason": "noise"}]}
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=set(),
        live_theme_basenames=set(),
        run_id=RUN_ID,
        catch_all="general",
    )
    assert plan_dict["dispositions"][0]["op"] == "DROP"


def test_run_plan_e2e_floors_unclassifiable_capture_to_first_domain(tmp_path) -> None:
    """FULL brain PLAN path (real e2e): _load_taxonomy → catch_all_domain → normalize_plan.

    A non-gated capture whose domain matches nothing floors to domains[0] instead of DROP —
    exercising the run_plan wiring (3-tuple unpack + catch_all threading), not normalize_plan alone.
    """
    (tmp_path / "taxonomy.yaml").write_text(
        "domains:\n  - general\n  - ai-tech\nallowed_tags:\n  - architecture\n", encoding="utf-8"
    )
    (tmp_path / "candidates.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "candidates": [
                    {
                        "candidate_id": "c1",
                        "text": "an unclassifiable durable fact",
                        "is_gated": False,
                        "domain": "no-such-domain",
                        "provenance": [{"event_id": E1}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def infer(_prompt: str) -> str:
        # The model originates a theme but names a domain that is NOT in the taxonomy.
        return json.dumps(
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "finished": True,
                "dispositions": [
                    {
                        "candidate_id": "c1",
                        "op": "CREATE_THEME",
                        "domain": "no-such-domain",
                        "title": "Homeless Fact",
                        "status": "active",
                        "summary": "no home domain",
                        "reason": "no matching domain",
                    }
                ],
            }
        )

    plan = json.loads(ob.run_plan(tmp_path, "", infer=infer))
    disp = plan["dispositions"][0]
    assert disp["op"] == "CREATE_THEME"  # NOT dropped
    assert disp["domain"] == "general"  # floored to domains[0] (first declared)
    assert disp["event_ids"] == [E1]


def test_normalize_plan_append_daily_floors_to_catch_all() -> None:
    """APPEND_DAILY with an unresolvable domain also floors; basename is f'{catch_all}-{date}'."""
    candidates = [
        _candidate("c1", text="a daily log line", event_id=E1, gated=False, domain="bad"),
    ]
    raw = {
        "dispositions": [
            {"candidate_id": "c1", "op": "APPEND_DAILY", "domain": "bad", "reason": "x"}
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=set(),
        live_theme_basenames=set(),
        run_id=RUN_ID,
        catch_all="general",
    )
    disp = plan_dict["dispositions"][0]
    assert disp["op"] == "APPEND_DAILY"  # not dropped
    assert disp["domain"] == "general"
    assert disp["basename"].startswith("general-")


def test_normalize_plan_merge_unknown_target_downgraded_to_drop() -> None:
    candidates = [
        _candidate("c1", text="claim", event_id=E1, gated=False, domain="ai-tech"),
    ]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "MERGE_INTO_THEME",
                "target_basename": "does-not-exist",
                "reason": "x",
            }
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"existing-note"},
        live_theme_basenames={"existing-note"},
        run_id=RUN_ID,
    )
    disp = plan_dict["dispositions"][0]
    assert disp["op"] == "DROP"
    assert disp["target_basename"] is None
    # And it still validates.
    plan = Plan.from_json(json.dumps(plan_dict))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"existing-note"},
        theme_basenames={"existing-note"},
        gated_candidate_ids=set(),
    )
    assert errors == [], errors


def test_normalize_plan_merge_non_theme_target_downgraded_to_drop() -> None:
    """A MERGE_INTO_THEME whose target is a live note but NOT a theme (e.g. the MOC) becomes DROP.

    The target IS in ``live_basenames`` but absent from ``live_theme_basenames`` — MERGE/CONTEST may
    only target a theme (apply._resolve_target_path theme_only=True / validate_plan), so the shim
    downgrades exactly like an unknown target rather than emitting a plan APPLY would crash on.
    """
    candidates = [_candidate("c1", text="claim", event_id=E1, gated=False, domain="ai-tech")]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "MERGE_INTO_THEME",
                "target_basename": "ai-tech-moc",  # live, but a MOC (not a theme)
                "reason": "x",
            }
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"existing-note", "ai-tech-moc"},
        live_theme_basenames={"existing-note"},  # MOC excluded
        run_id=RUN_ID,
    )
    disp = plan_dict["dispositions"][0]
    assert disp["op"] == "DROP"
    assert disp["target_basename"] is None
    plan = Plan.from_json(json.dumps(plan_dict))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"existing-note", "ai-tech-moc"},
        theme_basenames={"existing-note"},
        gated_candidate_ids=set(),
    )
    assert errors == [], errors


def test_normalize_plan_contest_non_theme_target_downgraded_to_drop() -> None:
    """A MARK_CONTESTED whose target is a non-theme (the MOC) becomes DROP."""
    candidates = [_candidate("c1", text="claim", event_id=E1, gated=False, domain="ai-tech")]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "MARK_CONTESTED",
                "target_basename": "ai-tech-moc",  # MOC, not a theme
                "links": ["existing-note"],  # a real theme competitor
                "reason": "x",
            }
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"existing-note", "ai-tech-moc"},
        live_theme_basenames={"existing-note"},
        run_id=RUN_ID,
    )
    disp = plan_dict["dispositions"][0]
    assert disp["op"] == "DROP"
    assert disp["target_basename"] is None
    assert disp["links"] == []


def test_normalize_plan_contest_only_non_theme_link_downgraded_to_drop() -> None:
    """A MARK_CONTESTED whose ONLY competing link is a non-theme downgrades to DROP (empty links).

    The target is a real theme, but the sole competitor names the MOC. A contest names rival THEMES;
    the non-theme competitor is filtered out, leaving zero competing links, which downgrades the
    contest to DROP via the existing empty-contest-links rule.
    """
    candidates = [_candidate("c1", text="claim", event_id=E1, gated=False, domain="ai-tech")]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "MARK_CONTESTED",
                "target_basename": "existing-note",  # a real theme
                "links": ["ai-tech-moc"],  # only competitor is a MOC -> filtered out
                "reason": "x",
            }
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"existing-note", "ai-tech-moc"},
        live_theme_basenames={"existing-note"},
        run_id=RUN_ID,
    )
    disp = plan_dict["dispositions"][0]
    assert disp["op"] == "DROP"
    assert disp["target_basename"] is None
    assert disp["links"] == []
    assert disp["status"] is None
    assert disp["event_ids"] == [E1]


def test_normalize_plan_unknown_op_becomes_drop() -> None:
    candidates = [_candidate("c1", text="x", event_id=E1, gated=False, domain="ai-tech")]
    raw = {"dispositions": [{"candidate_id": "c1", "op": "FRANKENSTEIN", "reason": "x"}]}
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=set(),
        live_theme_basenames=set(),
        run_id=RUN_ID,
    )
    assert plan_dict["dispositions"][0]["op"] == "DROP"


def test_normalize_plan_create_basename_uniqueness() -> None:
    candidates = [
        _candidate("c1", text="Mutex Guards", event_id=E1, gated=False, domain="ai-tech"),
        _candidate("c2", text="Mutex Guards", event_id=E2, gated=False, domain="ai-tech"),
    ]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "CREATE_THEME",
                "domain": "ai-tech",
                "title": "Mutex Guards",
                "reason": "x",
            },
            {
                "candidate_id": "c2",
                "op": "CREATE_THEME",
                "domain": "ai-tech",
                "title": "Mutex Guards",
                "reason": "x",
            },
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"mutex-guards"},
        live_theme_basenames={"mutex-guards"},
        run_id=RUN_ID,
    )
    bases = [d["basename"] for d in plan_dict["dispositions"]]
    # live collision -> -2, within-plan collision -> -3.
    assert bases == ["mutex-guards-2", "mutex-guards-3"]
    plan = Plan.from_json(json.dumps(plan_dict))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1, E2},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"mutex-guards"},
        theme_basenames={"mutex-guards"},
        gated_candidate_ids=set(),
    )
    assert errors == [], errors


def test_normalize_plan_no_valid_domain_downgrades() -> None:
    candidates = [_candidate("c1", text="x", event_id=E1, gated=False, domain="nope")]
    raw = {
        "dispositions": [
            {"candidate_id": "c1", "op": "CREATE_THEME", "domain": "nope", "reason": "x"}
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=set(),
        live_theme_basenames=set(),
        run_id=RUN_ID,
    )
    assert plan_dict["dispositions"][0]["op"] == "DROP"


def test_normalize_plan_contested_no_links_downgraded_to_drop(tmp_path) -> None:
    """A MARK_CONTESTED with no resolvable competing link must become DROP.

    validate_plan ACCEPTS op=MARK_CONTESTED with links=[], but apply._apply_contested raises
    ApplyError on it (worker.run does not catch ApplyError) — so the shim must not emit it. We
    assert the downgrade, that it still validates, AND that apply_plan would not hit the contested
    precondition (op is no longer MARK_CONTESTED).
    """
    from agora_kb.curator.apply import apply_plan

    candidates = [_candidate("c1", text="claim", event_id=E1, gated=False, domain="ai-tech")]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "MARK_CONTESTED",
                "target_basename": "existing-note",
                "links": [],
                "reason": "x",
            }
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"existing-note"},
        live_theme_basenames={"existing-note"},
        run_id=RUN_ID,
    )
    disp = plan_dict["dispositions"][0]
    assert disp["op"] == "DROP"
    assert disp["target_basename"] is None
    assert disp["links"] == []
    assert disp["status"] is None
    assert disp["tags"] == []
    assert disp["event_ids"] == [E1]
    # Validates as a §4.1-valid plan.
    plan = Plan.from_json(json.dumps(plan_dict))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"existing-note"},
        theme_basenames={"existing-note"},
        gated_candidate_ids=set(),
    )
    assert errors == [], errors
    # And APPLY does not raise (no MARK_CONTESTED disposition reaches _apply_contested). A DROP/NOOP
    # plan needs no worktree edits, so apply_plan is a no-op over an empty tree.
    assert (
        apply_plan(
            plan,
            worktree=tmp_path,
            run_date=RUN_ID[:10],
            provenance={"c1": candidates[0]["provenance"]},
        )
        == {}
    )


def test_normalize_plan_contested_with_link_survives() -> None:
    """A MARK_CONTESTED whose link resolves to a DIFFERENT live basename keeps non-empty links."""
    candidates = [_candidate("c1", text="claim", event_id=E1, gated=False, domain="ai-tech")]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "MARK_CONTESTED",
                "target_basename": "existing-note",
                "links": ["rival-note", "existing-note", "phantom"],
                "reason": "x",
            }
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"existing-note", "rival-note"},
        live_theme_basenames={"existing-note", "rival-note"},
        run_id=RUN_ID,
    )
    disp = plan_dict["dispositions"][0]
    assert disp["op"] == "MARK_CONTESTED"
    assert disp["target_basename"] == "existing-note"
    # self-link to the target and the phantom are dropped; only the resolvable rival survives.
    assert disp["links"] == ["rival-note"]
    assert disp["status"] == "contested"
    plan = Plan.from_json(json.dumps(plan_dict))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"existing-note", "rival-note"},
        theme_basenames={"existing-note", "rival-note"},
        gated_candidate_ids=set(),
    )
    assert errors == [], errors


def test_normalize_plan_colliding_alias_dropped() -> None:
    """Model aliases that collide globally are dropped (they would fail the post-apply LINT L1-15).

    Covers: alias == a live basename, alias == the disposition's own basename, and two
    CREATE_THEME dispositions sharing an alias.
    """
    candidates = [
        _candidate("c1", text="Foo Thing", event_id=E1, gated=False, domain="ai-tech"),
        _candidate("c2", text="Bar Thing", event_id=E2, gated=False, domain="ai-tech"),
    ]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "CREATE_THEME",
                "domain": "ai-tech",
                "title": "Foo Thing",
                # collides with a live basename, with its own basename, plus a clean one.
                "aliases": ["existing-note", "foo-thing", "shared-alias", "Clean Alias"],
                "reason": "x",
            },
            {
                "candidate_id": "c2",
                "op": "CREATE_THEME",
                "domain": "ai-tech",
                "title": "Bar Thing",
                # 'shared-alias' already taken by c1; only a fresh one survives.
                "aliases": ["shared-alias", "Bar Nickname"],
                "reason": "x",
            },
        ]
    }
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames={"existing-note"},
        live_theme_basenames={"existing-note"},
        run_id=RUN_ID,
    )
    by_id = {d["candidate_id"]: d for d in plan_dict["dispositions"]}
    # c1: 'existing-note' (live basename) and 'foo-thing' (its own basename) dropped; the rest kept.
    assert by_id["c1"]["aliases"] == ["shared-alias", "clean-alias"]
    # c2: 'shared-alias' already claimed by c1 in-plan -> dropped; only the fresh one survives.
    assert by_id["c2"]["aliases"] == ["bar-nickname"]


# --- #57: Korean no-loss — hash-fallback basename, alias skip count, summary boundary cut ------


def _normalize_one(
    raw: dict,
    candidates: list[dict],
    *,
    live_basenames: set[str] | None = None,
    stats: dict[str, int] | None = None,
) -> dict:
    """Run :func:`normalize_plan` with the module defaults used across this section."""
    plan_dict = ob.normalize_plan(
        raw,
        candidates=candidates,
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=live_basenames or set(),
        live_theme_basenames=set(),
        run_id=RUN_ID,
        stats=stats,
    )
    return plan_dict


def test_normalize_plan_korean_seed_survives_with_hash_fallback() -> None:
    """(#57 a) A purely-Korean CREATE_THEME seed is NOT dropped: note-<sha8> fallback basename."""
    text = "한국어 지식은 큐레이션 중에 소실되면 안 된다"
    candidates = [_candidate("c1", text=text, event_id=E1, gated=False, domain="ai-tech")]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "CREATE_THEME",
                "domain": "ai-tech",
                "title": "한국어 지식 보존",
                "summary": "한국어 요약.",
                "reason": "새 테마",
            }
        ]
    }
    plan_dict = _normalize_one(raw, candidates)
    disp = plan_dict["dispositions"][0]
    assert disp["op"] == "CREATE_THEME"  # NOT DROP (the pre-#57 behavior)
    assert disp["basename"] == f"note-{content_sha256(text)[:8]}"
    # The Korean meaning is preserved in title:/summary: (arbitrary strings), never the filename.
    assert disp["title"] == "한국어 지식 보존"
    assert disp["summary"] == "한국어 요약."

    # The fallback basename passes the REAL §4.1 validator — path-safety regex untouched.
    plan = Plan.from_json(json.dumps(plan_dict))
    errors = validate_plan(
        plan,
        manifest_event_ids={E1},
        allowed_tags=ALLOWED_TAGS,
        domains=DOMAINS,
        live_basenames=set(),
        theme_basenames=set(),
        gated_candidate_ids=set(),
    )
    assert errors == [], errors


def test_normalize_plan_hash_fallback_reuses_candidate_content_sha256() -> None:
    """(#57 a) The candidates.json ``content_sha256`` field is the single hash source."""
    sha = "ab" * 32  # a well-formed 64-hex canonical hash, deliberately != content_sha256(text)
    candidate = _candidate("c1", text="한글", event_id=E1, gated=False, domain="ai-tech")
    candidate["content_sha256"] = sha
    raw = {
        "dispositions": [
            {"candidate_id": "c1", "op": "CREATE_THEME", "domain": "ai-tech", "reason": "x"}
        ]
    }
    disp = _normalize_one(raw, [candidate])["dispositions"][0]
    assert disp["basename"] == f"note-{sha[:8]}"  # field reused verbatim, not recomputed


def test_normalize_plan_hash_fallback_same_text_twice_gets_suffix() -> None:
    """(#57 b) Same text twice → same fallback slug + the existing -2 uniqueness suffix."""
    text = "같은 한국어 본문"
    candidates = [
        _candidate("c1", text=text, event_id=E1, gated=False, domain="ai-tech"),
        _candidate("c2", text=text, event_id=E2, gated=False, domain="ai-tech"),
    ]
    raw = {
        "dispositions": [
            {"candidate_id": "c1", "op": "CREATE_THEME", "domain": "ai-tech", "reason": "x"},
            {"candidate_id": "c2", "op": "CREATE_THEME", "domain": "ai-tech", "reason": "x"},
        ]
    }
    by_id = {d["candidate_id"]: d for d in _normalize_one(raw, candidates)["dispositions"]}
    base = f"note-{content_sha256(text)[:8]}"
    assert by_id["c1"]["basename"] == base
    assert by_id["c2"]["basename"] == f"{base}-2"  # existing collision loop rides the fallback


def test_normalize_plan_hash_fallback_title_falls_back_to_text_not_hash() -> None:
    """(#57 a) With no model title, the fallback title derives from the TEXT, not note-<sha8>."""
    text = "한국어 문장 하나"
    candidates = [_candidate("c1", text=text, event_id=E1, gated=False, domain="ai-tech")]
    raw = {
        "dispositions": [
            {"candidate_id": "c1", "op": "CREATE_THEME", "domain": "ai-tech", "reason": "x"}
        ]
    }
    disp = _normalize_one(raw, candidates)["dispositions"][0]
    assert disp["basename"].startswith("note-")
    assert disp["title"] == "한국어 문장 하나"  # _title_from(text); capitalize() is a Hangul no-op


def test_normalize_plan_ascii_seed_path_byte_identical() -> None:
    """(#57 c) An ASCII seed keeps the exact pre-#57 slug path (no hash fallback involvement)."""
    candidates = [
        _candidate("c1", text="Mutexes guard shared state", event_id=E1, gated=False, domain=None)
    ]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "CREATE_THEME",
                "domain": "ai-tech",
                "title": "Mutex Basics",
                "summary": "About mutexes.",
                "reason": "x",
            }
        ]
    }
    disp = _normalize_one(raw, candidates)["dispositions"][0]
    assert disp["basename"] == "mutex-basics"  # slugified seed, exactly as before
    assert disp["summary"] == "About mutexes."  # brain-given summary path unchanged
    assert not disp["basename"].startswith("note-")


def test_normalize_plan_korean_basename_ascii_title_uses_title_slug() -> None:
    """(#57 review) A Korean basename + ASCII title takes the TITLE slug, not the hash fallback."""
    candidates = [_candidate("c1", text="한글 본문", event_id=E1, gated=False, domain="ai-tech")]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "CREATE_THEME",
                "domain": "ai-tech",
                "basename": "한글이름",
                "title": "Clean Ascii Title",
                "reason": "x",
            }
        ]
    }
    disp = _normalize_one(raw, candidates)["dispositions"][0]
    assert disp["basename"] == "clean-ascii-title"  # next seed in the chain, not note-<sha8>
    assert disp["title"] == "Clean Ascii Title"


def test_normalize_plan_korean_basename_and_title_falls_to_ascii_text() -> None:
    """(#57 review) With Korean basename AND title, the capture TEXT is the last slug seed."""
    candidates = [
        _candidate("c1", text="Mutexes guard state", event_id=E1, gated=False, domain="ai-tech")
    ]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "CREATE_THEME",
                "domain": "ai-tech",
                "basename": "한글이름",
                "title": "한글 제목",
                "reason": "x",
            }
        ]
    }
    disp = _normalize_one(raw, candidates)["dispositions"][0]
    assert disp["basename"] == "mutexes-guard-state"
    assert not disp["basename"].startswith("note-")


def test_normalize_plan_mixed_korean_ascii_seed_keeps_residual_ascii_slug() -> None:
    """(#57 review) A MIXED Korean/ASCII seed keeps its residual-ASCII slug (pre-#57 path lock)."""
    candidates = [_candidate("c1", text="한글 본문", event_id=E1, gated=False, domain="ai-tech")]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "CREATE_THEME",
                "domain": "ai-tech",
                "basename": "에이전트 Memory 설계",
                "reason": "x",
            }
        ]
    }
    disp = _normalize_one(raw, candidates)["dispositions"][0]
    assert disp["basename"] == "memory"  # residual ASCII survives _slugify — no hash fallback
    assert not disp["basename"].startswith("note-")


def test_normalize_plan_brain_summary_over_limit_is_never_truncated() -> None:
    """(#57 review) A brain-SUPPLIED summary is preserved verbatim even past 200 chars.

    _truncate_summary applies ONLY to the fallback (brain gave none); this locks the invariant so
    a future "consistency" refactor cannot silently start clipping brain-authored summaries.
    """
    long_summary = "브레인이 직접 쓴 아주 긴 요약. " * 20  # well past _SUMMARY_MAX_CHARS
    assert len(long_summary) > 200
    candidates = [_candidate("c1", text="본문", event_id=E1, gated=False, domain="ai-tech")]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "CREATE_THEME",
                "domain": "ai-tech",
                "title": "Title",
                "summary": long_summary,
                "reason": "x",
            }
        ]
    }
    disp = _normalize_one(raw, candidates)["dispositions"][0]
    assert disp["summary"] == long_summary.strip()  # verbatim (stripped), NOT boundary-cut
    assert len(disp["summary"]) > 200


def test_normalize_plan_unslugifiable_alias_skipped_and_counted() -> None:
    """(#57 d) A Korean alias is skipped (no hash alias) and counted via the stats out-param."""
    candidates = [
        _candidate("c1", text="Foo Thing", event_id=E1, gated=False, domain="ai-tech"),
    ]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "CREATE_THEME",
                "domain": "ai-tech",
                "title": "Foo Thing",
                "aliases": ["한글별칭", "또다른한글", "Clean Alias"],
                "reason": "x",
            }
        ]
    }
    stats: dict[str, int] = {}
    disp = _normalize_one(raw, candidates, stats=stats)["dispositions"][0]
    assert disp["aliases"] == ["clean-alias"]  # Korean aliases skipped, ASCII alias kept
    assert stats["aliases_skipped_unslugifiable"] == 2


def test_normalize_plan_alias_stats_zero_when_all_slugifiable() -> None:
    candidates = [_candidate("c1", text="Foo", event_id=E1, gated=False, domain="ai-tech")]
    raw = {
        "dispositions": [
            {
                "candidate_id": "c1",
                "op": "CREATE_THEME",
                "domain": "ai-tech",
                "aliases": ["Fine Alias"],
                "reason": "x",
            }
        ]
    }
    stats: dict[str, int] = {}
    _normalize_one(raw, candidates, stats=stats)
    assert stats["aliases_skipped_unslugifiable"] == 0


def test_run_plan_e2e_korean_candidate_reports_skipped_alias(tmp_path, monkeypatch, capsys) -> None:
    """(#57 a+d e2e) run_plan: Korean capture survives; skipped-alias count hits stderr + debug."""
    debug_path = tmp_path / "debug.jsonl"
    monkeypatch.setenv("AGORA_BRAIN_DEBUG", str(debug_path))
    text = "순수 한글 캡처"
    (tmp_path / "taxonomy.yaml").write_text(
        "domains:\n  - general\nallowed_tags:\n  - architecture\n", encoding="utf-8"
    )
    (tmp_path / "candidates.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "candidates": [
                    {
                        "candidate_id": "c1",
                        "text": text,
                        "is_gated": False,
                        "domain": "general",
                        "content_sha256": content_sha256(text),
                        "provenance": [{"event_id": E1}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def infer(_prompt: str) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "finished": True,
                "dispositions": [
                    {
                        "candidate_id": "c1",
                        "op": "CREATE_THEME",
                        "domain": "general",
                        "title": "한글 제목",
                        "aliases": ["한글별칭"],
                        "reason": "새 지식",
                    }
                ],
            }
        )

    plan = json.loads(ob.run_plan(tmp_path, "", infer=infer))
    disp = plan["dispositions"][0]
    assert disp["op"] == "CREATE_THEME"
    assert disp["basename"] == f"note-{content_sha256(text)[:8]}"
    assert disp["aliases"] == []
    # one stderr warning line, count in the debug dump — the plan schema itself never widens.
    assert "skipped 1 un-slugifiable alias" in capsys.readouterr().err
    record = json.loads(debug_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["aliases_skipped_unslugifiable"] == 1
    assert "aliases_skipped_unslugifiable" not in plan


# --- _truncate_summary (#57 e) ----------------------------------------------------------------


def test_truncate_summary_short_text_unchanged() -> None:
    assert ob._truncate_summary("짧은 요약.", 200) == "짧은 요약."
    assert ob._truncate_summary("", 200) == ""


def test_truncate_summary_prefers_sentence_boundary() -> None:
    text = "첫 문장입니다. 둘째 문장입니다. 셋째 문장은 아주 길어서 잘립니다"
    assert ob._truncate_summary(text, 20) == "첫 문장입니다. 둘째 문장입니다."


def test_truncate_summary_ascii_sentence_boundary() -> None:
    text = "First sentence. Second sentence keeps going well past the limit"
    assert ob._truncate_summary(text, 20) == "First sentence."


def test_truncate_summary_decimal_number_is_not_a_sentence_end() -> None:
    # "." inside 3.5 is followed by a digit → falls back to the word (어절) boundary.
    text = "버전 3.5는 좋다 그리고 더 긴 텍스트가 이어진다"
    assert ob._truncate_summary(text, 8) == "버전 3.5는"


def test_truncate_summary_word_boundary_when_no_sentence_end() -> None:
    text = "가나다 라마바 사아자 차카타"
    assert ob._truncate_summary(text, 8) == "가나다 라마바"


def test_truncate_summary_hard_cut_when_unbroken() -> None:
    text = "가나다라마바사아자차"
    assert ob._truncate_summary(text, 5) == "가나다라마"


def test_truncate_summary_early_sentence_end_does_not_collapse() -> None:
    """(#57 review) A lone early sentence end must NOT shrink the summary below the old hard cut."""
    text = "1. " + "마침표없이이어지는아주긴한국어토큰" * 20  # 343 chars, no later boundary
    got = ob._truncate_summary(text, 200)
    assert got == text[:200]  # falls through sentence AND word floors to the hard cut
    assert got != "1."


def test_truncate_summary_early_word_boundary_does_not_collapse() -> None:
    """(#57 review) Same floor on the word-boundary leg: '결론. ' + unbroken run keeps 200 chars."""
    text = "결론. " + "가" * 300
    got = ob._truncate_summary(text, 200)
    assert got == text[:200]
    assert got != "결론."


def test_truncate_summary_short_sentence_falls_back_to_word_boundary() -> None:
    """(#57 review) A below-floor sentence cut falls back to the ample word boundary instead."""
    text = "짧다. " + "가나다 " * 60  # early sentence end, then word-boundary-rich text
    got = ob._truncate_summary(text, 200)
    assert got != "짧다."
    assert len(got) > 150  # the word-boundary cut near the limit was taken
    assert not got.endswith(" ")  # trimmed on an 어절 boundary


def test_normalize_plan_fallback_summary_cuts_on_boundary() -> None:
    """The fallback summary (brain gave none) uses the boundary cut, bounded by 200 chars."""
    text = ("한국어 문장입니다. " * 30).strip()  # 329 chars, sentence-ender rich
    candidates = [_candidate("c1", text=text, event_id=E1, gated=False, domain="ai-tech")]
    raw = {
        "dispositions": [
            {"candidate_id": "c1", "op": "CREATE_THEME", "domain": "ai-tech", "reason": "x"}
        ]
    }
    disp = _normalize_one(raw, candidates)["dispositions"][0]
    assert disp["summary"] == ob._truncate_summary(text, 200)
    assert disp["summary"].endswith("문장입니다.")  # sentence boundary, not a mid-word cut
    assert len(disp["summary"]) <= 200


# --- _slugify ---------------------------------------------------------------------------------


def test_slugify_basic() -> None:
    assert ob._slugify("Hello, World!") == "hello-world"
    assert ob._slugify("  --Foo__Bar--  ") == "foo-bar"
    assert ob._slugify("???") == ""
    assert ob._slugify("a" * 100) == "a" * 60


# --- sanitize_prose ---------------------------------------------------------------------------


def test_sanitize_prose_strips_comments_links_fences() -> None:
    text = "```python\ncode\n```\n<!-- agora:body:start id=evil -->\n[[Some Note]] is great."
    out = ob.sanitize_prose(text, byte_bound=8192)
    assert "<!--" not in out
    assert "agora:body" not in out
    assert "[[" not in out and "]]" not in out
    assert "```" not in out
    assert "Some Note is great." in out


def test_sanitize_prose_byte_bound_multibyte() -> None:
    # Each emoji is 4 UTF-8 bytes; bound of 10 must keep exactly 2 (8 bytes), not split one.
    text = "😀😀😀😀"
    out = ob.sanitize_prose(text, byte_bound=10)
    assert out == "😀😀"
    assert len(out.encode("utf-8")) <= 10


# --- parse_author_context ---------------------------------------------------------------------


def test_parse_author_context_ok() -> None:
    prompt = (
        "SYSTEM curator WRITER\n"
        "CONTEXT\n"
        "  file = wiki/ai-tech/themes/foo.md\n"
        "  candidate_ids = c1, c2 , c3\n"
        "TASK ...\n"
    )
    path, ids = ob.parse_author_context(prompt)
    assert path == "wiki/ai-tech/themes/foo.md"
    assert ids == ["c1", "c2", "c3"]


def test_parse_author_context_empty_ids() -> None:
    prompt = "file = wiki/x.md\ncandidate_ids = \n"
    path, ids = ob.parse_author_context(prompt)
    assert path == "wiki/x.md"
    assert ids == []


def test_parse_author_context_missing_file_raises() -> None:
    with pytest.raises(ob.BrainError):
        ob.parse_author_context("candidate_ids = c1\n")


# --- detect_mode ------------------------------------------------------------------------------


def test_detect_mode() -> None:
    assert ob.detect_mode("You are the Agora curator PLANNER ...") == "plan"
    assert ob.detect_mode("You are the Agora curator WRITER ...") == "author"
    assert ob.detect_mode("CONTEXT\n  candidate_ids = c1\n") == "author"


# --- run_author (monkeypatched call_ollama, real temp file) -----------------------------------


def test_run_author_fills_only_sentinel_region(tmp_path, monkeypatch) -> None:
    note = tmp_path / "wiki" / "ai-tech" / "themes" / "foo.md"
    note.parent.mkdir(parents=True)
    original = (
        "---\n"
        "title: Foo\n"
        "summary: A note about foo.\n"
        "---\n\n"
        "## Foo\n"
        "<!-- agora:body:start id=c1 -->\n"
        "PLACEHOLDER\n"
        "<!-- agora:body:end id=c1 -->\n"
        "\nFooter line untouched.\n"
    )
    note.write_text(original, encoding="utf-8")

    monkeypatch.setattr(ob, "call_ollama", lambda *a, **k: "Authored body about foo.")

    prompt = "curator WRITER\n  file = wiki/ai-tech/themes/foo.md\n  candidate_ids = c1\n"
    ob.run_author(tmp_path, prompt, model="x", host="http://h", temperature=0.1)

    result = note.read_text(encoding="utf-8")
    # Only the region body changed.
    assert "Authored body about foo." in result
    assert "PLACEHOLDER" not in result
    # Frontmatter + markers + out-of-region bytes preserved.
    assert "title: Foo" in result
    assert "summary: A note about foo." in result
    assert "<!-- agora:body:start id=c1 -->" in result
    assert "<!-- agora:body:end id=c1 -->" in result
    assert "Footer line untouched." in result
    assert "## Foo" in result


def test_run_author_region_failure_leaves_region(tmp_path, monkeypatch) -> None:
    note = tmp_path / "foo.md"
    original = (
        "---\ntitle: Foo\n---\n\n"
        "<!-- agora:body:start id=c1 -->\nKEEP\n<!-- agora:body:end id=c1 -->\n"
    )
    note.write_text(original, encoding="utf-8")

    def _boom(*a, **k):
        raise ob.BrainError("ollama down")

    monkeypatch.setattr(ob, "call_ollama", _boom)
    prompt = "curator WRITER\n  file = foo.md\n  candidate_ids = c1\n"
    # A per-region failure must NOT raise.
    ob.run_author(tmp_path, prompt, model="x", host="http://h", temperature=0.1)
    assert note.read_text(encoding="utf-8") == original


def test_run_author_missing_file_raises(tmp_path) -> None:
    prompt = "curator WRITER\n  file = nope.md\n  candidate_ids = c1\n"
    with pytest.raises(ob.BrainError):
        ob.run_author(tmp_path, prompt, model="x", host="http://h", temperature=0.1)


def test_run_author_preserves_non_targeted_regions(tmp_path, monkeypatch) -> None:
    """Only THIS run's candidate_ids are (re)authored; other regions stay byte-identical.

    A MERGE target carries its original CREATE_THEME region (already-published prose) plus this
    run's new merge region. The worker hands PASS 2 only the new candidate id, so the old region's
    real prose must NOT be regenerated/clobbered.
    """
    note = tmp_path / "foo.md"
    original = (
        "---\ntitle: Foo\nsummary: A note.\n---\n\n"
        "<!-- agora:body:start id=c0 -->\n"
        "IMPORTANT PRIOR-RUN PROSE\n"
        "<!-- agora:body:end id=c0 -->\n\n"
        "<!-- agora:body:start id=c1 -->\n"
        "NEW SOURCE FACT\n"
        "<!-- agora:body:end id=c1 -->\n"
    )
    note.write_text(original, encoding="utf-8")

    monkeypatch.setattr(ob, "call_ollama", lambda *a, **k: "regenerated generic body")

    # Prompt declares ONLY c1 — c0 is a prior-run region not in this run.
    prompt = "curator WRITER\n  file = foo.md\n  candidate_ids = c1\n"
    ob.run_author(tmp_path, prompt, model="x", host="http://h", temperature=0.1)

    result = note.read_text(encoding="utf-8")
    # c0's published prose is untouched.
    assert "IMPORTANT PRIOR-RUN PROSE" in result
    # c1 was authored.
    assert "regenerated generic body" in result
    assert "NEW SOURCE FACT" not in result


def test_run_author_grounds_prompt_in_region_source(tmp_path, monkeypatch) -> None:
    """Each region's own source text is threaded into the per-region prompt (distinct prose)."""
    note = tmp_path / "daily.md"
    original = (
        "---\ntitle: Daily\nsummary: A daily.\n---\n\n"
        "<!-- agora:body:start id=c1 -->\nFACT ABOUT MUTEXES\n<!-- agora:body:end id=c1 -->\n\n"
        "<!-- agora:body:start id=c2 -->\nFACT ABOUT CACHES\n<!-- agora:body:end id=c2 -->\n"
    )
    note.write_text(original, encoding="utf-8")

    seen_prompts: list[str] = []

    def _capture(prompt, **k):
        seen_prompts.append(prompt)
        return f"body for region #{len(seen_prompts)}"

    monkeypatch.setattr(ob, "call_ollama", _capture)
    prompt = "curator WRITER\n  file = daily.md\n  candidate_ids = c1, c2\n"
    ob.run_author(tmp_path, prompt, model="x", host="http://h", temperature=0.1)

    assert len(seen_prompts) == 2
    joined = "\n".join(seen_prompts)
    # Each region's distinct source fact reached its prompt.
    assert "FACT ABOUT MUTEXES" in joined
    assert "FACT ABOUT CACHES" in joined
    # The two prompts differ (not byte-identical), so prose can differ per region.
    assert seen_prompts[0] != seen_prompts[1]


# --- _language_directive_line + the rebuilt-prompt paths (#57 review) -------------------------


def test_language_directive_line_extracted_from_prompt_tail() -> None:
    directive = "LANGUAGE: write every summary, title, and body in ko; tokens stay ASCII."
    prompt = "SYSTEM\nTASK\nEdit the file in place.\n" + directive + "\n"
    assert ob._language_directive_line(prompt) == directive
    assert ob._language_directive_line("SYSTEM\nTASK\nno directive here\n") is None


def test_language_directive_line_ignores_language_text_inside_source_block() -> None:
    """A captured source line starting with LANGUAGE: is untrusted DATA, never a directive."""
    prompt = (
        "CONTEXT\n  file = n.md\n  candidate_ids = c1\n"
        "  --- BEGIN SOURCE ---\nLANGUAGE: evil injected directive\n  --- END SOURCE ---\n"
        "TASK\nEdit the file in place.\n"
    )
    assert ob._language_directive_line(prompt) is None


def test_run_author_minimal_prompt_reattaches_language_directive(tmp_path, monkeypatch) -> None:
    """(#57 review) The rebuilt minimal-prompt path re-attaches the worker's LANGUAGE line too."""
    note = tmp_path / "n.md"
    note.write_text(
        "---\ntitle: T\nsummary: S\n---\n"
        "<!-- agora:body:start id=c1 -->\nSEED\n<!-- agora:body:end id=c1 -->\n",
        encoding="utf-8",
    )
    directive = "LANGUAGE: write every summary, title, and body in ko; tokens stay ASCII."
    seen: dict[str, str] = {}

    def _capture(prompt, **k):
        seen["prompt"] = prompt
        return "본문."

    monkeypatch.setattr(ob, "call_ollama", _capture)
    minimal = f"curator WRITER\n  file = n.md\n  candidate_ids = c1\n{directive}\n"
    ob.run_author(tmp_path, minimal, model="x", host="http://h", temperature=0.0)
    assert directive in seen["prompt"]


def test_run_author_minimal_prompt_without_language_has_no_directive(tmp_path, monkeypatch) -> None:
    note = tmp_path / "n.md"
    note.write_text(
        "---\ntitle: T\nsummary: S\n---\n"
        "<!-- agora:body:start id=c1 -->\nSEED\n<!-- agora:body:end id=c1 -->\n",
        encoding="utf-8",
    )
    seen: dict[str, str] = {}

    def _capture(prompt, **k):
        seen["prompt"] = prompt
        return "body."

    monkeypatch.setattr(ob, "call_ollama", _capture)
    ob.run_author(tmp_path, "curator WRITER\n  file = n.md\n  candidate_ids = c1\n", model="x")
    assert "LANGUAGE:" not in seen["prompt"]  # unset language keeps the rebuilt prompt unchanged


# --- grounded_author_prompt + the §8.2 grounded run_author path -------------------------------


def _grounded_prompt(rel_path: str, sid: str, *, op: str, source: str) -> str:
    """A §8.2 GROUNDED PASS-2 prompt (mirrors SubprocessBackend._PASS2_GROUNDED_PROMPT_TEMPLATE)."""
    return (
        "SYSTEM\nYou are the Agora curator WRITER. Write the BODY of ONE wiki note region.\n"
        "CONTEXT\n"
        f"  file = {rel_path}\n"
        f"  candidate_ids = {sid}\n"
        "GROUNDING (the facts to write from)\n"
        f"  op = {op}\n"
        "  title = Curator concurrency\n"
        "  summary = single writer\n"
        "  source facts (verbatim captured text; ground your prose ONLY in this):\n"
        "  --- BEGIN SOURCE ---\n"
        f"{source}\n"
        "  --- END SOURCE ---\n"
        "TASK\nThis is a NEW theme note: write the FULL note body from the source facts. "
        "Write a concise body (<= 8192 bytes).\n"
    )


def test_grounded_author_prompt_detects_and_strips_control_lines() -> None:
    """A prompt with a source block is grounded; ``file =`` / ``candidate_ids =`` lines stripped."""
    prompt = _grounded_prompt(
        "wiki/ai-tech/themes/foo.md", "r--c1", op="CREATE_THEME", source="ONE per-repo flock"
    )
    grounded = ob.grounded_author_prompt(prompt)
    assert grounded is not None
    # The verbatim source + op-aware instruction survive into the model prompt.
    assert "ONE per-repo flock" in grounded
    assert "write the FULL note body" in grounded
    # The engine-plumbing control lines are stripped before the model sees the prompt.
    assert "file =" not in grounded
    assert "candidate_ids =" not in grounded


def test_grounded_author_prompt_minimal_returns_none() -> None:
    """A MINIMAL prompt (no source block) → None, so run_author uses the frontmatter fallback."""
    minimal = "curator WRITER\n  file = wiki/x.md\n  candidate_ids = c1\n"
    assert ob.grounded_author_prompt(minimal) is None


def test_grounded_author_prompt_preserves_control_lookalikes_in_source() -> None:
    """Control-lookalike lines INSIDE the verbatim source block must survive (not be stripped).

    Regression for the bug where the ``file =`` / ``candidate_ids =`` strip ran over the WHOLE
    prompt, silently emptying a candidate's source of config/YAML/code lines that happen to start
    with those tokens — defeating the grounding for that input class.
    """
    source = (
        "The config uses these keys:\n"
        "file = is a YAML key naming the target note\n"
        "candidate_ids = [a, b] lists the run-scoped ids\n"
        "Both are load-bearing fields."
    )
    prompt = _grounded_prompt(
        "wiki/ai-tech/themes/cfg.md", "r--c1", op="CREATE_THEME", source=source
    )
    grounded = ob.grounded_author_prompt(prompt)
    assert grounded is not None
    # The control-lookalike SOURCE lines survive verbatim into the model prompt.
    assert "file = is a YAML key naming the target note" in grounded
    assert "candidate_ids = [a, b] lists the run-scoped ids" in grounded
    # The engine-plumbing control lines OUTSIDE the source block are still stripped.
    assert "file = wiki/ai-tech/themes/cfg.md" not in grounded
    assert "candidate_ids = r--c1" not in grounded


def test_run_author_grounds_in_provided_source_not_frontmatter(tmp_path, monkeypatch) -> None:
    """With a §8.2 grounded prompt, the model is grounded in the prompt's SOURCE, not frontmatter.

    The note frontmatter title/summary are deliberately generic; the prompt's verbatim source is the
    distinguishing fact. The model prompt must carry the source (and NOT be the frontmatter
    fallback), proving PASS-2 grounding reaches Ollama.
    """
    note = tmp_path / "wiki" / "ai-tech" / "themes" / "foo.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntitle: GENERIC TITLE\nsummary: GENERIC SUMMARY\n---\n\n"
        "<!-- agora:body:start id=r--c1 -->\nPLACEHOLDER\n<!-- agora:body:end id=r--c1 -->\n",
        encoding="utf-8",
    )

    seen: list[str] = []

    def _capture(prompt, **k):
        seen.append(prompt)
        return "Authored from the grounded source."

    monkeypatch.setattr(ob, "call_ollama", _capture)

    prompt = _grounded_prompt(
        "wiki/ai-tech/themes/foo.md",
        "r--c1",
        op="CREATE_THEME",
        source="the curator holds a per-repo flock so writes serialize",
    )
    ob.run_author(tmp_path, prompt, model="x", host="http://h", temperature=0.0)

    assert len(seen) == 1
    model_prompt = seen[0]
    # Grounded in the prompt's verbatim SOURCE, not the (generic) note frontmatter.
    assert "the curator holds a per-repo flock so writes serialize" in model_prompt
    assert "GENERIC TITLE" not in model_prompt
    assert "GENERIC SUMMARY" not in model_prompt
    # The region was authored from the model output.
    result = note.read_text(encoding="utf-8")
    assert "Authored from the grounded source." in result
    assert "PLACEHOLDER" not in result


def test_arg_parser_temperature_defaults_to_zero() -> None:
    # A curator wants reproducible plans: the shim defaults to greedy decoding (temp 0.0).
    args = ob._build_arg_parser().parse_args([])
    assert args.temperature == 0.0
    # ...but it stays overridable for exploratory use.
    assert ob._build_arg_parser().parse_args(["--temperature", "0.7"]).temperature == 0.7


def test_debug_dump_noop_when_env_unset(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(ob._DEBUG_ENV, raising=False)
    # Must not raise and must not create any file.
    ob._debug_dump({"pass": "plan", "x": 1})
    assert list(tmp_path.iterdir()) == []


def test_debug_dump_appends_json_lines_when_env_set(monkeypatch, tmp_path) -> None:
    log = tmp_path / "brain-debug.jsonl"
    monkeypatch.setenv(ob._DEBUG_ENV, str(log))
    ob._debug_dump({"pass": "plan", "op": "CREATE_THEME"})
    ob._debug_dump({"pass": "author", "region": "c1"})
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["op"] == "CREATE_THEME"
    assert json.loads(lines[1])["region"] == "c1"


def test_debug_dump_swallows_io_error(monkeypatch, tmp_path) -> None:
    # A path under a non-existent directory is unwritable; diagnostics must never fail the run.
    monkeypatch.setenv(ob._DEBUG_ENV, str(tmp_path / "no-such-dir" / "x.jsonl"))
    ob._debug_dump({"pass": "plan"})  # must not raise


def test_run_author_emits_debug_record_when_env_set(monkeypatch, tmp_path) -> None:
    # Locks the observability contract: run_author actually calls _debug_dump with the author shape.
    log = tmp_path / "dbg.jsonl"
    monkeypatch.setenv(ob._DEBUG_ENV, str(log))
    note = tmp_path / "note.md"
    note.write_text(
        "---\ntitle: T\nsummary: S\n---\n\n"
        "<!-- agora:body:start id=c1 -->\nseed\n<!-- agora:body:end id=c1 -->\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ob, "call_ollama", lambda *a, **k: "authored body")
    prompt = "curator WRITER\n  file = note.md\n  candidate_ids = c1\n"
    ob.run_author(tmp_path, prompt, model="m", host="http://h", temperature=0.0)

    recs = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["pass"] == "author"
    assert rec["file"] == "note.md"
    assert rec["region"] == "c1"
    assert rec["prose_bytes"] == len(b"authored body")


def test_run_author_grounded_prompt_not_reused_across_multiple_targets(
    monkeypatch, tmp_path
) -> None:
    # A §8.2 grounded prompt is single-region by construction. If one ever names >1 region, the shim
    # must NOT reuse that single region's source for every region — it falls back to per-region.
    note = tmp_path / "daily.md"
    note.write_text(
        "---\ntitle: T\nsummary: S\n---\n\n"
        "<!-- agora:body:start id=r--c1 -->\nseed1\n<!-- agora:body:end id=r--c1 -->\n\n"
        "<!-- agora:body:start id=r--c2 -->\nseed2\n<!-- agora:body:end id=r--c2 -->\n",
        encoding="utf-8",
    )
    captured: list[str] = []

    def _capture(prompt, **k):
        captured.append(prompt)
        return "body"

    monkeypatch.setattr(ob, "call_ollama", _capture)
    grounded_prompt = (
        "curator WRITER\nCONTEXT\n  file = daily.md\n  candidate_ids = r--c1, r--c2\n"
        "  --- BEGIN SOURCE ---\nONLY-C1-SOURCE-FACT\n  --- END SOURCE ---\nTASK write.\n"
    )
    # Sanity: this prompt IS detected as grounded (single-region path would use it verbatim).
    assert ob.grounded_author_prompt(grounded_prompt) is not None
    ob.run_author(tmp_path, grounded_prompt, model="m", host="http://h", temperature=0.0)
    assert len(captured) == 2
    # Guard kicked in (2 targets) -> per-region fallback; the single grounded source is not reused.
    assert all("ONLY-C1-SOURCE-FACT" not in p for p in captured)
