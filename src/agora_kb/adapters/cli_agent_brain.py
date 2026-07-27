"""Generic CLI-agent curator-brain WRITE-adapter — use ANY headless text CLI as a brain.

A capable headless CLI agent (``claude -p``, ``gemini -p``, ``codex exec`` …) is, for our
purposes, a text-in / text-out generator. This shim REUSES the Ollama brain's robust two-pass
pipeline (:func:`agora_kb.adapters.ollama_brain.run_plan` / ``run_author``): it reads the bundle FOR
the agent, asks ONLY for the semantic decision, then mechanically reshapes that decision into a
valid-by-construction plan (PASS 1) and fills the file's body-sentinel regions with sanitized prose
(PASS 2). The ONLY thing that changes versus the Ollama brain is the inference call: instead of an
Ollama HTTP request, the prompt is fed to a SUBPROCESS — the configured CLI run as a pure text
generator (stdin → stdout).

Why a text generator and not the agent's own file tools: using the CLI purely for text generation
needs NO file-read/-write tools, NO write access, and NO skip-permissions — so the brain needs no
elevated trust, and (as always) the worker re-grades every output deterministically OUTSIDE this
shim (ADR-0011 §4): a malformed or adversarial response is caught downstream, never trusted here.

Tool-agnostic by config (invariant 6 — never hard-code a tool/model): the registry's
``adapters.yaml`` provides the exact CLI argv to shell, after a ``--`` separator, e.g.::

    backends:
      claude:
        argv: [agora-cli-brain, --, claude, -p]
        network: loopback
      codex:
        argv: [agora-cli-brain, --, codex, exec, --skip-git-repo-check, --sandbox, read-only]
        network: loopback
      gemini:
        argv: [agora-cli-brain, --, gemini, -p, ""]
        network: loopback

This table is the prose rendering of :data:`KNOWN_CLI_AGENTS` below, and a test locks the two
together (they drifted once already — codex's flags lived only in ``docs/DATA-MODEL.md`` §8).

The configured CLI MUST read its prompt from stdin and print ONLY its text answer to stdout.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from agora_kb.adapters.ollama_brain import BrainError, detect_mode, run_author, run_plan

__all__ = ["CliAgentError", "KNOWN_CLI_AGENTS", "call_cli_agent", "main"]

# The CLI agents ADR-0016 documents as working brains: {backend name: full adapters.yaml argv}.
# ONE source of truth for this module's docstring table, DATA-MODEL.md §8, and `agora doctor`'s
# remediation hint (#96). A HINT registry ONLY — never routing: what actually runs is always the
# operator's adapters.yaml (invariant 6). An entry here changes NO behaviour beyond what doctor
# SUGGESTS when it finds that program already on PATH. The program to probe is ``argv[2]`` — the
# token right after the ``--`` separator.
KNOWN_CLI_AGENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claude", ("agora-cli-brain", "--", "claude", "-p")),
    (
        "codex",
        (
            "agora-cli-brain",
            "--",
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
        ),
    ),
    ("gemini", ("agora-cli-brain", "--", "gemini", "-p", "")),
)

# Per-invocation wall clock for the CLI agent. PASS-1 plan is a single completion; PASS-2 is one
# completion per region. Modest so a hung agent can't wedge a run; overridable via --agent-timeout.
_DEFAULT_TIMEOUT_S = 300.0


class CliAgentError(BrainError):
    """A CLI-agent invocation failed (missing executable, non-zero exit, timeout, or empty output).

    Subclasses :class:`~agora_kb.adapters.ollama_brain.BrainError` so the reused two-pass drivers
    treat it like an Ollama failure: PASS-1 fails the plan cleanly (the worker then rejects), PASS-2
    leaves the region unchanged (the worker's §4.2 gate degrades it).
    """


def call_cli_agent(prompt: str, *, argv: list[str], timeout: float) -> str:
    """Run ``argv`` as a text generator: feed ``prompt`` on stdin, return its stdout text.

    ``argv`` is a config-provided ARRAY (never a shell string), so there is no shell and no argv
    interpolation. A missing executable, a non-zero exit, a timeout, or empty stdout each becomes a
    :class:`CliAgentError` with an actionable message (the reused drivers handle it cleanly).

    The agent runs in a fresh THROWAWAY scratch directory, NOT the curator worktree: a real CLI
    agent (Claude Code, Gemini CLI, …) is a stateful process that writes session/state files
    (``.omc/``, ``.claude/`` …) into its cwd, which would otherwise pollute the worktree and trip
    the worker's FINAL-DIFF allowlist. The shim already read the bundle FOR the agent, so the agent
    needs no worktree access — it only generates text from the prompt on stdin.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="agora-cli-brain-") as scratch:
            proc = subprocess.run(  # noqa: S603 — argv is a config array, shell=False, no interp
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=scratch,
            )
    except FileNotFoundError as exc:
        raise CliAgentError(f"CLI agent executable not found: {argv[0]!r} ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise CliAgentError(f"CLI agent {argv[0]!r} timed out after {timeout:g}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:400] or "<no stderr>"
        raise CliAgentError(f"CLI agent {argv[0]!r} exited {proc.returncode}: {detail}")
    if not proc.stdout.strip():
        raise CliAgentError(f"CLI agent {argv[0]!r} produced no output")
    return proc.stdout


def _parse_args(argv: list[str] | None) -> tuple[argparse.Namespace, list[str]]:
    """Split our own options from the CLI argv that follows ``--`` (the agent to shell)."""
    parser = argparse.ArgumentParser(
        prog="agora-cli-brain",
        description="Generic CLI-agent curator-brain shim — any stdin→stdout text CLI as a brain.",
    )
    parser.add_argument(
        "--agent-timeout",
        type=float,
        default=_DEFAULT_TIMEOUT_S,
        help="per-invocation wall clock for the CLI agent in seconds (default 300)",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="brain label for debug dumps (default: the CLI program name)",
    )
    ns, rest = parser.parse_known_args(argv)
    # argparse leaves the literal ``--`` in ``rest``; drop a single leading one.
    if rest and rest[0] == "--":
        rest = rest[1:]
    return ns, rest


def main(argv: list[str] | None = None) -> int:
    """Entrypoint the registry shells: read stdin, dispatch on :func:`detect_mode`, exit 0/1.

    Mirrors :func:`agora_kb.adapters.ollama_brain.main` but drives the reused two-pass pipeline with
    a CLI-agent text-generator ``infer`` instead of Ollama. PLAN prints the normalized ``plan.json``
    to stdout (0), or a clear stderr message (1) the worker turns into a clean PLAN-parse failure;
    AUTHOR edits the worktree file in place (0), or returns 1 only on a TOTAL failure (no file).
    """
    ns, cli_argv = _parse_args(argv)
    if not cli_argv:
        print(
            "agora-cli-brain: no CLI agent given — expected `-- <cli> <args...>` "
            "(e.g. `agora-cli-brain -- claude -p`)",
            file=sys.stderr,
        )
        return 1

    label = ns.label or cli_argv[0]

    def infer(prompt: str) -> str:
        return call_cli_agent(prompt, argv=cli_argv, timeout=ns.agent_timeout)

    stdin_prompt = sys.stdin.read()
    cwd = Path.cwd()
    mode = detect_mode(stdin_prompt)

    if mode == "plan":
        try:
            print(run_plan(cwd, stdin_prompt, infer=infer, model_label=label))
        except BrainError as exc:
            print(f"agora-cli-brain (plan): {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        run_author(cwd, stdin_prompt, infer=infer, model_label=label, text_only=True)
    except BrainError as exc:
        print(f"agora-cli-brain (author): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
