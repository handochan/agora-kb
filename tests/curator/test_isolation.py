"""Tests for the curator OS-sandbox isolation package (ADR-0013 §"Required unit tests").

The seatbelt-requiring tests are skipped off macOS (``sys.platform != "darwin"``) so CI on Linux
still passes; the platform-agnostic tests (non-nested assertion, env scrub, fail-closed selection)
run everywhere. Real throwaway worktrees are built with ``git init`` + ``git worktree add
--detach`` under ``tmp_path`` so the linked-worktree topology (``.git`` is a pointer FILE; the real
``.git`` / ``_kb`` live in the main repo) is exercised exactly as in production. ``tmp_dir`` is
ALWAYS a distinct dir, never the worktree (ADR-0013).

The network probe targets the host Ollama daemon (``127.0.0.1:11434``): a LISTENING target is what
makes an EPERM provably mean "blocked", not "unreachable". When nothing is listening the network
assertion is skipped so the suite is not flaky.
"""

from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agora_kb.curator.isolation import (
    SandboxUnavailable,
    assert_non_nested_worktree,
    build_sandbox_spec,
    scrub_env,
    select_backend_isolation,
)
from agora_kb.curator.isolation.base import SandboxSpec
from agora_kb.curator.isolation.restricted import RestrictedIsolation
from agora_kb.curator.isolation.selftest import ollama_reachable

# Skip marker for tests that actually invoke /usr/bin/sandbox-exec (macOS only).
_macos_only = pytest.mark.skipif(
    sys.platform != "darwin", reason="seatbelt (sandbox-exec) is macOS-only"
)


# --- helpers -----------------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    """Run a hermetic git command (no shell, hooks neutralized) and return stdout."""
    cp = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return cp.stdout


def _make_main_repo(root: Path) -> Path:
    """Create a tiny committed main repo at ``root/main`` and return its path."""
    main = root / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "t")
    (main / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    return main


def _add_worktree(main: Path, wt: Path) -> Path:
    """Add a detached linked worktree at ``wt`` off ``main`` HEAD; return its realpath."""
    head = _git(main, "rev-parse", "HEAD").strip()
    _git(main, "worktree", "add", "--detach", str(wt), head)
    return wt.resolve(strict=True)


def _seatbelt():
    from agora_kb.curator.isolation.seatbelt import SeatbeltIsolation

    return SeatbeltIsolation()


def _run_probe(wt: Path, tmp: Path, code: str, *, timeout_s: int = 60) -> tuple[int, bytes, bytes]:
    """Run ``python -c code`` under the seatbelt sandbox in ``wt`` with scratch ``tmp``."""
    spec = build_sandbox_spec(
        argv=[sys.executable, "-c", code],
        worktree=wt,
        tmp_dir=tmp,
        read_roots=[Path(sys.prefix), Path(sys.base_prefix)],
        stdin_data=None,
        env=dict(os.environ),
        timeout_s=timeout_s,
    )
    r = _seatbelt().run(spec)
    return r.returncode, r.stdout, r.stderr


# --- platform-agnostic: assert_non_nested_worktree (runs everywhere) ----------------------------


def test_non_nested_assertion_raises_on_nested_path(tmp_path: Path) -> None:
    """A worktree NESTED inside the main repo is rejected (it would include main/.git, G4/G5)."""
    main = tmp_path / "repo"
    main.mkdir()
    nested = main / "sub" / "worktree"
    nested.mkdir(parents=True)
    with pytest.raises(ValueError, match="non-nested"):
        assert_non_nested_worktree(nested, main)


def test_non_nested_assertion_rejects_main_inside_worktree(tmp_path: Path) -> None:
    """The reverse nesting (main inside the worktree) is rejected — neither may contain other."""
    wt = tmp_path / "wt"
    inner_main = wt / "main"
    inner_main.mkdir(parents=True)
    with pytest.raises(ValueError):
        assert_non_nested_worktree(wt, inner_main)


def test_non_nested_assertion_passes_on_sibling(tmp_path: Path) -> None:
    """A sibling/external worktree passes (the normal repo.worktree() placement, outside repo)."""
    main = tmp_path / "repo"
    wt = tmp_path / "agora-wt-xyz" / "tree"
    main.mkdir()
    wt.mkdir(parents=True)
    assert_non_nested_worktree(wt, main)  # does not raise


# --- platform-agnostic: scrub_env (runs everywhere) ---------------------------------------------


def test_scrub_env_strips_named_credentials() -> None:
    """Every name in the ADR-0013 explicit list is removed."""
    env = {
        "ANTHROPIC_API_KEY": "x",
        "OPENAI_API_KEY": "x",
        "GEMINI_API_KEY": "x",
        "GITHUB_TOKEN": "x",
        "GH_TOKEN": "x",
        "AWS_ACCESS_KEY_ID": "x",
        "AWS_SECRET_ACCESS_KEY": "x",
        "AWS_SESSION_TOKEN": "x",
        "GOOGLE_APPLICATION_CREDENTIALS": "x",
        "GIT_ASKPASS": "x",
        "SSH_AUTH_SOCK": "x",
    }
    scrubbed = scrub_env(env)
    assert scrubbed == {}


def test_scrub_env_strips_regex_matches_case_insensitive() -> None:
    """Anything matching (?i)(token|secret|key|password|cred) in its NAME is removed."""
    env = {
        "MY_SECRET": "x",
        "db_password": "x",
        "Some_Token": "x",
        "API_KEY_FOO": "x",
        "FOO_CRED": "x",
        "CREDENTIALS_FILE": "x",
        "lowercase_secret_value": "x",
    }
    assert scrub_env(env) == {}


def test_scrub_env_keeps_innocuous_vars() -> None:
    """Innocuous variables survive — the match is on the NAME, never the value."""
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "LANG": "en_US.UTF-8",
        "EDITOR": "vim",
        "TERM": "xterm",
        "TZ": "UTC",
        # value LOOKS secret-ish but the NAME does not match → kept (we never inspect values)
        "GREETING": "my-token-is-safe",
    }
    assert scrub_env(env) == env


