"""``BackendIsolation`` adapter surface — the OS-sandbox contract (ADR-0013 §"Adapter surface").

ADR-0008 mandates that the curator's cognitive INGEST step runs "inside an OS sandbox with no
network by default and with the repo as its only writable mount," but leaves the *mechanism*
unspecified. ADR-0013 fixes the mechanism behind ONE swappable adapter interface so no single OS
facility is hard-coded (invariant #6) and so ``core`` never depends on a concrete sandbox. This
module defines that interface and its three frozen data-carriers — and NOTHING else: it holds no
platform logic, no subprocess call, and no policy text, so importing it is cheap and side-effect-
free on every OS.

The shapes here are VERBATIM from ADR-0013 §"Adapter surface" (the dataclass field lists are part
of the binding spec). The four data-carriers are:

* :class:`SandboxSpec` — the fully-resolved request the orchestrator (``curator/worker.py``) hands
  to :meth:`BackendIsolation.run`. By contract EVERY path in it is already realpath-resolved
  (``/tmp`` → ``/private/tmp``) and ``env`` is already credential-scrubbed; the adapter only adds
  ``HOME`` / ``TMPDIR`` / ``PATH`` pointing at the throwaway ``tmp_dir``.
* :class:`SandboxResult` — the raw process outcome plus which mechanism ran it and whether
  isolation was *reduced* (the restricted fallback, which the worker records in the manifest and
  which forces review-mode, ADR-0013 §"Restricted-fallback").
* :class:`SelfTestReport` — the four assertions the runtime self-test proves before any real run
  trusts the sandbox (write-inside OK, write-outside EPERM, network EPERM to a reachable target,
  Apple-shimmed binary runs), plus the mechanism it tested.
* :class:`BackendIsolation` — the Protocol every adapter (seatbelt/bwrap/restricted) satisfies.

Mechanism is OS-driven (``select_backend_isolation``); parameters (``read_roots`` / ``timeout_s`` /
``network``) are backend-driven, sourced from ``adapters.yaml`` (ADR-0013 §"adapters.yaml →
SandboxSpec mapping"). The transaction (claim / manifest / worktree / validate / commit / CAS /
finalize) stays in ``worker.py`` per ADR-0008 — this surface is ONLY the "run-in-a-sandbox" box.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "NetworkPosture",
    "SandboxSpec",
    "SandboxResult",
    "SelfTestReport",
    "BackendIsolation",
]

# The egress posture a backend may request (ADR-0013 §"Ollama / local-model boundary"). ``"none"``
# is
# the only Phase-1 default: a strict no-network sandbox blocks even loopback to the host Ollama
# daemon,
# so the default local-model brain does inference OUTSIDE the sandbox and only the file/tool step
# runs
# confined. ``"localhost-ollama"`` is the documented future alternative (a narrowly-scoped allow to
# ``127.0.0.1:11434`` and nothing else); it is REJECTED for any backend not explicitly marked.
NetworkPosture = Literal["none", "localhost-ollama"]


@dataclass(frozen=True)
class SandboxSpec:
    """A fully-resolved request to run ONE backend invocation inside the sandbox (ADR-0013).

    Frozen because the orchestrator builds it once, asserts its invariants, and hands it to the
    adapter unchanged; an adapter that mutated it could silently weaken confinement. The caller
    (``worker.py``) MUST uphold, before constructing this, the ADR-0013 §"Invariants the caller
    MUST uphold" set:

    1. every path here is already ``Path(...).resolve(strict=True)`` (realpath) — Seatbelt matches
       the resolved vnode, so an unresolved ``/tmp/...`` subpath grant silently fails (the ``/tmp``
       → ``/private/tmp`` bug, ADR-0013 §"Realpath normalization");
    2. ``worktree`` is NON-NESTED relative to the main repo checkout (asserted via
       :func:`agora_kb.curator.isolation.assert_non_nested_worktree`); and
    3. ``tmp_dir`` is a DISTINCT realpath-resolved directory that is NOT inside ``worktree`` and NOT
       inside any repo (``HOME`` / ``TMPDIR`` point here so dotfiles/caches never land in the
       worktree diff that ADR-0008 validates).

    ``env`` is ALREADY credential-scrubbed (ADR-0013 §"Env scrubbing") before it reaches any
    adapter; the adapter only ADDS ``HOME`` / ``TMPDIR`` / ``PATH``. ``argv`` is an array, run
    ``shell=False`` and appended verbatim after ``--`` — never concatenated into a shell string.
    """

    argv: list[str]
    """The backend command (program + args). Run ``shell=False``, appended verbatim; no concat."""

    worktree: Path
    """The ONLY writable CONTENT mount. MUST already be realpath-resolved by the caller."""

    tmp_dir: Path
    """A SEPARATE throwaway scratch dir OUTSIDE the worktree; ``HOME`` / ``TMPDIR`` point here."""

    read_roots: list[Path]
    """Runtime/model/interpreter paths the backend must read (per-backend, from adapters.yaml)."""

    stdin_data: bytes | None
    """A long prompt delivered over stdin (``input=``); ``None`` when the prompt rides in argv."""

    env: dict[str, str]
    """ALREADY credential-scrubbed; the adapter only adds ``HOME`` / ``TMPDIR`` / ``PATH``."""

    main_git: Path | None = None
    """Realpath of the MAIN repo's ``.git`` (denied as a subpath). ``None`` → the seatbelt adapter
    derives it from the worktree's linked-worktree ``git rev-parse --git-common-dir`` (G4)."""

    main_kb: Path | None = None
    """Realpath of the MAIN repo's ``_kb`` (denied as a subpath). ``None`` → derived from the main
    repo root alongside ``main_git`` (G4/G5)."""

    timeout_s: int = 600
    """Per-backend wall clock (``adapters.yaml``; local Qwen defaults to 1200s, hosted CLIs 600s).
    On timeout the adapter SIGTERMs, waits a 10s grace, then SIGKILLs the whole process GROUP."""

    network: NetworkPosture = "none"
    """Egress posture; ``"none"`` is the only Phase-1 default (inference happens outside)."""


