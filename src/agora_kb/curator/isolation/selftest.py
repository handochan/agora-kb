"""Runtime self-test + capability detection (ADR-0013 §"Runtime self-test").

Runs the REAL sandbox config against a throwaway worktree AND a separate throwaway ``tmp_dir``
before any real run trusts the sandbox, and doubles as the platform-capability detector. Three
hardenings make the verdict trustworthy (ADR-0013):

* the network assertion is **EPERM-specific** and uses a **REACHABLE** target (the host Ollama
  daemon at ``127.0.0.1:11434``) so "blocked" provably means EPERM, NOT "host unreachable /
  connection refused" — a non-EPERM ``OSError`` (``NET_AMBIGUOUS``) FAILS the self-test, never pass;
* it runs an **Apple-shimmed binary** (``/usr/bin/git --version``) to catch the ``xcrun_db`` /
  ``/dev/null`` fatal that a missing ``(allow file-write* /dev/null)`` would cause; and
* ``read_roots`` come from the REAL configured backend (passed in), so the probe exercises the same
  read posture a real run gets.

Two probes (ADR-0013 §"Runtime self-test", VERBATIM behavior):

1. a ``python -c`` that (a) writes INSIDE the worktree (must succeed), (b) writes OUTSIDE
   (``~/agora-selftest-probe``) and must get ``PermissionError`` — else prints ``OUTSIDE_WRITE_OK``,
   (c) ``socket.create_connection(("127.0.0.1", 11434), timeout=2)`` must raise ``PermissionError``
   / ``EPERM`` — else prints ``NET_OK`` / ``NET_AMBIGUOUS:<errno>``; prints ``SELFTEST_FS_NET_PASS``
   on success; and
2. ``["/usr/bin/git", "--version"]`` must succeed (returncode 0, ``git version`` in stdout) —
   proving the ``/dev/null`` write-allow.

The result is cached by the orchestrator keyed on ``{mechanism, OS build, sandbox-exec mtime}`` so
the self-test re-runs only when that key changes; :func:`selftest_cache_key` returns the string to
persist. This module builds the cache KEY but does NOT write ``_kb/state.json`` — persistence
belongs to the caller (the orchestrator), keeping this module free of any repo-layout coupling.
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

from .base import BackendIsolation, SandboxSpec, SelfTestReport

__all__ = ["self_test", "selftest_cache_key", "ollama_reachable"]

# The reachable target the network probe connects to (the host Ollama daemon). Using a LISTENING
# target
# is what makes an EPERM provably mean "blocked by the sandbox", not "nothing is there" (ADR-0013).
_OLLAMA_HOST = "127.0.0.1"
_OLLAMA_PORT = 11434

# The out-of-both-mounts path the write-outside probe targets. EXPANDED inside the probe via
# ``Path("~/...").expanduser()`` so it resolves to the SANDBOX's HOME (= tmp_dir) — which is itself
# a
# writable mount — so we instead target a path OUTSIDE tmp_dir by using the REAL invoking user's
# home.
_OUTSIDE_PROBE = "~/agora-selftest-probe"


def self_test(
    isolation: BackendIsolation,
    throwaway_worktree: Path,
    throwaway_tmp: Path,
    backend_read_roots: list[Path],
) -> SelfTestReport:
    """Run the two hardened probes against ``isolation``; return a :class:`SelfTestReport`.

    ``throwaway_worktree`` and ``throwaway_tmp`` MUST be DISTINCT realpath-resolved dirs with
    ``tmp`` NOT inside ``wt`` (asserted). Probe 1 is the filesystem+network ``python -c`` (EPERM-
    specific, reachable target); probe 2 is the Apple-shim ``git --version``. The booleans are
    derived ONLY from the probe markers / returncodes, never from "any error" (ADR-0013): a non-
    EPERM ``OSError`` on the network leg yields ``NET_AMBIGUOUS`` and FAILS — not treated as denied.
    """
    wt = throwaway_worktree.resolve(strict=True)
    tmp = throwaway_tmp.resolve(strict=True)
    if tmp == wt or str(tmp).startswith(str(wt) + os.sep):
        raise ValueError(
            f"self_test requires a tmp_dir ({tmp}) DISTINCT from and OUTSIDE the worktree ({wt})"
        )
    read_roots = [p.resolve(strict=True) for p in backend_read_roots]
    main_git, main_kb = _selftest_main_paths(wt, tmp)

    inside = wt / "probe.txt"
    # The write-outside target must be OUTSIDE both mounts. The sandbox sets HOME=tmp_dir, so a
    # ``~``-expansion INSIDE the sandbox would resolve to the (writable) tmp_dir. We instead resolve
    # the REAL invoking user's home HERE (outside the sandbox) and pass the absolute path into the
    # probe, so the probe writes to a path the sandbox must deny.
    outside = Path(_OUTSIDE_PROBE).expanduser()

    # Probe 1: filesystem + network, asserting EPERM specifically and using a REACHABLE target.
    probe_fs_net = textwrap.dedent(
        f"""
        import errno, socket, sys
        open({str(inside)!r}, "w").write("ok")                       # (a) inside worktree -> OK
        try:                                                          # (b) outside -> must be EPERM
            open({str(outside)!r}, "w").write("x"); print("OUTSIDE_WRITE_OK"); sys.exit(2)
        except PermissionError: pass
        # (c) network: target a REACHABLE host (the user's own Ollama 127.0.0.1:11434, listening)
        #     so "blocked" provably means EPERM, NOT "host unreachable / connection refused".
        try:
            socket.create_connection(({_OLLAMA_HOST!r}, {_OLLAMA_PORT}), timeout=2)
            print("NET_OK"); sys.exit(3)
        except PermissionError: pass                                  # the ONLY accepted "denied"
        except OSError as e:
            if e.errno == errno.EPERM: pass
            else: print(f"NET_AMBIGUOUS:{{e.errno}}"); sys.exit(4)    # refused/unreachable != block
        print("SELFTEST_FS_NET_PASS")
        """
    ).strip()

    spec1 = SandboxSpec(
        argv=[sys.executable, "-c", probe_fs_net],
        worktree=wt,
        tmp_dir=tmp,
        read_roots=read_roots,
        stdin_data=None,
        env=_scrub_environ(),
        main_git=main_git,
        main_kb=main_kb,
    )
    r1 = isolation.run(spec1)

    # Probe 2: run an Apple-shimmed binary to catch the xcrun_db / dev-null fatal. Without
    # ``(allow file-write* /dev/null)`` this FATALLY fails; with it, git succeeds (xcrun warns).
    spec2 = SandboxSpec(
        argv=["/usr/bin/git", "--version"],
        worktree=wt,
        tmp_dir=tmp,
        read_roots=read_roots,
        stdin_data=None,
        env=_scrub_environ(),
        main_git=main_git,
        main_kb=main_kb,
    )
    r2 = isolation.run(spec2)

    write_inside_ok = inside.exists()
    write_outside_denied = b"OUTSIDE_WRITE_OK" not in r1.stdout
    network_denied = b"NET_OK" not in r1.stdout and b"NET_AMBIGUOUS" not in r1.stdout
    apple_shim_ok = r2.returncode == 0 and b"git version" in r2.stdout
    passed = (
        r1.returncode == 0
        and b"SELFTEST_FS_NET_PASS" in r1.stdout
        and r2.returncode == 0
        and b"git version" in r2.stdout
    )
    return SelfTestReport(
        passed=passed,
        write_inside_ok=write_inside_ok,
        write_outside_denied=write_outside_denied,
        network_denied=network_denied,
        apple_shim_ok=apple_shim_ok,
        mechanism=isolation.name,
        detail={
            "probe1_returncode": str(r1.returncode),
            "probe1_stdout_tail": r1.stdout.decode("utf-8", "replace")[-200:],
            "probe2_returncode": str(r2.returncode),
            "probe2_stdout": r2.stdout.decode("utf-8", "replace").strip(),
        },
    )


def _selftest_main_paths(wt: Path, tmp: Path) -> tuple[Path | None, Path | None]:
    """Pin the ``(MAIN_GIT, MAIN_KB)`` deny-paths for the self-test, git-topology-independent.

    The ADR-0013 self-test contract (and a faithful ``agora doctor``) passes a PLAIN throwaway
    directory as the worktree — NOT a linked git worktree. If we let the adapter derive
    ``MAIN_GIT`` / ``MAIN_KB`` via ``git rev-parse --git-common-dir`` (the
    :func:`~agora_kb.curator.isolation.seatbelt._resolve_main_paths` default), that ``rev-parse``
    exits 128 on a non-git dir and the adapter raises ``RuntimeError`` — defeating the very routine
    meant to PROVE the sandbox. So when ``wt`` is not a linked worktree we PIN explicit sentinel
    deny-paths the probes never touch (two fresh subdirs of the throwaway ``tmp``, which is itself
    outside the worktree and auto-cleaned), keeping the deny-subpath machinery exercised without any
    git shell-out. When ``wt`` IS a real linked worktree (the production-fidelity path the suite
    exercises via ``git worktree add``) we return ``(None, None)`` so the adapter derives the true
    ``main/.git`` / ``main/_kb`` and the deny covers the real targets.
    """
    if _is_linked_worktree(wt):
        return (None, None)  # let the adapter derive the real main .git / _kb (true deny targets)
    sentinel_git = tmp / "_selftest_main_git"
    sentinel_kb = tmp / "_selftest_main_kb"
    sentinel_git.mkdir(exist_ok=True)
    sentinel_kb.mkdir(exist_ok=True)
    return (sentinel_git.resolve(strict=True), sentinel_kb.resolve(strict=True))


def _is_linked_worktree(wt: Path) -> bool:
    """True iff ``wt`` is a git worktree (its ``.git`` resolves), so real deny-paths can be derived.

    Probes WITHOUT a shell and WITHOUT running hooks; any non-zero / error means "not a worktree"
    and the self-test falls back to pinned sentinels (see :func:`_selftest_main_paths`).
    """
    try:
        cp = subprocess.run(  # noqa: S603 — fixed argv, no shell, hooks neutralized.
            ["git", "-c", f"core.hooksPath={os.devnull}", "rev-parse", "--git-common-dir"],
            cwd=str(wt),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return cp.returncode == 0


def ollama_reachable(
    host: str = _OLLAMA_HOST, port: int = _OLLAMA_PORT, timeout: float = 1.0
) -> bool:
    """True iff a TCP connect to ``host:port`` succeeds from OUTSIDE the sandbox (host-level probe).

    The self-test's network leg is only meaningful against a REACHABLE target (ADR-0013): if nothing
    is listening, a refused connection is indistinguishable from a sandbox block, so the suite skips
    the assertion. Tests call this first to skip/xfail cleanly when Ollama is down (keeps the suite
    non-flaky). This connects OUTSIDE any sandbox, so it reports plain host reachability.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def selftest_cache_key(mechanism: str) -> str:
    """Return the cache-key string keyed by ``{mechanism, OS build, sandbox-exec/bwrap mtime}``.

    The orchestrator persists this (e.g. in ``_kb/state.json``) and re-runs the self-test only when
    the key changes (ADR-0013 §"Runtime self-test" — resolves per-startup vs cache). This module
    BUILDS the key but never writes any repo file — persistence is the caller's job, keeping this
    module free of repo-layout coupling. The key blends the mechanism, ``platform.mac_ver`` +
    ``platform.uname`` (the OS build), and the mtime of the mechanism's pinned binary so an OS
    update or a swapped binary invalidates the cached verdict.
    """
    mac_ver = platform.mac_ver()[0]  # "" off macOS
    uname = platform.uname()
    os_build = f"{uname.system}/{uname.release}/{uname.machine}/{mac_ver}"
    binary_mtime = _binary_mtime(mechanism)
    return f"{mechanism}|{os_build}|{binary_mtime}"


def _binary_mtime(mechanism: str) -> str:
    """The mtime of the mechanism's confining binary (sandbox-exec / bwrap), or ``"-"`` if none."""
    binary = {
        "seatbelt": "/usr/bin/sandbox-exec",
        "bwrap": _which_bwrap(),
        "restricted": None,
    }.get(mechanism)
    if not binary:
        return "-"
    try:
        return str(os.stat(binary).st_mtime_ns)
    except OSError:
        return "-"


def _which_bwrap() -> str | None:
    import shutil

    return shutil.which("bwrap")


def _scrub_environ() -> dict[str, str]:
    """Return the current process environment with credentials scrubbed (ADR-0013 env-scrub list).

    Imported lazily from the package ``__init__`` to reuse the single canonical scrub implementation
    without a circular import at module load (``__init__`` imports the adapters, which import
    nothing back from it at top level). Every mechanism scrubs — including the probes here.
    """
    from . import scrub_env

    return scrub_env(os.environ)
