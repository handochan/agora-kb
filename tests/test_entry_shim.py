"""Lock the ``agora`` console-script platform shim (issue #103).

``src/agora_kb/_entry.py`` is a deliberately temporary line of defense: until #86 lands the
``fcntl`` platform seam, native Windows cannot import ``agora_kb.cli`` at all, and the console
script's first act must therefore be a check that runs *before* that import. These tests pin the
three properties that make it work, because each one is invisible to a reader of the diff and
silently reversible by a well-meaning edit:

1. **Order.** The refusal happens before ``agora_kb.cli`` is imported. A shim that checks the
   platform *after* the import chain has already run is not a shim at all — it is the original
   ``ModuleNotFoundError`` with extra steps. Asserted twice: structurally in-process
   (:func:`test_windows_refusal_never_imports_the_cli`) and end-to-end in a clean interpreter with
   ``fcntl`` genuinely unimportable (:func:`test_missing_fcntl_still_yields_the_friendly_failure`),
   which is the only form that proves the real Windows failure mode is covered.
2. **Narrowness.** Only ``win32`` is refused. An over-broad check (an allowlist of "darwin or
   linux", ``os.name == "nt"`` and friends) would turn a courtesy into a portability regression for
   every other POSIX platform that ships ``fcntl``.
3. **Stdlib-only module scope.** The shim may not import ``agora_kb`` anything at module scope, or
   it dies in the exact way it exists to prevent. Asserted over the file's AST, not left to a
   comment.

The wiring itself (``[project.scripts] agora``) is asserted too: a perfect shim that no console
script points at ships nothing.

When #86 lands, this file is deleted along with the module it guards — that is stated in
``_entry.py`` and in #86's acceptance criteria, and
:func:`test_removal_condition_is_recorded_in_code` keeps the in-code half of that promise from
being edited away.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from agora_kb import _entry

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRY_PATH = REPO_ROOT / "src" / "agora_kb" / "_entry.py"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

# Every module that must NOT be imported by a refused invocation. `fcntl` is the module that does
# not exist on Windows; the other three are the chain that reaches it (`cli` -> `curator` ->
# `curator.claim` -> `fcntl`).
POISONED_CHAIN = ("agora_kb.cli", "agora_kb.curator", "agora_kb.curator.claim", "fcntl")


def _run_shim_on_windows(monkeypatch: pytest.MonkeyPatch, argv: list[str] | None = None) -> int:
    """Call the shim with the interpreter pretending to be native Windows."""
    monkeypatch.setattr(sys, "platform", "win32")
    return _entry.main([] if argv is None else argv)


def test_windows_returns_the_platform_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_shim_on_windows(monkeypatch) == 2
    assert _entry.PLATFORM_EXIT_CODE == 2


def test_windows_guidance_goes_to_stderr_with_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """stderr carries the guidance; stdout stays empty and nothing looks like a crash."""
    _run_shim_on_windows(monkeypatch)
    captured = capsys.readouterr()

    assert captured.out == ""
    lines = captured.err.strip().splitlines()
    assert len(lines) == 3, f"the guidance is specified as three lines, got {len(lines)}"
    assert "Traceback" not in captured.err
    assert "Error" not in captured.err
    # It must name itself: a bare sentence in a shell scrollback belongs to nobody.
    assert lines[0].startswith("agora:")


def test_windows_guidance_carries_the_three_required_facts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(a) what IS supported, (b) WSL2 is unverified, (c) where the port is tracked.

    Substring assertions on prose are usually brittle, but here the prose *is* the deliverable —
    the acceptance criteria enumerate these three facts, and the whole issue is that a Windows user
    has no other channel. The assertions target the load-bearing tokens, not the sentences around
    them, so the wording can still be improved.
    """
    _run_shim_on_windows(monkeypatch)
    err = capsys.readouterr().err

    assert "macOS" in err and "Linux" in err
    assert "Windows" in err
    assert "WSL2" in err
    # Unverified must be stated, not hinted: the repo has zero WSL2 evidence, so a bare mention
    # would read as an endorsement.
    assert "UNVERIFIED" in err
    assert "https://github.com/handochan/agora-kb/issues/85" in err


def test_windows_refusal_never_imports_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordering lock: `agora_kb.cli` must not enter `sys.modules` on the refusal path.

    `agora_kb.cli` is imported by most of this suite, so the test cannot simply assert its absence
    — it evicts the module first (monkeypatch restores the *original* object at teardown, so no
    other test observes a re-imported duplicate) and asserts the call did not bring it back.
    """
    monkeypatch.delitem(sys.modules, "agora_kb.cli", raising=False)
    assert _run_shim_on_windows(monkeypatch) == 2
    assert "agora_kb.cli" not in sys.modules


@pytest.mark.parametrize("platform", ["darwin", "linux", "linux2", "freebsd14", "cygwin"])
def test_every_platform_but_win32_reaches_the_real_cli(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """Narrowness: the check refuses `win32` and nothing else.

    A check that over-matches is worse than no check — it refuses a platform that works, and the
    user has no way to override it.
    """
    import agora_kb.cli as cli_mod

    seen: list[list[str] | None] = []
    monkeypatch.setattr(cli_mod, "main", lambda argv=None: (seen.append(argv), 7)[1])
    monkeypatch.setattr(sys, "platform", platform)

    assert _entry.main(["doctor"]) == 7
    assert seen == [["doctor"]], "argv must reach the CLI untouched"


def test_argv_none_is_passed_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """`None` is argparse's "read sys.argv" sentinel — the console script always sends it."""
    import agora_kb.cli as cli_mod

    seen: list[object] = []
    monkeypatch.setattr(cli_mod, "main", lambda argv=None: (seen.append(argv), 0)[1])
    monkeypatch.setattr(sys, "platform", "darwin")

    assert _entry.main() == 0
    assert seen == [None], "the shim must not substitute a list for None"


