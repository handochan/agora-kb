"""Tests for the ``agora`` CLI (DESIGN/ROADMAP Phase 1).

These exercise the dependency-light argparse front-end over the core API. ``serve`` is never
invoked (it blocks on a stdio loop); we only assert exit codes + captured stdout/stderr substrings.
Commands that touch git (``repo init``, ``doctor`` over a real repo) are skipped if ``git`` is
not on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

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
    # read_roots is NOT optional for a sandboxed backend on Linux. The seatbelt profile (macOS)
    # ships a broad `(allow file-read*)`, so omitting these still works there — but bwrap is
    # deny-by-OMISSION: anything unbound simply does not exist inside the sandbox, so the stub's
    # interpreter is unreachable and PASS-2 degrades to a `_summary pending_` placeholder. This
    # is exactly what a Linux operator must configure (see issue #114).
    adapters.write_text(
        "backends:\n"
        f"  stub: {{ argv: [{sys.executable!r}, {str(brain)!r}], "
        'cwd: "{worktree}", prompt: stdin, '
        'read_roots: ["{venv}", "{interpreter}"] }\n'
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
def test_doctor_prints_the_routing_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR-0015: `agora doctor` reports which brain runs each act + its network posture. A fresh
    repo emits one `qwen` brain with no routing, so both acts resolve to it."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    capsys.readouterr()

    main(["doctor", "--repo", str(target)])
    out = capsys.readouterr().out
    assert "routing:" in out
    assert "plan=qwen" in out
    assert "author=qwen" in out


@requires_git
def test_curate_unknown_backend_override_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agora curate --backend NAME` with an undefined NAME exits non-zero with a clear message
    (the override escape hatch is fail-loud), not a traceback."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    capsys.readouterr()

    rc = main(["curate", "--repo", str(target), "--force", "--backend", "nonesuch"])
    assert rc == 1
    assert "unknown backend" in capsys.readouterr().err


@requires_git
def test_curate_invalid_routing_block_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed ADR-0015 `routing:` block (an undefined target) makes `agora curate` exit
    non-zero with a clean 'invalid adapters.yaml' message, not an uncaught ValueError."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    capsys.readouterr()
    adapters = target / "adapters.yaml"
    adapters.write_text(
        adapters.read_text(encoding="utf-8") + "routing:\n  plan: ghost\n", encoding="utf-8"
    )

    rc = main(["curate", "--repo", str(target), "--force"])
    assert rc == 1
    assert "invalid adapters.yaml" in capsys.readouterr().err


@requires_git
def test_doctor_tolerates_a_malformed_adapters_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agora doctor` never crashes on a malformed adapters.yaml — it notes it on the routing line
    and still reports a status."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    capsys.readouterr()
    (target / "adapters.yaml").write_text('a: "unterminated', encoding="utf-8")

    main(["doctor", "--repo", str(target)])
    out = capsys.readouterr().out
    assert "routing:" in out
    assert "unreadable" in out
    assert "status:" in out


@requires_git
@pytest.mark.xfail(
    sys.platform == "linux",
    reason=(
        "issue #115: under bwrap, PASS-2 produces no prose and the note is left as the "
        "`_summary pending_` placeholder while the run still reports status: published. "
        "PASS-1 succeeds with the SAME argv/interpreter, so this is not read-root visibility. "
        "strict=True on purpose — when #115 is fixed this test must start failing as XPASS "
        "so the marker gets removed rather than silently masking a regression."
    ),
    strict=True,
)
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
def test_watch_threads_curator_thresholds_into_worker_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `agora watch` tick reads curator.limits.related_k / curator.lint.max_orphans from
    repo.yaml and forwards them into worker.run — locks the face glue, not just the worker seam."""
    import agora_kb.cli as cli_mod

    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    doc = yaml.safe_load(repo_yaml.read_text(encoding="utf-8"))
    doc["curator"]["triggers"]["threshold"] = 1  # one backlog item makes the tick consolidate
    doc["curator"]["limits"] = {"related_k": 5}
    doc["curator"]["lint"] = {"max_orphans": 7}
    repo_yaml.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )

    seen: dict[str, object] = {}
    orig_run = cli_mod.run

    def run_spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return orig_run(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "run", run_spy)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0
    assert seen.get("related_k") == 5  # the watch face forwarded repo.yaml's related_k
    assert seen.get("max_orphans") == 7  # ...and max_orphans


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


