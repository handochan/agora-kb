"""ADR-0013 wiring tests for :class:`SubprocessBackend` confinement (the worker/CLI inject path).

The isolation PACKAGE is proven independently in ``test_isolation.py`` (write-outside EPERM, etc.).
Here we prove the SEAM: that :class:`SubprocessBackend`
ROUTES the file-writing PASS-2 step through an injected
:class:`~agora_kb.curator.isolation.BackendIsolation` when (and only when) the backend is
``network: 'none'`` — and that PASS 1 + a loopback brain stay UNCONFINED (inference outside the
sandbox, ADR-0013). The darwin-gated test additionally proves end-to-end, under the REAL seatbelt
sandbox, that an out-of-worktree write attempted during ``author`` is denied while the in-worktree
sentinel fill succeeds.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agora_kb.curator.apply import body_sentinels
from agora_kb.curator.backends import BackendSpec
from agora_kb.curator.isolation import (
    SandboxResult,
    SandboxSpec,
    select_backend_isolation,
)
from agora_kb.curator.subprocess_backend import BackendUnavailableError, SubprocessBackend
from agora_kb.curator.worker import AuthorRegion


class _RecordingIsolation:
    """A :class:`BackendIsolation` that RECORDS every spec it is handed (no real confinement).

    ``run`` appends the spec and returns a clean :class:`SandboxResult` WITHOUT executing the argv,
    so the routing decision (did ``SubprocessBackend`` confine this invocation?) is observable
    without a kernel sandbox or a real subprocess. ``self_test`` is never called by these tests.
    """

    name = "recording"

    def __init__(self) -> None:
        self.specs: list[SandboxSpec] = []

    def available(self) -> bool:
        return True

    def self_test(self, throwaway_worktree, throwaway_tmp, backend_read_roots):  # noqa: ANN001, ANN201
        raise NotImplementedError

    def run(self, spec: SandboxSpec) -> SandboxResult:
        self.specs.append(spec)
        return SandboxResult(
            returncode=0, stdout=b"", stderr=b"", mechanism=self.name, reduced_isolation=False
        )


def _fill_then_escape_code(escape_target: Path) -> str:
    """A ``python -c`` body: fill the wiki sentinel region, then ATTEMPT a write to the escape path.

    Mirrors the in-tree author stub (scan ``wiki/`` for the body sentinel, write between markers)
    and then tries to open an ABSOLUTE path OUTSIDE the worktree + tmp for write — which the sandbox
    must deny. The attempt swallows any OS error so a denied write does not crash the stub.
    """
    return (
        "import re\n"
        "from pathlib import Path\n"
        "S=re.compile(r'<!-- agora:body:start id=(.+?) -->')\n"
        "p=list(Path('wiki').rglob('*.md'))[0]\n"
        "t=p.read_text();out=[];reg=False\n"
        "for ln in t.split(chr(10)):\n"
        " if S.search(ln): out.append(ln); out.append('AUTHORED PROSE'); reg=True; continue\n"
        " if 'agora:body:end' in ln: reg=False; out.append(ln); continue\n"
        " if reg: continue\n"
        " out.append(ln)\n"
        "p.write_text(chr(10).join(out))\n"
        f"esc={str(escape_target)!r}\n"
        "try:\n"
        " open(esc,'w').write('escaped')\n"
        "except OSError:\n"
        " pass\n"
    )


def _note_with_sentinel(worktree: Path, note_rel: str) -> Path:
    start, end = body_sentinels("c1")
    note = worktree / note_rel
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        f"---\ntitle: T\nstatus: active\n---\n\n{start}\n> _summary pending_\n{end}\n",
        encoding="utf-8",
    )
    return note


# --- routing (platform-agnostic) ----------------------------------------------------------------


def test_author_routes_network_none_through_isolation(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """A ``network: 'none'`` backend's author() runs INSIDE the injected isolation adapter."""
    monkeypatch.setenv("MY_SECRET_TOKEN", "leak-me")  # must be scrubbed out of the spec env
    note_rel = "wiki/general/themes/probe.md"
    _note_with_sentinel(tmp_path, note_rel)
    spec = BackendSpec(name="confined", argv=(sys.executable, "-c", "pass"), network="none")
    iso = _RecordingIsolation()
    backend = SubprocessBackend(spec, isolation=iso)

    region = AuthorRegion(op="CREATE_THEME", title="T", summary="s", source_text="x")
    backend.author(tmp_path, {note_rel: ["c1"]}, {"c1": region})

    assert len(iso.specs) == 1  # the one region was confined
    sb = iso.specs[0]
    assert sb.network == "none"
    assert sb.argv[0] == sys.executable  # argv passed through verbatim (no shell)
    # tmp_dir is a DISTINCT realpath OUTSIDE the worktree (HOME/TMPDIR point there).
    assert sb.tmp_dir != sb.worktree
    assert not str(sb.tmp_dir).startswith(str(sb.worktree) + "/")
    # env is credential-scrubbed before the adapter sees it (G3) — the secret never enters.
    assert "MY_SECRET_TOKEN" not in sb.env
    assert "PATH" in sb.env  # innocuous vars survive


