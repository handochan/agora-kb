"""Tests for the harvest.redact config loader (issue #25, ADR-0023 addendum §5).

``load_redact_policy`` mirrors ``load_harvest_policy``: fail-loud, raw-mapping ``.get()`` path,
secure default (redaction ON with the Balanced-9 structural tier). The structural tier can only be
WIDENED via ``pii``/``deny`` or narrowly suppressed via ``allow`` — never dropped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.config import ConfigError, load_redact_policy, repo_config_path
from agora_kb.core.layout import RepoLayout
from agora_kb.core.redact import DEFAULT_ON_CLASSES


def _write_repo_yaml(layout: RepoLayout, text: str) -> None:
    layout.kb_dir.mkdir(parents=True, exist_ok=True)
    repo_config_path(layout).write_text(text, encoding="utf-8")


def test_default_is_enabled_with_the_structural_tier(tmp_path: Path) -> None:
    # No repo.yaml → secure default: redaction ON, Balanced-9 default-on set, empty allow/deny.
    settings = load_redact_policy(RepoLayout(tmp_path))
    assert settings.enabled is True
    assert settings.policy.classes == DEFAULT_ON_CLASSES
    assert settings.policy.allow == () and settings.policy.deny == ()


def test_absent_redact_block_takes_the_secure_default(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    _write_repo_yaml(layout, "name: demo\nkind: personal\nharvest:\n  enabled: true\n")
    settings = load_redact_policy(layout)
    assert settings.enabled is True
    assert settings.policy.classes == DEFAULT_ON_CLASSES


def test_kill_switch_disables_redaction(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    _write_repo_yaml(layout, "harvest:\n  redact:\n    enabled: false\n")
    settings = load_redact_policy(layout)
    assert settings.enabled is False
    # The policy still carries the structural tier; the connector honours `enabled` to skip it.
    assert settings.policy.classes == DEFAULT_ON_CLASSES


def test_pii_widens_but_never_drops_the_structural_tier(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    _write_repo_yaml(layout, "harvest:\n  redact:\n    pii: [generic_assigned_secret]\n")
    settings = load_redact_policy(layout)
    # every structural default-on class is still present, PLUS the opt-in one.
    assert DEFAULT_ON_CLASSES <= settings.policy.classes
    assert "generic_assigned_secret" in settings.policy.classes


def test_unknown_pii_class_fails_loud(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    _write_repo_yaml(layout, "harvest:\n  redact:\n    pii: [not_a_real_class]\n")
    with pytest.raises(ConfigError, match="unknown redaction class"):
        load_redact_policy(layout)


def test_allow_and_deny_literals_are_threaded(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    _write_repo_yaml(
        layout,
        "harvest:\n  redact:\n    allow: ['sk-sample-DEADBEEF']\n    deny: ['hunter2']\n",
    )
    settings = load_redact_policy(layout)
    assert settings.policy.allow == ("sk-sample-DEADBEEF",)
    assert settings.policy.deny == ("hunter2",)


def test_non_bool_enabled_fails_loud(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    _write_repo_yaml(layout, "harvest:\n  redact:\n    enabled: 'yes please'\n")
    with pytest.raises(ConfigError, match="harvest.redact.enabled"):
        load_redact_policy(layout)
