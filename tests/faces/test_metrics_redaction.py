"""Tests for the dormant redaction counter family (issue #39, ADR-0023 decision 5).

``agora_harvester_redacted{connector,class}`` is DECLARED in #39 but has NO live source yet: the
per-class counts come from a persisted ``HarvestCursor.redacted`` field that lands with the session
connector (#25). So over a real repo the family is present with zero samples; a synthetic status map
proves the (connector, class) label shape #25 will populate, and that no secret ever appears.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.core import Repo

prometheus_client = pytest.importorskip("prometheus_client")


def _init_repo(tmp_path: Path) -> Repo:
    repo = Repo.resolve(tmp_path)
    repo.init()
    return repo


def _plant_connector(repo: Repo) -> None:
    """Enable harvest + declare one connector with a planted cursor (no 'redacted' source yet)."""
    from agora_kb.harvester import CursorStore, HarvestCursor

    repo.layout.kb_dir.mkdir(parents=True, exist_ok=True)
    (repo.layout.kb_dir / "repo.yaml").write_text(
        "name: personal\nkind: personal\nharvest:\n  enabled: true\n  scope_lock: personal\n",
        encoding="utf-8",
    )
    (repo.layout.root / "adapters.yaml").write_text(
        'connectors:\n  file:claude-code:\n    path: "~/.claude/MEMORY.md"\n    scope: personal\n',
        encoding="utf-8",
    )
    CursorStore(repo.layout).save(
        HarvestCursor(connector="file:claude-code", source_path="/x/MEMORY.md", proposed=4)
    )


def _families(text: str) -> dict[str, object]:
    from prometheus_client.parser import text_string_to_metric_families

    return {fam.name: fam for fam in text_string_to_metric_families(text)}


def test_redacted_family_declared_but_dormant(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _plant_connector(repo)
    from agora_kb.faces.web.metrics import render_latest

    text = render_latest(repo)[0].decode("utf-8")
    # DECLARED: HELP + TYPE lines are emitted for the family (prometheus appends the _total suffix
    # to a counter's name) ...
    assert "# HELP agora_harvester_redacted_total " in text
    assert "# TYPE agora_harvester_redacted_total counter" in text
    # ... but DORMANT: no per-connector source key yet, so zero samples (not a fake 0).
    assert "agora_harvester_redacted_total{" not in text
    # the help text is metadata-only by wording (never a secret).
    assert "never the" in text  # "...metadata-only; never the secret..."


def test_redacted_family_populated_from_synthetic_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    from agora_kb.faces.mcp_server import AgoraHandlers

    def _fake_harvester_status(self: AgoraHandlers) -> dict[str, object]:
        # The shape #25 will produce: a per-connector 'redacted' dict of class -> count.
        return {
            "enabled": True,
            "connectors": [
                {
                    "name": "file:demo",
                    "path": "~/x/MEMORY.md",
                    "scope": "personal",
                    "follow_links": False,
                    "last_scan": None,
                    "proposed": 3,
                    "accepted": 0,
                    "rejected": 0,
                    "redacted": {"aws_access_key_id": 2, "pem_private_key": 1},
                }
            ],
        }

    monkeypatch.setattr(AgoraHandlers, "harvester_status", _fake_harvester_status)
    from agora_kb.faces.web.metrics import render_latest

    text = render_latest(repo)[0].decode("utf-8")
    fam = _families(text)["agora_harvester_redacted"]
    samples = {
        (s.labels["connector"], s.labels["class"]): s.value
        for s in fam.samples  # type: ignore[attr-defined]
    }
    assert samples == {
        ("file:demo", "aws_access_key_id"): 2.0,
        ("file:demo", "pem_private_key"): 1.0,
    }
    # the label is the CLASS NAME — a category, never a secret value.
    assert "aws_access_key_id" in text and "pem_private_key" in text


def test_redacted_family_tolerates_a_non_dict_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Defensive: a future #25 bug supplying a non-dict 'redacted' must be skipped, never crash a
    # /metrics scrape (the isinstance guard).
    repo = _init_repo(tmp_path)
    from agora_kb.faces.mcp_server import AgoraHandlers

    def _fake(self: AgoraHandlers) -> dict[str, object]:
        return {
            "enabled": True,
            "connectors": [
                {
                    "name": "file:demo",
                    "path": "~/x",
                    "scope": "personal",
                    "follow_links": False,
                    "last_scan": None,
                    "proposed": 1,
                    "accepted": 0,
                    "rejected": 0,
                    "redacted": None,  # malformed source
                }
            ],
        }

    monkeypatch.setattr(AgoraHandlers, "harvester_status", _fake)
    from agora_kb.faces.web.metrics import render_latest

    text = render_latest(repo)[0].decode("utf-8")  # must not raise
    assert "# TYPE agora_harvester_redacted_total counter" in text
    assert "agora_harvester_redacted_total{" not in text  # non-dict → no samples
