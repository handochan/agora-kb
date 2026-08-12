"""The ``agora`` console-script shim: a platform check that runs BEFORE the import that fails.

**This file is a temporary line of defense and its deletion is part of #86's definition of done.**
See :func:`_platform_supported` for the exact removal condition.

Why it exists (issue #103). On native Windows ``agora`` does not fail at argument parsing, it fails
at *import*: ``curator/claim.py`` imports :mod:`fcntl` at module scope (POSIX-only),
``curator/__init__.py`` re-exports from it, and ``cli.py`` imports ``.curator`` at module scope — so
even ``agora --help`` dies with ``ModuleNotFoundError: No module named 'fcntl'`` and a traceback
before argparse ever sees an argument. A traceback reads as *"this package is broken"*, not as
*"this platform is not supported yet"*, and the beta invites dogfooders who have no other way to
tell the two apart: the run-time message is the only guidance channel a Windows user reaches.

Two constraints hold this file's shape. Both are load-bearing, and both are locked by
``tests/test_entry_shim.py`` rather than left as a convention:

* **Module scope imports the standard library and nothing else.** Any ``agora_kb`` import here
  would pull in the very chain the check exists to get ahead of, and the shim would die exactly the
  way it is meant to prevent. (Importing this module does execute the package's
  ``agora_kb/__init__.py`` parent — which is deliberately a docstring plus a ``__version__``
  literal, with no imports of its own. Keeping *that* file side-effect-free is what makes a check
  in *this* one reachable at all, and is the reason the check does not live there: a library import
  must not be able to exit a process.)
* **The ``.cli`` import lives inside** :func:`main`, **after** the check. Moving it to module scope
  restores the original bug in full.

Scope. ``[project.scripts]`` also ships ``agora-ollama-brain`` and ``agora-cli-brain``, which are
NOT wrapped. Those are the curator's internal subprocess interface — it shells them itself, with
argv it builds from ``adapters.yaml`` — so they are never a user's first contact with the project,
and on a platform where the curator cannot run at all they are unreachable by construction. The
guidance belongs on the one command a person actually types.
"""

from __future__ import annotations

import sys

#: Exit code for "this platform cannot run agora at all".
#:
#: 2 is the code ``cli.main`` already returns when the invocation cannot proceed as given (no
#: subcommand, unknown subcommand — argparse's own usage-error convention), and this is the same
#: class of failure: no choice of arguments makes the command work, so it is a usage statement
#: about the environment rather than a runtime error. It is deliberately NOT 1, which across this
#: CLI means "the command ran and reported a problem" (``doctor`` unhealthy, a failed run, …).
PLATFORM_EXIT_CODE = 2

#: Exactly three facts, in the order a blocked user needs them. Each is something the repository
#: can actually back:
#:
#: (a) macOS and Linux are what runs — the two CI legs that gate a release and the only two OS
#:     sandboxes that exist (ADR-0013, ``curator/isolation/``);
#: (b) WSL2 is *unverified*, said plainly. The repo contains no WSL2 test, CI job, or doc, so
#:     "it probably works there" would be a guess dressed as advice — the one sentence a support
#:     channel cannot afford. Naming it at all still helps: it is the workaround a reader would
#:     otherwise go looking for, and this tells them what they would be buying.
#: (c) where the real port is tracked, as a URL rather than a bare ``#85`` that a terminal cannot
#:     resolve into anything clickable.
#:
#: What is NOT here is as deliberate: no stack frame, no "please report this", no install
#: instructions. Nothing the user can do to this machine fixes it.
_GUIDANCE: tuple[str, ...] = (
    "agora: native Windows is not supported yet — a platform limit, not a broken install.",
    (
        "Supported today: macOS and Linux. WSL2 may work but is UNVERIFIED — this project has "
        "never been tested there."
    ),
    "Native Windows support is in progress: https://github.com/handochan/agora-kb/issues/85",
)


def _platform_supported() -> bool:
    """Whether the interpreter's platform can import the CLI at all.

    **REMOVE WITH #86.** When the ``fcntl`` platform seam lands (``core/filelock.py``, the
    ``msvcrt`` locking path, and ``claim.py`` ported onto it), native Windows imports and this
    check becomes a lie the CLI tells its users — a refusal for a platform that works. The removal
    is three edits: delete this module, point ``[project.scripts] agora`` back at
    ``agora_kb.cli:main``, and delete ``tests/test_entry_shim.py``. #86's acceptance criteria name
    that deletion so this defense cannot outlive the defect.

    The test is exactly ``sys.platform == "win32"`` and stays that narrow on purpose. The failure
    mode to avoid is a check that over-matches and refuses a platform that would have run: an
    allowlist ("darwin or linux") would reject the BSDs, Cygwin, and anything else shipping a POSIX
    ``fcntl``, turning a Windows courtesy into a portability regression. ``os.name == "nt"`` names
    the same set today but through a coarser handle. So: refuse the one platform known to lack the
    module, let everything else proceed to the real import and fail honestly if it must.
    """
    return sys.platform != "win32"


def main(argv: list[str] | None = None) -> int:
    """``[project.scripts] agora`` — check the platform, then hand off to the real CLI.

    ``argv`` is passed through untouched (``None`` means "read ``sys.argv``", argparse's own
    contract), so on a supported platform this function is transparent: same parser, same exit
    codes, same behaviour for every subcommand, including ``--version``'s ``SystemExit(0)`` raised
    from inside ``parse_args``, which propagates through here as it always did.
    """
    if not _platform_supported():
        for line in _GUIDANCE:
            print(line, file=sys.stderr)
        return PLATFORM_EXIT_CODE

    # The first touch of the fcntl chain, and only ever after the check above.
    from .cli import main as _cli_main

    return _cli_main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
