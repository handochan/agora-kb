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
    BackupPolicy,
    ConfigError,
    RepoConfig,
    WebConfig,
    load_backend_registry,
    load_backup_policy,
    load_repo_config,
    load_web_config,
    repo_config_path,
    write_default_repo_config,
)
from agora_kb.core.layout import RepoLayout
from agora_kb.curator.constants import (
    DEFAULT_BODY_BYTE_BOUND,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RELATED_K,
)


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
    # ADR-0022 step 2: the repo-global thresholds in this §3 example are now WIRED (were ignored).
    assert cfg.body_byte_bound == 8192
    assert cfg.related_k == 8
    assert cfg.max_orphans == 0


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


# --- (4b) repo-global curator thresholds (ADR-0022 step 2) ---------------------------------------


def test_curator_thresholds_default_when_absent(tmp_path: Path) -> None:
    """No threshold keys → the curator/constants defaults; max_orphans stays None (check off)."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: r\ncurator:\n  backend: qwen\n")
    cfg = load_repo_config(layout)
    assert cfg.body_byte_bound == DEFAULT_BODY_BYTE_BOUND
    assert cfg.related_k == DEFAULT_RELATED_K
    assert cfg.max_orphans is None


def test_curator_thresholds_parsed(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_repo_yaml(
        layout,
        "name: r\ncurator:\n  limits:\n    body_byte_bound: 4096\n    related_k: 3\n"
        "  lint:\n    max_orphans: 5\n",
    )
    cfg = load_repo_config(layout)
    assert cfg.body_byte_bound == 4096
    assert cfg.related_k == 3
    assert cfg.max_orphans == 5


def test_curator_language_default_none(tmp_path: Path) -> None:
    """(#57) No curator.language key → None (prompts stay byte-identical downstream)."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: r\ncurator:\n  backend: qwen\n")
    assert load_repo_config(layout).language is None


def test_curator_language_parsed(tmp_path: Path) -> None:
    """(#57) curator.language: ko is read via the explicit-field mapping like curator.backend."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: r\ncurator:\n  language: ko\n")
    assert load_repo_config(layout).language == "ko"


def test_curator_language_yaml_bool_trap_fails_loud(tmp_path: Path) -> None:
    """(#57) `language: no` (Norwegian) is a YAML 1.1 boolean → fail-loud, never a silent None."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: r\ncurator:\n  language: no\n")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(layout)
    assert "curator.language" in str(exc.value)
    assert "quote" in str(exc.value)  # the message tells the operator the actual fix


def test_curator_language_quoted_no_is_norwegian(tmp_path: Path) -> None:
    """(#57) A QUOTED "no" survives YAML 1.1 and reads as the string language code."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, 'name: r\ncurator:\n  language: "no"\n')
    assert load_repo_config(layout).language == "no"


def test_curator_threshold_malformed_raises(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: r\ncurator:\n  limits:\n    body_byte_bound: huge\n")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(layout)
    assert "curator.limits.body_byte_bound" in str(exc.value)


def test_curator_related_k_zero_out_of_range_raises(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    # related_k: 0 violates the RepoConfig ge=1 bound (pydantic ValidationError).
    _write_repo_yaml(layout, "name: r\ncurator:\n  limits:\n    related_k: 0\n")
    with pytest.raises(ValueError):
        load_repo_config(layout)


def test_curator_max_orphans_negative_out_of_range_raises(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    # max_orphans: -1 violates the RepoConfig ge=0 bound (pydantic ValidationError).
    _write_repo_yaml(layout, "name: r\ncurator:\n  lint:\n    max_orphans: -1\n")
    with pytest.raises(ValueError):
        load_repo_config(layout)


def test_curator_threshold_mis_nested_key_silently_ignored(tmp_path: Path) -> None:
    """The loader hand-maps via raw.get(), so a threshold key under the wrong parent (or a flat
    curator.related_k, which is NOT the repo-global shape) loads without effect — defaults hold."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: r\ncurator:\n  related_k: 3\n  unknown_key: 9\n")
    cfg = load_repo_config(layout)  # no raise (extra='forbid' is bypassed by the hand-map)
    assert cfg.related_k == DEFAULT_RELATED_K  # flat curator.related_k is NOT consumed


def test_malformed_repo_yaml_raises_config_error(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    # An unquoted '@daily' is invalid YAML (a reserved indicator) — must surface as ConfigError, not
    # an unhandled ScannerError stacktrace.
    _write_repo_yaml(layout, "name: r\ncurator:\n  triggers:\n    cron: @daily\n")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(layout)
    assert "malformed YAML" in str(exc.value)


# --- (n) WebConfig — the web-face operator settings keystone (ADR-0025) -------------------------


def test_web_config_absent_file_loads_defaults(tmp_path: Path) -> None:
    """No repo.yaml (and no web: block) → every WebConfig field is its documented default."""
    cfg = load_web_config(_layout(tmp_path))

    assert isinstance(cfg, WebConfig)
    assert cfg.graph.max_nodes == 10_000  # LARGE default per ADR-0025
    assert cfg.graph.max_depth == 3
    assert cfg.upload.max_bytes == 25 * 1024 * 1024
    assert cfg.upload.max_files == 50
    assert cfg.upload.total_bytes == 200 * 1024 * 1024
    assert cfg.extensions.allowed is None
    assert cfg.features.graph_enabled is True


def test_web_config_absent_web_block_loads_defaults(tmp_path: Path) -> None:
    """A repo.yaml WITHOUT a web: block still yields all WebConfig defaults (tolerant loader)."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: r\nkind: personal\n")
    cfg = load_web_config(layout)
    assert cfg.graph.max_nodes == 10_000
    assert cfg.features.graph_enabled is True


def test_web_config_overrides_each_field(tmp_path: Path) -> None:
    """An explicit web: block overrides each field; the allow-list is normalized to dotted/lower."""
    layout = _layout(tmp_path)
    _write_repo_yaml(
        layout,
        "web:\n"
        "  graph:\n"
        "    max_nodes: 250\n"
        "    max_depth: 2\n"
        "  upload:\n"
        "    max_bytes: 1048576\n"
        "    max_files: 5\n"
        "    total_bytes: 5242880\n"
        "  extensions:\n"
        "    allowed: [PDF, .md, txt]\n"
        "  features:\n"
        "    graph_enabled: false\n",
    )
    cfg = load_web_config(layout)
    assert cfg.graph.max_nodes == 250
    assert cfg.graph.max_depth == 2
    assert cfg.upload.max_bytes == 1048576
    assert cfg.upload.max_files == 5
    assert cfg.upload.total_bytes == 5242880
    # Each allowed extension is normalized to a lowercased, dot-prefixed form.
    assert cfg.extensions.allowed == [".pdf", ".md", ".txt"]
    assert cfg.features.graph_enabled is False


def test_web_config_bad_int_fails_loud(tmp_path: Path) -> None:
    """A wrong-typed numeric (a string) fails loud with a ConfigError, not a default."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  graph:\n    max_nodes: lots\n")
    with pytest.raises(ConfigError, match="web.graph.max_nodes"):
        load_web_config(layout)


def test_web_config_bad_bool_fails_loud(tmp_path: Path) -> None:
    """A non-boolean feature flag (the string 'yes') fails loud, never truthy-coerces."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  features:\n    graph_enabled: 'yes'\n")
    with pytest.raises(ConfigError, match="web.features.graph_enabled"):
        load_web_config(layout)


def test_web_config_non_list_allowed_fails_loud(tmp_path: Path) -> None:
    """A non-list extensions.allowed (a scalar) is operator confusion → fail loud."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  extensions:\n    allowed: .pdf\n")
    with pytest.raises(ConfigError, match="web.extensions.allowed"):
        load_web_config(layout)


