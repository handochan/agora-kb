"""Tests for the harvester config seam + cursor-path layout (ADR-0007; DATA-MODEL §3/§6/§8).

Covers :func:`load_harvest_policy` (opt-in defaults, explicit values, fail-loud on bad scope/kind),
:func:`load_connector_specs` (absent → None, parsed entries, fail-loud on bad scope), the
``harvest_cursor_path`` sanitization/traversal guard, and the emitted-default round-trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.config import (
    ConfigError,
    load_connector_specs,
    load_harvest_policy,
    repo_config_path,
    write_default_adapters_yaml,
    write_default_repo_config,
)
from agora_kb.core.layout import InvalidWriterError, RepoLayout


def _layout(tmp_path: Path) -> RepoLayout:
    return RepoLayout(tmp_path)


def _write_repo_yaml(layout: RepoLayout, text: str) -> None:
    layout.kb_dir.mkdir(parents=True, exist_ok=True)
    repo_config_path(layout).write_text(text, encoding="utf-8")


def _write_adapters(layout: RepoLayout, text: str) -> Path:
    p = layout.root / "adapters.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --- harvest_cursor_path safety -----------------------------------------------------------------


def test_harvest_paths(tmp_path: Path) -> None:
    lo = RepoLayout(tmp_path)
    assert lo.harvest_dir == tmp_path / "_kb" / "harvest"
    # The ':' in a connector key is sanitized to '-' for the filename.
    assert lo.harvest_cursor_path("file:claude-code") == lo.harvest_dir / "file-claude-code.json"


@pytest.mark.parametrize("bad", ["file:../evil", "file:a/b", "../x", "a/b"])
def test_harvest_cursor_path_rejects_traversal(tmp_path: Path, bad: str) -> None:
    with pytest.raises(InvalidWriterError):
        RepoLayout(tmp_path).harvest_cursor_path(bad)


# --- load_harvest_policy ------------------------------------------------------------------------


def test_harvest_policy_defaults_when_absent(tmp_path: Path) -> None:
    pol = load_harvest_policy(_layout(tmp_path))
    assert pol.enabled is False  # opt-in (ADR-0007)
    assert pol.scope_lock == "personal"
    assert pol.repo_kind is None  # fail-closed input to the scope gate


def test_harvest_policy_reads_explicit_values(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    _write_repo_yaml(
        lo,
        "name: eng\nkind: team\nharvest:\n  enabled: true\n  scope_lock: team\n",
    )
    pol = load_harvest_policy(lo)
    assert pol.enabled is True
    assert pol.scope_lock == "team"
    assert pol.repo_kind == "team"


def test_harvest_policy_bad_scope_lock_raises(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    _write_repo_yaml(lo, "harvest:\n  enabled: true\n  scope_lock: nonsense\n")
    with pytest.raises(ConfigError):
        load_harvest_policy(lo)


def test_harvest_policy_bad_kind_raises(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    _write_repo_yaml(lo, "kind: bogus\nharvest:\n  enabled: true\n")
    with pytest.raises(ConfigError):
        load_harvest_policy(lo)


# --- load_connector_specs -----------------------------------------------------------------------


def test_connector_specs_absent_file_is_none(tmp_path: Path) -> None:
    assert load_connector_specs(tmp_path / "adapters.yaml") is None


def test_connector_specs_no_block_is_none(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    p = _write_adapters(lo, "backends:\n  qwen: { argv: [x] }\ndefault_backend: qwen\n")
    assert load_connector_specs(p) is None


def test_connector_specs_parsed(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    p = _write_adapters(
        lo,
        "backends:\n  qwen: { argv: [x] }\ndefault_backend: qwen\n"
        "connectors:\n"
        '  file:claude-code: { path: "~/.claude/**/MEMORY.md", scope: personal }\n'
        '  file:hermes: { path: "~/.hermes/MEMORY.md" }\n',  # scope omitted → defaults personal
    )
    specs = load_connector_specs(p)
    assert specs is not None
    by_name = {s.name: s for s in specs}
    assert by_name["file:claude-code"].scope == "personal"
    assert by_name["file:claude-code"].path == "~/.claude/**/MEMORY.md"
    assert by_name["file:hermes"].scope == "personal"  # default


def test_connector_specs_bad_scope_raises(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    p = _write_adapters(
        lo,
        "backends:\n  qwen: { argv: [x] }\ndefault_backend: qwen\n"
        "connectors:\n  file:x: { path: /m, scope: secret }\n",
    )
    with pytest.raises(ConfigError):
        load_connector_specs(p)


def test_connector_specs_non_mapping_raises(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    p = _write_adapters(
        lo,
        "backends:\n  qwen: { argv: [x] }\ndefault_backend: qwen\nconnectors: [a, b]\n",
    )
    with pytest.raises(ConfigError):
        load_connector_specs(p)


# --- emitted defaults ---------------------------------------------------------------------------


def test_default_repo_config_emits_disabled_harvest(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    write_default_repo_config(lo, name="demo", domains=["general"], kind="personal")
    pol = load_harvest_policy(lo)
    assert pol.enabled is False
    assert pol.scope_lock == "personal"
    assert pol.repo_kind == "personal"


def test_default_adapters_yaml_has_no_active_connectors(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    path = write_default_adapters_yaml(lo)
    # The commented-out connectors example must not parse as active connectors.
    assert load_connector_specs(path) is None
    # ...and the commented example is present for the operator to uncomment.
    assert "# connectors:" in path.read_text(encoding="utf-8")


# --- follow_links (ADR-0018) --------------------------------------------------------------------


def test_connector_specs_follow_links_default_false(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    p = _write_adapters(
        lo,
        "backends:\n  qwen: { argv: [x] }\ndefault_backend: qwen\n"
        "connectors:\n  file:x: { path: /m/MEMORY.md, scope: personal }\n",
    )
    specs = load_connector_specs(p)
    assert specs is not None
    assert specs[0].follow_links is False


def test_connector_specs_follow_links_true(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    p = _write_adapters(
        lo,
        "backends:\n  qwen: { argv: [x] }\ndefault_backend: qwen\n"
        "connectors:\n  file:x: { path: /m/MEMORY.md, scope: personal, follow_links: true }\n",
    )
    specs = load_connector_specs(p)
    assert specs is not None
    assert specs[0].follow_links is True


def test_connector_specs_follow_links_non_bool_raises(tmp_path: Path) -> None:
    # A string "true" must FAIL LOUD, not truthy-coerce (a read-surface flag, ADR-0018).
    lo = _layout(tmp_path)
    p = _write_adapters(
        lo,
        "backends:\n  qwen: { argv: [x] }\ndefault_backend: qwen\n"
        'connectors:\n  file:x: { path: /m/MEMORY.md, scope: personal, follow_links: "true" }\n',
    )
    with pytest.raises(ConfigError):
        load_connector_specs(p)


# --- format: the session-transcript parser selector (issue #147, invariant #6) -------------------

_BACKENDS = "backends:\n  qwen: { argv: [x] }\ndefault_backend: qwen\n"


def test_connector_specs_format_defaults_to_none(tmp_path: Path) -> None:
    """An adapters.yaml that predates `format:` parses to None — today's behaviour, untouched.

    None is deliberately NOT eagerly resolved to the default name here: the config seam records
    what the operator DECLARED, and the harvester owns what "undeclared" means.
    """
    lo = _layout(tmp_path)
    p = _write_adapters(
        lo,
        _BACKENDS + 'connectors:\n  session:demo: { path: "~/s/**/*.jsonl", scope: personal }\n',
    )
    specs = load_connector_specs(p)
    assert specs is not None
    assert specs[0].format is None


def test_connector_specs_format_parsed(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    p = _write_adapters(
        lo,
        _BACKENDS + "connectors:\n"
        '  session:demo: { path: "~/s/**/*.jsonl", scope: personal, format: claude-code-jsonl }\n',
    )
    specs = load_connector_specs(p)
    assert specs is not None
    assert specs[0].format == "claude-code-jsonl"


def test_connector_specs_unknown_format_raises_at_load(tmp_path: Path) -> None:
    """A typo'd format must fail LOUD at config load, not silently take the default parser.

    Silently defaulting would parse a foreign transcript with Claude Code's grammar and harvest
    zero facts, with nothing anywhere saying the declared format was never supported.
    """
    lo = _layout(tmp_path)
    p = _write_adapters(
        lo,
        _BACKENDS + "connectors:\n"
        '  session:demo: { path: "~/s/**/*.jsonl", scope: personal, format: claude-jsonl }\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_connector_specs(p)
    assert "claude-jsonl" in str(exc.value)


def test_connector_specs_format_on_a_file_connector_raises(tmp_path: Path) -> None:
    # A key that cannot possibly take effect must not look accepted (the max_files footgun).
    lo = _layout(tmp_path)
    p = _write_adapters(
        lo,
        _BACKENDS + "connectors:\n"
        "  file:x: { path: /m/MEMORY.md, scope: personal, format: claude-code-jsonl }\n",
    )
    with pytest.raises(ConfigError) as exc:
        load_connector_specs(p)
    assert "session:" in str(exc.value)


def test_connector_specs_format_non_string_raises(tmp_path: Path) -> None:
    lo = _layout(tmp_path)
    p = _write_adapters(
        lo,
        _BACKENDS + "connectors:\n"
        '  session:demo: { path: "~/s/**/*.jsonl", scope: personal, format: 7 }\n',
    )
    with pytest.raises(ConfigError):
        load_connector_specs(p)


def test_config_format_names_match_the_harvester_registry() -> None:
    """The config seam's plain-string names must never drift from the IMPLEMENTED registry half.

    `config` holds the names as strings so it never imports the harvester (the same split
    `_SCOPE_VALUES` uses). That duplication is only safe with this assertion. The vocabulary is the
    BUILDABLE subset: a registered slot with no parser must be rejected at config load, not at
    build time (see the next test for why that difference matters).
    """
    from agora_kb.config import _SESSION_FORMATS
    from agora_kb.harvester.session_sources import (
        DEFAULT_SESSION_FORMAT,
        implemented_session_formats,
    )

    assert set(_SESSION_FORMATS) == set(implemented_session_formats())
    assert DEFAULT_SESSION_FORMAT in _SESSION_FORMATS


def test_a_registered_slot_with_no_reader_is_rejected_at_config_load(tmp_path: Path) -> None:
    """`format: codex-jsonl` names a real registry SLOT — and is still an unknown format here.

    Deferring the rejection to `build_connectors` would take the whole harvest down: it builds
    EVERY connector before `agora harvest` applies its `--connector` filter, so one aspirational
    line would disable unrelated healthy connectors — on a repo `agora doctor` called healthy.
    """
    from agora_kb.harvester.session_sources import SESSION_READERS, is_implemented_format

    assert "codex-jsonl" in SESSION_READERS  # the slot exists ...
    assert not is_implemented_format("codex-jsonl")  # ... but this build cannot parse it

    lo = _layout(tmp_path)
    p = _write_adapters(
        lo,
        _BACKENDS + "connectors:\n"
        '  session:codex: { path: "~/c/**/*.jsonl", scope: personal, format: codex-jsonl }\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_connector_specs(p)
    assert "unknown format" in str(exc.value)
