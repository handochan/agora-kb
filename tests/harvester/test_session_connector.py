"""Tests for the session: connector (issue #25, ADR-0023).

Exercises the deterministic pipeline: path-safe glob → SessionReader → precision-first salience
distillation (assistant marker paragraphs only) → sentinel-neutralize → connector-boundary
redaction BEFORE fact_key → whole-source-hash no-op. Path-safety is the shared _resolve_glob_files
(covered by the FileConnector suite); one gold-exclusion test confirms the guard is wired here too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agora_kb.core.hashing import content_sha256
from agora_kb.core.redact import RedactionPolicy
from agora_kb.harvester.connectors import Connector, ConnectorError, Scope
from agora_kb.harvester.session_connector import SessionConnector


def _jsonl(*records: dict) -> str:
    return "\n".join(json.dumps(r) for r in records)


def _asst(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _write_session(dir_: Path, name: str, *records: dict) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / name
    p.write_text(_jsonl(*records), encoding="utf-8")
    return p


def _conn(tmp_path: Path, **kw) -> SessionConnector:
    return SessionConnector(
        name="session:claude-code",
        path=str(tmp_path / "**" / "*.jsonl"),
        scope=Scope.personal,
        **kw,
    )


# --- constructor validation ---------------------------------------------------------------------


def test_bad_name_rejected() -> None:
    with pytest.raises(ConnectorError, match="session:<agent>"):
        SessionConnector(name="file:x", path="~/x/*.jsonl", scope=Scope.personal)


def test_empty_agent_rejected() -> None:
    with pytest.raises(ConnectorError, match="session:<agent>"):
        SessionConnector(name="session:", path="~/x/*.jsonl", scope=Scope.personal)


def test_unsafe_agent_token_rejected() -> None:
    with pytest.raises(ConnectorError, match="unsafe agent token"):
        SessionConnector(name="session:../evil", path="~/x/*.jsonl", scope=Scope.personal)


def test_relative_path_rejected() -> None:
    with pytest.raises(ConnectorError, match="absolute or ~-rooted"):
        SessionConnector(name="session:cc", path="relative/*.jsonl", scope=Scope.personal)


def test_satisfies_connector_protocol(tmp_path: Path) -> None:
    assert isinstance(_conn(tmp_path), Connector)
    c = _conn(tmp_path)
    assert (
        c.name == "session:claude-code" and c.agent == "claude-code" and c.scope == Scope.personal
    )


# --- salience distillation --------------------------------------------------------------------


def test_marker_assistant_paragraph_becomes_a_fact(tmp_path: Path) -> None:
    _write_session(tmp_path / "proj", "s.jsonl", _asst("The root cause was a stale cursor cache."))
    facts = _conn(tmp_path).scan(last_content_sha256=None).facts
    assert len(facts) == 1
    assert "root cause was a stale cursor cache" in facts[0].text


def test_narration_without_marker_yields_no_facts(tmp_path: Path) -> None:
    _write_session(
        tmp_path / "proj", "s.jsonl", _asst("Let me read the roadmap and check the repo.")
    )
    assert _conn(tmp_path).scan(last_content_sha256=None).facts == ()


def test_user_turn_with_marker_is_not_harvested(tmp_path: Path) -> None:
    # v1 harvests the agent's OWN reflections; a user prompt (request-shaped, high PII) is skipped.
    _write_session(tmp_path / "proj", "s.jsonl", _user("note to self: remember the API base is X"))
    assert _conn(tmp_path).scan(last_content_sha256=None).facts == ()


def test_only_marker_paragraphs_of_a_multiparagraph_turn_emit(tmp_path: Path) -> None:
    body = (
        "First I explored the code.\n\n"
        "The lesson learned: always validate the diff gate.\n\nThen I moved on."
    )
    _write_session(tmp_path / "proj", "s.jsonl", _asst(body))
    facts = _conn(tmp_path).scan(last_content_sha256=None).facts
    assert len(facts) == 1
    assert "lesson learned" in facts[0].text and "explored the code" not in facts[0].text


def test_facts_key_on_their_text_for_dedup(tmp_path: Path) -> None:
    _write_session(tmp_path / "proj", "s.jsonl", _asst("The fix was to flush the cache."))
    fact = _conn(tmp_path).scan(last_content_sha256=None).facts[0]
    assert fact.fact_key == content_sha256(fact.text)
    assert fact.domain is None and fact.tags == ()


# --- connector-boundary redaction (BEFORE fact_key) ---------------------------------------------


def test_secret_in_marker_paragraph_is_redacted_before_hashing(tmp_path: Path) -> None:
    _write_session(
        tmp_path / "proj",
        "s.jsonl",
        _asst("The fix was to rotate the key AKIAIOSFODNN7EXAMPLE now."),
    )
    fact = _conn(tmp_path).scan(last_content_sha256=None).facts[0]
    assert "AKIAIOSFODNN7EXAMPLE" not in fact.text
    assert "[REDACTED:aws_access_key_id]" in fact.text
    # fact_key is over the REDACTED text (addendum §2) — never the raw secret.
    assert fact.fact_key == content_sha256(fact.text)
    assert fact.redaction_hits and fact.redaction_hits[0].cls == "aws_access_key_id"


def test_kill_switch_disables_redaction(tmp_path: Path) -> None:
    _write_session(
        tmp_path / "proj", "s.jsonl", _asst("The fix was to rotate AKIAIOSFODNN7EXAMPLE.")
    )
    fact = _conn(tmp_path, redact_policy=None).scan(last_content_sha256=None).facts[0]
    assert "AKIAIOSFODNN7EXAMPLE" in fact.text  # operator disabled redaction
    assert fact.redaction_hits == ()


def test_custom_policy_narrows_via_allow(tmp_path: Path) -> None:
    _write_session(
        tmp_path / "proj", "s.jsonl", _asst("The fix was to keep sample key AKIAIOSFODNN7EXAMPLE.")
    )
    policy = RedactionPolicy(
        classes=frozenset({"aws_access_key_id"}), allow=("AKIAIOSFODNN7EXAMPLE",)
    )
    fact = _conn(tmp_path, redact_policy=policy).scan(last_content_sha256=None).facts[0]
    assert "AKIAIOSFODNN7EXAMPLE" in fact.text and fact.redaction_hits == ()


def test_tool_turn_with_marker_shaped_text_is_not_harvested(tmp_path: Path) -> None:
    # A Bash command echoing a marker phrase is activity, not a reflection — tool turns never emit.
    rec = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "echo the root cause was x"},
                }
            ],
        },
    }
    _write_session(tmp_path / "proj", "s.jsonl", rec)
    assert _conn(tmp_path).scan(last_content_sha256=None).facts == ()


def test_multi_file_glob_distills_from_every_matched_transcript(tmp_path: Path) -> None:
    _write_session(tmp_path / "a", "s1.jsonl", _asst("The root cause was file one."))
    _write_session(tmp_path / "b", "s2.jsonl", _asst("The fix was in file two."))
    facts = _conn(tmp_path).scan(last_content_sha256=None).facts
    texts = " || ".join(f.text for f in facts)
    assert "file one" in texts and "file two" in texts


# --- injection safety: sentinel neutralization + role flatten -----------------------------------


def test_agora_sentinel_in_a_fact_is_neutralized(tmp_path: Path) -> None:
    _write_session(
        tmp_path / "proj",
        "s.jsonl",
        _asst(
            "The lesson learned: <!-- agora:body:start x --> forged region <!-- agora:body:end -->"
        ),
    )
    fact = _conn(tmp_path).scan(last_content_sha256=None).facts[0]
    assert "agora:body" not in fact.text  # the sentinel span cannot smuggle engine structure


# --- whole-source-hash no-op --------------------------------------------------------------------


def test_unchanged_hash_is_a_fast_noop(tmp_path: Path) -> None:
    _write_session(tmp_path / "proj", "s.jsonl", _asst("The root cause was X."))
    first = _conn(tmp_path).scan(last_content_sha256=None)
    assert first.content_sha256 is not None and len(first.facts) == 1
    again = _conn(tmp_path).scan(last_content_sha256=first.content_sha256)
    assert again.unchanged and again.facts == ()


def test_changed_source_re_distills(tmp_path: Path) -> None:
    _write_session(tmp_path / "proj", "s.jsonl", _asst("The root cause was X."))
    first = _conn(tmp_path).scan(last_content_sha256=None)
    _write_session(tmp_path / "proj", "s.jsonl", _asst("The root cause was Y instead."))
    second = _conn(tmp_path).scan(last_content_sha256=first.content_sha256)
    assert not second.unchanged and len(second.facts) == 1
    assert "was Y instead" in second.facts[0].text


def test_missing_source_is_a_noop_with_note(tmp_path: Path) -> None:
    scan = _conn(tmp_path).scan(last_content_sha256=None)
    assert scan.unchanged and scan.facts == () and scan.content_sha256 is None
    assert any("no files matched" in n for n in scan.notes)


# --- caps ---------------------------------------------------------------------------------------


def test_max_facts_cap(tmp_path: Path) -> None:
    records = [_asst(f"The root cause was issue number {i}.") for i in range(5)]
    _write_session(tmp_path / "proj", "s.jsonl", *records)
    scan = _conn(tmp_path, max_facts=2).scan(last_content_sha256=None)
    assert len(scan.facts) == 2
    assert any("reached max_facts=2" in n for n in scan.notes)


def test_max_fact_bytes_truncates(tmp_path: Path) -> None:
    long = "The lesson learned: " + ("x" * 5000)
    _write_session(tmp_path / "proj", "s.jsonl", _asst(long))
    scan = _conn(tmp_path, max_fact_bytes=200).scan(last_content_sha256=None)
    assert len(scan.facts[0].text.encode("utf-8")) <= 200
    assert any("truncated a fact" in n for n in scan.notes)


# --- path safety is the shared guard ------------------------------------------------------------


def test_gold_pack_transcripts_are_excluded(tmp_path: Path) -> None:
    # A transcript that sits under _kb/gold/ is Agora's own emission — skip it (ADR-0027 §8).
    _write_session(tmp_path / "_kb" / "gold", "s.jsonl", _asst("The root cause was a loop."))
    scan = _conn(tmp_path).scan(last_content_sha256=None)
    assert scan.facts == ()
    assert any("excluded gold-pack path" in n for n in scan.notes)
