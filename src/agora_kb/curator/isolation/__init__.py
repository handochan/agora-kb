"""Curator OS-sandbox isolation package (ADR-0013) — selection, env-scrub, spec building.

This is the public entry point ``worker.py`` imports to confine the curator's cognitive INGEST
step (ADR-0008 step 2) behind one swappable ``BackendIsolation`` adapter (invariant #6). It owns
the small amount of OS-INDEPENDENT policy that wraps the platform adapters:

* :func:`select_backend_isolation` — picks the adapter by OS, FAIL-CLOSED: ``darwin→seatbelt``;
  ``linux → bwrap`` (if usable) else ``restricted``; anything else ``→ restricted``.
  ``restricted`` is returned ONLY when ``allow_reduced_isolation`` is ``True``; otherwise selection
  raises :class:`SandboxUnavailable` so a ``sandbox: strict`` backend is never silently downgraded
  (ADR-0013 §"Restricted-fallback").
* :func:`scrub_env` — strips the EXACT ADR-0013 env-scrub list (named secrets + a case-insensitive
  ``(token|secret|key|password|cred)`` regex) BEFORE any adapter sees the env, for EVERY mechanism
  so a credential never enters the sandbox (G3).
* :func:`assert_non_nested_worktree` — the ``os.path.commonpath`` invariant ``worker.py`` must
  assert before building any ``SandboxSpec``: a worktree nested inside the main repo would
  transitively include ``main/.git`` and hand the backend write to objects/hooks/config (G4/G5).
* :func:`build_sandbox_spec` — maps a resolved ``(argv, worktree, tmp_dir, read_roots, stdin_data,
  env, timeout_s, network)`` into a :class:`SandboxSpec` with every path realpath-resolved
  (``Path.resolve(strict=True)``) — the spec's hard invariant (the ``/tmp``→``/private/tmp`` bug).

The adapter surface (:class:`SandboxSpec` / :class:`SandboxResult` / :class:`SelfTestReport` /
:class:`BackendIsolation` / :data:`NetworkPosture`) is re-exported from :mod:`.base` so callers
import one package. This module wires NOTHING into ``worker.py`` — the main loop does that
afterward (ADR-0013 §"Implementation checklist" item 7); here we only PROVIDE the helpers it calls.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from .base import (
    BackendIsolation,
    NetworkPosture,
    SandboxResult,
    SandboxSpec,
    SelfTestReport,
)

__all__ = [
    "SandboxUnavailable",
    "select_backend_isolation",
    "scrub_env",
    "assert_non_nested_worktree",
    "build_sandbox_spec",
    # re-exported from .base for one-package imports
    "BackendIsolation",
    "NetworkPosture",
    "SandboxSpec",
    "SandboxResult",
    "SelfTestReport",
]


class SandboxUnavailable(RuntimeError):
    """No usable kernel sandbox AND the operator did not opt into reduced isolation — fail closed.

    Raised by :func:`select_backend_isolation` when a platform has no kernel confinement (native
    Windows; Linux with userns blocked and no remedy) and ``allow_reduced_isolation`` is ``False``.
    Because ``adapters.yaml`` marks the default backends ``sandbox: strict``, this is the fail-close
    guarantee: a strict backend is NEVER run without a real sandbox unless the operator
    explicitly accepts the documented loss of network + out-of-worktree-write confinement (ADR-13).
    """


# The EXACT ADR-0013 §"Env scrubbing" named list (stripped from the env for EVERY mechanism). These
# are removed by exact (case-sensitive) name; the regex below additionally removes anything whose
# name
# matches (case-insensitively) a credential-shaped token.
_SCRUB_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GIT_ASKPASS",
        "SSH_AUTH_SOCK",
    }
)

# Case-insensitive name match: anything looking like a credential is dropped too (ADR-0013). Applied
# to
# the variable NAME (not value), so an innocuous ``EDITOR`` / ``LANG`` / ``PATH`` survives while
# ``MY_SECRET`` / ``DB_PASSWORD`` / ``API_TOKEN`` / ``FOO_CRED`` are removed.
_SCRUB_REGEX = re.compile(r"(?i)(token|secret|key|password|cred)")


def scrub_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``env`` with the ADR-0013 credential set removed (G3, ALL mechanisms).

    Drops every variable whose name is in the EXACT named list OR matches the case-insensitive
    ``(token|secret|key|password|cred)`` regex. The match is on the NAME, so values are never
    inspected and an innocuous variable (``PATH``, ``LANG``, ``HOME``) is preserved. Called BEFORE
    any adapter sees the env, so a git/cloud credential can never enter the sandbox — the backend
    only edits files and never invokes git nor holds credentials (commit + CAS run outside).
    """
    scrubbed: dict[str, str] = {}
    for name, value in env.items():
        if name in _SCRUB_NAMES:
            continue
        if _SCRUB_REGEX.search(name):
            continue
        scrubbed[name] = value
    return scrubbed