def test_web_config_non_string_allowed_entry_fails_loud(tmp_path: Path) -> None:
    """A non-string entry in extensions.allowed fails loud — never silently dropped.

    ``_str_list`` would discard the ``123`` and load ``['.pdf', '.md']``, silently changing the
    operator's stated policy. The loader must surface it (mirrors the "must be a list" rule).
    """
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  extensions:\n    allowed: [.pdf, 123, .md]\n")
    with pytest.raises(ConfigError, match="entries must be strings"):
        load_web_config(layout)


def test_web_config_ge_one_validators(tmp_path: Path) -> None:
    """A zero count violates the ge=1 validator on the sub-model → ValidationError surfaces."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  upload:\n    max_files: 0\n")
    with pytest.raises(ValueError):  # pydantic ValidationError is a ValueError subclass
        load_web_config(layout)


def test_web_config_unknown_subkey_is_ignored(tmp_path: Path) -> None:
    """An unknown sub-key under a sub-model is IGNORED at the loader level (only WIRED keys read).

    Consistent with the other .get()-based loaders — a stray key never crashes the face. (The
    pydantic sub-models are extra='forbid', but the loader never feeds them the raw dict; it reads
    only the wired keys, so an unknown key is silently dropped, matching load_repo_config.)
    """
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  graph:\n    max_nodes: 7\n    typo_key: 99\n")
    cfg = load_web_config(layout)
    assert cfg.graph.max_nodes == 7  # the wired key is read; the unknown one is dropped


def test_web_config_not_added_to_repo_config(tmp_path: Path) -> None:
    """RepoConfig stays a separate concern — it has no `web` field (web policy is its own model)."""
    assert "web" not in RepoConfig.model_fields


# --- backup policy (push-only git backup, issue #64) --------------------------------------------


def test_backup_policy_defaults_when_file_absent(tmp_path: Path) -> None:
    """No repo.yaml at all → backup OFF (remote=None, auto=False): the complete-no-op default."""
    policy = load_backup_policy(_layout(tmp_path))
    assert isinstance(policy, BackupPolicy)
    assert policy.remote is None
    assert policy.auto is False


def test_backup_policy_defaults_when_block_absent(tmp_path: Path) -> None:
    """A repo.yaml WITHOUT a backup: block → the same off defaults (existing repos unaffected)."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: kb\nkind: personal\n")
    policy = load_backup_policy(layout)
    assert policy.remote is None
    assert policy.auto is False