# --- harvest (ADR-0007/0017) --------------------------------------------------------------------
def _setup_harvest_repo(
    tmp_path: Path,
    *,
    kind: str = "personal",
    scope: str = "personal",
    enabled: bool = True,
    with_connector: bool = True,
) -> tuple[Path, Path]:
    """Init a repo, enable harvest in repo.yaml, and (optionally) wire a file: connector."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--kind", kind, "--domain", "general"]) == 0
    mem = tmp_path / "mem" / "MEMORY.md"
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text("# m\n\n- harvested fact one\n- harvested fact two\n", encoding="utf-8")

    rp = target / "_kb" / "repo.yaml"
    doc = yaml.safe_load(rp.read_text(encoding="utf-8"))
    doc.setdefault("harvest", {})["enabled"] = enabled
    rp.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    if with_connector:
        ap = target / "adapters.yaml"
        a = yaml.safe_load(ap.read_text(encoding="utf-8"))
        a["connectors"] = {"file:demo": {"path": str(mem), "scope": scope}}
        ap.write_text(yaml.safe_dump(a, sort_keys=False), encoding="utf-8")
    return target, mem


@requires_git
def test_harvest_disabled_is_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, _ = _setup_harvest_repo(tmp_path, enabled=False)
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target)])
    assert rc == 0
    assert "disabled" in capsys.readouterr().out


@requires_git
def test_harvest_writes_gated_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _ = _setup_harvest_repo(tmp_path)
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total candidates written: 2" in out
    items = sorted((target / "_kb" / "inbox").glob("*/*.md"))
    assert len(items) == 2
    for p in items:
        text = p.read_text(encoding="utf-8")
        assert "kind: candidate" in text
        assert "confidence: low" in text
        assert "source: harvest:demo" in text


@requires_git
def test_harvest_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, _ = _setup_harvest_repo(tmp_path)
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target), "--dry-run"])
    assert rc == 0
    assert "would harvest" in capsys.readouterr().out
    assert list((target / "_kb" / "inbox").glob("*/*.md")) == []


@requires_git
def test_harvest_no_connectors_is_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, _ = _setup_harvest_repo(tmp_path, with_connector=False)
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target)])
    assert rc == 1
    assert "no connectors configured" in capsys.readouterr().err


@requires_git
def test_harvest_unknown_connector_is_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _ = _setup_harvest_repo(tmp_path)
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target), "--connector", "file:bogus"])
    assert rc == 1
    assert "no connector named" in capsys.readouterr().out


@requires_git
def test_harvest_scope_refused_exits_zero_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A personal source into a team repo is refused (privacy); the run still completes (exit 0).
    target, _ = _setup_harvest_repo(tmp_path, kind="team", scope="personal")
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target)])
    assert rc == 0
    assert "SCOPE REFUSED" in capsys.readouterr().out
    assert list((target / "_kb" / "inbox").glob("*/*.md")) == []


@requires_git
def test_harvest_malformed_adapters_is_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _ = _setup_harvest_repo(tmp_path)
    ap = target / "adapters.yaml"
    a = yaml.safe_load(ap.read_text(encoding="utf-8"))
    a["connectors"] = {"file:demo": {"path": "/tmp/x/MEMORY.md", "scope": "bogus-scope"}}
    ap.write_text(yaml.safe_dump(a, sort_keys=False), encoding="utf-8")
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target)])
    assert rc == 1
    assert "invalid config" in capsys.readouterr().err


@requires_git
def test_doctor_prints_the_connectors_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _ = _setup_harvest_repo(tmp_path)
    capsys.readouterr()
    main(["doctor", "--repo", str(target)])
    out = capsys.readouterr().out
    assert "harvest: enabled (scope_lock=personal)" in out
    assert "file:demo (scope=personal)" in out
    assert "proposed=0" in out


@requires_git
def test_harvest_follow_links_harvests_sibling_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, mem = _setup_harvest_repo(tmp_path)
    # Reshape the memory as a pointer index + a same-dir sibling, and turn follow_links on.
    mem.write_text("# Index\n\n- [Curator](curator.md) — how it works\n", encoding="utf-8")
    (mem.parent / "curator.md").write_text(
        "# Curator\n\nOne curator holds a per-repo lock.\n", encoding="utf-8"
    )
    ap = target / "adapters.yaml"
    a = yaml.safe_load(ap.read_text(encoding="utf-8"))
    a["connectors"]["file:demo"]["follow_links"] = True
    ap.write_text(yaml.safe_dump(a, sort_keys=False), encoding="utf-8")
    capsys.readouterr()

    rc = main(["harvest", "--repo", str(target)])
    assert rc == 0
    items = sorted((target / "_kb" / "inbox").glob("*/*.md"))
    assert len(items) == 1
    body = items[0].read_text(encoding="utf-8")
    assert "One curator holds a per-repo lock." in body  # the SIBLING content was harvested
    assert "[Curator](curator.md)" not in body  # the thin pointer markup was replaced


# --- gold packs (ADR-0027, issue #37) -----------------------------------------------------------
def _gold_repo(tmp_path: Path) -> Path:
    """Init a repo, add one eligible theme note, and commit it (hermetic git env)."""
    import os
    import subprocess

    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    themes = target / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "curator-concurrency.md").write_text(
        "---\ntitle: Curator Concurrency\ntype: theme\naliases: []\ntags: []\n"
        "created: '2026-06-01'\nupdated: '2026-07-01'\nstatus: active\n"
        "summary: single-writer CAS keeps the wiki consistent\nsources: [raw/a.md]\n"
        "related: []\nconfidence: high\n---\n\n# Curator Concurrency\n\nbody\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_DATE": "2026-07-05T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-07-05T00:00:00+00:00",
    }
    subprocess.run(["git", "add", "-A"], cwd=target, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "theme"], cwd=target, check=True, capture_output=True, env=env
    )
    return target


@requires_git
def test_cli_gold_build_status_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _gold_repo(tmp_path)
    root = str(target)

    assert main(["gold", "build", "--repo", root]) == 0
    out = capsys.readouterr().out
    assert "built pack 'default'" in out
    assert RepoLayout(target).gold_pack_path("default").is_file()

    assert main(["gold", "status", "--repo", root]) == 0
    out = capsys.readouterr().out
    assert "FRESH" in out and "Curator Concurrency" not in out  # status is a meta read, not content

    # --check on a fresh pack passes (byte-identical rebuild contract).
    assert main(["gold", "build", "--repo", root, "--check"]) == 0
    assert "byte-identical" in capsys.readouterr().out


@requires_git
def test_cli_gold_check_detects_stale(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _gold_repo(tmp_path)
    root = str(target)
    assert main(["gold", "build", "--repo", root]) == 0
    capsys.readouterr()
    # Corrupt the on-disk pack: --check must fail (exit 1) against a fresh rebuild.
    RepoLayout(target).gold_pack_path("default").write_text("tampered\n", encoding="utf-8")
    assert main(["gold", "build", "--repo", root, "--check"]) == 1
    assert "DIFFERS" in capsys.readouterr().out


@requires_git
def test_cli_gold_status_absent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _gold_repo(tmp_path)
    assert main(["gold", "status", "--repo", str(target)]) == 0
    assert "absent" in capsys.readouterr().out


def test_cli_gold_missing_subcommand_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gold"]) == 2
    assert "usage: agora gold" in capsys.readouterr().out


@requires_git
def test_cli_doctor_reports_gold_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _gold_repo(tmp_path)
    assert main(["gold", "build", "--repo", str(target)]) == 0
    capsys.readouterr()
    main(["doctor", "--repo", str(target)])
    out = capsys.readouterr().out
    assert "gold: pack=fresh" in out and "_kb/gold/" in out


# --- sync + auto backup (push-only git backup, issue #64) ----------------------------------------
def _set_backup(layout: RepoLayout, *, remote: str, auto: bool = False) -> None:
    """Add a backup: block to an existing _kb/repo.yaml (the issue-#64 opt-in)."""
    repo_yaml = layout.kb_dir / "repo.yaml"
    doc = yaml.safe_load(repo_yaml.read_text(encoding="utf-8"))
    doc["backup"] = {"remote": remote, "auto": auto}
    repo_yaml.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _cli_bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True
    )
    return remote