def assert_non_nested_worktree(
    worktree: str | os.PathLike[str], main_repo: str | os.PathLike[str]
) -> None:
    """Assert the curator worktree is NON-NESTED relative to the main repo (G4/G5, ADR-0013).

    ADR-0008 step 2 does not pin the worktree's location. If it were created NESTED inside the main
    repo, ``{WORKTREE}`` would transitively include ``main/.git`` and the backend would gain write
    access to objects/hooks/config. Both paths are realpath-resolved, then ``os.path.commonpath``
    must NOT equal either path — i.e. neither contains the other. Raises :class:`ValueError` on
    nesting (the caller ``worker.py`` calls this before building any :class:`SandboxSpec`).
    ``repo.worktree()`` already creates the worktree OUTSIDE the repo (via ``tempfile.mkdtemp``), so
    this normally passes; we PROVIDE the assertion so the invariant is enforced in code, not luck.
    """
    wt = os.path.realpath(worktree)
    main = os.path.realpath(main_repo)
    if os.path.commonpath([wt, main]) in (wt, main):
        raise ValueError(
            f"curator worktree {wt!r} must be created OUTSIDE / non-nested relative to the main "
            f"repo {main!r}: a nested worktree transitively includes main/.git and would grant the "
            "backend write access to objects/hooks/config (ADR-0013 G4/G5)"
        )


def _reject_unsupported_network(network: str) -> None:
    """Fail closed on any egress posture other than ``"none"`` in Phase-1 (ADR-0013 §Ollama).

    ADR-0013 §"adapters.yaml → SandboxSpec mapping" / §"Ollama / local-model boundary": a backend
    requesting ``network`` other than ``"none"`` is **rejected** unless it is the documented future
    ``localhost-ollama`` alternative — which is itself "Documented as future work, not Phase-1
    default" and which NO adapter implements yet (the seatbelt SBPL has no localhost allow branch
    and unconditionally denies all network; bwrap ``--unshare-net`` is loopback-only). So in Phase-1
    a spec built with ANY non-``"none"`` posture would be SILENTLY accepted and then SILENTLY NOT
    honored. Per the ADR's "rejected unless explicitly marked" rule we therefore raise here so an
    un-marked or unsupported request fails closed rather than being ignored. ``localhost-ollama`` is
    rejected with a distinct message (recognized-but-unimplemented) so a future scoped-grant adapter
    can relax exactly this branch; every other value is rejected as unknown.
    """
    if network == "none":
        return
    if network == "localhost-ollama":
        raise SandboxUnavailable(
            "network='localhost-ollama' is the documented FUTURE Ollama-inside-sandbox alternative "
            "(ADR-0013 §'Ollama / local-model boundary') and is NOT implemented in Phase-1: no "
            "adapter grants the scoped 127.0.0.1:11434 allow, so the posture would be silently "
            "unhonored. The Phase-1 default brain does inference OUTSIDE the sandbox with "
            "network='none'; only the file/tool step is confined. Refusing to build a spec whose "
            "network posture cannot be enforced (fail-closed)."
        )
    raise SandboxUnavailable(
        f"unsupported network posture {network!r}: only 'none' is honored in Phase-1 (ADR-0013); "
        "'localhost-ollama' is documented-but-unimplemented and every other value is rejected. A "
        "non-'none' posture would be silently NOT enforced by any adapter, so it fails closed here."
    )


