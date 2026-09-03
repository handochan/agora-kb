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
    KbIdentity,
    RepoConfig,
    WebConfig,
    load_backend_registry,
    load_backup_policy,
    load_harvest_policy,
    load_kb_identity,
    load_repo_config,
    load_web_config,
    repo_config_path,
    write_default_repo_config,
    write_kb_identity,
)
from agora_kb.core.ids import new_ulid
from agora_kb.core.layout import RepoLayout
from agora_kb.curator.constants import (
    DEFAULT_BODY_BYTE_BOUND,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_CANDIDATES_PER_RUN,
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
    max_candidates_per_run: 32
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
    # ADR-0024 OD-3a (#60): the §1.3 per-run candidate cap in this example is now WIRED too.
    assert cfg.max_candidates_per_run == 32
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
    # #60 / ADR-0024 OD-3a: absent cap → the documented INGEST-CONTRACT §1.3 default (32).
    assert cfg.max_candidates_per_run == DEFAULT_MAX_CANDIDATES_PER_RUN == 32
    assert cfg.max_orphans is None


def test_curator_thresholds_parsed(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_repo_yaml(
        layout,
        "name: r\ncurator:\n  limits:\n    body_byte_bound: 4096\n    related_k: 3\n"
        "    max_candidates_per_run: 12\n"
        "  lint:\n    max_orphans: 5\n",
    )
    cfg = load_repo_config(layout)
    assert cfg.body_byte_bound == 4096
    assert cfg.related_k == 3
    assert cfg.max_candidates_per_run == 12
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


def test_curator_max_candidates_per_run_zero_out_of_range_raises(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    # max_candidates_per_run: 0 violates the RepoConfig ge=1 bound (pydantic ValidationError) —
    # an "unbounded" cap does not exist; the contract default is 32 (#60 / ADR-0024 OD-3a).
    _write_repo_yaml(layout, "name: r\ncurator:\n  limits:\n    max_candidates_per_run: 0\n")
    with pytest.raises(ValueError):
        load_repo_config(layout)


def test_curator_max_candidates_per_run_malformed_raises(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    # A non-integer cap fails LOUD with the full key path (mirrors body_byte_bound above).
    _write_repo_yaml(layout, "name: r\ncurator:\n  limits:\n    max_candidates_per_run: lots\n")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(layout)
    assert "curator.limits.max_candidates_per_run" in str(exc.value)


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
    # Hardening defaults (issues #53/#66): 10x the compressed per-file cap; url capture ON.
    assert cfg.upload.max_uncompressed_bytes == 250 * 1024 * 1024
    assert cfg.upload.url_enabled is True
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
        "    max_uncompressed_bytes: 9999\n"
        "    url_enabled: false\n"
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
    assert cfg.upload.max_uncompressed_bytes == 9999
    assert cfg.upload.url_enabled is False
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


def test_web_config_bad_url_enabled_fails_loud(tmp_path: Path) -> None:
    """A non-boolean url_enabled (issue #66 switch) fails loud — security policy never coerces."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  upload:\n    url_enabled: 'off'\n")
    with pytest.raises(ConfigError, match="web.upload.url_enabled"):
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


# --- (n.1) web.identity — reverse-proxy identity threading (issue #67) --------------------------


def test_web_config_identity_defaults_off(tmp_path: Path) -> None:
    """No web:/identity: block → trusted_header None (feature OFF) + strip_domain False.

    The default MUST be off: naming the header is the operator's opt-in signal; a client-forgeable
    header must never influence provenance without that explicit declaration (issue #67).
    """
    cfg = load_web_config(_layout(tmp_path))
    assert cfg.identity.trusted_header is None
    assert cfg.identity.strip_domain is False


def test_web_config_identity_configured(tmp_path: Path) -> None:
    """An explicit identity block reads trusted_header + strip_domain as stated."""
    layout = _layout(tmp_path)
    _write_repo_yaml(
        layout,
        "web:\n  identity:\n    trusted_header: X-Remote-User\n    strip_domain: true\n",
    )
    cfg = load_web_config(layout)
    assert cfg.identity.trusted_header == "X-Remote-User"
    assert cfg.identity.strip_domain is True


def test_web_config_identity_non_string_header_fails_loud(tmp_path: Path) -> None:
    """A non-string trusted_header fails loud — a security opt-in is never silently misread.

    A tolerant `_opt_str` would read `trusted_header: 123` as None, leaving the feature OFF while
    the operator believes it is on (every upload silently collapses back to web:local).
    """
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  identity:\n    trusted_header: 123\n")
    with pytest.raises(ConfigError, match="web.identity.trusted_header"):
        load_web_config(layout)


def test_web_config_identity_yaml_bool_trap_fails_loud(tmp_path: Path) -> None:
    """The YAML 1.1 trap: an unquoted `no` parses as False → ConfigError with quote guidance."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  identity:\n    trusted_header: no\n")
    with pytest.raises(ConfigError, match="web.identity.trusted_header"):
        load_web_config(layout)


def test_web_config_identity_bad_strip_domain_fails_loud(tmp_path: Path) -> None:
    """A non-boolean strip_domain (a string) fails loud, never truthy-coerces."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  identity:\n    strip_domain: 'true'\n")
    with pytest.raises(ConfigError, match="web.identity.strip_domain"):
        load_web_config(layout)


def test_web_config_identity_non_latin1_header_name_fails_loud(tmp_path: Path) -> None:
    """A non-ASCII header name (docs-copy-paste en-dash) fails at LOAD, not as a 500 per write.

    Without the RFC 7230 token check, `X–Remote–User` (U+2013) loads fine but starlette's
    latin-1 header-name encoding raises UnicodeEncodeError inside EVERY write request → 500.
    """
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, 'web:\n  identity:\n    trusted_header: "X–Remote–User"\n')
    with pytest.raises(ConfigError, match="header-name token"):
        load_web_config(layout)


def test_web_config_identity_whitespace_header_name_fails_loud(tmp_path: Path) -> None:
    """A whitespace-padded header name fails at LOAD — it could never match a real request.

    `"X-Remote-User "` (quoted trailing space survives YAML) silently matches nothing: every
    upload would fall back to web:<process-user> with zero operator-visible signal — the exact
    silent-off failure mode the loud identity loader exists to prevent.
    """
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, 'web:\n  identity:\n    trusted_header: "X-Remote-User "\n')
    with pytest.raises(ConfigError, match="header-name token"):
        load_web_config(layout)


def test_web_config_identity_unknown_key_fails_loud(tmp_path: Path) -> None:
    """An unknown web.identity key (the natural hyphen typo) fails loud, never silently OFF.

    The other web sub-mappings tolerate unknown keys (only wired keys are read); identity is the
    deliberate exception because `trusted-header:` (hyphen, mirroring the header name itself)
    would otherwise leave the security opt-in silently disabled.
    """
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  identity:\n    trusted-header: X-Remote-User\n")
    with pytest.raises(ConfigError, match="unknown web.identity key"):
        load_web_config(layout)


# --- (n.2) web.security — browser-mediated attack defense (issue #94) ---------------------------


def test_web_config_security_defaults_to_loopback_only(tmp_path: Path) -> None:
    """No web:/security: block → loopback-only Host allowlist + require_origin OFF.

    The default deliberately does NOT contain starlette's TestClient `testserver`: shipping a
    bypass host in a production default to make a test suite pass is exactly the wrong trade
    (the suite pins `base_url="http://127.0.0.1"` instead).
    """
    cfg = load_web_config(_layout(tmp_path))
    assert cfg.security.allowed_hosts == ["localhost", "127.0.0.1"]
    assert cfg.security.require_origin is False
    assert "testserver" not in cfg.security.allowed_hosts


def test_web_config_security_configured(tmp_path: Path) -> None:
    """An explicit block REPLACES the default list and reads require_origin as stated."""
    layout = _layout(tmp_path)
    _write_repo_yaml(
        layout,
        "web:\n  security:\n    allowed_hosts: [kb.example.com, '*.team.example.com']\n"
        "    require_origin: true\n",
    )
    cfg = load_web_config(layout)
    assert cfg.security.allowed_hosts == ["kb.example.com", "*.team.example.com"]
    assert cfg.security.require_origin is True


def test_web_config_security_hosts_are_normalized(tmp_path: Path) -> None:
    """Entries are stripped + lowercased so a copy-pasted `KB.Example.com ` still matches."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  security:\n    allowed_hosts: ['  KB.Example.com ']\n")
    assert load_web_config(layout).security.allowed_hosts == ["kb.example.com"]


def test_web_config_security_unknown_key_fails_loud(tmp_path: Path) -> None:
    """A typo'd security key fails loud, never silently leaving the permissive default.

    Same posture as web.identity (issue #67): `allowed_host:` / `require-origin:` reading as
    "absent" would have the operator believe a public hostname is allow-listed, or that Origin is
    mandatory, while neither is true.
    """
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  security:\n    allowed_host: [kb.example.com]\n")
    with pytest.raises(ConfigError, match="unknown web.security key"):
        load_web_config(layout)


def test_web_config_security_require_origin_non_bool_fails_loud(tmp_path: Path) -> None:
    """A quoted 'true' would truthy-coerce under a tolerant reader — refuse instead."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "web:\n  security:\n    require_origin: 'true'\n")
    with pytest.raises(ConfigError, match="web.security.require_origin"):
        load_web_config(layout)


@pytest.mark.parametrize(
    ("block", "match"),
    [
        ("    allowed_hosts: kb.example.com\n", "must be a list"),
        ("    allowed_hosts: [kb.example.com, 123]\n", "must be strings"),
        ("    allowed_hosts: []\n", "must not be empty"),
        ("    allowed_hosts: ['']\n", "non-empty hostnames"),
        ("    allowed_hosts: ['https://kb.example.com']\n", "looks like a URL"),
        ("    allowed_hosts: ['kb.example.com:8000']\n", "without a port"),
        ("    allowed_hosts: ['::1']\n", "IPv6 literals"),
        ("    allowed_hosts: ['[::1]']\n", "IPv6 literals"),
        ("    allowed_hosts: ['*']\n", "must not contain"),
        ("    allowed_hosts: ['*.*.example.com']\n", "not a valid wildcard"),
        ("    allowed_hosts: ['kb*.example.com']\n", "not a valid wildcard"),
    ],
)
def test_web_config_security_bad_allowed_hosts_fail_loud(
    tmp_path: Path, block: str, match: str
) -> None:
    """Every allowlist shape that could never match a real request is reported at LOAD.

    Left unchecked each of these manifests as an unexplained 400 on every request (a port or an
    IPv6 literal never survives starlette's `Host.split(":")[0]`), a bare AssertionError at app
    construction (malformed wildcards), or — worst — a silent kill switch (`*` disables Host
    validation entirely).
    """
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, f"web:\n  security:\n{block}")
    with pytest.raises(ConfigError, match=match):
        load_web_config(layout)


@pytest.mark.parametrize(
    "block",
    [
        "web:\n  security: strict\n",
        "web:\n  security:\n    - allowed_hosts: [kb.example.com]\n",
        "web:\n  security: true\n",
    ],
)
def test_web_config_security_non_mapping_fails_loud(tmp_path: Path, block: str) -> None:
    """A scalar / list under `security:` must not narrow to `{}` and restore the defaults.

    The tolerant `_sub_mapping` helper answers `{}` for any non-mapping, which for the OTHER web
    sub-blocks only means "use the defaults". Here it would mean the operator believes a public
    hostname is allow-listed (or that Origin is mandatory) while the deployment silently runs the
    permissive default — the same failure the unknown-key check exists to prevent, reached through
    an indentation slip instead of a typo.
    """
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, block)
    with pytest.raises(ConfigError, match="web.security must be a mapping"):
        load_web_config(layout)


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


def test_connector_max_files_is_operator_configurable(tmp_path: Path) -> None:
    """``connectors.<name>.max_files`` overrides the scan cap (absent = the class's own default).

    The cap was a constructor-only default (``file:`` 64) with no config key, so a source glob that
    matched more files than that was truncated with no supported way to raise it — and a truncated
    file never reaches the whole-source digest, so editing it did not re-trigger a scan either. A
    5-person ``notes/<person>/**`` layout hits this immediately.
    """
    from agora_kb.config import load_connector_specs

    p = tmp_path / "adapters.yaml"
    p.write_text(
        "connectors:\n"
        "  file:notes:\n"
        "    path: ~/notes/**/*.md\n"
        "    scope: team\n"
        "    max_files: 500\n"
        "  file:defaulted:\n"
        "    path: ~/other/**/*.md\n",
        encoding="utf-8",
    )

    specs = {s.name: s for s in load_connector_specs(p) or []}

    assert specs["file:notes"].max_files == 500
    assert specs["file:defaulted"].max_files is None  # absent → the connector class's own default


@pytest.mark.parametrize("bad", ["0", "-1", '"many"', "true"])
def test_connector_max_files_fails_loud_on_a_bad_value(tmp_path: Path, bad: str) -> None:
    """A typo'd cap raises rather than reinstating the default the operator meant to raise."""
    from agora_kb.config import ConfigError, load_connector_specs

    p = tmp_path / "adapters.yaml"
    p.write_text(
        f"connectors:\n  file:notes:\n    path: ~/notes/**/*.md\n    max_files: {bad}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="max_files"):
        load_connector_specs(p)


# --- (k) _meta/kb.yaml — the KB identity, closed key set (ADR-0041 D1.5) ------------------------


def _write_kb_yaml(layout: RepoLayout, text: str) -> None:
    (layout.root / "_meta").mkdir(parents=True, exist_ok=True)
    layout.kb_meta_file.write_text(text, encoding="utf-8")


def test_kb_identity_absent_file_is_none_not_an_error(tmp_path: Path) -> None:
    """Every schema-1 repo predates the file; the LOADER cannot know which schema it is under.

    So absence is "no identity declared" and the caller decides what that means — a schema-2 write
    path treats it as a broken repo, a read path and ``agora doctor`` carry on (ADR-0041 D1.5).
    """
    assert load_kb_identity(_layout(tmp_path)) is None


def test_kb_identity_round_trips_through_write_and_load(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    identity = KbIdentity(kb_id=new_ulid(), name="general", declared_kind="personal")

    path = write_kb_identity(layout, identity)

    assert path == layout.kb_meta_file
    assert load_kb_identity(layout) == identity
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "kb_id": identity.kb_id,
        "name": "general",
        "declared_kind": "personal",
    }


def test_kb_identity_omits_an_unset_declared_kind_rather_than_writing_null(tmp_path: Path) -> None:
    """A null advisory field reads as a value; the key is simply absent when unset."""
    layout = _layout(tmp_path)
    write_kb_identity(layout, KbIdentity(kb_id=new_ulid(), name="general"))

    raw = yaml.safe_load(layout.kb_meta_file.read_text(encoding="utf-8"))
    assert "declared_kind" not in raw
    assert load_kb_identity(layout).declared_kind is None


def test_kb_identity_write_is_idempotent_and_replaces(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    kb_id = new_ulid()
    write_kb_identity(layout, KbIdentity(kb_id=kb_id, name="one"))
    first = layout.kb_meta_file.read_text(encoding="utf-8")
    write_kb_identity(layout, KbIdentity(kb_id=kb_id, name="one"))

    assert layout.kb_meta_file.read_text(encoding="utf-8") == first
    write_kb_identity(layout, KbIdentity(kb_id=kb_id, name="two"))
    assert load_kb_identity(layout).name == "two"


def test_kb_identity_kb_id_must_be_a_ulid(tmp_path: Path) -> None:
    """``kb_id`` is a ULID minted once (D1.5) — a free-form string would split the join identity."""
    with pytest.raises(ValueError, match="ULID"):
        KbIdentity(kb_id="not-a-ulid", name="general")

    layout = _layout(tmp_path)
    _write_kb_yaml(layout, "kb_id: nope\nname: general\n")
    with pytest.raises(ConfigError, match="ULID"):
        load_kb_identity(layout)


def test_kb_identity_rejects_a_blank_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty display name"):
        KbIdentity(kb_id=new_ulid(), name="   ")


def test_kb_identity_declared_kind_vocabulary_is_validated(tmp_path: Path) -> None:
    """Advisory does not mean free-form: a typo'd advisory value is one nobody can act on."""
    with pytest.raises(ValueError, match="declared_kind"):
        KbIdentity(kb_id=new_ulid(), name="general", declared_kind="persnoal")


def test_kb_identity_declared_kind_is_advisory_and_never_the_enforcing_kind(tmp_path: Path) -> None:
    """THE D1.5 SAFETY PROPERTY: a git-tracked declaration must not become a remote claim.

    ``_meta/kb.yaml`` travels with a clone. If ``declared_kind`` were enforcing, an UPSTREAM
    author's ``declared_kind: personal`` would unlock a DOWNSTREAM operator's personal-scope
    connectors. The enforcing value stays ``kind`` in git-ignored ``_kb/repo.yaml``, which is what
    :func:`load_harvest_policy` reads for the ADR-0007 scope lock — asserted here rather than only
    stated, with the two files DISAGREEING so the assertion has teeth.
    """
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: r\nkind: team\n")
    write_kb_identity(layout, KbIdentity(kb_id=new_ulid(), name="r", declared_kind="personal"))

    assert load_repo_config(layout).kind == "team"  # repo.yaml wins…
    assert load_harvest_policy(layout).repo_kind == "team"  # …including for the scope lock
    assert load_kb_identity(layout).declared_kind == "personal"  # …while the advisory is recorded


@pytest.mark.parametrize(
    "policy_key",
    ["kind", "harvest", "curator", "domains", "allowed_tags", "schema_version", "web"],
)
def test_kb_identity_rejects_policy_keys_with_a_pointed_message(
    tmp_path: Path, policy_key: str
) -> None:
    """POLICY MUST NEVER LIVE IN kb.yaml (D1.5) — and the refusal says why, not "unknown key"."""
    layout = _layout(tmp_path)
    _write_kb_yaml(layout, f"kb_id: {new_ulid()}\nname: general\n{policy_key}: personal\n")

    with pytest.raises(ConfigError) as excinfo:
        load_kb_identity(layout)
    message = str(excinfo.value)
    assert policy_key in message
    assert "policy" in message.lower()
    assert "_kb/repo.yaml" in message


def test_kb_identity_key_set_is_closed_to_any_extra(tmp_path: Path) -> None:
    """Deliberately the OPPOSITE of repo.yaml's tolerant unknown-key rule — kb.yaml is git-TRACKED.

    An unknown key in git-ignored ``_kb/repo.yaml`` is a local typo; an unknown key here arrived
    from whoever authored the repo, so it fails loud.
    """
    layout = _layout(tmp_path)
    _write_kb_yaml(layout, f"kb_id: {new_ulid()}\nname: general\nnickname: kb\n")

    with pytest.raises(ConfigError, match="CLOSED"):
        load_kb_identity(layout)

    # The model enforces the same set by construction, so the writer cannot emit a fourth key.
    with pytest.raises(ValueError):
        KbIdentity(kb_id=new_ulid(), name="general", nickname="kb")


def test_kb_identity_present_but_incomplete_fails_loud(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_kb_yaml(layout, "name: general\n")
    with pytest.raises(ConfigError, match="kb_id"):
        load_kb_identity(layout)


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string\n"])
def test_kb_identity_present_but_not_a_mapping_fails_loud(tmp_path: Path, text: str) -> None:
    layout = _layout(tmp_path)
    _write_kb_yaml(layout, text)
    with pytest.raises(ConfigError):
        load_kb_identity(layout)


def test_kb_identity_malformed_yaml_fails_loud(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_kb_yaml(layout, "kb_id: [unclosed\n")
    with pytest.raises(ConfigError):
        load_kb_identity(layout)


def test_kb_identity_is_not_part_of_repo_config(tmp_path: Path) -> None:
    """Two files, two loaders: ``load_repo_config`` never reads (or is broken by) kb.yaml."""
    layout = _layout(tmp_path)
    write_kb_identity(layout, KbIdentity(kb_id=new_ulid(), name="identity-name"))

    cfg = load_repo_config(layout)
    assert cfg.name == "personal"  # the repo.yaml default, NOT the kb.yaml display name
    assert not hasattr(cfg, "kb_id")


# --- (k.1) reserved raw/ domain prefix — the D1.4 layer-2 taxonomy control (L1-23) --------------


@pytest.mark.parametrize("reserved", ["_blob", "_pages", "_kb", "_anything"])
def test_taxonomy_rejects_a_domain_beginning_with_underscore(tmp_path: Path, reserved: str) -> None:
    """``raw/<domain>/`` and ``raw/_blob/`` share ONE namespace (ADR-0041 D1.4 layer 2).

    A taxonomy declaring a domain literally named ``_blob`` would make APPLY write
    ``raw/_blob/<event_id>.md`` into the content-addressed tree. SCHEMA 2, matching lint's own
    ``version >= 2`` gate on L1-23 (a rule ADDED by ADR-0041 D3.1).
    """
    layout = _layout(tmp_path)
    _write_meta_taxonomy(layout, f"schema_version: 2\ndomains: [general, {reserved}]\n")

    with pytest.raises(ConfigError) as excinfo:
        load_repo_config(layout)
    assert reserved in str(excinfo.value)
    assert "raw/" in str(excinfo.value)


def test_taxonomy_reserved_domain_rejected_in_the_repo_yaml_fallback_too(tmp_path: Path) -> None:
    """The fallback path is the SAME namespace: a pre-emit repo must not slip one through."""
    layout = _layout(tmp_path)
    _write_repo_yaml(layout, "name: r\nschema_version: 2\ndomains: [general, _blob]\n")

    with pytest.raises(ConfigError, match="_blob"):
        load_repo_config(layout)


@pytest.mark.parametrize("reserved", ["_blob", "_pages"])
def test_taxonomy_reserved_domain_is_NOT_a_load_failure_on_a_schema_1_repo(
    tmp_path: Path, reserved: str
) -> None:
    """A schema-1 repo LOADS exactly as it did before ADR-0041 — the wave's additivity contract.

    L1-23 is a rule ADDED by ADR-0041 D3.1 (the ADR-0010 supersession banner) and ``schema/lint.py``
    gates its half on ``version >= 2``. Enforcing it at LOAD time for schema 1 too would be a new
    hard failure on the one class of repo this wave promises to leave untouched — and because
    every read surface loads config first (``agora curate``, ``AgoraHandlers.health()``, the
    dashboard, ``agora doctor``), it would arrive as an exception out of a READ path rather than as
    the lint finding ADR-0041 specifies.
    """
    layout = _layout(tmp_path)
    _write_meta_taxonomy(layout, f"schema_version: 1\ndomains: [general, {reserved}]\n")

    assert load_repo_config(layout).taxonomy.domains == ("general", reserved)


def test_taxonomy_ordinary_domains_are_unaffected(tmp_path: Path) -> None:
    """The control is a leading underscore ONLY — an internal one is a legal kebab-ish token."""
    layout = _layout(tmp_path)
    _write_meta_taxonomy(
        layout, "schema_version: 1\ndomains: [general, ai-tech, snake_case, a_b]\n"
    )

    assert load_repo_config(layout).taxonomy.domains == ("general", "ai-tech", "snake_case", "a_b")


def test_taxonomy_load_reads_the_schema_version_on_both_sides_of_the_gate(tmp_path: Path) -> None:
    """The reserved-domain gate is schema-keyed, so the version read itself must stay exact."""
    layout = _layout(tmp_path)
    _write_meta_taxonomy(layout, "schema_version: 1\ndomains: [general]\n")
    assert load_repo_config(layout).taxonomy.schema_version == 1

    _write_meta_taxonomy(layout, "schema_version: 2\ndomains: [general]\n")
    assert load_repo_config(layout).taxonomy.schema_version == 2