def _rev_parse(git_dir: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=str(git_dir), capture_output=True, text=True, check=True
    ).stdout.strip()


@requires_git
def test_sync_without_remote_is_a_guided_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(a) No backup.remote configured → `agora sync` explains how to enable it and exits 0
    (a no-op success, NOT an error — safe to script unconditionally)."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    capsys.readouterr()

    assert main(["sync", "--repo", str(target)]) == 0

    captured = capsys.readouterr()
    assert "no backup remote configured" in captured.out
    assert "backup.remote" in captured.out
    assert captured.err == ""
    assert not (RepoLayout(target).kb_dir / "backup.json").exists()  # nothing recorded


@requires_git
def test_sync_pushes_to_local_bare_remote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(b) A configured remote → `agora sync` really pushes: the bare remote's main reaches the
    local curated tip, and the outcome lands in _kb/backup.json for doctor."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    layout = RepoLayout(target)
    remote = _cli_bare_remote(tmp_path)
    _set_backup(layout, remote=str(remote))
    capsys.readouterr()

    assert main(["sync", "--repo", str(target)]) == 0

    out = capsys.readouterr().out
    assert "sync: pushed main @" in out
    assert _rev_parse(remote, "refs/heads/main") == _rev_parse(target, "HEAD")
    state = json.loads((layout.kb_dir / "backup.json").read_text(encoding="utf-8"))
    assert state["ok"] is True
    assert state["remote"] == str(remote)


