"""Tests for the repo + adapter config loaders (agora_kb.config; DATA-MODEL §3 / §8).

Covers the config-mapping module that owns ``_kb/repo.yaml`` (§3) and ``adapters.yaml`` (§8)
parsing: documented defaults for a pre-config repo, the write→load round-trip + byte-determinism,
the ``_meta/taxonomy.yaml``-wins precedence rule (and the never-widen-the-closed-set invariant), a
full §3 doc loading without tripping ``extra='forbid'``, the backend-registry wrapper's
absent→None / malformed→raise behavior, and the decided edge cases (integral-float coercion,
non-integral/typed-mismatch raise, malformed YAML raising a clean ConfigError).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agora_kb.config import (
    ConfigError,
    RepoConfig,
    load_backend_registry,
    load_repo_config,
    repo_config_path,
    write_default_repo_config,
)
from agora_kb.core.layout import RepoLayout
from agora_kb.curator.constants import DEFAULT_MAX_ATTEMPTS


def _layout(tmp_path: Path) -> RepoLayout:
    return RepoLayout(tmp_path)


def _write_repo_yaml(layout: RepoLayout, text: str) -> None:
    layout.kb_dir.mkdir(parents=True, exist_ok=True)
    repo_config_path(layout).write_text(text, encoding="utf-8")


def _write_meta_taxonomy(layout: RepoLayout, text: str) -> None:
    (layout.root / "_meta").mkdir(parents=True, exist_ok=True)
    (layout.root / "_meta" / "taxonomy.yaml").write_text(text, encoding="utf-8")


# --- (1) missing repo.yaml -> documented defaults -----------------------------------------------


def test_missing_repo_yaml_loads_documented_defaults(tmp_path: Path) -> None:
    cfg = load_repo_config(_layout(tmp_path))

    assert isinstance(cfg, RepoConfig)
    assert cfg.name == "personal"
    assert cfg.kind == "personal"
    assert cfg.default_backend == "qwen"
    # The §5.1 budget default tracks the worker's single source of truth (no silent drift).
    assert cfg.max_attempts == DEFAULT_MAX_ATTEMPTS
    # No _meta/taxonomy.yaml + no repo.yaml domains -> empty closed vocabulary.
    assert cfg.taxonomy.allowed_tags == ()
    assert cfg.taxonomy.domains == ()


# --- (2) write_default -> load round-trip + byte-determinism ------------------------------------


def test_write_default_then_load_round_trips(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    write_default_repo_config(layout, name="engineering", domains=["ai-tech", "general"])

    cfg = load_repo_config(layout)
    assert cfg.name == "engineering"
    assert cfg.kind == "personal"
    assert cfg.default_backend == "qwen"
    assert cfg.max_attempts == DEFAULT_MAX_ATTEMPTS
    # No _meta/taxonomy.yaml emitted here, so domains fall back to repo.yaml domains.
    assert cfg.taxonomy.domains == ("ai-tech", "general")


def test_write_default_is_byte_deterministic_on_rewrite(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    p1 = write_default_repo_config(layout, name="engineering", domains=["ai-tech"])
    first = p1.read_bytes()
    p2 = write_default_repo_config(layout, name="engineering", domains=["ai-tech"])
    assert p2.read_bytes() == first


def test_write_default_kind_team_is_settable(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    write_default_repo_config(layout, name="engineering", domains=["ai-tech"], kind="team")
    assert load_repo_config(layout).kind == "team"


# --- (3) _meta/taxonomy.yaml wins; repo.yaml can never widen allowed_tags -----------------------


def test_meta_taxonomy_wins_over_repo_domains_and_owns_allowed_tags(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    # repo.yaml lists DIFFERENT domains + no tags; _meta/taxonomy.yaml is authoritative.
    _write_repo_yaml(layout, "name: r\ndomains: [repo-only-domain]\n")
    _write_meta_taxonomy(
        layout,
        yaml.safe_dump(
            {
                "schema_version": 2,
                "taxonomy_policy": "review-only",
                "allowed_tags": {"curator": {}, "concurrency": {}},
                "domains": ["ai-tech", "general"],
            },
            sort_keys=False,
        ),
    )

    cfg = load_repo_config(layout)
    # _meta wins for domains/policy/schema_version.
    assert cfg.taxonomy.domains == ("ai-tech", "general")
    assert cfg.taxonomy.taxonomy_policy == "review-only"
    assert cfg.taxonomy.schema_version == 2
    # allowed_tags comes ONLY from _meta — repo.yaml (which has no tags) can never widen it.
    assert set(cfg.taxonomy.allowed_tags) == {"curator", "concurrency"}


def test_repo_yaml_never_synthesizes_allowed_tags(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    # repo.yaml has domains but NO _meta/taxonomy.yaml -> allowed_tags stays empty (never widened).
    _write_repo_yaml(layout, "name: r\ndomains: [ai-tech]\n")
    cfg = load_repo_config(layout)
    assert cfg.taxonomy.allowed_tags == ()
    assert cfg.taxonomy.domains == ("ai-tech",)


# --- (4) full §3 doc loads without tripping extra='forbid' --------------------------------------


def test_full_data_model_section3_doc_loads(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    # The full DATA-MODEL §3 example shape (git_remote/routing/limits/lint/health/harvest included).
    _write_repo_yaml(
        layout,
        """\
