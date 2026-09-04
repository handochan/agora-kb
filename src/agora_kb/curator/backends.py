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

import os
import subprocess
from collections.abc import Mapping
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


# ADR-0015: the CLOSED set of routable cognitive acts — exactly the two methods of the worker's
# ``Backend`` Protocol (``plan`` = PASS-1, ``author`` = PASS-2), the only two points a brain is
# invoked. Per-op / per-tier routing is intentionally OUT OF SCOPE for v1 (PASS-1 plans the whole
# batch in one ``plan()`` call), so the key-space cannot promise sub-act routing it can't deliver.
_ROUTABLE_ACTS: tuple[str, ...] = ("plan", "author")


class BackendRegistry:
    """The pluggable write-adapter registry parsed from ``adapters.yaml`` (DATA-MODEL §8).

    Holds the named ``backends`` plus the ``default_backend`` pointer and an optional per-act
    ``routing`` table (ADR-0015). This is the single place the curator resolves "which brain" —
    swapping the brain, or pinning a different brain per cognitive act, is a config edit, not a code
    change (ADR-0004). Construct via :meth:`from_yaml` / :meth:`from_file`.
    """

    def __init__(
        self,
        backends: dict[str, BackendSpec],
        default_backend: str,
        routing: dict[str, str] | None = None,
    ) -> None:
        if not backends:
            raise ValueError("adapters.yaml defines no backends")
        if default_backend not in backends:
            raise ValueError(
                f"default_backend {default_backend!r} is not among the defined "
                f"backends {sorted(backends)}"
            )
        # ADR-0015: validate the optional per-act routing HERE so every construction path (including
        # a direct test build) is guarded fail-loud, mirroring the ``default_backend`` invariant. An
        # unknown act key or a value naming an undefined backend is a hard config error, never a
        # silent fallback.
        self._routing: dict[str, str] = dict(routing or {})
        for act, name in self._routing.items():
            if act not in _ROUTABLE_ACTS:
                raise ValueError(
                    f"routing key {act!r} is not a routable act; routable acts are "
                    f"{list(_ROUTABLE_ACTS)} (per-op / per-tier routing is not supported in v1)"
                )
            if name not in backends:
                raise ValueError(
                    f"routing[{act!r}] → {name!r} is not among the defined backends "
                    f"{sorted(backends)}"
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
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            # A YAML syntax error is a yaml.YAMLError (NOT a ValueError); normalize it so the faces'
            # ``except ValueError`` surfaces it cleanly instead of crashing with a traceback.
            raise ValueError(f"adapters.yaml is not valid YAML: {exc}") from exc
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

        # ADR-0015: optional per-act ``routing`` block (sibling of ``backends``/``default_backend``;
        # NOT a backend field, so ``BackendSpec`` stays frozen/extra='forbid'). Absent or empty →
        # no routing (the default brain handles every act). Key/value validity is enforced in
        # ``__init__`` so this parse stays thin and direct construction is guarded identically.
        raw_routing = data.get("routing")
        routing: dict[str, str] = {}
        if raw_routing is not None:
            if not isinstance(raw_routing, dict):
                raise ValueError("'routing' must be a mapping of act → backend name")
            for act, name in raw_routing.items():
                if not isinstance(name, str):
                    raise ValueError(f"routing[{str(act)!r}] must name a backend (a string)")
                routing[str(act)] = name

        return cls(backends=backends, default_backend=default_backend, routing=routing)

    @classmethod
    def from_file(cls, path: str | Path) -> BackendRegistry:
        """Load and parse an ``adapters.yaml`` file from disk (UTF-8)."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            # A present-but-unreadable file raises OSError (not ValueError); normalize it so the
            # faces' ``except ValueError`` reports it cleanly rather than crashing.
            raise ValueError(f"adapters.yaml could not be read: {exc}") from exc
        return cls.from_yaml(text)

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

    def resolve(self, act: str, *, default: str | None = None) -> BackendSpec:
        """Return the backend spec for a routable cognitive act (``'plan'`` | ``'author'``).

        Precedence (ADR-0015): a ``routing:`` entry for the act wins; else the caller-supplied
        ``default`` (the repo's default brain — ``repo.yaml`` ``curator.backend``, which the faces
        thread through so an UNROUTED act keeps honoring it); else the registry's own
        ``default_backend``. Pure and deterministic — same config → same brain per act. Raises
        ``ValueError`` for a non-routable act, and ``KeyError`` when the resolved name (e.g. a
        ``default`` from ``repo.yaml`` that names no defined backend) is unknown.
        """
        if act not in _ROUTABLE_ACTS:
            raise ValueError(
                f"unknown routable act {act!r}; routable acts are {list(_ROUTABLE_ACTS)}"
            )
        name = self._routing.get(act) or default or self._default
        if name not in self._backends:
            raise KeyError(f"unknown backend {name!r}; known backends: {sorted(self._backends)}")
        return self._backends[name]

    def routed_backends(self, *, default: str | None = None) -> dict[str, str]:
        """Return the resolved ``{act: backend_name}`` table for every routable act.

        Observability for ``agora doctor`` (ADR-0015): shows which brain each act will use under the
        same precedence as :meth:`resolve` (``routing`` → ``default`` → registry default), so an
        operator sees the wiring before a run.
        """
        return {act: (self._routing.get(act) or default or self._default) for act in _ROUTABLE_ACTS}


def _substitute_worktree(value: str, worktree: str) -> str:
    return value.replace(_WORKTREE_TOKEN, worktree)


def with_utf8_child_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """Return ``env`` (or the current process env when ``env`` is ``None``) plus a forced UTF-8
    child-side stdio encoding.

    Pinning ``encoding="utf-8"`` on THIS process's end of the pipe (below) only fixes the PARENT
    side of the stdin/stdout protocol; it does nothing to the CHILD's own locale-driven encoding
    (#85). The built-in ``agora-ollama-brain`` / ``agora-cli-brain`` shims read ``sys.stdin`` /
    write ``sys.stdout`` — on a non-UTF-8 child locale (a Python child inheriting
    ``PYTHONIOENCODING=cp949``, or a cp949 Windows console) a Korean prompt sent as UTF-8 can raise
    ``UnicodeDecodeError`` in the child before planning ever starts, and cp949 child output cannot
    be decoded back here. ``PYTHONIOENCODING=utf-8`` and ``PYTHONUTF8=1`` are standard CPython
    env vars that force a Python child's stdio streams to UTF-8 regardless of the host locale —
    they make every backend launched through :func:`run_backend` (and, via
    ``adapters.cli_agent_brain.call_cli_agent``, the CLI agent it shells out to) speak the same
    protocol this process does, with no code change needed in the child.

    Merged ONTO the base env (``dict(env)`` copy, or ``dict(os.environ)`` when ``env is None``) —
    never replacing it — so credential-scrubbing (:func:`agora_kb.curator.isolation.scrub_env`) and
    every other key the caller composed survive unchanged; only these two keys are added/overridden.
    Harmless to a non-Python backend (``claude``/``codex``/``gemini`` CLIs and the like): they are
    UTF-8 by nature and simply ignore env vars naming a CPython behaviour they do not implement.

    PUBLIC because :func:`run_backend` is not the only spawn point: the ADR-0013 SANDBOXED PASS-2
    path (``curator.subprocess_backend.SubprocessBackend._invoke_sandboxed``) builds its own
    :class:`~agora_kb.curator.isolation.SandboxSpec` env and never reaches ``run_backend``, so it
    composes the same two keys through this one helper rather than re-spelling them.
    """
    merged = dict(env) if env is not None else dict(os.environ)
    merged["PYTHONIOENCODING"] = "utf-8"
    merged["PYTHONUTF8"] = "1"
    return merged


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
    ``None`` inherits the parent environment (see below), otherwise the given mapping is used as
    given, in both cases plus a forced UTF-8 child-side stdio encoding.

    ``env`` is passed through :func:`with_utf8_child_env`, which ADDS ``PYTHONIOENCODING=utf-8``
    and ``PYTHONUTF8=1`` on top of whatever ``env`` already carries (never replacing it — the
    caller's credential-scrubbed env, e.g. ``curator.isolation.scrub_env``, survives unchanged) so
    the CHILD's own stdio encoding matches the UTF-8 this call already pins on the parent side
    below (#85 — see :func:`with_utf8_child_env` for the full failure mode). External CLI agents
    (``claude``/``codex``/``gemini`` and the like, reached via ``adapters.cli_agent_brain``) are
    UTF-8-native by construction, so these two Python-specific env vars are inert noise to them,
    never a behaviour change.
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
        # Pinned, not left to the process locale (#85): with no `encoding=`, a non-ASCII `prompt`
        # (a Korean candidate body, say) is ENCODED for stdin using the locale's preferred encoding,
        # so a non-UTF-8 locale can raise UnicodeEncodeError on the way IN — before the brain ever
        # runs — and any non-UTF-8-locale host would decode the brain's stdout/stderr the same
        # locale-dependent way on the way OUT. UTF-8 is what every backend here actually speaks.
        # KNOWN RESIDUAL (not #85's scope): `errors` stays at the default "strict", so a backend
        # that emits one invalid UTF-8 byte makes this call raise UnicodeDecodeError, which
        # `SubprocessBackend._invoke`'s `except (FileNotFoundError, PermissionError)` does not
        # catch. Python children now emit UTF-8 (see the env above); a non-Python CLI agent writing
        # binary noise on stderr still can. The sandboxed twin already decodes with
        # errors="replace" — deciding both together is a follow-up, not a drive-by here.
        encoding="utf-8",
        timeout=timeout,
        env=with_utf8_child_env(env),
        check=False,
    )
    return BackendResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