@requires_git
def test_sync_push_failure_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(c) An unreachable remote → exit 1 with a clean stderr message (no traceback), and the
    failure is recorded for the doctor line."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    layout = RepoLayout(target)
    _set_backup(layout, remote=str(tmp_path / "missing-remote.git"))
    capsys.readouterr()

    assert main(["sync", "--repo", str(target)]) == 1

    err = capsys.readouterr().err
    assert "sync: push failed" in err
    state = json.loads((layout.kb_dir / "backup.json").read_text(encoding="utf-8"))
    assert state["ok"] is False

    # doctor renders the recorded failure as ONE compressed line (first error line only — a
    # multi-line git stderr must never flood the health report; the full text stays in the file).
    main(["doctor", "--repo", str(target)])
    out = capsys.readouterr().out
    backup_lines = [ln for ln in out.splitlines() if ln.lstrip().startswith("backup: remote=")]
    assert len(backup_lines) == 1
    assert "FAILED (" in backup_lines[0]


@requires_git
def test_sync_on_an_uninitialized_repo_path_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typoed/unmounted --repo path exits 1 BEFORE the guided no-op: an unreadable repo also
    reads as "no remote configured", so checking remote first would make a cron'd sync report
    success forever while never pushing — the silent-not-backing-up failure mode the backup
    config parsing itself refuses."""
    missing = tmp_path / "no-such-kb"

    assert main(["sync", "--repo", str(missing)]) == 1

    captured = capsys.readouterr()
    assert "not initialized" in captured.err
    assert "no backup remote configured" not in captured.out


@requires_git
def test_watch_auto_backup_pushes_after_published_curation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """backup.auto=true → a watch tick that PUBLISHES pushes best-effort afterwards; the remote
    ends at the published curated tip."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    remote = _cli_bare_remote(tmp_path)
    _set_backup(layout, remote=str(remote), auto=True)
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "ran (threshold)" in out and "status=published" in out
    assert "backup pushed: main @" in out
    assert _rev_parse(remote, "refs/heads/main") == _rev_parse(target, "refs/heads/main")


@requires_git
def test_watch_auto_backup_failure_never_fails_the_curation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(c) auto=true + an unreachable remote → the tick still publishes and exits 0; the push
    failure is one best-effort warning line, not a curation failure."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    _set_backup(layout, remote=str(tmp_path / "missing-remote.git"), auto=True)
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "status=published" in out  # the curation itself succeeded
    assert "backup push failed (best-effort; curation unaffected)" in out