def test_backup_policy_parses_remote_and_auto(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "backup:\n  remote: git@example.com:me/kb.git\n  auto: true\n")
    policy = load_backup_policy(layout)
    assert policy.remote == "git@example.com:me/kb.git"
    assert policy.auto is True


def test_backup_policy_remote_alone_defaults_auto_off(tmp_path: Path) -> None:
    """`remote` alone enables `agora sync` but NOT the watch auto-push (auto stays opt-in)."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "backup:\n  remote: origin\n")
    policy = load_backup_policy(layout)
    assert policy.remote == "origin"
    assert policy.auto is False


def test_backup_remote_non_string_fails_loud(tmp_path: Path) -> None:
    """A SUPPLIED non-string remote raises (the #57 posture: stated policy is never silently
    dropped — a silent None here would mean silently not backing up)."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "backup:\n  remote: 12345\n")
    with pytest.raises(ConfigError, match="backup.remote"):
        load_backup_policy(layout)


def test_backup_auto_non_bool_fails_loud(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, 'backup:\n  remote: origin\n  auto: "yes"\n')
    with pytest.raises(ConfigError, match="backup.auto"):
        load_backup_policy(layout)


def test_backup_unknown_key_fails_loud(tmp_path: Path) -> None:
    """A typoed key (`remot:`) must never read as "no remote configured": silently NOT backing up
    is this feature's worst failure mode, so the block is strict about its own keys (unlike the
    tolerant harvest:/index:/web: siblings, whose absence degrades observably)."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "backup:\n  remot: git@example.com:me/kb.git\n")
    with pytest.raises(ConfigError, match="remot"):
        load_backup_policy(layout)


@pytest.mark.parametrize("block", ["backup: origin\n", "backup:\n- remote: origin\n"])
def test_backup_non_mapping_block_fails_loud(tmp_path: Path, block: str) -> None:
    """A present-but-non-mapping backup: (a scalar or a list — a nesting mistake) raises instead
    of silently reading as backup-off."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, block)
    with pytest.raises(ConfigError, match="mapping"):
        load_backup_policy(layout)


def test_backup_policy_not_added_to_repo_config(tmp_path: Path) -> None:
    """RepoConfig stays a separate concern — no `backup` field (backup policy is its own model),
    and a repo.yaml carrying a backup: block still loads through load_repo_config unchanged."""
    assert "backup" not in RepoConfig.model_fields
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: kb\nbackup:\n  remote: origin\n  auto: true\n")
    cfg = load_repo_config(layout)  # must not trip extra='forbid'
    assert cfg.name == "kb"
