"""Tests for the generic CLI-agent curator-brain shim (``agora-cli-brain``).

The shim's own responsibilities are: (1) :func:`call_cli_agent` — run a configured CLI as a
stdin→stdout text generator and normalize every failure to a :class:`CliAgentError`; (2) argv
parsing of our options vs the ``-- <cli> <args...>`` agent argv; (3) wiring an ``infer`` callable
into the REUSED Ollama two-pass pipeline (:func:`run_plan` / :func:`run_author`). The bundle-reading
+ plan-normalization + prose-sanitization internals belong to ``ollama_brain`` and are tested there;
the full reuse against a real model is covered by the live e2e.
"""

from __future__ import annotations

import io
import sys

import pytest

from agora_kb.adapters import cli_agent_brain as cb
from agora_kb.adapters.cli_agent_brain import CliAgentError, _parse_args, call_cli_agent
from agora_kb.adapters.ollama_brain import BrainError

try:
    "x".encode("cp949")
    _HAS_CP949 = True
except LookupError:  # pragma: no cover - defensive only; cp949 ships with CPython
    _HAS_CP949 = False


# --- call_cli_agent (real subprocess) ------------------------------------------------------------
def test_call_cli_agent_feeds_stdin_returns_stdout() -> None:
    out = call_cli_agent(
        "hello world",
        argv=[sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        timeout=30,
    )
    assert out == "HELLO WORLD"


def test_call_cli_agent_round_trips_non_ascii_prompt_and_output() -> None:
    # Pins the #85 encoding pin (tests-3): a Korean candidate body must survive both the stdin
    # ENCODE and the stdout DECODE regardless of the process locale — this exercises the real
    # subprocess path, not a mocked one, so it fails if the `encoding="utf-8"` pin regresses.
    out = call_cli_agent(
        "한국어 메모",
        argv=[sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        timeout=30,
    )
    assert out == "한국어 메모"


def test_call_cli_agent_nonzero_exit_raises() -> None:
    with pytest.raises(CliAgentError, match="exited 3"):
        call_cli_agent("x", argv=[sys.executable, "-c", "import sys; sys.exit(3)"], timeout=30)


def test_call_cli_agent_missing_executable_raises() -> None:
    with pytest.raises(CliAgentError, match="not found"):
        call_cli_agent("x", argv=["definitely-not-a-real-binary-xyzzy"], timeout=30)


def test_call_cli_agent_timeout_raises() -> None:
    with pytest.raises(CliAgentError, match="timed out"):
        call_cli_agent("x", argv=[sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)


def test_call_cli_agent_empty_output_raises() -> None:
    with pytest.raises(CliAgentError, match="no output"):
        call_cli_agent("x", argv=[sys.executable, "-c", "pass"], timeout=30)


def test_cli_agent_error_is_a_brain_error() -> None:
    # So the reused drivers' ``except BrainError`` treats a CLI failure like an Ollama failure.
    assert issubclass(CliAgentError, BrainError)


def test_call_cli_agent_env_carries_forced_utf8_vars() -> None:
    # (codex-P1) call_cli_agent's env must carry BOTH vars regardless of what the outer process env
    # says, so a Python-based CLI-agent child opens its own stdio as UTF-8 (#85).
    out = call_cli_agent(
        "x",
        argv=[
            sys.executable,
            "-c",
            "import os,sys; sys.stdout.write("
            "os.environ.get('PYTHONIOENCODING','') + '|' + os.environ.get('PYTHONUTF8',''))",
        ],
        timeout=30,
    )
    assert out == "utf-8|1"


@pytest.mark.skipif(not _HAS_CP949, reason="cp949 codec unavailable in this interpreter")
def test_call_cli_agent_round_trips_korean_despite_hostile_outer_pythonioencoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # (codex-P1) Pinning encoding="utf-8" on THIS process's end of the pipe does not touch the
    # CHILD's own locale-driven stdio encoding: a Python child inheriting a non-UTF-8
    # PYTHONIOENCODING (a cp949 Windows console, say) would otherwise fail to decode a UTF-8 prompt
    # on the way in. Setting a hostile PYTHONIOENCODING on the OUTER (this-process) env must not
    # leak into the child, because call_cli_agent forces its own child env regardless.
    monkeypatch.setenv("PYTHONIOENCODING", "cp949")
    out = call_cli_agent(
        "한국어 메모",
        argv=[sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        timeout=30,
    )
    assert out == "한국어 메모"


# --- stdio reconfiguration (codex-P1: the shim must be correct even launched by a third party) ----
# The helper itself lives in ollama_brain (BOTH shims' main() calls it); these exercise it through
# the cli_agent_brain namespace it is imported into, which is where main() reaches it.
def test_reconfigure_stdio_utf8_is_a_noop_on_streams_without_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # io.StringIO (what tests inject as sys.stdin below, and what a caller could plausibly pass)
    # has no `reconfigure` method — must be skipped, never raise AttributeError.
    monkeypatch.setattr(sys, "stdin", io.StringIO("x"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    cb.reconfigure_stdio_utf8()  # must not raise


def test_reconfigure_stdio_utf8_reconfigures_a_real_text_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    class _FakeStream:
        errors = "backslashreplace"

        def reconfigure(self, *, encoding: str, errors: str | None = None) -> None:
            calls.append((encoding, errors))

    monkeypatch.setattr(sys, "stdin", _FakeStream())
    monkeypatch.setattr(sys, "stdout", _FakeStream())
    monkeypatch.setattr(sys, "stderr", _FakeStream())
    cb.reconfigure_stdio_utf8()
    # The encoding is PINNED; the stream's own error handler is CARRIED THROUGH, never reset.
    assert calls == [("utf-8", "backslashreplace")] * 3


def test_reconfigure_stdio_utf8_keeps_stderr_writable_for_surrogates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # (postfix-1) `reconfigure(encoding=...)` with no `errors=` silently resets the handler to
    # "strict", and CPython's sys.stderr defaults to "backslashreplace". Downgrading it would make
    # main()'s own `print(f"... {exc}", file=sys.stderr)` raise UnicodeEncodeError on any message
    # carrying a surrogate (an OSError naming an os.fsdecode'd path) — the shim's diagnostics would
    # be strictly LESS robust than before the #85 pin. The handler must survive.
    raw = io.BytesIO()
    stderr = io.TextIOWrapper(raw, encoding="utf-8", errors="backslashreplace")
    monkeypatch.setattr(sys, "stderr", stderr)
    cb.reconfigure_stdio_utf8()
    assert stderr.encoding == "utf-8"
    print("boom \udcff", file=sys.stderr)  # must NOT raise UnicodeEncodeError
    stderr.flush()
    assert b"\\udcff" in raw.getvalue()


@pytest.mark.parametrize("break_it", ["close", "read"])
def test_reconfigure_stdio_utf8_tolerates_an_unreconfigurable_stream(
    monkeypatch: pytest.MonkeyPatch, break_it: str
) -> None:
    # (postfix-3) A closed TextIOWrapper raises ValueError from reconfigure(), and one already read
    # from raises io.UnsupportedOperation (a ValueError subclass) — both while HAVING the attribute,
    # so an `is not None` guard does not cover them. A third-party launcher starting the shim with
    # stdin closed must NOT get a traceback out of main()'s first statement.
    stream = io.TextIOWrapper(io.BytesIO(b"xy"), encoding="utf-8")
    if break_it == "close":
        stream.close()
    else:
        stream.read(1)
    monkeypatch.setattr(sys, "stdin", stream)
    cb.reconfigure_stdio_utf8()  # must not raise


# --- argv parsing ---------------------------------------------------------------------------------
def test_parse_args_splits_agent_argv_after_separator() -> None:
    ns, cli_argv = _parse_args(["--", "claude", "-p"])
    assert cli_argv == ["claude", "-p"]
    assert ns.agent_timeout == 300.0
    assert ns.label is None


def test_parse_args_honors_options_before_separator() -> None:
    ns, cli_argv = _parse_args(["--label", "claude", "--agent-timeout", "60", "--", "gemini", "-p"])
    assert ns.label == "claude"
    assert ns.agent_timeout == 60.0
    assert cli_argv == ["gemini", "-p"]


def test_parse_args_no_agent_argv() -> None:
    _ns, cli_argv = _parse_args([])
    assert cli_argv == []


# --- main dispatch (infer wired into the reused pipeline; run_plan/run_author spied) --------------
def _set_stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def test_main_no_agent_argv_is_clean_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cb.main([]) == 1
    assert "no CLI agent" in capsys.readouterr().err


def test_main_plan_wires_infer_and_prints_plan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}

    def _spy_run_plan(cwd, stdin_prompt, *, infer, model_label):  # noqa: ANN001, ANN202
        captured["infer"] = infer
        captured["label"] = model_label
        return '{"schema_version":1,"dispositions":[]}'

    monkeypatch.setattr(cb, "run_plan", _spy_run_plan)
    _set_stdin(monkeypatch, "You are the Agora curator PLANNER ...")

    rc = cb.main(["--label", "claude", "--", "claude", "-p"])
    assert rc == 0
    assert '{"schema_version":1,"dispositions":[]}' in capsys.readouterr().out
    assert callable(captured["infer"])
    assert captured["label"] == "claude"


def test_main_plan_label_defaults_to_cli_program(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _spy_run_plan(cwd, stdin_prompt, *, infer, model_label):  # noqa: ANN001, ANN202
        captured["label"] = model_label
        return "{}"

    monkeypatch.setattr(cb, "run_plan", _spy_run_plan)
    _set_stdin(monkeypatch, "curator PLANNER")
    assert cb.main(["--", "gemini", "-p"]) == 0
    assert captured["label"] == "gemini"  # defaults to argv[0] of the agent


def test_main_author_dispatches_to_run_author(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _spy_run_author(cwd, stdin_prompt, *, infer, model_label, text_only):  # noqa: ANN001, ANN202
        seen["called"] = True
        seen["infer_callable"] = callable(infer)
        seen["text_only"] = text_only

    monkeypatch.setattr(cb, "run_author", _spy_run_author)
    _set_stdin(monkeypatch, "curator WRITER\n  file = x.md\n  candidate_ids = c1\n")
    assert cb.main(["--", "claude", "-p"]) == 0
    # the CLI-agent face always asks run_author for prose-only output (text_only=True)
    assert seen == {"called": True, "infer_callable": True, "text_only": True}


def test_main_plan_brain_error_is_clean_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(cwd, stdin_prompt, *, infer, model_label):  # noqa: ANN001, ANN202
        raise BrainError("model said no")

    monkeypatch.setattr(cb, "run_plan", _boom)
    _set_stdin(monkeypatch, "curator PLANNER")
    assert cb.main(["--", "claude", "-p"]) == 1
    assert "model said no" in capsys.readouterr().err


def test_main_infer_invokes_the_configured_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # End-to-end of the wiring: the infer the shim builds actually shells the agent argv. We capture
    # the infer from run_plan and call it; it should run our stub CLI as a text generator.
    grabbed: dict[str, object] = {}

    def _grab(cwd, stdin_prompt, *, infer, model_label):
        grabbed["infer"] = infer
        return "{}"

    monkeypatch.setattr(cb, "run_plan", _grab)
    _set_stdin(monkeypatch, "curator PLANNER")
    cb.main(
        [
            "--",
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('ECHO:' + sys.stdin.read())",
        ]
    )
    infer = grabbed["infer"]
    assert infer("ping") == "ECHO:ping"  # the configured CLI ran as a text generator


# --- text-only PASS-2 (a text-generator agent must get a prose-only prompt, not "edit the file") --
def test_run_author_text_only_sends_prose_only_grounded_prompt(tmp_path) -> None:
    from agora_kb.adapters.ollama_brain import run_author

    note = tmp_path / "n.md"
    note.write_text(
        "---\ntitle: T\nsummary: S\n---\n"
        "<!-- agora:body:start id=c1 -->\nPLACEHOLDER\n<!-- agora:body:end id=c1 -->\n",
        encoding="utf-8",
    )
    grounded = (
        "SYSTEM curator WRITER\n  file = n.md\n  candidate_ids = c1\n"
        "  --- BEGIN SOURCE ---\nThe single curator holds a per-repo flock.\n  --- END SOURCE ---\n"
        "Edit the file in place, writing ONLY inside this region's markers."
    )
    seen: dict[str, str] = {}

    def _infer(prompt: str) -> str:
        seen["prompt"] = prompt
        return "Clean body prose, no preamble."

    run_author(tmp_path, grounded, infer=_infer, model_label="claude", text_only=True)

    sent = seen["prompt"]
    assert "TEXT GENERATOR" in sent  # the strict prose-only contract
    assert "per-repo flock" in sent  # grounded in the verbatim §8.2 SOURCE
    assert "edit the file in place" not in sent.lower()  # NOT the worker's file-edit framing
    assert "Clean body prose, no preamble." in note.read_text(encoding="utf-8")


def test_run_author_text_only_minimal_prompt_grounds_in_region_body(tmp_path) -> None:
    # The text_only FALLBACK leg: a MINIMAL prompt (no §8.2 SOURCE block) must still use the
    # prose-only template, grounded in the region's seeded body, not leaking worker control lines.
    from agora_kb.adapters.ollama_brain import run_author

    note = tmp_path / "n.md"
    note.write_text(
        "---\ntitle: T\nsummary: S\n---\n"
        "<!-- agora:body:start id=c1 -->\n"
        "SEEDED REGION BODY TEXT here.\n"
        "<!-- agora:body:end id=c1 -->\n",
        encoding="utf-8",
    )
    minimal = "SYSTEM curator WRITER\n  file = n.md\n  candidate_ids = c1\n"
    seen: dict[str, str] = {}

    def _infer(prompt: str) -> str:
        seen["prompt"] = prompt
        return "Generated prose."

    run_author(tmp_path, minimal, infer=_infer, model_label="claude", text_only=True)

    sent = seen["prompt"]
    assert "TEXT GENERATOR" in sent  # prose-only contract preserved on the fallback path too
    assert "SEEDED REGION BODY TEXT here." in sent  # grounded in the region's seeded body
    assert "file = n.md" not in sent  # the worker control lines never leak to the agent
    assert "candidate_ids = c1" not in sent
    assert "LANGUAGE:" not in sent  # no curator.language → the rebuilt prompt stays directive-free
    assert "Generated prose." in note.read_text(encoding="utf-8")


def test_run_author_text_only_reattaches_language_directive(tmp_path) -> None:
    """(#57 review) curator.language must reach the CLI-agent PASS-2: the text_only path REBUILDS
    the prompt from the SOURCE block, so the worker-appended LANGUAGE line has to be re-attached —
    without this the repo-language contract silently drops for every CLI brain."""
    from agora_kb.adapters.ollama_brain import run_author

    note = tmp_path / "n.md"
    note.write_text(
        "---\ntitle: T\nsummary: S\n---\n"
        "<!-- agora:body:start id=c1 -->\nPLACEHOLDER\n<!-- agora:body:end id=c1 -->\n",
        encoding="utf-8",
    )
    directive = (
        "LANGUAGE: write every summary, title, and body in ko; slug / domain / tag tokens "
        "still follow the schema's ASCII rules."
    )
    grounded = (
        "SYSTEM curator WRITER\n  file = n.md\n  candidate_ids = c1\n"
        "  --- BEGIN SOURCE ---\nThe single curator holds a per-repo flock.\n  --- END SOURCE ---\n"
        "Edit the file in place, writing ONLY inside this region's markers.\n"
        f"{directive}\n"  # SubprocessBackend appends the directive AFTER the TASK block
    )
    seen: dict[str, str] = {}

    def _infer(prompt: str) -> str:
        seen["prompt"] = prompt
        return "국문 본문."

    run_author(tmp_path, grounded, infer=_infer, model_label="claude", text_only=True)

    sent = seen["prompt"]
    assert "TEXT GENERATOR" in sent  # still the strict prose-only contract
    assert "per-repo flock" in sent  # still grounded in the verbatim §8.2 SOURCE
    assert directive in sent  # the worker's LANGUAGE line rides the rebuilt prompt
    assert "국문 본문." in note.read_text(encoding="utf-8")
