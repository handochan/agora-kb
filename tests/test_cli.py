"""Tests for the ``agora`` CLI (DESIGN/ROADMAP Phase 1).

These exercise the dependency-light argparse front-end over the core API. ``serve`` is never
invoked (it blocks on a stdio loop); we only assert exit codes + captured stdout/stderr substrings.
Commands that touch git (``repo init``, ``doctor`` over a real repo) are skipped if ``git`` is
not on PATH.
"""

from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agora_kb.cli import main
from agora_kb.core import Inbox, Repo, RepoLayout
from agora_kb.core.wiki import Wiki
from agora_kb.schema import lint

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


# A model-free stub curator brain shelled by SubprocessBackend (the Phase-1/2 seam): PASS 1 (cwd =
# bundle dir, candidates.json present) emits a single CREATE_THEME plan to stdout; PASS 2 (cwd =
# worktree) fills every agora:body sentinel region with canned prose. No real model in the loop.
_STUB_BRAIN = """\
import json, re, sys
from pathlib import Path

cwd = Path.cwd()
candidates = cwd / "candidates.json"
START = re.compile(r"<!-- agora:body:start id=(.+?) -->")

if candidates.is_file():
    doc = json.loads(candidates.read_text())
    cands = doc["candidates"]
    c0 = cands[0]
    dispositions = [{
        "candidate_id": c0["candidate_id"],
        "event_ids": [p["event_id"] for p in c0["provenance"]],
        "op": "CREATE_THEME",
        "domain": c0.get("domain") or "ai-tech",
        "basename": "curator-concurrency",
        "title": "Curator concurrency model",
        "summary": "One curator advances the curated branch under a per-repo lock.",
        "status": "active",
        "tags": ["curator", "concurrency"],
        "aliases": [],
        "links": [],
        "needs_prose": True,
        "reason": "New concept.",
    }]
    for c in cands[1:]:
        dispositions.append({
            "candidate_id": c["candidate_id"],
            "event_ids": [p["event_id"] for p in c["provenance"]],
            "op": "DROP", "target_basename": None, "needs_prose": False,
            "reason": "Redundant for this run.",
        })
    print(json.dumps({"schema_version": 1, "run_id": doc["run_id"], "finished": True,
                      "dispositions": dispositions}))
    sys.exit(0)

for note in (cwd / "wiki").rglob("*.md"):
    text = note.read_text()
    out, in_region = [], False
    for line in text.split("\\n"):
        if START.search(line):
            out.append(line)
            out.append("The single curator holds a per-repo flock.")
            in_region = True
            continue
        if "agora:body:end" in line:
            in_region = False
            out.append(line)
            continue
        if in_region:
            continue
        out.append(line)
    note.write_text("\\n".join(out))
sys.exit(0)
"""


def _write_stub_adapters(repo_root: Path) -> Path:
    """Write a stub brain script + an adapters.yaml pointing SubprocessBackend at it."""
    brain = repo_root / "stub_brain.py"
    brain.write_text(_STUB_BRAIN, encoding="utf-8")
    adapters = repo_root / "adapters.yaml"
    adapters.write_text(
        "backends:\n"
        f"  stub: {{ argv: [{sys.executable!r}, {str(brain)!r}], "
        'cwd: "{worktree}", prompt: stdin }\n'
        "default_backend: stub\n",
        encoding="utf-8",
    )
    return repo_root


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