# ---------------------------------------------------------------------------------------------
# Structural locks: the shim's shape, not its behaviour.
# ---------------------------------------------------------------------------------------------


def _module_scope_import_roots(tree: ast.Module) -> list[str]:
    """Top-level (module-scope) import roots only — imports inside functions are the point."""
    roots: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import can only be agora_kb itself
                roots.append(f".{node.module or ''}")
            elif node.module:
                roots.append(node.module.split(".")[0])
    return roots


def test_module_scope_imports_are_stdlib_only() -> None:
    """The shim's single design constraint, asserted against the file rather than a convention.

    Any `agora_kb` import at module scope pulls the `fcntl` chain in before the check can run, and
    the failure it reintroduces is precisely the one this module exists to prevent — a regression
    that no behavioural test on a POSIX machine can see.
    """
    roots = _module_scope_import_roots(ast.parse(ENTRY_PATH.read_text(encoding="utf-8")))

    assert roots, "expected at least `sys`"
    non_stdlib = [root for root in roots if root not in sys.stdlib_module_names]
    assert not non_stdlib, f"_entry.py must import only the stdlib at module scope: {non_stdlib}"


def test_the_cli_import_lives_inside_main() -> None:
    """The counterpart to the rule above: the deferred import must actually be there."""
    tree = ast.parse(ENTRY_PATH.read_text(encoding="utf-8"))
    main_fn = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    deferred = [
        node
        for node in ast.walk(main_fn)
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "cli"
    ]
    assert len(deferred) == 1, "main() must import .cli exactly once, lazily"


def test_console_script_points_at_the_shim() -> None:
    """A shim nothing points at ships nothing — this is the whole wiring of the feature."""
    scripts = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["agora"] == "agora_kb._entry:main"


def test_removal_condition_is_recorded_in_code() -> None:
    """The disposal note is part of the deliverable (#103 acceptance criterion 7).

    A temporary defense with no removal trigger written on it becomes permanent by default; this
    keeps the trigger attached to the code that has to go.
    """
    source = ENTRY_PATH.read_text(encoding="utf-8")
    assert "#86" in source
    assert "REMOVE WITH #86" in source


# ---------------------------------------------------------------------------------------------
# End-to-end: a clean interpreter where `fcntl` genuinely cannot be imported.
# ---------------------------------------------------------------------------------------------

# Runs in a child process: fake the platform, make `fcntl` unimportable the way win32 CPython
# does, then invoke the shim exactly as the console script would.
_SIMULATE_WINDOWS = """\
import sys


class _NoFcntl:
    def find_spec(self, name, path=None, target=None):
        if name == "fcntl":
            raise ModuleNotFoundError("No module named 'fcntl'", name="fcntl")
        return None


sys.meta_path.insert(0, _NoFcntl())
sys.platform = {platform!r}

from agora_kb._entry import main

rc = main([{argv}])
print("IMPORTED:" + ",".join(m for m in {chain!r} if m in sys.modules))
sys.exit(rc)
"""


def _simulate(platform: str, argv: str = "") -> subprocess.CompletedProcess[str]:
    script = _SIMULATE_WINDOWS.format(platform=platform, argv=argv, chain=POISONED_CHAIN)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )


def test_missing_fcntl_still_yields_the_friendly_failure() -> None:
    """The real thing: `fcntl` absent, platform `win32`, nothing pre-imported.

    In-process tests can only prove the shim does not import the CLI *when the import would have
    worked*. This proves the shim never needs it — the chain is unreachable and the process still
    exits 2 with the guidance.
    """
    result = _simulate("win32")

    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "WSL2" in result.stderr
    assert "issues/85" in result.stderr
    assert "IMPORTED:\n" in result.stdout, f"the poisoned chain was imported: {result.stdout!r}"


def test_the_simulation_is_not_vacuous() -> None:
    """Control: with the same `fcntl` block but a supported platform, the child MUST fail.

    Without this, `test_missing_fcntl_still_yields_the_friendly_failure` would keep passing if the
    meta-path block silently stopped working — it would be asserting nothing at all.
    """
    result = _simulate("darwin", argv="'--help'")

    assert result.returncode != 2
    assert "No module named 'fcntl'" in result.stderr