@dataclass(frozen=True)
class SandboxResult:
    """The raw outcome of one sandboxed backend invocation (ADR-0013 §"Adapter surface").

    Frozen: the orchestrator inspects ``returncode`` / ``stdout`` / ``stderr`` to grade the run
    deterministically (the model is outside the integrity boundary, ADR-0011 §4) and records
    ``mechanism`` + ``reduced_isolation`` in the manifest. ``reduced_isolation`` is ``True`` ONLY
    for the restricted fallback; when set, ``worker.py`` forces review-mode (publish to a branch/PR,
    never direct CAS) regardless of ``repo.yaml`` (ADR-0013 §"Restricted-fallback").
    """

    returncode: int
    stdout: bytes
    stderr: bytes
    mechanism: str  # "seatbelt" | "bwrap" | "restricted"
    reduced_isolation: bool  # True ONLY for the restricted fallback


@dataclass(frozen=True)
class SelfTestReport:
    """The runtime self-test verdict (ADR-0013 §"Runtime self-test") — proof, not assumption.

    The self-test runs the REAL sandbox config against a throwaway worktree + a separate throwaway
    ``tmp_dir`` before any real run trusts the sandbox, and doubles as the capability detector. Each
    boolean is an INDEPENDENTLY-asserted fact:

    * ``write_inside_ok`` — a write inside the worktree succeeded (the realpath grant works);
    * ``write_outside_denied`` — a write to ``~/agora-selftest-probe`` was denied via **EPERM**,
      asserted by the ABSENCE of the probe's ``OUTSIDE_WRITE_OK`` marker (a non-EPERM error is NOT
      accepted as "denied", ADR-0013);
    * ``network_denied`` — outbound TCP to a REACHABLE target (host Ollama ``127.0.0.1:11434``)
      was denied via **EPERM** (``NET_AMBIGUOUS``, a non-EPERM ``OSError``, fails — never passes);
    * ``apple_shim_ok`` — an Apple-shimmed binary (``/usr/bin/git --version``) ran WITHOUT the
      ``/dev/null`` fatal (proves the ``/dev/null`` write-allow).

    ``passed`` is the conjunction the orchestrator gates on; a failing self-test means the sandbox
    is NOT trustworthy → fail closed (default) unless ``allow_reduced_isolation`` drops to
    restricted.
    """

    passed: bool
    write_inside_ok: bool
    write_outside_denied: bool  # asserted via EPERM, not "any error"
    network_denied: bool  # asserted via EPERM to a REACHABLE target
    apple_shim_ok: bool  # Apple-shimmed binary (git) ran without the /dev/null fatal
    mechanism: str
    # Diagnostic detail (not part of the ADR field list; aids `agora doctor` output). Defaulted so
    # the spec's positional construction shape is unaffected.
    detail: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class BackendIsolation(Protocol):
    """The swappable OS-sandbox adapter (ADR-0013 §"Adapter surface"; invariant #6).

    Implemented by :class:`~agora_kb.curator.isolation.seatbelt.SeatbeltIsolation` (macOS Phase-1
    default), :class:`~agora_kb.curator.isolation.bwrap.BwrapIsolation` (Linux), and
    :class:`~agora_kb.curator.isolation.restricted.RestrictedIsolation` (opt-in fallback).
    ``select_backend_isolation`` picks one by OS; the adapter confines the backend per mechanism.
    """

    name: str

    def available(self) -> bool:
        """Cheap capability probe: is this mechanism usable on this host RIGHT NOW?"""
        ...

    def self_test(
        self,
        throwaway_worktree: Path,
        throwaway_tmp: Path,
        backend_read_roots: list[Path],
    ) -> SelfTestReport:
        """Run the ADR-0013 hardened probes against a throwaway worktree + separate tmp; report."""
        ...

    def run(self, spec: SandboxSpec) -> SandboxResult:
        """Run ``spec.argv`` confined per this mechanism; return the raw :class:`SandboxResult`."""
        ...
