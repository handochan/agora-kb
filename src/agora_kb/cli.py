"""``agora`` CLI entry point (DESIGN/ROADMAP Phase 1).

A dependency-light :mod:`argparse` front-end over the core API. Subcommands:

- ``agora repo init <path>`` — initialize a knowledge repo: ``Repo.init`` + emit the KB schema +
  taxonomy + a starter ``_kb/repo.yaml``, committed as one admin commit (the result lints clean).
- ``agora status [--repo PATH]`` — print inbox depth + curator state (last run/commit, counters).
- ``agora curate [--repo PATH] [--force]`` — recover any in-flight run, then execute ONE real
  consolidation run via :mod:`agora_kb.curator` ``worker`` against the configured ``adapters.yaml``
  backend, and print the resulting :class:`~agora_kb.curator.worker.RunReport`.
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

from .config import load_backend_registry, load_repo_config, write_default_repo_config
from .core import Inbox, Repo, RepoLayout, StateStore
from .curator import evaluate
from .curator.subprocess_backend import SubprocessBackend
from .curator.worker import recover, run
from .schema import Taxonomy, emit_schema, lint

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
    p_repo_init.add_argument(
        "--name", default=None, help="repo name written to _kb/repo.yaml (default: dir basename)"
    )
    p_repo_init.add_argument(
        "--kind",
        choices=("personal", "team"),
        default="personal",
        help="repo kind written to _kb/repo.yaml (DATA-MODEL §3; default: personal)",
    )
    p_repo_init.add_argument(
        "--domain",
        action="append",
        default=None,
        metavar="DOMAIN",
        help="an allowed taxonomy domain (repeatable; default: general)",
    )
    p_repo_init.add_argument(
        "--tag",
        action="append",
        default=None,
        metavar="TAG",
        help="an allowed taxonomy tag (repeatable; default: none)",
    )
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
    """``agora repo init <path>``: init git + emit schema/taxonomy + repo.yaml in one admin commit.

    The single ``now`` is injected into ``Repo.init`` and ``commit_all`` so the seed ``index.md``
    and the admin commit carry the SAME date (reproducible, no wall-clock drift, ADR-0010 D1). The
    emitted taxonomy's ``domains``/``allowed_tags`` come from ``--domain``/``--tag`` (defaulting to
    a single ``general`` domain and no tags), so the seed ``index.md`` (empty tags) lints clean. The
    init commit sha is printed last.

    IDEMPOTENT: re-running on an already-initialized repo re-emits the (idempotent) schema and the
    git-ignored ``_kb/repo.yaml`` without a new curated commit — there is nothing new to commit, so
    the existing HEAD is re-printed (matching ``Repo.init``'s own idempotency).
    """
    now = datetime.now(UTC)
    repo = Repo.resolve(args.path)
    layout = repo.layout

    domains = tuple(args.domain) if args.domain else ("general",)
    tags = tuple(args.tag) if args.tag else ()
    name = args.name or layout.root.name

    already = repo.is_initialized()
    repo.init(when=now)
    taxonomy = Taxonomy(
        schema_version=1, taxonomy_policy="open", allowed_tags=tags, domains=domains
    )
    # emit_schema + the starter repo.yaml are idempotent (existing curated files are left untouched;
    # _kb/repo.yaml is git-ignored), so a re-init produces no curated diff — only the first init's
    # admin commit advances the curated branch.
    emit_schema(layout, taxonomy=taxonomy)
    write_default_repo_config(layout, name=name, domains=domains, kind=args.kind)

    if already:
        sha = repo.head_commit()
    else:
        sha = repo.commit_all("chore: emit KB schema + repo config", when=now)

    # The freshly-initialized repo MUST lint clean (schema.lint ok) — a dashboard-style read (no
    # run_date) so only the structural rules apply. A non-clean result here is a setup bug, surfaced
    # rather than swallowed.
    result = lint(layout, taxonomy=taxonomy)
    if not result.ok:
        for finding in result.findings:
            print(f"  lint {finding.code} {finding.path}: {finding.message}", file=sys.stderr)
        print(f"{_PROG} repo init: emitted repo did not lint clean", file=sys.stderr)
        return 1

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
    """``agora curate``: recover in-flight runs, then run ONE real consolidation if due (or forced).

    Order (ADR-0011 §9 then §0): recover() first so any crashed run is finalized/returned before a
    fresh run; then evaluate the configured triggers (``--force`` overrides). When a run is due,
    load the ``adapters.yaml`` WRITE-adapter registry, build a :class:`SubprocessBackend` over the
    configured default brain, and execute :func:`agora_kb.curator.worker.run`. The integrity verdict
    is the worker's (the backend is outside the boundary), so this prints the resulting RunReport
    (status / published commit / per-op counts). A missing/absent ``adapters.yaml`` is a clear
    error, not a crash.
    """
    repo = Repo.resolve(args.repo)
    layout = repo.layout
    cfg = load_repo_config(layout)
    now = datetime.now(UTC)

    # ADR-0011 §9: finalize/return any in-flight run BEFORE deciding on a new one.
    for rep in recover(repo, state_store=StateStore(layout)):
        print(f"recovered: run={rep.run_id} status={rep.status} counts={rep.counts}")

    depth = Inbox(layout).depth()
    state = StateStore(layout).load()
    decision = evaluate(
        inbox_depth=depth,
        now=now,
        last_write=None,
        last_run=state.last_run,
        config=cfg.triggers,
        cron_due=False,
    )
    should_run = True if args.force else decision.should_run
    reason = "force" if args.force else decision.reason

    print(f"repo: {layout.root}")
    print(f"inbox depth: {depth}")
    print(f"should_run: {should_run}")
    print(f"reason: {reason}")

    if not should_run:
        print("note: no consolidation run was due; nothing was changed")
        return 0

    backend = _build_backend(layout, cfg.default_backend)
    if backend is None:
        return 1

    report = run(
        repo,
        backend=backend,
        state_store=StateStore(layout),
        now=now,
        taxonomy=cfg.taxonomy,
        max_attempts=cfg.max_attempts,
    )
    counts = ", ".join(f"{op}={n}" for op, n in sorted(report.counts.items())) or "-"
    print(f"status: {report.status}")
    print(f"published_commit: {report.published_commit or '-'}")
    print(f"counts: {counts}")
    return 0


def _build_backend(layout: RepoLayout, backend_name: str) -> SubprocessBackend | None:
    """Resolve the configured WRITE-adapter into a :class:`SubprocessBackend`, or print why not.

    Loads ``adapters.yaml`` (DATA-MODEL §8) from the repo root. Returns ``None`` (after printing a
    clear stderr message) when the file is absent (no brain configured) or the configured
    ``default_backend`` is not among its ``backends`` — so the caller exits non-zero instead of
    crashing. The actual missing-executable case surfaces later, at invocation, as a clear error.
    """
    adapters_path = layout.root / "adapters.yaml"
    registry = load_backend_registry(adapters_path)
    if registry is None:
        print(
            f"{_PROG} curate: no backend configured — create {adapters_path} with a 'backends:' "
            f"mapping and 'default_backend' (DATA-MODEL §8).",
            file=sys.stderr,
        )
        return None
    try:
        spec = registry.get(backend_name)
    except KeyError as exc:
        print(f"{_PROG} curate: {exc}", file=sys.stderr)
        return None
    return SubprocessBackend(spec)


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