def test_plan_is_never_confined(tmp_path: Path) -> None:
    """PASS 1 (plan) writes no wiki files; it runs UNCONFINED even with an isolation adapter."""
    canned = '{"schema_version": 1, "run_id": "r", "finished": true, "dispositions": []}'
    spec = BackendSpec(
        name="confined",
        argv=(sys.executable, "-c", f"import sys; sys.stdout.write({canned!r})"),
        network="none",
    )
    iso = _RecordingIsolation()
    backend = SubprocessBackend(spec, isolation=iso)

    out = backend.plan(tmp_path)

    assert out == canned
    assert iso.specs == []  # isolation.run was NOT called for PASS 1


def test_loopback_backend_is_not_confined(tmp_path: Path) -> None:
    """A ``network: 'loopback'`` (Ollama) brain does inference OUTSIDE the sandbox — unconfined."""
    note_rel = "wiki/general/themes/probe.md"
    note = _note_with_sentinel(tmp_path, note_rel)
    start, end = body_sentinels("c1")
    # The real stub (run unconfined via run_backend) fills the sentinel in place.
    code = (
        "import re\n"
        "from pathlib import Path\n"
        "S=re.compile(r'<!-- agora:body:start id=(.+?) -->')\n"
        "p=list(Path('wiki').rglob('*.md'))[0]\n"
        "t=p.read_text();out=[];reg=False\n"
        "for ln in t.split(chr(10)):\n"
        " if S.search(ln): out.append(ln); out.append('AUTHORED PROSE'); reg=True; continue\n"
        " if 'agora:body:end' in ln: reg=False; out.append(ln); continue\n"
        " if reg: continue\n"
        " out.append(ln)\n"
        "p.write_text(chr(10).join(out))\n"
    )
    spec = BackendSpec(name="ollama", argv=(sys.executable, "-c", code), network="loopback")
    iso = _RecordingIsolation()
    backend = SubprocessBackend(spec, isolation=iso)

    region = AuthorRegion(op="CREATE_THEME", title="T", summary="s", source_text="x")
    backend.author(tmp_path, {note_rel: ["c1"]}, {"c1": region})

    assert iso.specs == []  # loopback brain was NOT confined (inference outside)
    text = note.read_text(encoding="utf-8")
    assert "AUTHORED PROSE" in text[text.find(start) + len(start) : text.find(end)]


