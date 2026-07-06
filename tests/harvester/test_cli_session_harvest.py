"""CLI e2e for the session: connector wiring (issue #25, ADR-0023).

Drives the whole path through the real CLI: repo.yaml + adapters.yaml → load_redact_policy →
build_connectors → SessionConnector → connector-boundary redaction → Inbox.write → HarvestCursor.
The dry-run must never print a secret; the live run must persist REDACTED text and bump the
per-class HarvestCursor.redacted counter (surfaced by doctor).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agora_kb.cli import main
from agora_kb.core.frontmatter import parse
from agora_kb.core.layout import RepoLayout
from agora_kb.harvester import CursorStore

_SECRET = "AKIAIOSFODNN7EXAMPLE"  # AWS access-key-id shape (default-on class)


def _reflection(text: str) -> str:
    rec = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    return json.dumps(rec) + "\n"


def _setup_repo(
    tmp_path: Path, *, marker_text: str, redact_block: dict | None = None
) -> RepoLayout:
    layout = RepoLayout(tmp_path)
    layout.kb_dir.mkdir(parents=True, exist_ok=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text(_reflection(marker_text), encoding="utf-8")
    harvest: dict = {"enabled": True, "scope_lock": "personal"}
    if redact_block is not None:
        harvest["redact"] = redact_block
    (layout.kb_dir / "repo.yaml").write_text(
        yaml.safe_dump({"name": "demo", "kind": "personal", "harvest": harvest}), encoding="utf-8"
    )
    (tmp_path / "adapters.yaml").write_text(
        yaml.safe_dump(
            {"connectors": {"session:cc": {"path": str(sessions / "*.jsonl"), "scope": "personal"}}}
        ),
        encoding="utf-8",
    )
    return layout


def _inbox_bodies(layout: RepoLayout) -> list[str]:
    return [
        parse(p.read_text(encoding="utf-8"))[1] for p in sorted(layout.inbox_dir.glob("*/*.md"))
    ]


def test_cli_dry_run_previews_without_leaking_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = _setup_repo(tmp_path, marker_text=f"The fix was to rotate {_SECRET} now.")
    rc = main(["harvest", "--repo", str(tmp_path), "--dry-run"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "session:cc" in out and "would harvest 1 fact" in out
    assert _SECRET not in out  # THE blocker: the raw secret never reaches the terminal
    assert "[REDACTED:aws_access_key_id]" in out
    assert "would redact" in out
    assert list(layout.inbox_dir.glob("*/*.md")) == []  # dry-run wrote nothing


def test_cli_live_run_persists_redacted_text_and_bumps_counter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = _setup_repo(tmp_path, marker_text=f"The fix was to rotate {_SECRET} now.")
    rc = main(["harvest", "--repo", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    bodies = _inbox_bodies(layout)
    assert len(bodies) == 1
    # the inbox item carries the REDACTED text — never the raw secret (addendum §2).
    assert _SECRET not in bodies[0]
    assert "[REDACTED:aws_access_key_id]" in bodies[0]
    # the per-class HarvestCursor.redacted counter is bumped.
    cur = CursorStore(layout).load("session:cc")
    assert cur.proposed == 1
    assert cur.redacted == {"aws_access_key_id": 1}
    assert "redacted secret/PII" in out


def test_cli_doctor_shows_session_connector_and_redacted_counter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_repo(tmp_path, marker_text=f"The root cause was a leaked {_SECRET}.")
    main(["harvest", "--repo", str(tmp_path)])
    capsys.readouterr()
    main(["doctor", "--repo", str(tmp_path)])
    out = capsys.readouterr().out
    assert "session:cc" in out
    assert "redacted={aws_access_key_id=1}" in out


def test_cli_kill_switch_disables_redaction(tmp_path: Path) -> None:
    layout = _setup_repo(
        tmp_path,
        marker_text=f"The fix was to rotate {_SECRET} now.",
        redact_block={"enabled": False},
    )
    rc = main(["harvest", "--repo", str(tmp_path)])
    assert rc == 0
    bodies = _inbox_bodies(layout)
    # operator disabled redaction → the raw text persists verbatim (their explicit risk).
    assert _SECRET in bodies[0]
    assert CursorStore(layout).load("session:cc").redacted == {}


def test_cli_unknown_connector_type_fails_loud(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    layout.kb_dir.mkdir(parents=True, exist_ok=True)
    (layout.kb_dir / "repo.yaml").write_text(
        yaml.safe_dump({"name": "demo", "kind": "personal", "harvest": {"enabled": True}}),
        encoding="utf-8",
    )
    (tmp_path / "adapters.yaml").write_text(
        yaml.safe_dump({"connectors": {"git:x": {"path": "~/x", "scope": "personal"}}}),
        encoding="utf-8",
    )
    rc = main(["harvest", "--repo", str(tmp_path)])
    assert rc == 1  # dir:/git:/letta:/mem0: are deferred → fail loud