@requires_git
def test_watch_auto_backup_skips_a_tick_that_did_not_publish(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The auto push fires ONLY on a published tick: a run that FAILS (brain crash) with
    backup.auto=true and a live remote produces ZERO backup side effects — no push output, no
    _kb/backup.json, no branch on the remote. Locks the `status == "published"` gate (a
    regression to push-on-every-ran-tick would pass every other watch test)."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    remote = _cli_bare_remote(tmp_path)
    _set_backup(layout, remote=str(remote), auto=True)
    # Break the brain AFTER init: the tick still runs, but the consolidation FAILS.
    (target / "stub_brain.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "status=failed" in out  # the tick ran and did NOT publish
    assert "backup pushed" not in out
    assert "backup push failed" not in out
    assert not (layout.kb_dir / "backup.json").exists()
    probe = subprocess.run(  # the bare remote never received the branch
        ["git", "rev-parse", "--verify", "refs/heads/main"],
        cwd=str(remote),
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0


@requires_git
def test_watch_auto_push_is_non_interactive_and_time_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The watch tick's auto push must never stall the scheduler: it calls push_backup with
    interactive=False (credential/host-key prompts fail fast) and a FINITE timeout — locks the
    call-site wiring of the unattended-push posture."""
    import agora_kb.cli as cli_mod

    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    _set_backup(layout, remote="git@example.com:me/kb.git", auto=True)
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    seen: dict[str, object] = {}

    def push_spy(self, remote, *, branch=None, timeout=None, interactive=True):  # type: ignore[no-untyped-def]
        seen.update(remote=remote, timeout=timeout, interactive=interactive)
        return "f" * 40

    monkeypatch.setattr(cli_mod.Repo, "push_backup", push_spy)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    assert "status=published" in capsys.readouterr().out
    assert seen["interactive"] is False
    assert isinstance(seen["timeout"], float)
    assert seen["timeout"] <= 300.0


@requires_git
def test_watch_without_backup_config_stays_silent_about_backup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(a) With NO backup: block (auto defaults off), a publishing watch tick emits no backup
    output and records no backup state — the pre-#64 path is undisturbed."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "status=published" in out
    # None of the #64 backup markers appear (the tmp_path itself contains "backup", so match the
    # actual output lines, not the bare word).
    assert "backup pushed" not in out
    assert "backup push failed" not in out
    assert "backup config invalid" not in out
    assert not (layout.kb_dir / "backup.json").exists()


@requires_git
def test_doctor_prints_the_backup_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`agora doctor` reports backup observability: unconfigured → a clear off note; after a real
    sync → remote + auto + the last recorded push (never affecting the health verdict)."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    capsys.readouterr()
    main(["doctor", "--repo", str(target)])
    assert "backup: no remote configured" in capsys.readouterr().out

    layout = RepoLayout(target)
    remote = _cli_bare_remote(tmp_path)
    _set_backup(layout, remote=str(remote), auto=True)
    assert main(["sync", "--repo", str(target)]) == 0
    capsys.readouterr()
    main(["doctor", "--repo", str(target)])
    out = capsys.readouterr().out
    assert f"backup: remote={remote} auto=True" in out
    assert "last_push=" in out and " ok @ " in out


@requires_git
def test_doctor_backup_line_compresses_and_survives_corrupt_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """doctor's backup line stays ONE line under a recorded oversized/multi-line push error (first
    line only, truncated at 140 chars with '…'), and a corrupted _kb/backup.json — wrong types or
    broken JSON — degrades to last_push=unreadable, NEVER a doctor crash (doctor's whole job is
    diagnosing broken repos)."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    layout = RepoLayout(target)
    _set_backup(layout, remote="git@example.com:me/kb.git")
    state = layout.kb_dir / "backup.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "remote": "git@example.com:me/kb.git",
                "ok": False,
                "at": "2026-07-24T00:00:00Z",
                "commit": None,
                "error": ("E" * 200) + "\nfatal: second stderr line",
            }
        ),
        encoding="utf-8",
    )
    capsys.readouterr()

    main(["doctor", "--repo", str(target)])
    out = capsys.readouterr().out
    [line] = [ln for ln in out.splitlines() if ln.lstrip().startswith("backup: remote=")]
    assert "FAILED (" in line
    assert "…" in line  # the 140-char truncation fired
    assert "second stderr line" not in out  # later stderr lines never reach the report

    # Wrong-typed fields (a non-string commit) and non-JSON bytes both degrade, never crash.
    for corrupt in ('{"ok": true, "commit": 12345}', "{not json"):
        state.write_text(corrupt, encoding="utf-8")
        capsys.readouterr()
        main(["doctor", "--repo", str(target)])
        assert "last_push=unreadable" in capsys.readouterr().out
