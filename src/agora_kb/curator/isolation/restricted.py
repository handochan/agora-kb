"""Restricted fallback — opt-in, NO kernel confinement ``BackendIsolation`` (ADR-0013).

For platforms with no usable kernel sandbox (native Windows; Linux with userns disabled and no
AppArmor remedy; old kernels). It runs the backend with ``cwd`` = the worktree and ``HOME`` /
``TMPDIR`` = ``tmp_dir`` (outside the worktree), with the env ALREADY credential-scrubbed and
prompts over stdin — but it CANNOT kernel-block network egress OR out-of-worktree writes during the
run, and it does NOT pretend to. ``reduced_isolation=True`` propagates so ``worker.py`` records it
in the manifest and FORCES review-mode (publish to a branch/PR, never direct CAS), regardless of
``repo.yaml`` (ADR-0013).

**What is LOST during the run (BOTH — enumerated in :data:`WARNING`):**

1. **Network deny.** A malicious backend CAN reach the network and exfiltrate captured secrets; the
   ADR-0008 post-hoc validator does NOT see network egress.
2. **Out-of-worktree WRITE confinement.** A malicious backend CAN write ANYWHERE the invoking user
   can (e.g. ``~/.ssh/authorized_keys``, ``~/.bashrc``, another repo's files); the validator only
   diffs the WORKTREE tree, so it will NOT catch direct out-of-worktree writes.

This is why ``config.curator.allow_reduced_isolation`` defaults to **False** (selection raises
``SandboxUnavailable`` → fail closed) and a ``sandbox: strict`` backend is NEVER satisfied by this
fallback unless the operator explicitly opts in. The deterministic ADR-0008 post-run validator
(allowlist + reject symlinks/path-escapes/``_kb``/git-config/hooks) STILL runs over the worktree
tree — transaction integrity does not depend on the OS sandbox — but it catches NEITHER lost
guarantee, hence the loud, repeated warning the operator must see in logs + the run manifest +
``kb_status`` output (ADR-0013).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from .base import SandboxResult, SandboxSpec, SelfTestReport

__all__ = ["RestrictedIsolation", "WARNING"]

_KILL_GRACE_S = 10.0
_SANDBOX_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

# The LOUD, repeated warning text that MUST enumerate BOTH lost guarantees (not only network),
# emitted into ``_kb/logs``, the run manifest (``reduced_isolation: true``), and ``kb_status``
# output by the orchestrator (ADR-0013 §"Restricted-fallback"). The warning enumerates both.
WARNING = (
    "REDUCED ISOLATION ACTIVE (restricted fallback): the curator backend is running with NO kernel "
    "sandbox. TWO protections are LOST for this run and the ADR-0008 post-hoc validator catches "
    "NEITHER: (1) NETWORK EGRESS is NOT blocked — a malicious backend can reach the network and "
    "exfiltrate captured secrets; (2) OUT-OF-WORKTREE WRITES are NOT confined — a malicious brain "
    "can write anywhere the invoking user can (e.g. ~/.ssh/authorized_keys, ~/.bashrc, other "
    "repos). This run is FORCED into review-mode (publish to a branch/PR, never direct CAS) and "
    "reduced_isolation=true is recorded in the manifest. Recommended hardening: run the backend as "
    "a throwaway low-privilege OS user, or inside a container (Docker/Podman), to recover some "
    "confinement."
)


class RestrictedIsolation:
    """``BackendIsolation`` that runs the backend with NO kernel confinement (opt-in fallback)."""

    name = "restricted"

    def available(self) -> bool:
        """Always ``True`` — the fallback runs anywhere; WHETHER it is selected is the policy gate.

        Availability is not the safety gate: ``select_backend_isolation`` only ever returns this
        when ``allow_reduced_isolation`` is explicitly set (else it raises ``SandboxUnavailable``
        and fails closed, ADR-0013). So returning ``True`` here cannot weaken the default posture.
        """
        return True

    def self_test(
        self,
        throwaway_worktree: Path,
        throwaway_tmp: Path,
        backend_read_roots: list[Path],
    ) -> SelfTestReport:
        """Delegate to the shared probes; the FS/network assertions are EXPECTED to fail here.

        The restricted fallback provides no kernel confinement, so write-outside and network probes
        do NOT get EPERM — ``passed`` will be ``False``. That is correct and honest: a restricted
        run is used only with the operator's explicit opt-in + forced review-mode, never on the
        strength of a passing self-test (ADR-0013).
        """
        from .selftest import self_test

        return self_test(self, throwaway_worktree, throwaway_tmp, backend_read_roots)

    def run(self, spec: SandboxSpec) -> SandboxResult:
        """Run ``spec.argv`` with ``cwd`` = worktree and scrubbed env; NO confinement (ADR-0013).

        Sets ``HOME`` / ``TMPDIR`` → ``tmp_dir`` (outside the worktree) and a sane ``PATH`` on top
        of the already-scrubbed env, runs ``shell=False`` with stdin prompts. Returns
        ``reduced_isolation=True`` so the orchestrator records it and forces review-mode. Does NOT
        pretend to confine network or out-of-worktree writes — those guarantees are LOST (see
        :data:`WARNING`). On timeout the process GROUP is killed (SIGTERM → 10s grace → SIGKILL) and
        ``TimeoutExpired`` re-raised.
        """
        worktree = Path(spec.worktree).resolve(strict=True)
        tmp_dir = Path(spec.tmp_dir).resolve(strict=True)
        env = spec.env | {
            "HOME": str(tmp_dir),
            "TMPDIR": str(tmp_dir),
            "PATH": _SANDBOX_PATH,
        }
        proc = subprocess.Popen(  # noqa: S603 — argv list; shell=False; no interpolation.
            list(spec.argv),
            stdin=subprocess.PIPE if spec.stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(worktree),
            env=env,
            shell=False,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(input=spec.stdin_data, timeout=spec.timeout_s)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            try:
                proc.communicate(timeout=_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                pass
            raise
        return SandboxResult(
            returncode=proc.returncode,
            stdout=stdout or b"",
            stderr=stderr or b"",
            mechanism="restricted",
            reduced_isolation=True,
        )


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM then (after a grace) SIGKILL the child's process GROUP (ADR-0013 timeout)."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + _KILL_GRACE_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