def test_author_reports_a_failed_region_instead_of_swallowing_its_exit_code(
    tmp_path: Path,
) -> None:
    """A non-zero PASS-2 region must come back as a DIAGNOSTIC, not as silence (#115).

    The exit code used to be discarded, so a backend that could not start (bwrap failing at
    ``execvp`` for every region) left the worker with an empty diff — indistinguishable from
    "nothing to do" — and the run published placeholder bodies while reporting success.
    """
    note_rel = "wiki/general/themes/probe.md"
    _note_with_sentinel(tmp_path, note_rel)
    spec = BackendSpec(
        name="broken",
        argv=(sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"),
        network="loopback",  # unconfined: this asserts the reporting, not the sandbox
    )
    backend = SubprocessBackend(spec)

    failures = backend.author(
        tmp_path,
        {note_rel: ["c1"]},
        {"c1": AuthorRegion(op="CREATE_THEME", title="T", summary="s", source_text="x")},
    )

    assert len(failures) == 1
    assert "exited 3" in failures[0]
    assert "boom" in failures[0]  # the backend's own stderr, not a generic message
    assert note_rel in failures[0] and "c1" in failures[0]


def test_author_reports_nothing_when_every_region_succeeds(tmp_path: Path) -> None:
    """The happy path returns an EMPTY diagnostic list — no warning noise on a good run."""
    note_rel = "wiki/general/themes/probe.md"
    _note_with_sentinel(tmp_path, note_rel)
    spec = BackendSpec(name="ok", argv=(sys.executable, "-c", "pass"), network="loopback")

    failures = SubprocessBackend(spec).author(
        tmp_path,
        {note_rel: ["c1"]},
        {"c1": AuthorRegion(op="CREATE_THEME", title="T", summary="s", source_text="x")},
    )

    assert failures == []


def test_sandboxed_bare_name_program_is_resolved_on_the_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare-name ``argv[0]`` is resolved against the HOST PATH before entering the sandbox.

    The sandbox carries a minimal ``PATH`` of its own, so a console script installed in a venv's
    ``bin`` (``agora-ollama-brain``, ``agora-cli-brain``) is readable inside but not FINDABLE — the
    launch fails and PASS 2 writes nothing. Resolving on the host, where the operator's PATH already
    selected the program, keeps the in-sandbox lookup out of the picture.
    """
    note_rel = "wiki/general/themes/probe.md"
    _note_with_sentinel(tmp_path, note_rel)
    monkeypatch.setattr(
        "agora_kb.curator.subprocess_backend.shutil.which",
        lambda name: f"/opt/venv/bin/{name}" if name == "agora-ollama-brain" else None,
    )
    spec = BackendSpec(name="brain", argv=("agora-ollama-brain",), network="none")
    iso = _RecordingIsolation()

    SubprocessBackend(spec, isolation=iso).author(
        tmp_path,
        {note_rel: ["c1"]},
        {"c1": AuthorRegion(op="CREATE_THEME", title="T", summary="s", source_text="x")},
    )

    assert iso.specs[0].argv[0] == "/opt/venv/bin/agora-ollama-brain"


def test_sandboxed_unresolvable_program_fails_loudly(tmp_path: Path) -> None:
    """An argv[0] that is on no PATH raises instead of degrading to a placeholder body."""
    note_rel = "wiki/general/themes/probe.md"
    _note_with_sentinel(tmp_path, note_rel)
    spec = BackendSpec(name="ghost", argv=("definitely-not-installed-brain",), network="none")

    with pytest.raises(BackendUnavailableError, match="not found on PATH"):
        SubprocessBackend(spec, isolation=_RecordingIsolation()).author(
            tmp_path,
            {note_rel: ["c1"]},
            {"c1": AuthorRegion(op="CREATE_THEME", title="T", summary="s", source_text="x")},
        )


# --- real seatbelt confinement (macOS) ----------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt confinement is macOS-only")
def test_author_confines_out_of_worktree_write_under_seatbelt(tmp_path: Path) -> None:
    """END-TO-END: under the REAL seatbelt sandbox, author()'s in-worktree write succeeds but an
    out-of-worktree write attempted by the same backend is DENIED (ADR-0013 G1)."""
    main = tmp_path / "main"
    main.mkdir()
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin",
    }

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), env=env, check=True, capture_output=True)

    git("init", "-q", cwd=main)
    git(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
        cwd=main,
    )
    wt = tmp_path / "wt"  # sibling of main → non-nested (ADR-0013 G4/G5)
    git("worktree", "add", "--detach", "-q", str(wt), "HEAD", cwd=main)

    note_rel = "wiki/general/themes/probe.md"
    note = _note_with_sentinel(wt, note_rel)
    start, end = body_sentinels("c1")

    # An absolute path OUTSIDE the worktree AND outside any sandbox tmp (its parent exists so the
    # ONLY barrier to the write is the sandbox, proving EPERM rather than a missing-dir error).
    escape_dir = tmp_path / "escape"
    escape_dir.mkdir()
    escape = escape_dir / "probe.txt"

    spec = BackendSpec(
        name="confined",
        argv=(sys.executable, "-c", _fill_then_escape_code(escape)),
        network="none",
    )
    iso = select_backend_isolation(allow_reduced_isolation=False)  # seatbelt on darwin
    assert iso.name == "seatbelt"
    backend = SubprocessBackend(spec, isolation=iso)

    region = AuthorRegion(op="CREATE_THEME", title="T", summary="s", source_text="x")
    backend.author(wt, {note_rel: ["c1"]}, {"c1": region})

    text = note.read_text(encoding="utf-8")
    assert "AUTHORED PROSE" in text[text.find(start) + len(start) : text.find(end)]  # inside OK
    assert not escape.exists()  # out-of-worktree write was DENIED by the sandbox
