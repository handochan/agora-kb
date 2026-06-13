"""Tests for the curator WRITE-adapter registry + runner (ADR-0004; DATA-MODEL §8)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agora_kb.curator.backends import (
    BackendRegistry,
    BackendResult,
    BackendSpec,
    run_backend,
)

# A representative adapters.yaml: two backends + a default, plus other adapter families that the
# WRITE registry must ignore (DATA-MODEL §8).
SAMPLE_YAML = """\
backends:
  qwen:   { argv: ["qwen", "--headless"], cwd: "{worktree}", prompt: stdin, sandbox: strict }
  claude: { argv: ["claude", "--headless"], cwd: "{worktree}", prompt: stdin, sandbox: strict }
default_backend: qwen

extractors:
  "text/html": url

connectors:
  file:claude-code: { path: "~/.claude/**/MEMORY.md", scope: personal }
"""


# --- registry parsing -----------------------------------------------------------------------------
def test_from_yaml_get_default_names() -> None:
    reg = BackendRegistry.from_yaml(SAMPLE_YAML)

    assert reg.names() == ["claude", "qwen"]  # sorted for stability

    qwen = reg.get("qwen")
    assert isinstance(qwen, BackendSpec)
    assert qwen.name == "qwen"
    assert qwen.argv == ("qwen", "--headless")  # tuple, not list
    assert qwen.cwd == "{worktree}"
    assert qwen.prompt == "stdin"
    assert qwen.sandbox == "strict"

    claude = reg.get("claude")
    assert claude.argv == ("claude", "--headless")

    assert reg.default() is reg.get("qwen")
    assert reg.default().name == "qwen"


def test_from_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "adapters.yaml"
    path.write_text(SAMPLE_YAML, encoding="utf-8")
    reg = BackendRegistry.from_file(path)
    assert reg.names() == ["claude", "qwen"]
    assert reg.default().name == "qwen"


def test_get_unknown_backend_raises() -> None:
    reg = BackendRegistry.from_yaml(SAMPLE_YAML)
    with pytest.raises(KeyError):
        reg.get("gemini")


def test_missing_default_backend_raises() -> None:
    bad = """\
backends:
  qwen: { argv: ["qwen"] }
"""
    with pytest.raises(ValueError, match="default_backend"):
        BackendRegistry.from_yaml(bad)


def test_default_pointing_at_unknown_backend_raises() -> None:
    bad = """\
backends:
  qwen: { argv: ["qwen"] }
default_backend: claude
"""
    with pytest.raises(ValueError, match="not among the defined"):
        BackendRegistry.from_yaml(bad)


def test_missing_backends_raises() -> None:
    with pytest.raises(ValueError, match="backends"):
        BackendRegistry.from_yaml("default_backend: qwen\n")


def test_empty_backends_raises() -> None:
    with pytest.raises(ValueError, match="no backends"):
        BackendRegistry.from_yaml("backends: {}\ndefault_backend: qwen\n")


def test_empty_argv_rejected() -> None:
    with pytest.raises(ValueError):
        BackendSpec(name="x", argv=())


def test_unknown_spec_field_rejected() -> None:
    bad = """\
backends:
  qwen: { argv: ["qwen"], bogus: 1 }
default_backend: qwen
"""
    with pytest.raises(ValueError):
        BackendRegistry.from_yaml(bad)


def test_spec_defaults() -> None:
    # cwd/prompt/sandbox/network/timeout_s/read_roots all default when omitted from the YAML spec.
    reg = BackendRegistry.from_yaml(
        'backends:\n  qwen: { argv: ["qwen"] }\ndefault_backend: qwen\n'
    )
    spec = reg.get("qwen")
    assert spec.cwd == "{worktree}"
    assert spec.prompt == "stdin"
    assert spec.sandbox == "strict"
    assert spec.network == "none"
    assert spec.timeout_s is None
    assert spec.read_roots == ()


# The verbatim backends block from ADR-0013 §147-156: each entry carries the sandbox parameters
# (network, timeout_s, read_roots) the curator.worker needs to build a SandboxSpec. The registry —
# the gate the worker reads these from — must accept the documented config (extra='forbid' must not
# reject the spec's own mandated keys).
ADR_0013_YAML = """\
backends:
  qwen:   { argv: ["qwen", "--headless"], cwd: "{worktree}", prompt: stdin,
            sandbox: strict, network: none, timeout_s: 1200,
            read_roots: ["{venv}", "{interpreter}"] }
  claude: { argv: ["claude", "--headless"], cwd: "{worktree}", prompt: stdin,
            sandbox: strict, network: none, timeout_s: 600,
            read_roots: ["{venv}", "{interpreter}"] }
  codex:  { argv: ["codex", "exec"], cwd: "{worktree}", prompt: stdin,
            sandbox: strict, network: none, timeout_s: 600,
            read_roots: ["{venv}", "{interpreter}"] }