@requires_git
def test_repo_init_emits_schema_and_repo_config_and_lints_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`repo init` yields a git repo with an emitted schema, a `_kb/repo.yaml`, and a CLEAN lint."""
    target = tmp_path / "kb"
    argv = ["repo", "init", str(target), "--name", "personal"]
    argv += ["--domain", "ai-tech", "--tag", "curator"]
    rc = main(argv)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert len(out) in (40, 64)  # the admin commit sha

    layout = RepoLayout(target)
    # The schema doc, the fixed taxonomy, and the per-repo config were all emitted.
    assert layout.schema_file.is_file()  # AGENTS.md
    assert (target / "_meta" / "taxonomy.yaml").is_file()
    assert (layout.kb_dir / "repo.yaml").is_file()
    # The freshly-initialized repo lints CLEAN (the schema.lint ok contract for `repo init`).
    assert lint(layout).ok


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
    # Not due => no backend is even loaded; nothing was changed.
    assert "nothing was changed" in out


@requires_git
def test_curate_force_without_backend_reports_no_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A forced curate over a repo with NO adapters.yaml is a clear error (rc=1), not a crash."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    capsys.readouterr()
    # `repo init` now emits a default adapters.yaml (the OSS brain wiring); remove it to exercise
    # the explicit "no backend configured" path this test guards.
    (target / "adapters.yaml").unlink()

    rc = main(["curate", "--repo", str(target), "--force"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no backend configured" in err


@requires_git
def test_curate_with_stub_backend_publishes_and_query_reflects_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`curate --force` with a STUB backend PUBLISHES a theme; query then reflects it.

    End-to-end Phase-2 wiring: `repo init` -> a `kb_remember`-shaped capture -> an adapters.yaml
    stub brain -> `agora curate --force` runs curator.worker, which CREATE_THEMEs + authors prose +
    commits + CAS. After publish the owner working copy is fast-forwarded (ADR-0008
    read-after-publish), so the on-disk theme exists and `core.Wiki.query` resolves it.
    """
    target = tmp_path / "kb"
    assert (
        main(
            [
                "repo",
                "init",
                str(target),
                "--name",
                "personal",
                "--domain",
                "ai-tech",
                "--tag",
                "curator",
                "--tag",
                "concurrency",
            ]
        )
        == 0
    )
    capsys.readouterr()
    _write_stub_adapters(target)

    layout = RepoLayout(target)
    # Point the per-repo config's default brain at the stub (init wrote the OSS default 'qwen').
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("backend: qwen", "backend: stub"),
        encoding="utf-8",
    )
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )

    rc = main(["curate", "--repo", str(target), "--force"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "status: published" in out
    assert "CREATE_THEME=1" in out

    # The published theme is on disk (read-after-publish sync) and the deterministic query finds it.
    theme = layout.wiki_dir / "ai-tech" / "themes" / "curator-concurrency.md"
    assert theme.is_file()
    assert "The single curator holds a per-repo flock." in theme.read_text(encoding="utf-8")
    result = Wiki(layout).query("curator concurrency")
    assert result.status == "ok"
    assert any(h.path == "wiki/ai-tech/themes/curator-concurrency.md" for h in result.hits)
    # The inbox is drained.
    assert Inbox(layout).depth() == 0


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


# --- watch (scheduler loop) ---------------------------------------------------------------------
def _init_stub_repo(target: Path) -> RepoLayout:
    """Init a repo with the stub brain wired (domain/tags matching the stub plan) → its layout."""
    assert (
        main(
            [
                "repo",
                "init",
                str(target),
                "--domain",
                "ai-tech",
                "--tag",
                "curator",
                "--tag",
                "concurrency",
            ]
        )
        == 0
    )
    _write_stub_adapters(target)
    layout = RepoLayout(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("backend: qwen", "backend: stub"),
        encoding="utf-8",
    )
    return layout


@requires_git
def test_watch_once_is_idle_on_empty_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`watch --once` on an empty repo evaluates the triggers, finds nothing due, and exits 0."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "idle: depth=0" in out
    assert "reason=none" in out


@requires_git
def test_watch_once_runs_when_threshold_met(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A backlog at/above ``threshold`` makes `watch --once` consolidate (reason=threshold)."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    capsys.readouterr()
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "ran (threshold)" in out
    assert "status=published" in out


@requires_git
def test_watch_once_runs_on_cron_due(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A cron matching every minute fires `watch --once` (reason=cron) with a backlog present."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    # cron "* * * * *" is due every minute; threshold stays 10 so ONLY the cron signal can fire.
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("cron: 0 3 * * *", "cron: '* * * * *'"),
        encoding="utf-8",
    )
    capsys.readouterr()
    # Current-timestamp event so the 30-min idle trigger cannot pre-empt the cron reason.
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime.now(UTC),
    )

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "ran (cron)" in out
    assert "status=published" in out


# --- import (the opt-in Obsidian/markdown vault normalizer, ADR-0014 D5) ------------------------
@requires_git
def test_import_happy_path_creates_dest_and_prints_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agora import` on a tiny vault exits 0, git-inits dest, and prints the imported count."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    note = src / "wiki" / "general" / "themes" / "topic.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Topic\n\nA body paragraph about the topic.\n", encoding="utf-8")

    rc = main(["import", str(src), str(dest)])

    assert rc == 0  # a best-effort import is a success even with lint findings
    out = capsys.readouterr().out
    assert "imported 1 note(s)" in out
    # dest is a real, git-inited Agora repo with the schema emitted.
    assert (dest / ".git").exists()
    assert (dest / "AGENTS.md").is_file()


@requires_git
def test_import_defaults_to_general_domain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With --domain omitted, an off-layout note lands under wiki/general/themes/ (the default)."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    note = src / "Loose Idea.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Loose Idea\n\nA stray note.\n", encoding="utf-8")

    rc = main(["import", str(src), str(dest)])

    assert rc == 0
    capsys.readouterr()
    assert (dest / "wiki" / "general" / "themes" / "loose-idea.md").is_file()


@requires_git
def test_import_missing_src_exits_1_with_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing source vault is a HARD error: exit 1 + an error on stderr (ADR-0014 D5)."""
    dest = tmp_path / "out"

    rc = main(["import", str(tmp_path / "nope"), str(dest)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "import:" in err


@requires_git
def test_import_with_warnings_still_exits_0_and_prints_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A vault with findings (off-layout move + stripped tag) exits 0 and prints the warnings."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    note = src / "Loose Idea.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    # Off-layout (forces a move warning) + a tag outside the declared taxonomy (forces a strip).
    note.write_text(
        "---\ntitle: Loose\ntags: [unknown-tag]\n---\n\n# Loose Idea\n\nA stray note.\n",
        encoding="utf-8",
    )

    rc = main(["import", str(src), str(dest), "--tag", "architecture"])

    assert rc == 0  # warnings are NOT a failure
    out = capsys.readouterr().out
    assert "moved to fit" in out
    assert "stripped tags: unknown-tag" in out


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
