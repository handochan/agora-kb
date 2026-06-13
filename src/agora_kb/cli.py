"""``agora`` CLI entry point (DESIGN/ROADMAP Phase 1).

A dependency-light :mod:`argparse` front-end over the core API. Subcommands:

- ``agora repo init <path>`` — initialize a knowledge repo (``Repo.init``) and print its commit.
- ``agora status [--repo PATH]`` — print inbox depth + curator state (last run/commit, counters).
- ``agora curate [--repo PATH] [--force]`` — evaluate the consolidation triggers and print the
  decision. The real consolidation run is :mod:`agora_kb.curator` ``worker`` (pending).
- ``agora serve [--repo PATH] [--writer W]`` — run the MCP stdio server face. The face is imported
  lazily so the rest of the CLI works even when an MCP transport dependency is missing.
- ``agora doctor`` — print a health report (git, python, key deps, repo init state).

The MCP face is imported lazily (only inside ``serve``) on purpose: it keeps ``repo init`` /
``status`` / ``curate`` / ``doctor`` usable in environments where the transport stack is not
installed (invariant 4: every component has an OSS path, optional pieces stay optional).
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from .core import Inbox, Repo, RepoLayout, StateStore
from .curator import TriggerConfig, evaluate

__all__ = ["main", "build_parser"]

_PROG = "agora"


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with one subparser per command."""
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Agora — markdown + git shared-memory hub for AI agents.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # repo (group) — currently only `repo init`.
    p_repo = sub.add_parser("repo", help="manage a knowledge repo")
    repo_sub = p_repo.add_subparsers(dest="repo_command", metavar="<subcommand>")
    p_repo_init = repo_sub.add_parser("init", help="initialize a knowledge repo")
    p_repo_init.add_argument("path", help="repo root to initialize")
    p_repo_init.set_defaults(func=_cmd_repo_init)
    p_repo.set_defaults(func=_cmd_repo_missing)

    # status
    p_status = sub.add_parser("status", help="show inbox depth + curator state")
    p_status.add_argument("--repo", default=".", help="repo root (default: .)")
    p_status.set_defaults(func=_cmd_status)

    # curate
    p_curate = sub.add_parser("curate", help="evaluate consolidation triggers")
    p_curate.add_argument("--repo", default=".", help="repo root (default: .)")
    p_curate.add_argument(
        "--force", action="store_true", help="treat the run as due regardless of triggers"
    )
    p_curate.set_defaults(func=_cmd_curate)

    # serve
    p_serve = sub.add_parser("serve", help="run the MCP stdio server face")
    p_serve.add_argument("--repo", default=".", help="repo root (default: .)")
    p_serve.add_argument("--writer", default=None, help="writer name for captures")
    p_serve.set_defaults(func=_cmd_serve)

    # doctor
    p_doctor = sub.add_parser("doctor", help="print a health report")
    p_doctor.add_argument("--repo", default=".", help="repo root to check (default: .)")
    p_doctor.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (wired as ``[project.scripts] agora``).

    Returns a process exit code. With no subcommand (or an unknown one) the parser help is written
    to stderr and ``2`` is returned.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 2
    return func(args)


# --- commands -----------------------------------------------------------------------------------
def _cmd_repo_init(args: argparse.Namespace) -> int:
    repo = Repo.resolve(args.path)
    sha = repo.init()
    print(sha)
    return 0


def _cmd_repo_missing(args: argparse.Namespace) -> int:
    # `agora repo` with no subcommand: usage to stderr, exit 2 (mirrors the no-command path).
    print(f"{_PROG} repo: missing subcommand (try 'repo init <path>')", file=sys.stderr)
    return 2


def _cmd_status(args: argparse.Namespace) -> int:
    layout = RepoLayout(Path(args.repo))
    depth = Inbox(layout).depth()
    state = StateStore(layout).load()
    c = state.counters
    print(f"repo: {layout.root}")
    print(f"inbox depth: {depth}")
    print(f"last_run: {_fmt_dt(state.last_run)}")
    print(f"last_commit: {state.last_commit or '-'}")
    print(
        f"counters: ingested={c.ingested} merged={c.merged} dropped={c.dropped} failed={c.failed}"
    )
    return 0


def _cmd_curate(args: argparse.Namespace) -> int:
    layout = RepoLayout(Path(args.repo))
    depth = Inbox(layout).depth()
    state = StateStore(layout).load()
    config = TriggerConfig()
    decision = evaluate(
        inbox_depth=depth,
        now=datetime.now(UTC),
        last_write=None,
        last_run=state.last_run,
        config=config,
        cron_due=False,
    )
    # --force = operator override: run regardless of the trigger policy. We still print the
    # underlying policy decision so the operator can see what *would* have happened.
    should_run = True if args.force else decision.should_run
    reason = "force" if args.force else decision.reason
    print(f"repo: {layout.root}")
    print(f"inbox depth: {depth}")
    print(f"should_run: {should_run}")
    print(f"reason: {reason}")
    print("note: consolidation run is curator.worker (pending); no changes were made")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    # Lazy import: keep `repo init` / `status` / `curate` / `doctor` usable even when the MCP
    # transport stack is not installed (the face is an optional surface, invariant 4).
    try:
        mcp_server = importlib.import_module("agora_kb.faces.mcp_server")
    except ImportError as exc:  # pragma: no cover - exercised manually, not in tests
        print(
            f"{_PROG} serve: MCP face unavailable ({exc}); "
            f"install the server dependencies (fastmcp).",
            file=sys.stderr,
        )
        return 1
    # build_server is keyword-only: build_server(*, repo_path, writer=DEFAULT_WRITER).
    # Only forward --writer when the operator set it, so build_server's own default ('local')
    # applies otherwise (argparse default is None, which would otherwise null out the identity).
    kwargs: dict[str, object] = {"repo_path": Path(args.repo)}
    if args.writer is not None:
        kwargs["writer"] = args.writer
    server = mcp_server.build_server(**kwargs)
    server.run()  # pragma: no cover - blocking stdio loop, never run in tests
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    print("agora doctor")

    git_path = shutil.which("git")
    if git_path:
        print(f"  git: ok ({git_path})")
    else:
        print("  git: MISSING (required for the curated source of truth)")
        ok = False

    v = sys.version_info
    py_ok = (v.major, v.minor) >= (3, 12)
    print(f"  python: {v.major}.{v.minor}.{v.micro} ({'ok' if py_ok else 'need >= 3.12'})")
    if not py_ok:
        ok = False

    # Key deps: pydantic is core (hard requirement); fastmcp/yaml are surface/config helpers.
    for dep, required in (("pydantic", True), ("fastmcp", False), ("yaml", False)):
        if _can_import(dep):
            print(f"  dep {dep}: ok")
        else:
            label = "MISSING" if required else "missing (optional)"
            print(f"  dep {dep}: {label}")
            if required:
                ok = False

    layout = RepoLayout(Path(args.repo))
    if Repo(layout).is_initialized():
        print(f"  repo {layout.root}: initialized")
    else:
        print(f"  repo {layout.root}: not initialized (run 'agora repo init')")

    print(f"status: {'healthy' if ok else 'unhealthy'}")
    return 0 if ok else 1


# --- helpers ------------------------------------------------------------------------------------
def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "never"
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _can_import(module: str) -> bool:
    try:
        importlib.import_module(module)
    except ImportError:
        return False
    return True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
