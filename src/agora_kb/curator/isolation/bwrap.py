"""Linux bubblewrap adapter — ``BackendIsolation`` via ``bwrap --unshare-all`` (ADR-0013 §"Linux").

Confines the backend with ``bubblewrap`` (``bwrap``), a separate LGPL-2.0 EXECUTABLE invoked as a
subprocess (never linked — same posture as calling ``/usr/bin/git``), so no copyleft attaches to
the permissively-licensed core (invariant #4, ADR-0013 §"OSS-license posture"). The authoritative
network deny is the kernel ``--unshare-net`` network namespace (loopback-only), NOT a proxy.

**G4/G5 on Linux is by OMISSION, cleaner than macOS** (ADR-0013): the main repo's
``.git/{config,hooks,worktrees}`` and ``_kb/`` are simply NEVER bind-mounted, so they do not exist
in the sandbox namespace — no carve-out needed. We bind ONLY the worktree checkout dir (the single
writable CONTENT mount) and a SEPARATE ``$TMP`` rw-bind OUTSIDE the worktree (= ``HOME`` /
``TMPDIR``), mirroring macOS so the worktree diff stays clean of the backend's dotfiles/caches. A
linked worktree's ``.git`` pointer file points at a ``main/.git/worktrees/<id>`` path that does not
exist in the namespace, so a rewrite resolves to nothing, and the ADR-0008 post-hoc validator still
rejects the modified pointer.

**Unprivileged-userns caveat** (ADR-0013): Ubuntu 24.04+ and many containers set
``kernel.apparmor_restrict_unprivileged_userns=1``, blocking ``bwrap``'s user namespace.
:meth:`available` best-effort probes this; the remediation is an AppArmor profile granting userns to
``/usr/bin/bwrap``, surfaced by the self-test / ``agora doctor``. If unavailable, selection falls
through to the restricted fallback (opt-in only) or fails closed.

This adapter is correct-but-lightly-tested on the macOS dev host (it cannot run here); it is kept
REAL, not a stub, so a Linux deployment works without further code. Optional Landlock/seccomp layers
(ADR-0013) are documented as later hardening and not implemented here.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .base import SandboxResult, SandboxSpec, SelfTestReport

__all__ = ["BwrapIsolation", "USERNS_REMEDIATION"]

# Grace between SIGTERM and SIGKILL on timeout, mirroring the seatbelt adapter (ADR-0013).
_KILL_GRACE_S = 10.0

# The minimal in-sandbox PATH (matches the ADR-0013 bwrap plan: `--setenv PATH "/usr/bin:/bin"`).
_SANDBOX_PATH = "/usr/bin:/bin"

# Operator remediation surfaced when an unprivileged user namespace is blocked (ADR-0013 §"Linux
# plan", the AppArmor-userns caveat). Printed by the self-test / ``agora doctor`` so the fix is
# actionable.
USERNS_REMEDIATION = (
    "bubblewrap could not create an unprivileged user namespace. On Ubuntu 24.04+ / hardened hosts "
    "this is blocked by kernel.apparmor_restrict_unprivileged_userns=1. Remediation: install an "
    "AppArmor profile granting userns to /usr/bin/bwrap (see the bubblewrap docs), OR run the "
    "curator inside a container that permits userns. Until then the curator falls through to the "
    "restricted fallback (opt-in only) or fails closed."
)


class BwrapIsolation:
    """``BackendIsolation`` for Linux via ``bwrap --unshare-all`` (netns = egress deny)."""

    name = "bwrap"

    def available(self) -> bool:
        """True iff ``bwrap`` is on PATH AND an unprivileged userns is usable (best-effort probe).

        The userns probe runs a trivial ``bwrap ... true`` with all namespaces unshared; if the
        kernel blocks the user namespace (AppArmor restriction) it exits non-zero and we report
        unavailable so selection can surface :data:`USERNS_REMEDIATION` and fall through (ADR-0013).
        """
        if shutil.which("bwrap") is None:
            return False
        return self._userns_usable()

    @staticmethod
    def _userns_usable() -> bool:
        """Best-effort: can ``bwrap`` actually create the namespaces? Run a tiny no-op under it."""
        try:
            cp = subprocess.run(  # noqa: S603 — fixed argv, no shell.
                [
                    "bwrap",
                    "--unshare-all",
                    "--die-with-parent",
                    "--ro-bind",
                    "/usr",
                    "/usr",
                    "--ro-bind",
                    "/bin",
                    "/bin",
                    "--",
                    "true",
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return False
        return cp.returncode == 0

    def self_test(
        self,
        throwaway_worktree: Path,
        throwaway_tmp: Path,
        backend_read_roots: list[Path],
    ) -> SelfTestReport:
        """Delegate to the shared hardened probes (ADR-0013 §"Runtime self-test")."""
        from .selftest import self_test

        return self_test(self, throwaway_worktree, throwaway_tmp, backend_read_roots)

    def run(self, spec: SandboxSpec) -> SandboxResult:
        """Run ``spec.argv`` under ``bwrap``; return the raw :class:`SandboxResult`.

        Binds ONLY the worktree (rw, the single writable CONTENT mount) and a SEPARATE ``$TMP`` (rw,
        = ``HOME`` / ``TMPDIR``, OUTSIDE the worktree). ``/usr`` ``/bin`` ``/lib`` ``/lib64`` and
        every ``read_root`` are ro-binds. The main repo's ``.git`` / ``_kb`` are NEVER bound (G4/G5
        by omission). ``--unshare-all`` (network namespace) is the egress deny. On timeout the
        process GROUP is killed (SIGTERM → 10s grace → SIGKILL) and ``TimeoutExpired`` re-raised.
        """
        worktree = Path(spec.worktree).resolve(strict=True)
        tmp_dir = Path(spec.tmp_dir).resolve(strict=True)
        if _is_within(tmp_dir, worktree):
            raise ValueError(
                f"tmp_dir {tmp_dir} must be OUTSIDE the worktree {worktree} (ADR-0013)"
            )
        read_roots = [Path(p).resolve(strict=True) for p in spec.read_roots]

        argv = [
            "bwrap",
            # == --unshare-{user-try,ipc,pid,net,uts,cgroup-try}: the network namespace is the
            # authoritative egress deny (loopback-only), kernel-enforced — not a proxy.
            "--unshare-all",
            "--die-with-parent",
            "--new-session",  # setsid; blocks TIOCSTI keystroke injection
            "--clearenv",
            "--setenv",
            "HOME",
            str(tmp_dir),
            "--setenv",
            "TMPDIR",
            str(tmp_dir),
            "--setenv",
            "PATH",
            _SANDBOX_PATH,
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        # ro-bind the system dirs needed for the interpreter/CLI to start; skip any that are absent
        # on this host (e.g. /lib64) so a minimal layout does not fail the bind.
        for sysdir in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(sysdir).exists():
                argv += ["--ro-bind", sysdir, sysdir]
        for root in read_roots:
            argv += ["--ro-bind", str(root), str(root)]
        # The ONLY writable mounts: the worktree (content) and the separate scratch (HOME/TMPDIR).
        argv += ["--bind", str(worktree), str(worktree)]
        argv += ["--bind", str(tmp_dir), str(tmp_dir)]
        argv += ["--chdir", str(worktree)]
        argv += ["--"]
        argv += spec.argv  # verbatim, no shell

        return self._spawn(argv, spec=spec)

    def _spawn(self, argv: list[str], *, spec: SandboxSpec) -> SandboxResult:
        """Spawn ``bwrap`` in its own process group; enforce the wall clock with a group kill."""
        proc = subprocess.Popen(  # noqa: S603 — argv list; shell=False; no interpolation.
            argv,
            stdin=subprocess.PIPE if spec.stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
            mechanism="bwrap",
            reduced_isolation=False,
        )


def _is_within(inner: Path, outer: Path) -> bool:
    """True iff ``inner`` is ``outer`` or lives under it (both assumed realpath-resolved)."""
    try:
        inner.relative_to(outer)
    except ValueError:
        return False
    return True


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
