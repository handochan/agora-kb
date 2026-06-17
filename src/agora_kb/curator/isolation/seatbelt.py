"""macOS Seatbelt adapter — the Phase-1 default ``BackendIsolation`` (ADR-0013 §"macOS Phase-1").

Confines the backend with Apple's ``/usr/bin/sandbox-exec`` and the deny-default SBPL profile in
``profiles/base.sbpl``. The profile is an ALLOWLIST (``(deny default)`` first, so a later
``(deny ...)`` always wins) granting writes ONLY to ``/dev/null`` + the worktree + a separate
throwaway scratch, and explicitly denying ALL network (``(deny network*)`` /
``(deny system-socket)``) plus the main repo's ``.git`` / ``_kb`` realpaths and the worktree's own
``.git`` pointer file (G2/G4/G5, ADR-0013).

Five empirically-verified hazards this adapter encodes (ADR-0013 §"Context"):

* **Realpath (the ``/tmp`` → ``/private/tmp`` bug).** Seatbelt matches the RESOLVED vnode path, so
  a ``(subpath (param "WORKTREE"))`` keyed on an unresolved ``/tmp/...`` path silently fails to
  grant the write (EPERM, no policy error). :meth:`run` therefore re-asserts every spec path is
  already realpath (the spec contract requires it) and resolves what it derives.
* **Linked-worktree topology.** A linked worktree's ``.git`` is a 64-byte ``gitdir:`` pointer FILE;
  the real hooks/config/objects and ``_kb/`` live in the MAIN repo OUTSIDE the worktree. The MAIN
  repo root is derived from ``git rev-parse --git-common-dir`` (which yields the realpath of
  ``main/.git``), and ``MAIN_GIT`` / ``MAIN_KB`` are denied by subpath — not by sibling placement.
* **Apple-shim ``/dev/null``.** Apple-shimmed CLIs (git via ``xcrun``) fatally crash unless
  ``/dev/null`` is writable; the profile grants exactly that one character-device write.
* **Two writable mounts.** ``HOME`` / ``TMPDIR`` point at ``tmp_dir`` (OUTSIDE the worktree) so the
  backend's dotfiles/caches land there and never pollute the worktree diff ADR-0008 validates.
* **No string interpolation.** Paths reach the policy ONLY as ``-D KEY=value`` params referenced
  via ``(param "KEY")``; ``argv`` is appended verbatim after ``--`` with ``shell=False``. A path is
  never concatenated into the SBPL or a shell line.

On timeout the whole process GROUP is killed (SIGTERM → 10s grace → SIGKILL), not just the leader,
so a forked-children backend cannot survive the kill (ADR-0013 §"adapters.yaml → SandboxSpec map").
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from .base import SandboxResult, SandboxSpec, SelfTestReport

__all__ = ["SeatbeltIsolation", "SANDBOX_EXEC"]

# Pin the absolute binary so a tampered ``PATH`` cannot substitute it (ADR-0013 §"macOS Phase-1").
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# The static deny-default allowlist, shipped beside this module. Read at run time (not import) so
# the
# package imports cleanly on any OS even though the profile is macOS-only.
_PROFILE_PATH = Path(__file__).parent / "profiles" / "base.sbpl"

# Grace between SIGTERM and SIGKILL when a run exceeds its wall clock (ADR-0013): SIGTERM the group,
# wait this long, then SIGKILL the group, then re-raise ``subprocess.TimeoutExpired``.
_KILL_GRACE_S = 10.0

# A sane, minimal PATH handed into the sandbox so Apple-shimmed tools + Homebrew binaries resolve
# without inheriting a tampered host PATH. The egress deny means PATH cannot widen the attack
# surface.
_SANDBOX_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"


class SeatbeltIsolation:
    """``BackendIsolation`` for macOS via ``/usr/bin/sandbox-exec`` + ``profiles/base.sbpl``."""

    name = "seatbelt"

    def available(self) -> bool:
        """True iff the pinned ``/usr/bin/sandbox-exec`` exists and is executable (cheap probe)."""
        return os.path.isfile(SANDBOX_EXEC) and os.access(SANDBOX_EXEC, os.X_OK)

    def self_test(
        self,
        throwaway_worktree: Path,
        throwaway_tmp: Path,
        backend_read_roots: list[Path],
    ) -> SelfTestReport:
        """Delegate to the shared hardened probes (ADR-0013 §"Runtime self-test")."""
        from .selftest import self_test  # local import: selftest imports nothing back from here

        return self_test(self, throwaway_worktree, throwaway_tmp, backend_read_roots)

    def run(self, spec: SandboxSpec) -> SandboxResult:
        """Run ``spec.argv`` under Seatbelt; return the raw :class:`SandboxResult`.

        Builds the full policy text (static ``base.sbpl`` + one ``(allow file-read* (subpath (param
        "READABLE_ROOT_n")))`` line per read root), then invokes ``sandbox-exec -p <policy> -D...
        -- argv`` with ``HOME`` / ``TMPDIR`` → ``tmp_dir`` and a sane ``PATH``, ``cwd`` = worktree,
        ``shell=False``. ``MAIN_GIT`` / ``MAIN_KB`` come from ``spec`` if the caller pinned them,
        else are derived from the worktree's linked-worktree git-common-dir (the main repo root).
        On timeout the process GROUP is killed (SIGTERM → 10s grace → SIGKILL) and
        ``subprocess.TimeoutExpired`` re-raised.
        """
        # realpath-assert: the spec contract requires resolved paths; re-assert so a caller bug
        # surfaces HERE (a stray loud failure) rather than as a silent EPERM from Seatbelt matching
        # an unresolved vnode (the /tmp -> /private/tmp bug, ADR-0013 §realpath).
        worktree = _assert_realpath(spec.worktree, "worktree")
        tmp_dir = _assert_realpath(spec.tmp_dir, "tmp_dir")
        if _is_within(tmp_dir, worktree):
            raise ValueError(
                f"tmp_dir {tmp_dir} must be OUTSIDE the worktree {worktree} "
                "(HOME/TMPDIR point here; scratch inside the worktree would pollute the diff)"
            )
        read_roots = [_assert_realpath(p, "read_root") for p in spec.read_roots]
        main_git, main_kb = _resolve_main_paths(spec, worktree)

        # policy text: static base + one read-root allow line per root. Paths are NEVER interpolated
        # into the policy body — only their KEYS appear, referenced via (param "READABLE_ROOT_n").
        policy = _PROFILE_PATH.read_text(encoding="utf-8")
        read_root_lines = "".join(
            f'(allow file-read* (subpath (param "READABLE_ROOT_{i}")))\n'
            for i in range(len(read_roots))
        )
        if read_root_lines:
            tail = f";; dynamic read roots (one per backend read_root)\n{read_root_lines}"
            policy = f"{policy}\n{tail}"

        # ── -D params: every path crosses as a value bound to a KEY, referenced inside the SBPL via
        #    (param "KEY"). No path is ever spliced into the policy or a shell string.
        cmd: list[str] = [
            SANDBOX_EXEC,
            "-p",
            policy,
            f"-DWORKTREE={worktree}",
            f"-DTMP={tmp_dir}",
            f"-DMAIN_GIT={main_git}",
            f"-DMAIN_KB={main_kb}",
        ]
        for i, root in enumerate(read_roots):
            cmd.append(f"-DREADABLE_ROOT_{i}={root}")
        cmd.append("--")
        cmd.extend(spec.argv)  # backend command, appended VERBATIM — no shell, no concat.

        # ── env: the caller already scrubbed credentials; we only add HOME/TMPDIR (→ scratch
        # OUTSIDE
        # the worktree) and a sane PATH. The dict-union keeps the scrubbed env and overrides exactly
        #    these three keys.
        env = spec.env | {
            "HOME": str(tmp_dir),
            "TMPDIR": str(tmp_dir),
            "PATH": _SANDBOX_PATH,
        }

        return self._spawn(cmd, spec=spec, worktree=worktree, env=env)

    def _spawn(
        self,
        cmd: list[str],
        *,
        spec: SandboxSpec,
        worktree: Path,
        env: dict[str, str],
    ) -> SandboxResult:
        """Spawn the sandboxed process in its OWN process group and enforce the wall clock.

        ``start_new_session=True`` puts the child (and the children it forks) in a new process
        group, so a timeout kills the WHOLE group (SIGTERM → 10s grace → SIGKILL), not just the
        leader — a double-forking backend cannot outlive the kill (ADR-0013). ``shell=False`` is a
        hard requirement.
        """
        proc = subprocess.Popen(  # noqa: S603 — argv list; shell=False; no interpolation.
            cmd,
            stdin=subprocess.PIPE if spec.stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(worktree),
            env=env,
            shell=False,
            start_new_session=True,  # new process group → group-wide kill on timeout
        )
        try:
            stdout, stderr = proc.communicate(input=spec.stdin_data, timeout=spec.timeout_s)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            # Drain the pipes so the killed children's fds are reaped (avoid a ResourceWarning
            # leak), then re-raise the timeout for the orchestrator's FAILED-run channel (§4).
            try:
                proc.communicate(timeout=_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                pass
            raise
        return SandboxResult(
            returncode=proc.returncode,
            stdout=stdout or b"",
            stderr=stderr or b"",
            mechanism="seatbelt",
            reduced_isolation=False,
        )


def _assert_realpath(path: Path, label: str) -> Path:
    """Return ``path`` if it is already its own realpath (resolved, strict); else raise ValueError.

    The spec contract requires the caller pass realpath-resolved paths (ADR-0013 §realpath). We
    re-resolve and compare so a caller that forgot fails LOUDLY here rather than producing a silent
    EPERM from Seatbelt matching an unresolved vnode.
    """
    resolved = Path(path).resolve(strict=True)
    if resolved != Path(path):
        raise ValueError(
            f"{label} {path!r} is not realpath-resolved (resolves to {resolved!r}); "
            "the caller MUST pass Path(...).resolve(strict=True) paths (ADR-0013 §realpath)"
        )
    return resolved


def _is_within(inner: Path, outer: Path) -> bool:
    """True iff ``inner`` is ``outer`` or lives under it (both assumed realpath-resolved)."""
    try:
        inner.relative_to(outer)
    except ValueError:
        return False
    return True


def _resolve_main_paths(spec: SandboxSpec, worktree: Path) -> tuple[Path, Path]:
    """Return the realpaths to deny: ``(MAIN_GIT, MAIN_KB)`` for the main repo behind ``worktree``.

    If the caller pinned them on the spec (the documented explicit-input path) they are used
    verbatim (re-resolved). Otherwise they are DERIVED from the linked worktree: ``git rev-parse
    --git-common-dir`` run inside the worktree yields the realpath of the MAIN repo's ``.git``; its
    parent is the main repo root, and ``_kb`` sits beside it. This is why a linked worktree's own
    ``.git`` pointer file (which points INTO ``main/.git/worktrees/<id>``) cannot reach the main
    objects/hooks: those live under ``MAIN_GIT`` and are denied by subpath (G4/G5, ADR-0013).
    """
    if spec.main_git is not None and spec.main_kb is not None:
        return (
            Path(spec.main_git).resolve(strict=True),
            Path(spec.main_kb).resolve(strict=True),
        )
    main_git = _git_common_dir(worktree)
    main_repo = main_git.parent
    main_kb = (main_repo / "_kb").resolve(strict=False)  # _kb may not exist yet; deny anyway
    return main_git, main_kb


def _git_common_dir(worktree: Path) -> Path:
    """Realpath of the MAIN repo's ``.git`` for the linked ``worktree`` (``git rev-parse``).

    ``--git-common-dir`` returns the shared (main) ``.git`` even from a linked worktree — on macOS
    it comes back already realpath-resolved (``/private/tmp/...``). Run hermetically (no shell,
    hooks neutralized) so a planted hook can never execute during path resolution (ADR-0008 step 3).
    """
    cp = subprocess.run(  # noqa: S603 — argv list, no shell, hooks neutralized.
        ["git", "-c", f"core.hooksPath={os.devnull}", "rev-parse", "--git-common-dir"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            f"could not resolve the main .git for worktree {worktree} "
            f"(git rev-parse --git-common-dir rc={cp.returncode}): {cp.stderr.strip()}"
        )
    common = Path(cp.stdout.strip())
    if not common.is_absolute():
        common = worktree / common
    return common.resolve(strict=True)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM the child's process GROUP, wait a grace, then SIGKILL the group (ADR-0013 timeout).

    Uses ``os.killpg`` against the group created by ``start_new_session=True`` so forked children
    die too. Swallows ``ProcessLookupError`` (the group may already be gone); teardown best-effort.
    """
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
