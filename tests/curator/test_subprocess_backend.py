"""Tests for :class:`agora_kb.curator.subprocess_backend.SubprocessBackend` (the Phase-1/2 seam).

ZERO real model: every backend is a tiny ``python -c`` / ``python <script>`` stub argv exercised via
the no-shell :func:`agora_kb.curator.backends.run_backend` primitive. We cover the contract this
module owns TODAY (the deterministic gates own success):

* :meth:`plan` runs the configured argv with ``cwd`` = the bundle dir and returns its STDOUT
  verbatim (the worker parses that as ``plan.json``); a non-zero PASS-1 exit becomes a clear error;
* :meth:`author` runs the configured argv with ``cwd`` = the worktree once per REGION, feeding the
  §8.2 GROUNDED prompt (op + title + summary + verbatim source + the file/candidate_ids control
  lines) on stdin, and the stub fills the candidate-id body sentinel in place;
* a missing/unrunnable executable surfaces as :class:`BackendUnavailableError`, not a raw OSError.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from agora_kb.curator import subprocess_backend as sb
from agora_kb.curator.apply import body_sentinels
from agora_kb.curator.backends import BackendSpec
from agora_kb.curator.subprocess_backend import BackendUnavailableError, SubprocessBackend
from agora_kb.curator.worker import AuthorRegion

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _python_spec(name: str, code: str) -> BackendSpec:
    """A backend spec that runs ``python -c <code>`` in the given cwd ({worktree} substituted)."""
    return BackendSpec(name=name, argv=(sys.executable, "-c", code))


# --- plan() (PASS 1) ----------------------------------------------------------------------------


def test_plan_returns_backend_stdout_verbatim(tmp_path: Path) -> None:
    """`plan` hands back exactly what the backend prints to STDOUT (the worker parses it)."""
    canned = '{"schema_version": 1, "run_id": "r", "finished": true, "dispositions": []}'
    # The stub echoes the canned plan. We embed it as a repr so the no-shell argv stays literal.
    spec = _python_spec("stub", f"import sys; sys.stdout.write({canned!r})")
    backend = SubprocessBackend(spec)

    out = backend.plan(tmp_path)

    assert out == canned


def test_plan_reads_bundle_from_cwd(tmp_path: Path) -> None:
    """`plan` runs with cwd = the bundle dir, so the backend reads candidates.json from there."""
    (tmp_path / "candidates.json").write_text(
        '{"run_id": "r2", "candidates": []}', encoding="utf-8"
    )
    # The stub reads candidates.json from its cwd and echoes the run_id back inside a plan.
    code = (
        "import json,sys;"
        "d=json.load(open('candidates.json'));"
        "sys.stdout.write(json.dumps("
        "{'schema_version':1,'run_id':d['run_id'],'finished':True,'dispositions':[]}))"
    )
    backend = SubprocessBackend(_python_spec("stub", code))

    out = backend.plan(tmp_path)

    assert '"run_id": "r2"' in out


def test_plan_nonzero_exit_raises_clear_error(tmp_path: Path) -> None:
    """A non-zero PASS-1 exit becomes a clear BackendUnavailableError (PLAN parse then fails)."""
    spec = _python_spec("stub", "import sys; sys.stderr.write('boom'); sys.exit(3)")
    backend = SubprocessBackend(spec)

    with pytest.raises(BackendUnavailableError) as excinfo:
        backend.plan(tmp_path)
    assert "exited 3" in str(excinfo.value)
    assert "boom" in str(excinfo.value)


# --- author() (PASS 2) --------------------------------------------------------------------------


def test_author_fills_sentinel_region_in_worktree(tmp_path: Path) -> None:
    """`author` runs with cwd = the worktree; the stub fills the candidate-id sentinel region."""
    start, end = body_sentinels("c1")
    note_rel = "wiki/ai-tech/themes/curator-concurrency.md"
    note = tmp_path / note_rel
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        f"---\ntitle: T\nstatus: active\n---\n\n{start}\n> _summary pending_\n{end}\n",
        encoding="utf-8",
    )

    # A stub that scans wiki/ for body sentinels and writes prose into each region, in place.
    code = (
        "import re;"
        "from pathlib import Path;"
        "S=re.compile(r'<!-- agora:body:start id=(.+?) -->');"
        "p=list(Path('wiki').rglob('*.md'))[0];"
        "t=p.read_text();out=[];reg=False\n"
        "for ln in t.split(chr(10)):\n"
        " if S.search(ln): out.append(ln); out.append('AUTHORED PROSE'); reg=True; continue\n"
        " if 'agora:body:end' in ln: reg=False; out.append(ln); continue\n"
        " if reg: continue\n"
        " out.append(ln)\n"
        "p.write_text(chr(10).join(out))"
    )
    backend = SubprocessBackend(_python_spec("stub", code))

    region = AuthorRegion(
        op="CREATE_THEME", title="T", summary="s", source_text="curator holds a per-repo flock"
    )
    backend.author(tmp_path, {note_rel: ["c1"]}, {"c1": region})

    text = note.read_text(encoding="utf-8")
    region_text = text[text.find(start) + len(start) : text.find(end)]
    assert "AUTHORED PROSE" in region_text
    # Frontmatter and markers are preserved (the stub only rewrites between the markers).
    assert "title: T" in text
    assert start in text and end in text


def test_author_nonzero_exit_does_not_raise(tmp_path: Path) -> None:
    """A non-zero PASS-2 exit is NOT fatal: the worker's author-diff gate degrades that note."""
    note_rel = "wiki/ai-tech/themes/t.md"
    note = tmp_path / note_rel
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("body\n", encoding="utf-8")
    backend = SubprocessBackend(_python_spec("stub", "import sys; sys.exit(2)"))

    # Must not raise: a flaky prose pass is handled by the deterministic AUTHOR-diff gate, not here.
    backend.author(tmp_path, {note_rel: ["c1"]}, {})


