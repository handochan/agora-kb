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