name: engineering
kind: team
schema_version: 1
domains: [ai-tech, economy, general]
git_remote: https://forgejo.internal/agora/engineering.git
review_mode: pr
curator:
  backend: claude
  routing:
    bulk_daily: qwen
    hard_merge: claude
    ambiguity_band: { low: 0.35, high: 0.70 }
    top2_delta: 0.10
  limits:
    body_byte_bound: 8192
    related_k: 8
  lint:
    max_orphans: 0
  max_attempts: 5
  triggers:
    cron: "0 3 * * *"
    threshold: 10
    idle_minutes: 30
health:
  stale_days: 90
harvest:
  enabled: true
  scope_lock: personal
""",
    )

    cfg = load_repo_config(layout)
    assert cfg.name == "engineering"
    assert cfg.kind == "team"
    assert cfg.default_backend == "claude"
    # The headline deliverable: an operator-set max_attempts is HONORED (not silently 3).
    assert cfg.max_attempts == 5
    assert cfg.triggers.threshold == 10
    assert cfg.triggers.idle_minutes == 30
    assert cfg.triggers.cron == "0 3 * * *"


# --- (5) backend registry: absent -> None; malformed -> raise -----------------------------------


def test_load_backend_registry_absent_is_none(tmp_path: Path) -> None:
    assert load_backend_registry(tmp_path / "adapters.yaml") is None


def test_load_backend_registry_present_loads(tmp_path: Path) -> None:
    p = tmp_path / "adapters.yaml"
    p.write_text(
        'backends:\n  qwen: { argv: ["qwen", "--headless"], cwd: "{worktree}" }\n'
        "default_backend: qwen\n",
        encoding="utf-8",
    )
    registry = load_backend_registry(p)
    assert registry is not None
    assert registry.get("qwen").argv == ("qwen", "--headless")


def test_load_backend_registry_malformed_raises(tmp_path: Path) -> None:
    p = tmp_path / "adapters.yaml"
    # A backends mapping with no default_backend is a config error the registry must reject.
    p.write_text('backends:\n  qwen: { argv: ["qwen"] }\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_backend_registry(p)


# --- (6) edge cases: integral-float coercion, typed-mismatch raise, malformed YAML --------------


def test_integral_float_max_attempts_is_coerced(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: r\ncurator:\n  max_attempts: 5.0\n")
    assert load_repo_config(layout).max_attempts == 5


def test_non_integral_max_attempts_raises_clear_error(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: r\ncurator:\n  max_attempts: 5.5\n")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(layout)
    assert "curator.max_attempts" in str(exc.value)


def test_boolean_threshold_raises_clear_error(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: r\ncurator:\n  triggers:\n    threshold: true\n")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(layout)
    assert "curator.triggers.threshold" in str(exc.value)


def test_out_of_range_max_attempts_raises(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    # max_attempts: 0 violates the RepoConfig ge=1 bound (pydantic ValidationError).
    _write_repo_yaml(layout, "name: r\ncurator:\n  max_attempts: 0\n")
    with pytest.raises(ValueError):
        load_repo_config(layout)


def test_malformed_repo_yaml_raises_config_error(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    # An unquoted '@daily' is invalid YAML (a reserved indicator) — must surface as ConfigError, not
    # an unhandled ScannerError stacktrace.
    _write_repo_yaml(layout, "name: r\ncurator:\n  triggers:\n    cron: @daily\n")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(layout)
    assert "malformed YAML" in str(exc.value)