def _stdin_capture_spec(capture_path: Path) -> BackendSpec:
    """A stub argv that appends its STDIN (one record, NUL-delimited) to ``capture_path``."""
    code = f"import sys;open({str(capture_path)!r}, 'a').write(sys.stdin.read() + chr(0))"
    return BackendSpec(name="capture", argv=(sys.executable, "-c", code))


def test_author_invokes_once_per_region_with_grounded_prompt(tmp_path: Path) -> None:
    """`author` invokes the argv ONCE PER REGION; each prompt carries that region's §8.2 grounding.

    Two regions in one note → two invocations, each prompt grounded in its OWN op + source text +
    the load-bearing ``file =`` / ``candidate_ids =`` control lines (the shim parses by them).
    """
    note_rel = "wiki/ai-tech/themes/multi.md"
    note = tmp_path / note_rel
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("body\n", encoding="utf-8")

    capture = tmp_path / "stdin_capture.txt"
    backend = SubprocessBackend(_stdin_capture_spec(capture))

    context = {
        "r--c1": AuthorRegion(
            op="CREATE_THEME",
            title="Curator concurrency",
            summary="single writer",
            source_text="ONE curator holds a per-repo flock",
        ),
        "r--c2": AuthorRegion(
            op="MERGE_INTO_THEME",
            title="Curator concurrency",
            summary="single writer",
            source_text="the lock is a per-tenant boundary",
        ),
    }
    backend.author(tmp_path, {note_rel: ["r--c1", "r--c2"]}, context)

    records = [r for r in capture.read_text(encoding="utf-8").split("\0") if r.strip()]
    assert len(records) == 2  # ONE invocation per region, not one per note.

    by_sid = {("r--c1" if "r--c1" in r else "r--c2"): r for r in records}
    assert set(by_sid) == {"r--c1", "r--c2"}

    # Each prompt carries the load-bearing control lines pointing at its OWN region + the note.
    for sid, prompt in by_sid.items():
        assert f"file = {note_rel}" in prompt
        assert f"candidate_ids = {sid}" in prompt

    create_prompt = by_sid["r--c1"]
    assert "ONE curator holds a per-repo flock" in create_prompt  # this region's source.
    assert "the lock is a per-tenant boundary" not in create_prompt  # not the OTHER region's.
    assert "op = CREATE_THEME" in create_prompt
    assert "write the FULL note body" in create_prompt  # op-aware CREATE instruction.

    merge_prompt = by_sid["r--c2"]
    assert "the lock is a per-tenant boundary" in merge_prompt
    assert "op = MERGE_INTO_THEME" in merge_prompt
    assert "write ONLY the NEW claim" in merge_prompt  # op-aware MERGE instruction.

    # The grounded prompt now asks for markdown STRUCTURE while KEEPING the existing constraints.
    # NOTE: the model's actual structured output (real `##`/`-` markers in the body) is verified by
    # a live curate e2e, NOT here — this only asserts the deterministic PROMPT STRING.
    for prompt in (create_prompt, merge_prompt):
        assert "sub-headings" in prompt  # the new markdown-structure instruction.
        assert "## " in prompt  # the `##` sub-heading guidance literal.
        assert "Do NOT add a top-level `# heading`" in prompt  # no-top-#-title clause.
        assert "stripped to plain text" in prompt  # existing no-wikilinks constraint kept.
        assert "<!-- agora:body:start id=<candidate_id> -->" in prompt  # markers kept.
    # CREATE authors a full structured body; MERGE must NOT add its own sub-headings.
    assert "do NOT add your own `##`" in merge_prompt


def test_author_missing_context_falls_back_to_minimal_prompt(tmp_path: Path) -> None:
    """A region with no §8.2 context entry feeds the minimal prompt (keeps the control lines)."""
    note_rel = "wiki/ai-tech/themes/m.md"
    note = tmp_path / note_rel
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("body\n", encoding="utf-8")

    capture = tmp_path / "stdin_capture.txt"
    backend = SubprocessBackend(_stdin_capture_spec(capture))

    backend.author(tmp_path, {note_rel: ["r--c1"]}, {})  # empty context → minimal fallback.

    prompt = capture.read_text(encoding="utf-8").rstrip("\0")
    assert f"file = {note_rel}" in prompt
    assert "candidate_ids = r--c1" in prompt
    assert "--- BEGIN SOURCE ---" not in prompt  # the grounded source block is absent.


