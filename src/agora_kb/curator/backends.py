"""Curator WRITE-adapter registry + runner (ADR-0004; DATA-MODEL §8 ``adapters.yaml``).

The curator's *brain* is a swappable **write adapter**: a headless CLI agent invoked once per batch
(``claude -p``, ``codex exec``, ``qwen -p``, ``gemini -p``, ``opencode run``, ``hermes chat`` …).
This module is the **invocation + registry primitive** only — it knows how to look up a backend by
name from ``adapters.yaml`` and how to spawn it over a pipe. It is deliberately tool-agnostic: no
model name, network access, git credential, or writable path is baked in here (DATA-MODEL §8;
invariant 6 — go through the adapter registry, never hard-code a tool/model).

Two responsibilities live in :mod:`agora_kb.curator.worker`, NOT here, and are noted so callers do
not mistake this primitive for the whole story:

- **OS-sandbox wrapping** (ADR-0013 — macOS Seatbelt / cross-platform): the worker is responsible
  for wrapping the spawned ``argv`` so the backend gets no shell, no network, no credentials, and no
  writable path outside the temporary worktree. :func:`run_backend` only spawns the bare ``argv``.
- **Deterministic diff-validation** (ADR-0011 §4): the worker decides run success WITHOUT trusting
  the model, by validating the produced plan/diff against the closed contract. :func:`run_backend`
  returns the raw process result and makes no success judgement of its own (a zero return code does
  not imply a valid ingest).

Execution discipline (DATA-MODEL §8): the registry stores an **argv array**, never a shell string,
and prompt data travels over **stdin**. ``run_backend`` therefore spawns with ``shell=False`` and
never builds a shell command line — argv injection is structurally impossible.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

__all__ = ["BackendSpec", "BackendResult", "BackendRegistry", "run_backend"]

# The token substituted (in ``cwd`` and any ``argv`` element) with the run's temporary worktree
# path at invocation time. Backends only ever see the throwaway worktree, never the live repo.
_WORKTREE_TOKEN = "{worktree}"


class BackendSpec(BaseModel):
    """One write-adapter entry from ``adapters.yaml`` ``backends:`` (DATA-MODEL §8).

    Frozen + ``extra='forbid'`` so a backend definition cannot silently grow unsupported keys (a
    typo'd field is a config error, not a no-op). ``argv`` is an array — never a shell string — so
    interpolation is structurally impossible; ``prompt`` is fixed to ``stdin`` because that is the
    only delivery channel this primitive supports (DATA-MODEL §8: prompt data travels over stdin).

    Beyond the invocation fields this primitive consumes (``argv``/``cwd``/``prompt``), the spec
    also carries the **sandbox parameters** the curator.worker needs to build a ``SandboxSpec``
    (ADR-0013 §142-157): ``sandbox`` (isolation policy), ``network`` (egress posture), ``timeout_s``
    (per-backend wall clock), and ``read_roots`` (extra read-only mounts, e.g. ``{venv}`` /
    ``{interpreter}`` for an out-of-sandbox interpreter). These are *backend-driven* parameters that
    the worker reads from here; the worker — not this primitive — resolves placeholders and enforces
    them (ADR-0013 §163-165). They are typed (rather than allowed via ``extra='ignore'``) so the
    documented config parses while typo protection on backend keys is preserved.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    argv: tuple[str, ...]
    cwd: str = _WORKTREE_TOKEN
    prompt: Literal["stdin"] = "stdin"
    sandbox: str = "strict"
    network: str = "none"
    timeout_s: int | None = None
    read_roots: tuple[str, ...] = ()

    @field_validator("argv")
    @classmethod
    def _check_argv(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("argv must be a non-empty array (the program plus its arguments)")
        return v

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v:
            raise ValueError("backend name must be non-empty")
        return v


class BackendResult(BaseModel):
    """Raw outcome of one backend invocation. Success detection is the worker's job (ADR-0011 §4):
    a ``returncode`` of 0 does NOT imply a valid ingest — only deterministic diff-validation does.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int
    stdout: str
    stderr: str


class BackendRegistry:
    """The pluggable write-adapter registry parsed from ``adapters.yaml`` (DATA-MODEL §8).

    Holds the named ``backends`` plus the ``default_backend`` pointer. This is the single place the
    curator resolves "which brain" — swapping the brain is a config edit, not a code change
    (ADR-0004). Construct via :meth:`from_yaml` / :meth:`from_file`.
    """

    def __init__(self, backends: dict[str, BackendSpec], default_backend: str) -> None:
        if not backends:
            raise ValueError("adapters.yaml defines no backends")
        if default_backend not in backends:
            raise ValueError(
                f"default_backend {default_backend!r} is not among the defined "
                f"backends {sorted(backends)}"
            )
        self._backends = dict(backends)
        self._default = default_backend

    @classmethod
    def from_yaml(cls, text: str) -> BackendRegistry:
        """Parse the ``adapters.yaml`` shape:
        ``backends: {name: {argv, cwd, prompt, sandbox, network, timeout_s, read_roots}}`` plus a
        top-level ``default_backend``. Other top-level adapter families (``extractors``,
        ``connectors``) are ignored here — this registry owns only the WRITE family.
        """
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("adapters.yaml must be a mapping at the top level")

        raw_backends = data.get("backends")
        if raw_backends is None:
            raise ValueError("adapters.yaml is missing the 'backends' mapping")
        if not isinstance(raw_backends, dict):
            raise ValueError("'backends' must be a mapping of name → backend spec")

        backends: dict[str, BackendSpec] = {}
        for name, spec in raw_backends.items():
            if not isinstance(spec, dict):
                raise ValueError(f"backend {name!r} must be a mapping of fields")
            # The name lives on the spec; the YAML key is authoritative. ``extra='forbid'`` rejects
            # a stray inline ``name:`` that disagrees with the key.
            backends[str(name)] = BackendSpec(name=str(name), **spec)

        default_backend = data.get("default_backend")
        if default_backend is None:
            raise ValueError("adapters.yaml is missing 'default_backend'")
        if not isinstance(default_backend, str):
            raise ValueError("'default_backend' must be a string naming a defined backend")

        return cls(backends=backends, default_backend=default_backend)

    @classmethod
    def from_file(cls, path: str | Path) -> BackendRegistry:
        """Load and parse an ``adapters.yaml`` file from disk (UTF-8)."""
        return cls.from_yaml(Path(path).read_text(encoding="utf-8"))

    def get(self, name: str) -> BackendSpec:
        """Return the named backend spec. Raise ``KeyError`` for an unknown name."""
        try:
            return self._backends[name]
        except KeyError:
            raise KeyError(
                f"unknown backend {name!r}; known backends: {sorted(self._backends)}"
            ) from None

    def default(self) -> BackendSpec:
        """Return the spec named by ``default_backend`` (guaranteed to exist by construction)."""
        return self._backends[self._default]

    def names(self) -> list[str]:
        """Return the defined backend names, sorted for stable output."""
        return sorted(self._backends)


def _substitute_worktree(value: str, worktree: str) -> str:
    return value.replace(_WORKTREE_TOKEN, worktree)


def run_backend(
    spec: BackendSpec,
    *,
    worktree: Path,
    prompt: str,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> BackendResult:
    """Spawn one backend invocation and return its raw result.

    Substitutes the ``{worktree}`` token in ``spec.cwd`` (and in any ``argv`` element that contains
    it) with ``worktree``, then runs the resolved ``argv`` with ``shell=False``, feeding ``prompt``
    on stdin and capturing stdout/stderr as text. Returns a :class:`BackendResult`.

    This is the invocation primitive only:

    - **OS-sandbox wrapping is the caller's job** (ADR-0013). ``run_backend`` spawns the bare
      ``argv`` as given; if a backend must be confined (no network / no writes outside the
      worktree), the curator.worker wraps the argv with the platform sandbox *before* building the
      ``spec``. No network access or credentials are added here.
    - **Success detection is the caller's job** (ADR-0011 §4). The returned ``returncode`` is the
      process exit code, not a verdict on whether a valid ingest was produced; the worker validates
      the resulting plan/diff deterministically.

    ``timeout`` (seconds) and ``env`` are passed through to :func:`subprocess.run`; an ``env`` of
    ``None`` inherits the parent environment unchanged.
    """
    worktree_str = str(worktree)
    argv = [_substitute_worktree(arg, worktree_str) for arg in spec.argv]
    cwd = _substitute_worktree(spec.cwd, worktree_str)

    completed = subprocess.run(  # noqa: S603 — argv is an array; shell=False; no interpolation.
        argv,
        cwd=cwd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    return BackendResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