def build_sandbox_spec(
    *,
    argv: list[str],
    worktree: str | os.PathLike[str],
    tmp_dir: str | os.PathLike[str],
    read_roots: list[str | os.PathLike[str]] | None = None,
    stdin_data: bytes | None,
    env: Mapping[str, str],
    main_git: str | os.PathLike[str] | None = None,
    main_kb: str | os.PathLike[str] | None = None,
    timeout_s: int = 600,
    network: NetworkPosture = "none",
) -> SandboxSpec:
    """Build a :class:`SandboxSpec` with every path realpath-resolved and the env scrubbed (ADR-13).

    Resolves ``worktree`` / ``tmp_dir`` / every ``read_root`` (and ``main_git`` / ``main_kb`` if
    given) with ``Path.resolve(strict=True)`` — the spec's hard invariant, because Seatbelt matches
    the RESOLVED vnode and an unresolved ``/tmp/...`` subpath grant silently fails (``/tmp`` →
    ``/private/tmp`` bug). Scrubs ``env`` (G3) so no credential reaches the adapter. Asserts
    ``tmp_dir`` is OUTSIDE ``worktree`` (``HOME`` / ``TMPDIR`` point at it; scratch inside the
    worktree would fail the ADR-0008 diff allowlist). The caller (``worker.py``) MUST separately
    assert the worktree is non-nested via :func:`assert_non_nested_worktree` before calling this.
    """
    _reject_unsupported_network(network)
    wt = Path(worktree).resolve(strict=True)
    tmp = Path(tmp_dir).resolve(strict=True)
    if tmp == wt or str(tmp).startswith(str(wt) + os.sep):
        raise ValueError(
            f"tmp_dir {tmp} must be a DISTINCT directory OUTSIDE the worktree {wt} "
            "(HOME/TMPDIR point here; scratch inside the worktree pollutes the ADR-0008 diff)"
        )
    roots = [Path(p).resolve(strict=True) for p in (read_roots or [])]
    return SandboxSpec(
        argv=list(argv),
        worktree=wt,
        tmp_dir=tmp,
        read_roots=roots,
        stdin_data=stdin_data,
        env=scrub_env(env),
        main_git=Path(main_git).resolve(strict=True) if main_git is not None else None,
        main_kb=Path(main_kb).resolve(strict=False) if main_kb is not None else None,
        timeout_s=timeout_s,
        network=network,
    )


def select_backend_isolation(*, allow_reduced_isolation: bool) -> BackendIsolation:
    """Select the OS-appropriate :class:`BackendIsolation`, FAIL-CLOSED (ADR-0013 §selection).

    Selection order: ``darwin → seatbelt``; ``linux → bwrap`` if :meth:`BwrapIsolation.available`
    else ``restricted``; anything else ``→ restricted``. The mechanism is OS-driven; parameters
    (``read_roots`` / ``timeout_s`` / ``network``) are backend-driven and filled into the spec by
    the caller. ``restricted`` is returned ONLY when ``allow_reduced_isolation`` is ``True``,
    otherwise selection raises :class:`SandboxUnavailable` (fail-closed), so a ``sandbox: strict``
    backend is never satisfied by the reduced fallback without an explicit operator opt-in
    (ADR-0013). The seatbelt path is also validated for availability so a macOS host missing
    ``/usr/bin/sandbox-exec`` fails closed (or drops to restricted only when opted in) rather than
    handing back a dead adapter.
    """
    # Imported here (not at module top) so importing this package is cheap and does not pull a
    # platform adapter's transitive deps on an OS where it cannot run.
    from .bwrap import BwrapIsolation
    from .restricted import RestrictedIsolation
    from .seatbelt import SeatbeltIsolation

    def _restricted_or_closed() -> BackendIsolation:
        if allow_reduced_isolation:
            return RestrictedIsolation()
        raise SandboxUnavailable(
            "no usable kernel sandbox on this platform and allow_reduced_isolation is False; "
            "refusing to run a sandbox:strict backend without confinement (ADR-0013 fail-closed). "
            "Set config.curator.allow_reduced_isolation=true to opt into the restricted fallback "
            "(forced review-mode; network egress and out-of-worktree writes are NOT prevented)."
        )

    if sys.platform == "darwin":
        seatbelt = SeatbeltIsolation()
        if seatbelt.available():
            return seatbelt
        # macOS without a usable sandbox-exec: fail closed unless opted in.
        return _restricted_or_closed()

    if sys.platform.startswith("linux"):
        bwrap = BwrapIsolation()
        if bwrap.available():
            return bwrap
        return _restricted_or_closed()

    # Native Windows / anything else: no kernel sandbox here.
    return _restricted_or_closed()