# --- missing executable -------------------------------------------------------------------------


def test_plan_missing_executable_raises_backend_unavailable(tmp_path: Path) -> None:
    """A configured program that is not on PATH becomes a clear BackendUnavailableError."""
    spec = BackendSpec(name="ghost", argv=("definitely-not-a-real-program-xyz", "--go"))
    backend = SubprocessBackend(spec)

    with pytest.raises(BackendUnavailableError) as excinfo:
        backend.plan(tmp_path)
    msg = str(excinfo.value)
    assert "ghost" in msg
    assert "could not be executed" in msg


def test_author_missing_executable_raises_backend_unavailable(tmp_path: Path) -> None:
    """A missing executable is fatal on PASS 2 too (the brain cannot run at all)."""
    spec = BackendSpec(name="ghost", argv=("definitely-not-a-real-program-xyz",))
    backend = SubprocessBackend(spec)

    with pytest.raises(BackendUnavailableError):
        backend.author(tmp_path, {"wiki/x.md": ["c1"]}, {})


# --- #57 output-language directive (repo.yaml curator.language) ---------------------------------


def test_plan_prompt_byte_identical_without_language(tmp_path: Path) -> None:
    """language=None (the default) keeps the PASS-1 prompt byte-identical to _PASS1_PROMPT."""
    capture = tmp_path / "cap.txt"
    backend = SubprocessBackend(_stdin_capture_spec(capture))

    backend.plan(tmp_path)

    prompt = capture.read_text(encoding="utf-8").rstrip("\0")
    assert prompt == sb._PASS1_PROMPT
    assert "LANGUAGE:" not in prompt


def test_plan_prompt_language_directive_appended(tmp_path: Path) -> None:
    """language='ko' appends exactly ONE directive line to PASS-1 (nothing else moves)."""
    capture = tmp_path / "cap.txt"
    backend = SubprocessBackend(_stdin_capture_spec(capture), language="ko")

    backend.plan(tmp_path)

    prompt = capture.read_text(encoding="utf-8").rstrip("\0")
    expected = sb._PASS1_PROMPT + sb._LANGUAGE_DIRECTIVE_TEMPLATE.format(language="ko") + "\n"
    assert prompt == expected
    assert "in ko;" in prompt  # prose in the repo language...
    assert "ASCII rules" in prompt  # ...but slug/domain/tag tokens stay schema-ASCII.


def test_pass2_prompts_carry_language_directive_when_set(tmp_path: Path) -> None:
    """Both PASS-2 variants (grounded + minimal fallback) carry the directive when language set."""
    capture = tmp_path / "cap.txt"
    backend = SubprocessBackend(_stdin_capture_spec(capture), language="ko")
    region = AuthorRegion(op="CREATE_THEME", title="T", summary="s", source_text="src facts")

    # r--c1 has grounded context; r--c2 falls back to the minimal prompt.
    backend.author(tmp_path, {"wiki/x.md": ["r--c1", "r--c2"]}, {"r--c1": region})

    prompts = [p for p in capture.read_text(encoding="utf-8").split("\0") if p]
    assert len(prompts) == 2
    for prompt in prompts:
        assert "LANGUAGE: write every summary, title, and body in ko" in prompt


def test_pass2_prompts_have_no_directive_without_language(tmp_path: Path) -> None:
    """language=None leaves both PASS-2 prompt variants free of any LANGUAGE line."""
    capture = tmp_path / "cap.txt"
    backend = SubprocessBackend(_stdin_capture_spec(capture))
    region = AuthorRegion(op="CREATE_THEME", title="T", summary="s", source_text="src facts")

    backend.author(tmp_path, {"wiki/x.md": ["r--c1", "r--c2"]}, {"r--c1": region})

    prompts = [p for p in capture.read_text(encoding="utf-8").split("\0") if p]
    assert len(prompts) == 2
    for prompt in prompts:
        assert "LANGUAGE:" not in prompt


# --- resolve_program_on_path (shared with `agora doctor`'s brain probe, #96) ---------------------


@requires_git
def test_resolve_program_on_path_resolves_a_bare_name() -> None:
    """A bare name goes through ``shutil.which`` — the same lookup the operator's PATH already did
    when they configured it, and the answer a spawn would use."""
    found = sb.resolve_program_on_path("git")
    assert found is not None
    assert Path(found).is_absolute()


def test_resolve_program_on_path_passes_a_path_ish_program_through() -> None:
    """A path-ish argv[0] is already unambiguous and is returned UNCHANGED — including one that
    does not exist, which is why doctor's probe adds its own is_file/X_OK check on top."""
    assert sb.resolve_program_on_path("/no/such/x") == "/no/such/x"


def test_resolve_program_on_path_returns_none_for_an_unresolvable_name() -> None:
    """``None`` is the shared signal: ``_absolute_program`` RAISES on it, doctor REPORTS it."""
    assert sb.resolve_program_on_path("agora-definitely-not-installed-xyz") is None
