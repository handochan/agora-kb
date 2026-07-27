"""Linux bubblewrap adapter — ``BackendIsolation`` via ``bwrap --unshare-all`` (ADR-0013 §"Linux").

Confines the backend with ``bubblewrap`` (``bwrap``), a separate LGPL-2.0 EXECUTABLE invoked as a
subprocess (never linked — same posture as calling ``/usr/bin/git``), so no copyleft attaches to
the permissively-licensed core (invariant #4, ADR-0013 §"OSS-license posture"). The authoritative
network deny is the kernel ``--unshare-net`` network namespace (loopback-only), NOT a proxy.

**The read posture is whole-filesystem READ-ONLY — deliberate parity with macOS** (ADR-0013
appendix, issue #115). The mount plan is ``--ro-bind / /`` (everything READABLE, nothing writable)
plus exactly two writable binds: the worktree checkout dir (the single writable CONTENT mount) and a
SEPARATE ``$TMP`` rw-bind OUTSIDE the worktree (= ``HOME`` / ``TMPDIR``), so the worktree diff stays
clean of the backend's dotfiles/caches. That is byte-for-byte the seatbelt policy: broad
``(allow file-read*)``, ``file-write*`` only for ``/dev/null`` + ``WORKTREE`` + ``TMP``.

This REPLACES the original deny-by-omission bind set (``/usr`` ``/bin`` ``/lib`` ``/lib64`` + the
operator's ``read_roots``), which could not express a real backend: every root reaching an adapter
is ``resolve(strict=True)``-ed, while ``execvp`` walks the ACCESS path, so a venv interpreter
(``.venv/bin/python`` → ``<uv>/cpython-3.12-linux-x86_64-gnu/bin/python3.12``, itself a
minor-version alias symlink) had an unbound intermediate component and died with a misleading
``bwrap: execvp …: No such file or directory``. PASS-2 then wrote nothing and the run published
empty bodies (#115). No ``read_roots`` value could fix it — the alias mountpoint is unexpressible by
construction. ``read_roots`` are still bound (harmless no-ops under the root bind) so the config
keeps its meaning if the read posture is tightened later — on BOTH platforms together.

**G4/G5 (the main repo's ``.git`` / ``_kb``) remain protected by the READ-ONLY root**: they are
visible but unwritable (``EROFS``), exactly as on macOS where the profile denies ``file-write*`` on
their realpaths. The worktree's own ``.git`` pointer file is inside the writable mount and CAN be
rewritten (git refuses to index any path with a ``.git`` component, so the final-diff gate does not
see it) — the protection is that everything such a pointer could name is read-only, so a repointed
worktree cannot write another repo's objects, hooks or config. That is the same guarantee the
seatbelt profile buys with its explicit ``(deny file-write* (path WORKTREE/.git))``.

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


def _mount_plan() -> list[str]:
    """The namespace mount plan SHARED by the real invocation and the availability probe.

    ``--ro-bind / /`` is the whole read surface (macOS-parity, #115): every path the backend needs
    to *start* — its interpreter, that interpreter's stdlib, every intermediate symlink component,
    its shebang, its shared libraries — exists at the same path it does on the host, so nothing has
    to be enumerated and nothing can be silently missing. Writability is unaffected: the root is
    READ-ONLY and the caller appends the only two writable binds afterwards.

    ``--proc`` / ``--dev`` and the :func:`_mask_socket_dirs` tmpfs masks MUST come AFTER the root
    bind: applied in order, an earlier ``--proc`` would be covered by the later root bind and the
    sandbox would see the HOST's ``/proc`` (every process on the machine) and ``/dev``. Verified
    empirically — with the binds in the wrong order the namespace sees the host's process table.

    SHARED with :meth:`BwrapIsolation.available` on purpose. Every past drift between the two has
    been a bug in one direction or the other: a probe binding LESS than the run hit the missing-ELF
    -interpreter ENOENT and reported "no usable sandbox" on ordinary Linux hosts (#113), and a probe
    OMITTING a mount the run performs would green-light a sandbox that then fails at run time —
    which on the PASS-2 path publishes placeholder bodies while reporting success (#115). One
    function, so they cannot disagree.
    """
    return [
        "--ro-bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        *_mask_socket_dirs(),
    ]


# Directories that carry AF_UNIX sockets, masked with an empty tmpfs. `--unshare-net` does NOT
# cover a PATHNAME unix socket (it is reached through the filesystem, not the network stack) and a
# read-only mount does not stop `connect()` — only writes to reg/dir/link return EROFS. So under a
# bare whole-filesystem read bind a confined backend could talk to `/run/docker.sock`, the session
# bus, a gpg/ssh agent, or anything else listening on a socket FILE. Masking is what keeps the
# posture at seatbelt parity, whose `(deny default)` + `(deny system-socket)` refuse those sockets
# outright. VERIFIED both ways on Linux: without the mask a connect() SUCCEEDS; with it, ENOENT.
#
# `/tmp` is masked too, which also restores the private WRITABLE tmpfs the pre-#115 plan gave the
# backend (a hard-coded `/tmp/...` write would otherwise hit the read-only root).
_SOCKET_DIRS = ("/run", "/var/run", "/tmp")


def _masked_dirs() -> list[str]:
    """The :data:`_SOCKET_DIRS` that exist on THIS host, realpath-resolved and de-duplicated.

    Resolved, because bwrap cannot mount onto a symlink (``Can't mount tmpfs on …: No such file or
    directory``, which aborts the whole namespace rather than the one mount) — and on a merged-/run
    distro ``/var/run`` IS a symlink to ``/run``, exactly the layout most hosts have. Resolving
    instead of skipping keeps the mask effective there; de-duplicating keeps the two entries from
    stacking two tmpfs mounts on the same directory. A path that does not exist is simply dropped.
    """
    seen: list[str] = []
    for path in _SOCKET_DIRS:
        try:
            resolved = Path(path).resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and str(resolved) not in seen:
            seen.append(str(resolved))
    return seen


def _mask_socket_dirs() -> list[str]:
    """``--tmpfs`` argv pairs masking every :func:`_masked_dirs` entry with an empty tmpfs."""
    argv: list[str] = []
    for path in _masked_dirs():
        argv += ["--tmpfs", path]
    return argv


def _argv_file_binds(argv_in: list[str]) -> list[str]:
    """Re-admit the operator's own argv FILES that a :func:`_mask_socket_dirs` mask would hide.

    The masks are blunt, and a brain script or its interpreter may legitimately live under one of
    them (the test suite's stub brain sits in a ``pytest`` tmp dir under ``/tmp``; an operator may
    keep a scratch brain there too). Each existing regular file named in ``argv`` and located under
    a masked dir is ro-bound back at its own path — bwrap can create the mountpoint because the
    mask underneath is a writable tmpfs.

    Deliberately narrow: FILES only (never their parent directory, which for a brain script at a KB
    repo root would expose ``_kb/``), and only under a masked dir — everything else is already
    readable through the root bind. This is the operator's OWN configured command, not something a
    model can influence: ``argv`` comes from ``adapters.yaml``, and a plan cannot reach it.
    """
    masked = _masked_dirs()
    argv: list[str] = []
    for arg in argv_in:
        if not arg.startswith("/"):
            continue
        p = Path(arg)
        try:
            resolved = str(p.resolve(strict=True))
        except OSError:
            continue
        # Compare BOTH the literal and the resolved path: a mask lands on the resolved dir, but an
        # argv entry may reach it through a symlinked prefix (a ``/tmp`` → ``/private/tmp`` style
        # layout is not macOS-only).
        if not any(
            candidate == d or candidate.startswith(d + "/")
            for d in masked
            for candidate in (arg, resolved)
        ):
            continue
        if p.is_file():
            argv += ["--ro-bind", arg, arg]
    return argv


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
        """Best-effort: can ``bwrap`` actually create the namespaces + mounts? Run a no-op under it.

        The probe runs the REAL :func:`_mount_plan` — not a subset — so it proves what the run
        needs: the user namespace AND the ``/proc`` mount (which a container without the right
        capabilities refuses even when the namespaces themselves are fine). A probe weaker than the
        real invocation is worse than none: it green-lights a sandbox that then fails at run time,
        which on the PASS-2 path degrades to a silently-empty note (#115).
        """
        try:
            cp = subprocess.run(  # noqa: S603 — fixed argv, no shell.
                [
                    "bwrap",
                    "--unshare-all",
                    "--die-with-parent",
                    *_mount_plan(),
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

        The mount plan is :func:`_mount_plan` (whole-filesystem READ-ONLY + a private ``/proc`` and
        ``/dev``) plus the ONLY two writable binds: the worktree (the single writable CONTENT
        mount) and a SEPARATE ``$TMP`` (= ``HOME`` / ``TMPDIR``, OUTSIDE the worktree). The main
        repo's ``.git`` / ``_kb`` are readable but NOT writable (``EROFS``), matching the seatbelt
        profile's explicit ``(deny file-write* …)`` on the same paths. ``--unshare-all`` (network
        namespace) is the egress deny. On timeout the process GROUP is killed (SIGTERM → 10s grace →
        SIGKILL) and ``TimeoutExpired`` re-raised.
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
            *_mount_plan(),
        ]
        # The operator's per-backend read_roots. Redundant under the whole-filesystem root bind
        # (they are already readable), kept because they remain the declared read contract in
        # adapters.yaml and become load-bearing again the day the read posture is tightened — on
        # BOTH platforms together, not one silently ahead of the other (#115). Bound AFTER the
        # masks so a read root under a masked dir still resolves.
        for root in read_roots:
            argv += ["--ro-bind", str(root), str(root)]
        argv += _argv_file_binds(spec.argv)
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
