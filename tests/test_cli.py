"""Tests for the ``agora`` CLI (DESIGN/ROADMAP Phase 1).

These exercise the dependency-light argparse front-end over the core API. ``serve`` is never
invoked (it blocks on a stdio loop); we only assert exit codes + captured stdout/stderr substrings.
Commands that touch git (``repo init``, ``doctor`` over a real repo) are skipped if ``git`` is
not on PATH.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agora_kb.cli import main
from agora_kb.core import Inbox, Repo, RepoLayout

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


# --- repo init ----------------------------------------------------------------------------------
@requires_git
def test_repo_init_initializes_and_prints_sha(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "kb"
    rc = main(["repo", "init", str(target)])
    assert rc == 0
    assert Repo.resolve(target).is_initialized()
    out = capsys.readouterr().out.strip()
    # The init commit sha is printed: a full hex object id (40 for sha-1, 64 for sha-256).
    assert len(out) in (40, 64)
    assert all(ch in "0123456789abcdef" for ch in out)


@requires_git
def test_repo_init_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    first = capsys.readouterr().out.strip()
    assert main(["repo", "init", str(target)]) == 0
    second = capsys.readouterr().out.strip()
    assert first == second  # same commit returned on the second call


def test_repo_without_subcommand_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["repo"])
    assert rc == 2
    assert "subcommand" in capsys.readouterr().err


# --- status -------------------------------------------------------------------------------------
def test_status_prints_zero_depth_on_fresh_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["status", "--repo", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "inbox depth: 0" in out
    assert "last_run: never" in out
    assert "ingested=0" in out


def test_status_reflects_a_pending_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = RepoLayout(tmp_path)
    Inbox(layout).write(text="remember this", writer="tester", source="manual")
    rc = main(["status", "--repo", str(tmp_path)])
    assert rc == 0
    assert "inbox depth: 1" in capsys.readouterr().out


# --- curate -------------------------------------------------------------------------------------
def test_curate_on_empty_repo_should_not_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["curate", "--repo", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "should_run: False" in out
    assert "reason: none" in out


def test_curate_force_runs_regardless(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["curate", "--repo", str(tmp_path), "--force"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "should_run: True" in out
    assert "reason: force" in out


# --- doctor -------------------------------------------------------------------------------------
def test_doctor_prints_report_and_returns_health_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["doctor", "--repo", str(tmp_path)])
    out = capsys.readouterr().out
    assert "agora doctor" in out
    assert "python:" in out
    assert "status:" in out
    # pydantic is a hard core dep and importable in the test env, so it must report ok.
    assert "dep pydantic: ok" in out
    # Health is binary: 0 (healthy) or 1 (unhealthy); both are valid outcomes for this report.
    assert rc in (0, 1)


@requires_git
def test_doctor_reports_initialized_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "kb"
    Repo.resolve(target).init()
    rc = main(["doctor", "--repo", str(target)])
    assert rc in (0, 1)
    assert "initialized" in capsys.readouterr().out


# --- serve --------------------------------------------------------------------------------------
def test_serve_invokes_build_server_with_repo_path_and_default_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Monkeypatch build_server with a recording stub returning an object whose .run is a no-op,
    # so we exercise _cmd_serve's call shape without blocking on the stdio loop.
    import agora_kb.faces.mcp_server as mcp_server

    captured: dict[str, object] = {}

    class _StubServer:
        def run(self) -> None:  # no-op: never blocks
            captured["ran"] = True

    def _fake_build_server(
        *, repo_path: Path, writer: str = mcp_server.DEFAULT_WRITER
    ) -> _StubServer:
        captured["repo_path"] = repo_path
        captured["writer"] = writer
        return _StubServer()

    monkeypatch.setattr(mcp_server, "build_server", _fake_build_server)

    rc = main(["serve", "--repo", str(tmp_path)])
    assert rc == 0
    # repo_path is forwarded as a Path of the expected value.
    assert isinstance(captured["repo_path"], Path)
    assert captured["repo_path"] == Path(str(tmp_path))
    # --writer omitted: build_server's own default ('local') applies, never None.
    assert captured["writer"] == mcp_server.DEFAULT_WRITER
    assert captured["ran"] is True


def test_serve_forwards_explicit_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agora_kb.faces.mcp_server as mcp_server

    captured: dict[str, object] = {}

    class _StubServer:
        def run(self) -> None:
            captured["ran"] = True

    def _fake_build_server(
        *, repo_path: Path, writer: str = mcp_server.DEFAULT_WRITER
    ) -> _StubServer:
        captured["repo_path"] = repo_path
        captured["writer"] = writer
        return _StubServer()

    monkeypatch.setattr(mcp_server, "build_server", _fake_build_server)

    rc = main(["serve", "--repo", str(tmp_path), "--writer", "agent-7"])
    assert rc == 0
    assert isinstance(captured["repo_path"], Path)
    assert captured["writer"] == "agent-7"
    assert captured["ran"] is True


# --- no / unknown command -----------------------------------------------------------------------
def test_no_command_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_unknown_command_exits_2() -> None:
    # argparse rejects an unknown subcommand by raising SystemExit(2) during parsing.
    with pytest.raises(SystemExit) as excinfo:
        main(["definitely-not-a-command"])
    assert excinfo.value.code == 2