def test_scrub_env_returns_a_copy() -> None:
    """scrub_env never mutates its input mapping."""
    env = {"GITHUB_TOKEN": "x", "PATH": "/usr/bin"}
    out = scrub_env(env)
    assert "GITHUB_TOKEN" in env  # input untouched
    assert out == {"PATH": "/usr/bin"}


# --- platform-agnostic: select_backend_isolation fail-closed (monkeypatched) --------------------


def test_selection_fails_closed_without_kernel_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """No kernel sandbox + allow_reduced_isolation=False → SandboxUnavailable (fail-closed)."""
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SandboxUnavailable):
        select_backend_isolation(allow_reduced_isolation=False)


def test_selection_returns_restricted_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """No kernel sandbox + allow_reduced_isolation=True → the restricted fallback."""
    monkeypatch.setattr(sys, "platform", "win32")
    iso = select_backend_isolation(allow_reduced_isolation=True)
    assert iso.name == "restricted"


def test_selection_linux_without_bwrap_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux with bwrap unusable + not opted in → fail closed (strict not satisfied by fallback)."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "agora_kb.curator.isolation.bwrap.BwrapIsolation.available", lambda self: False
    )
    with pytest.raises(SandboxUnavailable):
        select_backend_isolation(allow_reduced_isolation=False)


