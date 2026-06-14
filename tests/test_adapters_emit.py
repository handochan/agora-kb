"""Tests for ``write_default_adapters_yaml`` (config.py) — the DATA-MODEL §8 brain-wiring emitter.

A fresh ``agora repo init`` must drop an ``adapters.yaml`` that points the default ``qwen`` backend
at the ``agora-ollama-brain`` console script, so the repo is immediately curate-able with the local
model. These assert: the file is created, it round-trips through ``load_backend_registry`` (its
``default()`` is the ``qwen`` spec shelling ``agora-ollama-brain``), a re-call is non-destructive
(an operator's hand-tuned file survives re-init), and the ``model`` variant appends ``--model <m>``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agora_kb.config import load_backend_registry, write_default_adapters_yaml
from agora_kb.core import RepoLayout

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def test_emits_adapters_yaml_at_repo_root(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    path = write_default_adapters_yaml(layout)
    assert path == tmp_path / "adapters.yaml"
    assert path.is_file()


def test_emitted_registry_roundtrips_to_qwen_default(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    path = write_default_adapters_yaml(layout)

    registry = load_backend_registry(path)
    assert registry is not None
    spec = registry.default()
    assert spec.name == "qwen"
    assert spec.argv[0] == "agora-ollama-brain"
    # No --model token in the default variant: just the bare console-script argv.
    assert list(spec.argv) == ["agora-ollama-brain"]
    # The cwd placeholder + stdin delivery the SubprocessBackend/worker expect.
    assert spec.cwd == "{worktree}"
    assert spec.prompt == "stdin"


def test_model_variant_appends_model_flag(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    path = write_default_adapters_yaml(layout, model="qwen3.6:35b-a3b")

    registry = load_backend_registry(path)
    assert registry is not None
    spec = registry.default()
    assert spec.name == "qwen"
    assert list(spec.argv) == ["agora-ollama-brain", "--model", "qwen3.6:35b-a3b"]


def test_recall_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    """A second call leaves a pre-existing adapters.yaml untouched (re-init is non-destructive)."""
    layout = RepoLayout(tmp_path)
    path = tmp_path / "adapters.yaml"
    sentinel = "# operator-tuned registry — do not clobber\nbackends: {}\n"
    path.write_text(sentinel, encoding="utf-8")

    returned = write_default_adapters_yaml(layout)
    assert returned == path
    # Unchanged: the hand-authored content survives.
    assert path.read_text(encoding="utf-8") == sentinel


@requires_git
def test_repo_init_produces_adapters_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agora repo init` drops an adapters.yaml wiring the qwen/agora-ollama-brain backend."""
    from agora_kb.cli import main

    target = tmp_path / "kb"
    rc = main(["repo", "init", str(target), "--domain", "ai-tech"])
    assert rc == 0

    adapters = target / "adapters.yaml"
    assert adapters.is_file()
    # The init output references the adapters registry path (stderr keeps stdout the bare sha).
    err = capsys.readouterr().err
    assert str(adapters) in err

    registry = load_backend_registry(adapters)
    assert registry is not None
    assert registry.default().name == "qwen"
    assert registry.default().argv[0] == "agora-ollama-brain"