default_backend: qwen
"""


def test_from_yaml_accepts_adr_0013_sandbox_fields() -> None:
    reg = BackendRegistry.from_yaml(ADR_0013_YAML)
    assert reg.names() == ["claude", "codex", "qwen"]

    qwen = reg.get("qwen")
    assert qwen.sandbox == "strict"
    assert qwen.network == "none"
    assert qwen.timeout_s == 1200
    assert qwen.read_roots == ("{venv}", "{interpreter}")  # tuple, not list

    claude = reg.get("claude")
    assert claude.timeout_s == 600
    assert claude.read_roots == ("{venv}", "{interpreter}")


# --- {worktree} substitution ----------------------------------------------------------------------
def test_worktree_substitution_in_cwd_and_argv(tmp_path: Path) -> None:
    # Echo the cwd via a portable Python one-liner; the program-name (sys.executable) has no token,
    # but an argv element does — both the cwd template and the argv token must be substituted.
    wt = tmp_path / "wt"
    wt.mkdir()
    spec = BackendSpec(
        name="probe",
        argv=(
            sys.executable,
            "-c",
            "import os,sys; sys.stdout.write(os.getcwd()); sys.stderr.write(sys.argv[1])",
            "{worktree}/marker",
        ),
        cwd="{worktree}",
    )
    result = run_backend(spec, worktree=wt, prompt="")
    assert result.returncode == 0
    # cwd template -> the real worktree (resolve to absorb /private symlink on macOS).
    assert Path(result.stdout).resolve() == wt.resolve()
    # argv token -> substituted too.
    assert result.stderr == f"{wt}/marker"


# --- real subprocess round-trip (no model, no shell) ----------------------------------------------
def test_run_backend_stdin_to_stdout_python(tmp_path: Path) -> None:
    # A trivial, model-free "backend": uppercases stdin to stdout. Proves stdin->stdout plumbing
    # and a captured returncode, with argv as an ARRAY (no shell, no shell=True).
    spec = BackendSpec(
        name="upper",
        argv=(sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"),
        cwd="{worktree}",
    )
    result = run_backend(spec, worktree=tmp_path, prompt="hello agora")
    assert isinstance(result, BackendResult)
    assert result.returncode == 0
    assert result.stdout == "HELLO AGORA"
    assert result.stderr == ""


@pytest.mark.skipif(shutil.which("cat") is None, reason="cat not available")
def test_run_backend_cat_echoes_stdin(tmp_path: Path) -> None:
    # `cat` with no args copies stdin to stdout verbatim — a real external binary over a pipe.
    spec = BackendSpec(name="cat", argv=("cat",), cwd="{worktree}")
    result = run_backend(spec, worktree=tmp_path, prompt="round-trip payload")
    assert result.returncode == 0
    assert result.stdout == "round-trip payload"


def test_run_backend_propagates_nonzero_returncode(tmp_path: Path) -> None:
    # A nonzero exit must surface as returncode, NOT raise — success detection is the worker's job.
    spec = BackendSpec(
        name="fail",
        argv=(sys.executable, "-c", "import sys; sys.exit(7)"),
        cwd="{worktree}",
    )
    result = run_backend(spec, worktree=tmp_path, prompt="")
    assert result.returncode == 7


def test_run_backend_timeout_raises_not_returns(tmp_path: Path) -> None:
    # timeout is a passthrough to subprocess.run, which RAISES TimeoutExpired rather than returning
    # a BackendResult. Callers (the worker, per ADR-0013 §169-171) must handle the raise; document
    # that contract here with a backend that sleeps well past the deadline.
    spec = BackendSpec(
        name="slow",
        argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        cwd="{worktree}",
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_backend(spec, worktree=tmp_path, prompt="", timeout=0.2)


def test_run_backend_env_reaches_child(tmp_path: Path) -> None:
    # An explicit env dict is applied to the child process verbatim.
    spec = BackendSpec(
        name="env-echo",
        argv=(
            sys.executable,
            "-c",
            "import os,sys; sys.stdout.write(os.environ.get('AGORA_X',''))",
        ),
        cwd="{worktree}",
    )
    result = run_backend(spec, worktree=tmp_path, prompt="", env={"AGORA_X": "set-by-test"})
    assert result.returncode == 0
    assert result.stdout == "set-by-test"


def test_run_backend_env_none_inherits_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # env=None inherits the parent environment unchanged (a var set on the parent is visible).
    monkeypatch.setenv("AGORA_INHERIT", "from-parent")
    spec = BackendSpec(
        name="env-inherit",
        argv=(
            sys.executable,
            "-c",
            "import os,sys; sys.stdout.write(os.environ.get('AGORA_INHERIT',''))",
        ),
        cwd="{worktree}",
    )
    result = run_backend(spec, worktree=tmp_path, prompt="", env=None)
    assert result.returncode == 0
    assert result.stdout == "from-parent"


def test_run_backend_no_shell_metacharacters_are_literal(tmp_path: Path) -> None:
    # If argv were ever passed through a shell, this would expand/redirect. With shell=False it is
    # delivered verbatim as a single stdin payload echoed back unchanged.
    payload = "$(touch /tmp/should_not_exist); rm -rf / ; `whoami` && echo pwned > x"
    spec = BackendSpec(
        name="cat-like",
        argv=(sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"),
        cwd="{worktree}",
    )
    result = run_backend(spec, worktree=tmp_path, prompt=payload)
    assert result.returncode == 0
    assert result.stdout == payload