def test_userns_probe_binds_the_same_system_dirs_as_the_real_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe must ro-bind what the real sandbox binds, or it is a false negative.

    ``/usr/bin/true`` is dynamically linked: without ``/lib64`` (or ``/lib``) its ELF interpreter
    is missing inside the sandbox and ``execvp`` fails with a misleading ENOENT even though the
    namespaces were created. That made ``available()`` False — and the curator fail-closed with
    "no usable kernel sandbox" — on ordinary x86-64 Linux hosts where bwrap works fine.
    """
    from agora_kb.curator.isolation import bwrap as bwrap_mod

    captured: list[list[str]] = []

    def _fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(bwrap_mod.shutil, "which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr(bwrap_mod.subprocess, "run", _fake_run)
    assert bwrap_mod.BwrapIsolation().available() is True

    argv = captured[0]
    bound = {argv[i + 1] for i, tok in enumerate(argv) if tok == "--ro-bind"}
    expected = {d for d in bwrap_mod._SYSTEM_RO_BIND_DIRS if Path(d).exists()}
    assert bound == expected, "probe binds must match the real invocation's system dirs"


def test_selection_linux_with_bwrap_returns_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux with a usable bwrap → the bwrap adapter (mechanism is OS-driven)."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "agora_kb.curator.isolation.bwrap.BwrapIsolation.available", lambda self: True
    )
    iso = select_backend_isolation(allow_reduced_isolation=False)
    assert iso.name == "bwrap"


def test_restricted_marks_reduced_isolation_and_runs(tmp_path: Path) -> None:
    """The restricted fallback runs the argv and flags reduced_isolation (worker forces review)."""
    wt = tmp_path / "wt"
    tmp = tmp_path / "scratch"
    wt.mkdir()
    tmp.mkdir()
    spec = build_sandbox_spec(
        argv=[sys.executable, "-c", "print('hi')"],
        worktree=wt,
        tmp_dir=tmp,
        read_roots=[],
        stdin_data=None,
        env=dict(os.environ),
    )
    result = RestrictedIsolation().run(spec)
    assert result.returncode == 0
    assert result.reduced_isolation is True
    assert result.mechanism == "restricted"
    assert b"hi" in result.stdout


def test_restricted_warning_enumerates_both_guarantees() -> None:
    """The restricted WARNING text names BOTH lost guarantees (network AND out-of-worktree)."""
    from agora_kb.curator.isolation.restricted import WARNING

    low = WARNING.lower()
    assert "network" in low
    assert "out-of-worktree" in low or "out of worktree" in low
    assert "review-mode" in low or "review mode" in low


# --- macOS seatbelt: the realpath grant + write confinement -------------------------------------


@_macos_only
def test_write_inside_succeeds_under_tmp_symlink(tmp_path: Path) -> None:
    """A worktree under a /tmp-symlinked path: a write INSIDE it SUCCEEDS (the realpath bug guard).

    build_sandbox_spec resolves the path (/tmp → /private/tmp) so Seatbelt's subpath grant matches
    the resolved vnode; without that the write would silently EPERM.
    """
    main = _make_main_repo(tmp_path)
    wt = _add_worktree(main, tmp_path / "wt")
    tmp = tmp_path / "scratch"
    tmp.mkdir()
    code = "open('inside.txt','w').write('ok'); print('WROTE_INSIDE')"
    rc, out, err = _run_probe(wt, tmp, code)
    assert rc == 0, err.decode("utf-8", "replace")
    assert b"WROTE_INSIDE" in out
    assert (wt / "inside.txt").read_text() == "ok"


@_macos_only
def test_cannot_write_worktree_git_pointer(tmp_path: Path) -> None:
    """The backend CANNOT write {WORKTREE}/.git (its own pointer file) → EPERM (G4)."""
    main = _make_main_repo(tmp_path)
    wt = _add_worktree(main, tmp_path / "wt")
    tmp = tmp_path / "scratch"
    tmp.mkdir()
    code = textwrap.dedent(
        """
        import errno
        try:
            open('.git','w').write('gitdir: /evil')
            print('GIT_POINTER_WRITE_OK')
        except PermissionError:
            print('GIT_POINTER_EPERM')
        except OSError as e:
            print('GIT_POINTER_EPERM' if e.errno == errno.EPERM else f'GIT_POINTER_OTHER:{e.errno}')
        """
    ).strip()
    rc, out, err = _run_probe(wt, tmp, code)
    assert rc == 0, err.decode("utf-8", "replace")
    assert b"GIT_POINTER_EPERM" in out, out
    assert b"GIT_POINTER_WRITE_OK" not in out


@_macos_only
def test_cannot_write_main_git_or_main_kb(tmp_path: Path) -> None:
    """The backend CANNOT write under MAIN_GIT (main/.git/hooks) nor MAIN_KB (main/_kb) → EPERM."""
    main = _make_main_repo(tmp_path)
    wt = _add_worktree(main, tmp_path / "wt")
    tmp = tmp_path / "scratch"
    tmp.mkdir()
    # Pre-create the MAIN _kb dir so the deny is over an existing dir (the deny is by subpath either
    # way).
    (main / "_kb").mkdir()
    main_git = (main / ".git").resolve(strict=True)
    main_kb = (main / "_kb").resolve(strict=True)
    hook = main_git / "hooks" / "pre-commit"
    kb_target = main_kb / "x"
    code = textwrap.dedent(
        f"""
        import errno
        def attempt(path):
            try:
                open(path, 'w').write('x'); return 'WROTE'
            except PermissionError:
                return 'EPERM'
            except OSError as e:
                return 'EPERM' if e.errno == errno.EPERM else f'OTHER:{{e.errno}}'
        print('HOOK', attempt({str(hook)!r}))
        print('KB', attempt({str(kb_target)!r}))
        """
    ).strip()
    rc, out, err = _run_probe(wt, tmp, code)
    assert rc == 0, err.decode("utf-8", "replace")
    assert b"HOOK EPERM" in out, out
    assert b"KB EPERM" in out, out
    assert (main_git / "hooks" / "pre-commit").exists() is False
    assert (main_kb / "x").exists() is False


@_macos_only
def test_write_outside_denied_and_tmp_scratch_isolated(tmp_path: Path) -> None:
    """Write-outside → EPERM; tmp_dir scratch is writable; tmp writes do NOT land in worktree."""
    main = _make_main_repo(tmp_path)
    wt = _add_worktree(main, tmp_path / "wt")
    tmp = tmp_path / "scratch"
    tmp.mkdir()
    outside = Path("~/agora-selftest-probe").expanduser()
    # HOME/TMPDIR point at tmp_dir, so writing under $TMPDIR is the scratch-write; we resolve the
    # absolute scratch path here so the probe targets it directly.
    scratch_file = tmp / "scratch_only.txt"
    code = textwrap.dedent(
        f"""
        import errno, os
        # (a) outside both mounts -> must EPERM
        try:
            open({str(outside)!r}, 'w').write('x'); print('OUTSIDE_WRITE_OK')
        except PermissionError:
            print('OUTSIDE_EPERM')
        except OSError as e:
            print('OUTSIDE_EPERM' if e.errno == errno.EPERM else f'OUTSIDE_OTHER:{{e.errno}}')
        # (b) tmp scratch is writable
        try:
            open({str(scratch_file)!r}, 'w').write('scratch'); print('SCRATCH_OK')
        except OSError as e:
            print(f'SCRATCH_FAIL:{{e.errno}}')
        """
    ).strip()
    rc, out, err = _run_probe(wt, tmp, code)
    assert rc == 0, err.decode("utf-8", "replace")
    assert b"OUTSIDE_EPERM" in out, out
    assert b"OUTSIDE_WRITE_OK" not in out
    assert b"SCRATCH_OK" in out, out
    # The scratch write landed in tmp_dir, NOT in the worktree (it never appears in the worktree
    # diff).
    assert scratch_file.read_text() == "scratch"
    assert not (wt / "scratch_only.txt").exists()
    assert outside.exists() is False


@_macos_only
def test_outbound_tcp_to_ollama_is_eperm(tmp_path: Path) -> None:
    """Outbound TCP to a REACHABLE 127.0.0.1:11434 → EPERM (skip cleanly if Ollama is down)."""
    if not ollama_reachable():
        pytest.skip("Ollama not listening on 127.0.0.1:11434 — cannot prove EPERM vs unreachable")
    main = _make_main_repo(tmp_path)
    wt = _add_worktree(main, tmp_path / "wt")
    tmp = tmp_path / "scratch"
    tmp.mkdir()
    code = textwrap.dedent(
        """
        import errno, socket
        try:
            socket.create_connection(("127.0.0.1", 11434), timeout=2)
            print("NET_OK")
        except PermissionError:
            print("NET_EPERM")
        except OSError as e:
            print("NET_EPERM" if e.errno == errno.EPERM else f"NET_AMBIGUOUS:{e.errno}")
        """
    ).strip()
    rc, out, err = _run_probe(wt, tmp, code)
    assert rc == 0, err.decode("utf-8", "replace")
    assert b"NET_EPERM" in out, out
    assert b"NET_OK" not in out
    assert b"NET_AMBIGUOUS" not in out


@_macos_only
def test_apple_shimmed_git_runs_under_sandbox(tmp_path: Path) -> None:
    """Apple-shimmed /usr/bin/git --version runs under the sandbox (rc 0) — the /dev/null allow."""
    main = _make_main_repo(tmp_path)
    wt = _add_worktree(main, tmp_path / "wt")
    tmp = tmp_path / "scratch"
    tmp.mkdir()
    spec = build_sandbox_spec(
        argv=["/usr/bin/git", "--version"],
        worktree=wt,
        tmp_dir=tmp,
        read_roots=[],
        stdin_data=None,
        env=dict(os.environ),
        timeout_s=60,
    )
    result = _seatbelt().run(spec)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert b"git version" in result.stdout


@_macos_only
def test_self_test_passes_on_this_host(tmp_path: Path) -> None:
    """The full hardened self-test passes end-to-end on this macOS host (skip net if no Ollama)."""
    if not ollama_reachable():
        pytest.skip("Ollama not listening — the network leg cannot be proven")
    main = _make_main_repo(tmp_path)
    wt = _add_worktree(main, tmp_path / "wt")
    tmp = tmp_path / "scratch"
    tmp.mkdir()
    report = _seatbelt().self_test(wt, tmp, [Path(sys.prefix), Path(sys.base_prefix)])
    assert report.write_inside_ok, report.detail
    assert report.write_outside_denied, report.detail
    assert report.network_denied, report.detail
    assert report.apple_shim_ok, report.detail
    assert report.passed, report.detail
    assert report.mechanism == "seatbelt"


@_macos_only
def test_timeout_kills_process_group(tmp_path: Path) -> None:
    """A backend exceeding timeout_s is killed (whole group) and TimeoutExpired re-raised."""
    main = _make_main_repo(tmp_path)
    wt = _add_worktree(main, tmp_path / "wt")
    tmp = tmp_path / "scratch"
    tmp.mkdir()
    spec = build_sandbox_spec(
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        worktree=wt,
        tmp_dir=tmp,
        read_roots=[],
        stdin_data=None,
        env=dict(os.environ),
        timeout_s=1,
    )
    with pytest.raises(subprocess.TimeoutExpired):
        _seatbelt().run(spec)


# --- spec building: realpath + tmp-outside-worktree enforcement (platform-agnostic) -------------


def test_build_spec_resolves_paths_and_scrubs_env(tmp_path: Path) -> None:
    """build_sandbox_spec realpath-resolves paths and scrubs the env (the spec invariants)."""
    wt = tmp_path / "wt"
    tmp = tmp_path / "scratch"
    wt.mkdir()
    tmp.mkdir()
    spec = build_sandbox_spec(
        argv=["echo"],
        worktree=wt,
        tmp_dir=tmp,
        read_roots=[tmp_path],
        stdin_data=b"hi",
        env={"GITHUB_TOKEN": "x", "PATH": "/usr/bin"},
    )
    assert isinstance(spec, SandboxSpec)
    assert spec.worktree == wt.resolve(strict=True)
    assert spec.tmp_dir == tmp.resolve(strict=True)
    assert spec.env == {"PATH": "/usr/bin"}  # token scrubbed
    assert spec.stdin_data == b"hi"


def test_build_spec_rejects_tmp_inside_worktree(tmp_path: Path) -> None:
    """tmp_dir inside the worktree is rejected (scratch must be a separate mount, ADR-0013)."""
    wt = tmp_path / "wt"
    inside_tmp = wt / "scratch"
    inside_tmp.mkdir(parents=True)
    with pytest.raises(ValueError, match="OUTSIDE the worktree"):
        build_sandbox_spec(
            argv=["echo"],
            worktree=wt,
            tmp_dir=inside_tmp,
            read_roots=[],
            stdin_data=None,
            env={},
        )


def test_build_spec_rejects_localhost_ollama_network(tmp_path: Path) -> None:
    """network='localhost-ollama' is the documented-but-UNIMPLEMENTED future posture → fail closed.

    ADR-0013 §"Ollama / local-model boundary": localhost-ollama is "rejected for any backend not
    explicitly marked" and is "future work, not Phase-1 default" — no adapter grants the scoped
    127.0.0.1:11434 allow, so a spec carrying it would be silently NOT honored. build_sandbox_spec
    must refuse it rather than build an unenforceable spec.
    """
    wt = tmp_path / "wt"
    tmp = tmp_path / "scratch"
    wt.mkdir()
    tmp.mkdir()
    with pytest.raises(SandboxUnavailable, match="localhost-ollama"):
        build_sandbox_spec(
            argv=["echo"],
            worktree=wt,
            tmp_dir=tmp,
            read_roots=[],
            stdin_data=None,
            env={},
            network="localhost-ollama",  # type: ignore[arg-type]
        )


def test_build_spec_rejects_unknown_network_posture(tmp_path: Path) -> None:
    """Any network value outside the NetworkPosture vocabulary (e.g. 'loopback') is rejected.

    The default adapters.yaml historically emitted ``network: "loopback"``; an unmapped token must
    fail closed at spec-build time, never be coerced or silently sandboxed-with-no-network.
    """
    wt = tmp_path / "wt"
    tmp = tmp_path / "scratch"
    wt.mkdir()
    tmp.mkdir()
    with pytest.raises(SandboxUnavailable, match="unsupported network posture"):
        build_sandbox_spec(
            argv=["echo"],
            worktree=wt,
            tmp_dir=tmp,
            read_roots=[],
            stdin_data=None,
            env={},
            network="loopback",  # type: ignore[arg-type]
        )


def test_build_spec_accepts_none_network(tmp_path: Path) -> None:
    """network='none' (the Phase-1 default; inference happens outside the sandbox) is accepted."""
    wt = tmp_path / "wt"
    tmp = tmp_path / "scratch"
    wt.mkdir()
    tmp.mkdir()
    spec = build_sandbox_spec(
        argv=["echo"],
        worktree=wt,
        tmp_dir=tmp,
        read_roots=[],
        stdin_data=None,
        env={},
        network="none",
    )
    assert spec.network == "none"


def test_self_test_does_not_crash_on_plain_non_git_worktree(tmp_path: Path) -> None:
    """The self-test must NOT raise on a PLAIN (non-git) throwaway worktree — the ADR's documented
    usage (``agora doctor`` / curator startup pass a throwaway dir, not a linked git worktree).

    Regression for the RuntimeError where the seatbelt adapter shelled out to
    ``git rev-parse --git-common-dir`` (rc=128 on a non-git dir) while deriving MAIN_GIT/MAIN_KB.
    The restricted fallback exercises the same shared self_test() path on EVERY platform, so this
    runs everywhere; the verdict must be a real SelfTestReport, never an exception.
    """
    wt = tmp_path / "throwaway"
    tmp = tmp_path / "scratch"
    wt.mkdir()
    tmp.mkdir()
    # The restricted fallback has no confinement, so its self-test write-outside probe actually
    # writes ~/agora-selftest-probe; remove it after so the test leaves no home-dir litter.
    outside_probe = Path("~/agora-selftest-probe").expanduser()
    try:
        # Plain dir — NOT a git worktree. Must return a report, not raise.
        report = RestrictedIsolation().self_test(wt, tmp, [])
    finally:
        outside_probe.unlink(missing_ok=True)
    assert report.mechanism == "restricted"
    # restricted provides no kernel confinement, so the FS/net assertions cannot pass — but the
    # routine itself completed and produced an honest verdict.
    assert report.passed is False


@_macos_only
def test_seatbelt_self_test_on_plain_non_git_worktree(tmp_path: Path) -> None:
    """On macOS the real seatbelt self-test also completes on a PLAIN throwaway dir (no git init).

    Without the fix this raised RuntimeError; now it derives sentinel MAIN_GIT/MAIN_KB deny-paths
    and the FS assertions hold. The network leg only proves EPERM against a REACHABLE target, so we
    gate the full ``passed`` on Ollama being up; the FS/apple-shim legs hold regardless.
    """
    wt = tmp_path / "throwaway"
    tmp = tmp_path / "scratch"
    wt.mkdir()
    tmp.mkdir()
    report = _seatbelt().self_test(wt, tmp, [Path(sys.prefix), Path(sys.base_prefix)])
    assert report.mechanism == "seatbelt"
    assert report.write_inside_ok, report.detail
    assert report.write_outside_denied, report.detail
    assert report.apple_shim_ok, report.detail
    if ollama_reachable():
        assert report.network_denied, report.detail
        assert report.passed, report.detail


def test_eperm_constant_is_importable() -> None:
    """Sanity: errno.EPERM is the value the probes assert against (1 on POSIX)."""
    assert errno.EPERM == 1
    # silence unused-import linters for socket (used by ollama_reachable indirectly in this suite)
    assert socket.AF_INET is not None
