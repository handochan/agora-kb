"""Tests for :class:`agora_kb.curator.subprocess_backend.SubprocessBackend` (the Phase-1/2 seam).

ZERO real model: every backend is a tiny ``python -c`` / ``python <script>`` stub argv exercised via
the no-shell :func:`agora_kb.curator.backends.run_backend` primitive. We cover the contract this
module owns TODAY (the deterministic gates own success):

* :meth:`plan` runs the configured argv with ``cwd`` = the bundle dir and returns its STDOUT
  verbatim (the worker parses that as ``plan.json``); a non-zero PASS-1 exit becomes a clear error;
* :meth:`author` runs the configured argv with ``cwd`` = the worktree once per ``needs_prose`` note,
  and the stub fills the candidate-id body sentinel in place;
* a missing/unrunnable executable surfaces as :class:`BackendUnavailableError`, not a raw OSError.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agora_kb.curator.apply import body_sentinels
from agora_kb.curator.backends import BackendSpec
from agora_kb.curator.subprocess_backend import BackendUnavailableError, SubprocessBackend


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

    backend.author(tmp_path, {note_rel: ["c1"]})

    text = note.read_text(encoding="utf-8")
    region = text[text.find(start) + len(start) : text.find(end)]
    assert "AUTHORED PROSE" in region
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
    backend.author(tmp_path, {note_rel: ["c1"]})


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
        backend.author(tmp_path, {"wiki/x.md": ["c1"]})
