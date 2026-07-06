"""``agora`` CLI entry point (DESIGN/ROADMAP Phase 1).

A dependency-light :mod:`argparse` front-end over the core API. Subcommands:

- ``agora repo init <path>`` — initialize a knowledge repo: ``Repo.init`` + emit the KB schema +
  taxonomy + a starter ``_kb/repo.yaml``, committed as one admin commit (the result lints clean).
- ``agora status [--repo PATH]`` — print inbox depth + curator state (last run/commit, counters).
- ``agora curate [--repo PATH] [--force]`` — recover any in-flight run, then execute ONE real
  consolidation run via :mod:`agora_kb.curator` ``worker`` against the configured ``adapters.yaml``
  backend, and print the resulting :class:`~agora_kb.curator.worker.RunReport`.
- ``agora watch [--repo PATH] [--interval N] [--once]`` — the in-process scheduler: loop, evaluating
  the cron + threshold + idle triggers each tick and consolidating when due (``--once`` for a single
  evaluation, e.g. driven by an external system cron / launchd).
- ``agora serve [--repo PATH] [--writer W]`` — run the MCP stdio server face. The face is imported
  lazily so the rest of the CLI works even when an MCP transport dependency is missing.
- ``agora web [--repo PATH] [--host H] [--port P] [--writer W] [--user U]`` — run the FastAPI + HTMX
  web face (browse / search / upload; ADR-0019). Imported lazily (the optional ``web`` extra), so a
  missing fastapi gives a clean ``install agora-kb[web]`` message, not an import error.
- ``agora doctor`` — print a health report (git, python, key deps, repo init state).

The MCP and web faces are imported lazily (only inside ``serve`` / ``web``) on purpose: it keeps
``repo init`` / ``status`` / ``curate`` / ``doctor`` usable in environments where the transport /
web stack is not installed (invariant 4: every component has an OSS path, optional pieces stay
optional).
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import (
    ConfigError,
    load_backend_registry,
    load_connector_specs,
    load_harvest_policy,
    load_index_policy,
    load_redact_policy,
    load_repo_config,
    write_default_adapters_yaml,
    write_default_repo_config,
)
from .core import Inbox, Repo, RepoLayout, StateStore
from .core.repo import GitError
from .curator import evaluate
from .curator.constants import DEFAULT_BODY_BYTE_BOUND
from .curator.cron import is_cron_due
from .curator.isolation import SandboxUnavailable, select_backend_isolation
from .curator.subprocess_backend import RoutedBackend, SubprocessBackend, build_routed_backend
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

    # import — the opt-in Obsidian/markdown vault normalizer (ADR-0014 D5).
    p_import = sub.add_parser(
        "import", help="normalize an external Obsidian/markdown vault into a new Agora repo"
    )
    p_import.add_argument("src", help="source vault to read (NEVER modified)")
    p_import.add_argument("dest", help="destination repo to write (created)")
    p_import.add_argument(
        "--domain",
        action="append",
        default=None,
        metavar="DOMAIN",
        help="a destination taxonomy domain (repeatable; the FIRST is the move target; "
        "default: general)",
    )
    p_import.add_argument(
        "--tag",
        action="append",
        default=None,
        metavar="TAG",
        help="an allowed destination taxonomy tag (repeatable; source tags outside this set are "
        "stripped + reported; default: none)",
    )
    p_import.set_defaults(func=_cmd_import)

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
    p_curate.add_argument(
        "--backend",
        default=None,
        metavar="NAME",
        help="pin BOTH cognitive acts to this adapters.yaml backend, bypassing routing (ADR-0015)",
    )
    p_curate.set_defaults(func=_cmd_curate)

    # harvest — scan configured memory connectors into gated candidates (ADR-0007; opt-in).
    p_harvest = sub.add_parser(
        "harvest",
        help="scan configured memory connectors into gated candidates (ADR-0007; opt-in)",
    )
    p_harvest.add_argument("--repo", default=".", help="repo root (default: .)")
    p_harvest.add_argument(
        "--connector",
        default=None,
        metavar="NAME",
        help="scan only this connector (default: all configured connectors)",
    )
    p_harvest.add_argument(
        "--dry-run",
        action="store_true",
        help="report what WOULD be harvested without writing to the inbox or advancing any cursor",
    )
    p_harvest.set_defaults(func=_cmd_harvest)

    # index (group) — the ADR-0012 §2 derived query reader cache (build/status/clear). Issue #26.
    p_index = sub.add_parser(
        "index", help="manage the derived query reader cache (_kb/index/, ADR-0012 §2)"
    )
    index_sub = p_index.add_subparsers(dest="index_command", metavar="<subcommand>")
    p_index_build = index_sub.add_parser(
        "build", help="(re)build the reader cache from the curated markdown"
    )
    p_index_build.add_argument("--repo", default=".", help="repo root (default: .)")
    p_index_build.set_defaults(func=_cmd_index_build)
    p_index_status = index_sub.add_parser(
        "status", help="show cache presence + freshness vs the curated tip"
    )
    p_index_status.add_argument("--repo", default=".", help="repo root (default: .)")
    p_index_status.set_defaults(func=_cmd_index_status)
    p_index_clear = index_sub.add_parser(
        "clear", help="remove the cache artifacts (rebuilt on the next build / curate)"
    )
    p_index_clear.add_argument("--repo", default=".", help="repo root (default: .)")
    p_index_clear.set_defaults(func=_cmd_index_clear)
    p_index.set_defaults(func=_cmd_index_missing)

    # gold (group) — the ADR-0027 derived context packs (build/status). Issue #37.
    p_gold = sub.add_parser("gold", help="manage derived gold context packs (_kb/gold/, ADR-0027)")
    gold_sub = p_gold.add_subparsers(dest="gold_command", metavar="<subcommand>")
    p_gold_build = gold_sub.add_parser("build", help="(re)build a gold pack from the curated wiki")
    p_gold_build.add_argument("--repo", default=".", help="repo root (default: .)")
    p_gold_build.add_argument("--pack", default="default", help="pack name (default: default)")
    p_gold_build.add_argument(
        "--check",
        action="store_true",
        help="verify the on-disk pack byte-matches a fresh rebuild (CI); write nothing",
    )
    p_gold_build.set_defaults(func=_cmd_gold_build)
    p_gold_status = gold_sub.add_parser(
        "status", help="show pack presence + freshness vs the curated tip"
    )
    p_gold_status.add_argument("--repo", default=".", help="repo root (default: .)")
    p_gold_status.add_argument("--pack", default="default", help="pack name (default: default)")
    p_gold_status.set_defaults(func=_cmd_gold_status)
    p_gold.set_defaults(func=_cmd_gold_missing)

    # watch — the in-process scheduler loop (cron + threshold + idle).
    p_watch = sub.add_parser(
        "watch", help="run the curator scheduler loop (cron + threshold + idle triggers)"
    )
    p_watch.add_argument("--repo", default=".", help="repo root (default: .)")
    p_watch.add_argument(
        "--interval",
        type=int,
        default=60,
        help="seconds between trigger evaluations (default: 60)",
    )
    p_watch.add_argument(
        "--once", action="store_true", help="evaluate the triggers once and exit (no loop)"
    )
    p_watch.set_defaults(func=_cmd_watch)

    # serve
    p_serve = sub.add_parser("serve", help="run the MCP stdio server face")
    p_serve.add_argument("--repo", default=".", help="repo root (default: .)")
    p_serve.add_argument("--writer", default=None, help="writer name for captures")
    p_serve.set_defaults(func=_cmd_serve)

    # web — the FastAPI + HTMX web face (browse / search / upload; ADR-0019). Optional `web` extra.
    p_web = sub.add_parser("web", help="run the web face (FastAPI + HTMX; needs the 'web' extra)")
    p_web.add_argument("--repo", default=".", help="repo root (default: .)")
    p_web.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    p_web.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    p_web.add_argument("--writer", default="web", help="inbox writer namespace (default: web)")
    p_web.add_argument(
        "--user", default="local", help="identity stamped into source=web:<user> (default: local)"
    )
    p_web.set_defaults(func=_cmd_web)

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
    # Wire the OSS default brain so the fresh repo is IMMEDIATELY curate-able (idempotent +
    # non-destructive: an existing adapters.yaml is left untouched). adapters.yaml lives at the
    # repo root and is operator-facing registry config, not part of the curated admin commit.
    adapters_path = write_default_adapters_yaml(layout)

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

    # The adapters registry path is informational init output (the brain wiring), printed to stderr
    # so stdout stays the single machine-parseable admin-commit sha (the established init contract).
    print(f"adapters: {adapters_path}", file=sys.stderr)
    print(sha)
    return 0


def _cmd_repo_missing(args: argparse.Namespace) -> int:
    # `agora repo` with no subcommand: usage to stderr, exit 2 (mirrors the no-command path).
    print(f"{_PROG} repo: missing subcommand (try 'repo init <path>')", file=sys.stderr)
    return 2


def _cmd_import(args: argparse.Namespace) -> int:
    """``agora import <src> <dest>``: normalize an external vault into a new Agora repo (ADR-0014).

    The opt-in Obsidian/markdown vault NORMALIZER. ``src`` is read NON-DESTRUCTIVELY; a normalized,
    closer-to-ADR-0010-conformant repo is written to ``dest`` plus a human report. ``import_date``
    is derived HERE from ``datetime.now(UTC)`` (the CLI boundary owns the wall clock) and injected
    into :func:`~agora_kb.ingest.vault_import.import_vault`, which stays a pure function of its
    inputs (ADR-0010 D1). Domains default to ``general`` (the first is the move target for
    off-layout notes); tags default to none. Prints a concise digest — the counts, each note's
    warnings, and the final lint summary — and exits non-zero ONLY on a HARD error (e.g. ``src``
    missing), never on report warnings (a best-effort import with findings is a success; ADR-0014).
    """
    from .ingest.vault_import import import_vault

    import_date = datetime.now(UTC).strftime("%Y-%m-%d")
    domains = list(args.domain) if args.domain else ["general"]
    tags = list(args.tag) if args.tag else []

    try:
        report = import_vault(
            Path(args.src),
            Path(args.dest),
            domains=domains,
            import_date=import_date,
            tags=tags,
        )
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(f"{_PROG} import: {exc}", file=sys.stderr)
        return 1

    s = report.summary
    print(f"imported {s['notes']} note(s) from {args.src} -> {args.dest}")
    print(
        f"repaired_frontmatter={s['repaired_frontmatter']} moved={s['moved']} "
        f"converted_links={s['converted_links']} unresolved_links={s['unresolved_links']} "
        f"stripped_tags={s['stripped_tags']} themes_without_sources={s['themes_without_sources']}"
    )
    for note in report.notes:
        if note.warnings or note.stripped_tags or note.unresolved_links:
            print(f"  {note.rel_path} ({note.type_inferred}):")
            for w in note.warnings:
                print(f"    - {w}")
            if note.stripped_tags:
                print(f"    - stripped tags: {', '.join(note.stripped_tags)}")
            if note.unresolved_links:
                print(f"    - unresolved links: {', '.join(note.unresolved_links)}")

    lr = report.lint
    if lr.ok:
        print("lint: clean")
    else:
        print(f"lint: {len(lr.findings)} finding(s) still need hands:")
        for finding in lr.findings:
            print(f"  {finding.code} {finding.path}: {finding.message}")
    return 0


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

    inbox = Inbox(layout)
    depth = inbox.depth()
    state = StateStore(layout).load()
    # The cron schedule is now evaluated for real (ADR-0010 D1 / DESIGN §4): a bare ``agora curate``
    # invoked by an external scheduler (system cron / launchd / `agora watch`) consolidates only
    # when the cron is DUE with a backlog — replacing the previous hardcoded ``cron_due=False``.
    decision = evaluate(
        inbox_depth=depth,
        now=now,
        last_write=inbox.last_write(),
        last_run=state.last_run,
        config=cfg.triggers,
        cron_due=is_cron_due(cfg.triggers.cron, now=now, last_run=state.last_run),
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

    backend = _build_backend(
        layout,
        default_backend=cfg.default_backend,
        override=args.backend,
        allow_reduced_isolation=cfg.allow_reduced_isolation,
        body_byte_bound=cfg.body_byte_bound,
    )
    if backend is None:
        return 1

    report = run(
        repo,
        backend=backend,
        state_store=StateStore(layout),
        now=now,
        taxonomy=cfg.taxonomy,
        max_attempts=cfg.max_attempts,
        related_k=cfg.related_k,
        max_orphans=cfg.max_orphans,
    )
    counts = ", ".join(f"{op}={n}" for op, n in sorted(report.counts.items())) or "-"
    print(f"status: {report.status}")
    print(f"published_commit: {report.published_commit or '-'}")
    print(f"counts: {counts}")
    return 0


def _build_backend(
    layout: RepoLayout,
    *,
    default_backend: str | None = None,
    override: str | None = None,
    allow_reduced_isolation: bool = False,
    body_byte_bound: int = DEFAULT_BODY_BYTE_BOUND,
) -> RoutedBackend | SubprocessBackend | None:
    """Resolve the configured WRITE-adapter(s) into a worker backend, or print why not.

    Loads ``adapters.yaml`` (DATA-MODEL §8) from the repo root and delegates to
    :func:`~agora_kb.curator.subprocess_backend.build_routed_backend`, which honors the optional
    per-act ``routing`` table (ADR-0015): ``plan`` (PASS-1) and ``author`` (PASS-2) may run on
    different brains. Returns a plain :class:`SubprocessBackend` when both acts use one brain,
    else a :class:`RoutedBackend`. ``override`` (``agora curate --backend NAME``) pins BOTH acts
    to one brain, bypassing routing.

    Returns ``None`` (after a clear stderr message) when the file is absent (no brain configured),
    an ``override`` names an unknown brain, or a ``network: 'none'`` act has no usable OS sandbox
    and ``allow_reduced_isolation=False`` (fail-closed — ADR-0013; the default loopback Ollama
    brain does inference OUTSIDE the sandbox and never needs one). The missing-executable case
    surfaces later,
    at invocation, as a clear error.
    """
    adapters_path = layout.root / "adapters.yaml"
    try:
        registry = load_backend_registry(adapters_path)
    except ValueError as exc:
        # Fail-loud but CLEAN (no traceback): a malformed adapters.yaml — including an invalid
        # ADR-0015 ``routing:`` block (unknown act key or a value naming an undefined backend).
        print(f"{_PROG} curate: invalid adapters.yaml — {exc}", file=sys.stderr)
        return None
    if registry is None:
        print(
            f"{_PROG} curate: no backend configured — create {adapters_path} with a 'backends:' "
            f"mapping and 'default_backend' (DATA-MODEL §8).",
            file=sys.stderr,
        )
        return None
    return build_routed_backend(
        registry,
        allow_reduced_isolation=allow_reduced_isolation,
        default_backend=default_backend,
        override=override,
        body_byte_bound=body_byte_bound,
        report=lambda msg: print(f"{_PROG} curate: {msg}", file=sys.stderr),
    )


def _cmd_harvest(args: argparse.Namespace) -> int:
    """``agora harvest``: scan configured memory connectors into gated candidates (ADR-0007).

    The read-side mirror of ``agora curate``. Loads the repo's ``harvest:`` policy (opt-in; a no-op
    with a clear note when disabled) and the ``adapters.yaml`` ``connectors:`` block, builds the
    connectors, and runs the :class:`~agora_kb.harvester.Harvester`: each connector is scope-gated
    (privacy, fail-closed), scanned since its cursor, and its new facts appended to the inbox as
    ``kind=candidate`` / ``confidence=low`` for the curator's keep/merge/drop gate. ``--dry-run``
    reports what WOULD be harvested without writing anything (the noise-pollution preview);
    ``--connector NAME`` restricts the run to one connector. A malformed config or an unsupported
    connector type is a clean error (exit 1), as is any per-connector scan error.
    """
    from .harvester import Harvester, build_connectors
    from .harvester.connectors import ConnectorError

    layout = RepoLayout(Path(args.repo))
    now = datetime.now(UTC)
    try:
        policy = load_harvest_policy(layout)
        redact = load_redact_policy(layout)
        repo_name = load_repo_config(layout).name
        specs = load_connector_specs(layout.root / "adapters.yaml")
    except ConfigError as exc:
        print(f"{_PROG} harvest: invalid config — {exc}", file=sys.stderr)
        return 1

    print(f"repo: {layout.root}")
    if not policy.enabled:
        print("harvest: disabled (set harvest.enabled: true in _kb/repo.yaml — ADR-0007)")
        return 0
    if not specs:
        print(
            f"harvest: no connectors configured — add a 'connectors:' block to "
            f"{layout.root / 'adapters.yaml'} (DATA-MODEL §8).",
            file=sys.stderr,
        )
        return 1

    try:
        connectors = build_connectors(specs, redact=redact)
    except (ConnectorError, ValueError) as exc:
        print(f"{_PROG} harvest: {exc}", file=sys.stderr)
        return 1

    report = Harvester(layout).run(
        connectors,
        policy=policy,
        repo_name=repo_name,
        now=now,
        dry_run=args.dry_run,
        only=args.connector,
    )

    if args.connector is not None and not report.connectors:
        print(
            f"harvest: no connector named {args.connector!r} (have: {[c.name for c in connectors]})"
        )
        return 1

    mode = " [dry-run]" if args.dry_run else ""
    print(f"harvest{mode}: scope_lock={policy.scope_lock} connectors={len(report.connectors)}")
    had_error = False
    for cr in report.connectors:
        if cr.status == "scope-refused":
            print(f"  {cr.name} (scope={cr.scope}): SCOPE REFUSED — {cr.message}")
        elif cr.status == "error":
            had_error = True
            print(f"  {cr.name} (scope={cr.scope}): ERROR — {cr.message}")
        elif cr.status == "unchanged":
            print(f"  {cr.name} (scope={cr.scope}): unchanged (source hash matches last scan)")
        elif args.dry_run:
            print(f"  {cr.name} (scope={cr.scope}): would harvest {cr.facts_found} fact(s)")
            for fact in cr.preview:
                preview = " ".join(fact.text.split())
                if len(preview) > 100:
                    preview = preview[:100].rstrip() + "…"
                print(f"      [{fact.fact_key[:12]}] {preview}")
        else:
            print(
                f"  {cr.name} (scope={cr.scope}): found={cr.facts_found} "
                f"written={cr.written} deduped={cr.deduped}"
            )
        for note in cr.notes:
            print(f"      - {note}")
    if not args.dry_run:
        print(f"total candidates written: {report.total_written}")
    return 1 if had_error else 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """``agora watch``: the in-process curator scheduler (DESIGN §4 — cron + threshold + idle).

    Each tick recovers any in-flight run, evaluates the three triggers against the live inbox depth,
    last-write, last-run, and the configured cron schedule (:func:`is_cron_due`), and runs ONE
    consolidation when due. The repo config is reloaded every tick so an operator's ``repo.yaml`` /
    ``adapters.yaml`` edits take effect without a restart. ``--once`` evaluates a single tick and
    exits (the unit the ADR scheduler test drives, and the right shape when an EXTERNAL scheduler
    — system cron / launchd — owns the cadence); otherwise it loops every ``--interval`` seconds
    until interrupted (Ctrl-C exits cleanly). This is the OSS-pure scheduler: no daemon framework,
    just a loop over the deterministic trigger policy.
    """
    repo = Repo.resolve(args.repo)
    interval = max(1, args.interval)
    mode = " [once]" if args.once else f" (interval={interval}s)"
    print(f"agora watch: {repo.layout.root}{mode}")
    try:
        while True:
            _watch_tick(repo)
            if args.once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        print("agora watch: stopped")
    return 0


def _watch_tick(repo: Repo) -> None:
    """One scheduler iteration: recover, evaluate the triggers, and run ONE consolidation if due.

    Loads the repo config fresh, evaluates ``threshold``/``idle``/``cron`` (``cron_due`` derived
    from the configured schedule + ``last_run``), and — when a signal fires — builds the configured
    sandbox-confined backend and executes :func:`agora_kb.curator.worker.run`. Prints one concise,
    timestamped status line per tick (idle / ran / due-but-no-backend). The integrity verdict is the
    worker's; this only decides *when* to wake it.
    """
    layout = repo.layout
    cfg = load_repo_config(layout)
    now = datetime.now(UTC)
    stamp = _fmt_dt(now)

    for rep in recover(repo, state_store=StateStore(layout)):
        print(f"{stamp} recovered: run={rep.run_id} status={rep.status} counts={rep.counts}")

    inbox = Inbox(layout)
    depth = inbox.depth()
    state = StateStore(layout).load()
    decision = evaluate(
        inbox_depth=depth,
        now=now,
        last_write=inbox.last_write(),
        last_run=state.last_run,
        config=cfg.triggers,
        cron_due=is_cron_due(cfg.triggers.cron, now=now, last_run=state.last_run),
    )
    if not decision.should_run:
        print(f"{stamp} idle: depth={depth} reason={decision.reason}")
        return

    backend = _build_backend(
        layout,
        default_backend=cfg.default_backend,
        allow_reduced_isolation=cfg.allow_reduced_isolation,
        body_byte_bound=cfg.body_byte_bound,
    )
    if backend is None:
        print(f"{stamp} due ({decision.reason}) but no usable backend — skipping this tick")
        return

    report = run(
        repo,
        backend=backend,
        state_store=StateStore(layout),
        now=now,
        taxonomy=cfg.taxonomy,
        max_attempts=cfg.max_attempts,
        related_k=cfg.related_k,
        max_orphans=cfg.max_orphans,
    )
    counts = ", ".join(f"{op}={n}" for op, n in sorted(report.counts.items())) or "-"
    commit = report.published_commit or "-"
    print(
        f"{stamp} ran ({decision.reason}): status={report.status} commit={commit} counts={counts}"
    )


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


def _cmd_web(args: argparse.Namespace) -> int:
    """``agora web``: run the FastAPI + HTMX web face over the repo (ADR-0019 / DESIGN §5.2).

    The web face is an OPTIONAL surface behind the ``web`` extra (fastapi/uvicorn/jinja2/
    markdown-it-py), so — exactly like ``agora serve`` with the MCP face — it is imported LAZILY
    here: a missing dependency prints a clean ``install agora-kb[web]`` message and exits 1 rather
    than raising an ``ImportError`` at module load (invariant 4: optional pieces stay optional). It
    binds ``--host``/``--port`` (default localhost:8000, Phase-3 single-user) and threads
    ``--writer``/``--user`` into the app (the upload source is ``web:<user>``). Runs via uvicorn.
    """
    try:
        web = importlib.import_module("agora_kb.faces.web")
        uvicorn = importlib.import_module("uvicorn")
    except ImportError as exc:  # pragma: no cover - exercised manually, not in tests
        print(
            f"{_PROG} web: web face unavailable ({exc}); "
            f"install the web dependencies: pip install 'agora-kb[web]' "
            f"(or uv sync --extra web).",
            file=sys.stderr,
        )
        return 1
    app = web.build_app(repo_path=Path(args.repo), writer=args.writer, user=args.user)
    print(f"{_PROG} web: serving {Path(args.repo).resolve()} on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)  # pragma: no cover - blocking server loop
    return 0


def _cmd_index_build(args: argparse.Namespace) -> int:
    """``agora index build``: deterministically (re)build the ADR-0012 §2 reader cache.

    Explicit operator action: builds regardless of the ``index.enabled`` READ-path kill-switch
    (that flag gates whether the read path CONSUMES the cache, not whether one may build it).
    """
    from .core.wiki import build_cache

    repo = Repo.resolve(args.repo)
    if not repo.is_initialized():
        print(f"repo {repo.root}: not initialized (run 'agora repo init')")
        return 1
    try:
        result = build_cache(repo)
    except (ConfigError, GitError) as exc:
        print(f"index: could not build ({exc})")
        return 1
    print(f"index: built {result.note_count} notes at commit {result.curated_commit[:12]}")
    print(f"  notes cache: {result.notes_path}")
    return 0


def _cmd_index_status(args: argparse.Namespace) -> int:
    """``agora index status``: report cache presence + freshness vs the curated tip."""
    from .core import index_cache

    repo = Repo.resolve(args.repo)
    layout = repo.layout
    print(f"repo: {layout.root}")
    if not repo.is_initialized():
        print("  index: repo not initialized")
        return 0
    try:
        commit: str | None = repo.branch_commit()
    except GitError as exc:
        print(f"  index: cannot resolve curated tip ({exc})")
        commit = None
    try:
        policy = load_index_policy(layout)
        print(f"  enabled={policy.enabled}")
    except ConfigError as exc:
        print(f"  config: ERROR ({exc})")
    try:
        notes_path = layout.index_notes_path()
    except Exception as exc:  # noqa: BLE001 — an unsafe repo name means no usable cache path.
        print(f"  cache: unavailable ({exc})")
        return 0
    payload = index_cache.read_payload(notes_path)
    if payload is None:
        print(f"  cache: absent/unreadable ({notes_path})")
    elif commit is not None and payload.curated_commit == commit:
        print(f"  cache: FRESH ({len(payload.notes)} notes) @ {payload.curated_commit[:12]}")
    else:
        cached = payload.curated_commit[:12]
        tip = commit[:12] if commit else "?"
        print(f"  cache: STALE ({len(payload.notes)} notes; cache={cached} tip={tip})")
    return 0


def _cmd_index_clear(args: argparse.Namespace) -> int:
    """``agora index clear``: remove the reader-cache artifacts (rebuilt on next build/curate)."""
    layout = RepoLayout(Path(args.repo))
    try:
        notes_path = layout.index_notes_path()
    except Exception as exc:  # noqa: BLE001 — unsafe repo name → no cache path to clear.
        print(f"index: no cache to clear ({exc})")
        return 0
    try:
        if notes_path.is_file():
            notes_path.unlink()
            print(f"index: cleared {notes_path.name}")
        else:
            print("index: cleared nothing (no cache present)")
    except OSError as exc:
        print(f"index: could not remove {notes_path} ({exc})")
    return 0


def _cmd_index_missing(args: argparse.Namespace) -> int:
    print("usage: agora index <build|status|clear> [--repo PATH]")
    return 2


def _cmd_gold_build(args: argparse.Namespace) -> int:
    """``agora gold build``: assemble + write the pack (or ``--check`` a byte-identical rebuild).

    Explicit operator action (builds regardless of curation cadence). A pack is a pure function
    of ``(curated commit, spec)``; ``--check`` assembles a fresh pack in memory and compares it to
    on-disk bytes WITHOUT writing — a CI guard for the byte-identical-rebuild contract (ADR-0027
    decision 3). ``--check`` exits non-zero when the pack is absent or has drifted (stale).
    """
    from .core.gold import PackAssembler, PackSpec, build_gold

    repo = Repo.resolve(args.repo)
    if not repo.is_initialized():
        print(f"repo {repo.root}: not initialized (run 'agora repo init')")
        return 1
    spec = PackSpec(name=args.pack)
    now = datetime.now(UTC)
    if args.check:
        try:
            fresh = PackAssembler(repo).assemble(spec, generated_at=now).text
            pack_path = repo.layout.gold_pack_path(spec.name)
        except (GitError, ValueError) as exc:
            print(f"gold: could not assemble ({exc})")
            return 1
        if not pack_path.is_file():
            print(f"gold: pack {spec.name!r} absent ({pack_path}); run 'agora gold build'")
            return 1
        if pack_path.read_text(encoding="utf-8") == fresh:
            print(f"gold: pack {spec.name!r} is byte-identical to a fresh rebuild")
            return 0
        print(f"gold: pack {spec.name!r} DIFFERS from a fresh rebuild (stale)")
        return 1
    try:
        result = build_gold(repo, spec, generated_at=now)
    except (GitError, ValueError) as exc:
        print(f"gold: could not build ({exc})")
        return 1
    print(
        f"gold: built pack {result.pack!r} ({result.note_count} notes, ~{result.est_tokens} "
        f"tokens) at commit {result.curated_sha[:12]}"
    )
    print(f"  pack: {result.pack_path}")
    return 0


def _cmd_gold_status(args: argparse.Namespace) -> int:
    """``agora gold status``: report pack presence + freshness vs the curated tip (ADR-0027)."""
    from .core.gold import read_meta

    repo = Repo.resolve(args.repo)
    layout = repo.layout
    print(f"repo: {layout.root}")
    if not repo.is_initialized():
        print("  gold: repo not initialized")
        return 0
    try:
        commit: str | None = repo.branch_commit()
    except GitError as exc:
        print(f"  gold: cannot resolve curated tip ({exc})")
        commit = None
    meta = read_meta(layout, args.pack)
    if meta is None:
        print(f"  gold: pack {args.pack!r} absent/unreadable")
        return 0
    fresh = commit is not None and meta.curated_sha == commit
    print(
        f"  gold: {'FRESH' if fresh else 'STALE'} pack {meta.pack!r} "
        f"({meta.note_count} notes, ~{meta.est_tokens}/{meta.budget_tokens} tokens) "
        f"@ {meta.curated_sha[:12]} generated {meta.generated_at}"
    )
    if not fresh and commit is not None:
        print(f"  tip={commit[:12]} — run 'agora gold build'")
    return 0


def _cmd_gold_missing(args: argparse.Namespace) -> int:
    print("usage: agora gold <build|status> [--repo PATH] [--pack NAME] [--check]")
    return 2


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

    # ADR-0013: PROVE the curator's OS-sandbox confinement on this host (mechanism + the four
    # assertions), rather than assume it. A sandbox that is present but does NOT actually confine is
    # a real health failure; a platform with no kernel sandbox is reported (fail-closed only bites a
    # network: none backend at curate time) without flagging the whole host unhealthy.
    ok = _doctor_sandbox(load_repo_config(layout).allow_reduced_isolation) and ok

    # ADR-0015: observability — which brain runs each cognitive act (default or routed). Reporting
    # only; never affects the health verdict.
    _doctor_routing(layout, load_repo_config(layout).default_backend)

    # ADR-0007: observability — the harvester policy + configured connectors with cursor state.
    # Reporting only; never affects the health verdict and never crashes on malformed config.
    _doctor_connectors(layout)

    # ADR-0012 §2 / issue #26: observability — the derived reader-cache state (present/fresh/stale).
    # Reporting only; never affects the health verdict and never crashes on malformed config.
    _doctor_index(layout)

    # ADR-0027 / issue #37: observability — the derived gold-pack state + the §8 scan exclusion.
    # Reporting only; never affects the health verdict and never crashes on malformed config.
    _doctor_gold(layout)

    print(f"status: {'healthy' if ok else 'unhealthy'}")
    return 0 if ok else 1


def _doctor_connectors(layout: RepoLayout) -> None:
    """Print the ADR-0007 harvest policy + configured connectors with per-connector cursor state.

    Observability only — never affects the health verdict and never crashes: an unreadable
    ``repo.yaml`` / ``adapters.yaml`` is noted (``agora harvest`` surfaces the real config error
    loudly). Shows whether harvesting is enabled, the ``scope_lock``, and for each connector its
    scope and cursor (last scan + the §6 ``proposed`` / ``accepted`` / ``rejected`` counters — the
    latter two are curator-owned and remain 0 until that wiring lands, ADR-0017).
    """
    from .harvester import CursorStore

    try:
        policy = load_harvest_policy(layout)
    except Exception as exc:  # noqa: BLE001 — doctor must never crash on a malformed repo.yaml.
        print(f"  harvest: repo.yaml present but unreadable ({exc})")
        return
    print(
        f"  harvest: {'enabled' if policy.enabled else 'disabled'} (scope_lock={policy.scope_lock})"
    )

    try:
        specs = load_connector_specs(layout.root / "adapters.yaml")
    except Exception as exc:  # noqa: BLE001 — doctor must never crash on a malformed adapters.yaml.
        print(f"  connectors: adapters.yaml present but unreadable ({exc})")
        return
    if not specs:
        print("  connectors: none configured")
        return

    store = CursorStore(layout)
    for spec in specs:
        try:
            cursor = store.load(spec.name)
            last = _fmt_dt(cursor.last_scan)
            counters = (
                f"last_scan={last} proposed={cursor.proposed} "
                f"accepted={cursor.accepted} rejected={cursor.rejected}"
            )
            if cursor.redacted:
                # ADR-0023 §6: per-class facts-with-redaction (metadata-only; never the secret).
                breakdown = ", ".join(f"{c}={cursor.redacted[c]}" for c in sorted(cursor.redacted))
                counters += f" redacted={{{breakdown}}}"
        except Exception as exc:  # noqa: BLE001 — a bad connector name must not crash doctor.
            counters = f"cursor unreadable ({exc})"
        print(f"    {spec.name} (scope={spec.scope}): {counters}")


def _doctor_index(layout: RepoLayout) -> None:
    """Print the ADR-0012 §2 reader-cache state (issue #26). Observability only — never crashes.

    Shows the ``index.enabled`` kill-switch and whether the cache is present + fresh (its stamped
    ``curated_commit`` == the live curated tip) or stale/absent. A malformed ``repo.yaml`` or an
    uninitialized repo is noted, never fatal; it never affects the health verdict (a missing cache
    degrades to a full scan, not an error).
    """
    from .core import index_cache

    try:
        policy = load_index_policy(layout)
    except Exception as exc:  # noqa: BLE001 — doctor must never crash on a malformed repo.yaml.
        print(f"  index: repo.yaml present but unreadable ({exc})")
        return
    repo = Repo(layout)
    if not repo.is_initialized():
        print(f"  index: enabled={policy.enabled} (repo not initialized — read path full-scans)")
        return
    try:
        commit: str | None = repo.branch_commit()
    except Exception:  # noqa: BLE001 — a git failure is not a doctor failure here.
        commit = None
    try:
        payload = index_cache.read_payload(layout.index_notes_path())
    except Exception:  # noqa: BLE001 — an unsafe repo name / IO issue → treat as no cache.
        payload = None
    if payload is None:
        state = "absent"
    elif commit is not None and payload.curated_commit == commit:
        state = f"fresh ({len(payload.notes)} notes)"
    else:
        state = "stale"
    print(f"  index: enabled={policy.enabled} cache={state}")


def _doctor_gold(layout: RepoLayout) -> None:
    """Print the ADR-0027 gold-pack state (issue #37). Observability only — never crashes.

    Shows whether the default pack is present + fresh (its stamped ``curated_sha`` == the live
    curated tip) or stale/absent, and confirms the §8 ``_kb/gold/`` harvester scan exclusion. An
    uninitialized repo / IO issue is noted, never fatal; it never affects the health verdict (an
    absent pack degrades to no injection, not an error).
    """
    from .core.gold import DEFAULT_PACK, read_meta

    repo = Repo(layout)
    if not repo.is_initialized():
        print("  gold: repo not initialized (no packs)")
        return
    try:
        commit: str | None = repo.branch_commit()
    except Exception:  # noqa: BLE001 — a git failure is not a doctor failure here.
        commit = None
    try:
        meta = read_meta(layout, DEFAULT_PACK)
    except Exception:  # noqa: BLE001 — any read issue → treat as no pack.
        meta = None
    if meta is None:
        state = "absent"
    elif commit is not None and meta.curated_sha == commit:
        state = f"fresh ({meta.note_count} notes, ~{meta.est_tokens} tokens)"
    else:
        state = "stale"
    print(f"  gold: pack={state} (harvester scan excludes _kb/gold/, §8)")


def _doctor_routing(layout: RepoLayout, default_backend: str | None = None) -> None:
    """Print the ADR-0015 per-act routing table (which brain runs ``plan`` / ``author``).

    Observability only — never affects the health verdict and never crashes: an absent
    ``adapters.yaml`` is noted and a malformed one is skipped (``agora curate`` surfaces the real
    config error loudly). ``default_backend`` (the repo's ``curator.backend``) is threaded so the
    table reflects the SAME precedence a real run uses. Showing each act's ``network`` posture lets
    an operator see BEFORE a run that e.g. routing an act to a ``network: 'none'`` brain on a
    sandbox-less host will fail closed (ADR-0013), or that routing ``author`` to a metered API
    multiplies cost (PASS-2 runs per region).
    """
    adapters_path = layout.root / "adapters.yaml"
    try:
        registry = load_backend_registry(adapters_path)
    except Exception as exc:  # noqa: BLE001 — doctor must never crash on a malformed adapters.yaml.
        print(f"  routing: adapters.yaml present but unreadable ({exc})")
        return
    if registry is None:
        print("  routing: no adapters.yaml (no backend configured)")
        return
    parts = []
    for act, name in registry.routed_backends(default=default_backend).items():
        try:
            parts.append(f"{act}={name} (network: {registry.get(name).network})")
        except KeyError:
            # repo.yaml curator.backend names no defined brain — surface it, don't crash.
            parts.append(f"{act}={name} (UNKNOWN backend)")
    print(f"  routing: {'  '.join(parts)}")


def _doctor_sandbox(allow_reduced_isolation: bool) -> bool:
    """Run the ADR-0013 sandbox self-test and print its report; return whether the host is healthy.

    Selects the OS-appropriate :class:`~agora_kb.curator.isolation.BackendIsolation` and runs the
    hardened self-test against a throwaway worktree + a SEPARATE throwaway tmp (the ADR's
    EPERM-specific probes: write-inside OK, write-outside denied, network denied to a reachable
    target, Apple-shimmed binary runs). Returns ``False`` ONLY when a sandbox is present but its
    self-test FAILS (a confinement that lies is worse than none). A platform with no kernel sandbox
    (``SandboxUnavailable``) prints a fail-closed note and returns ``True`` — the default loopback
    Ollama brain does inference outside the sandbox and never needs one; the fail-closed guard bites
    only a ``network: none`` backend at curate time. Never raises: any unexpected error is reported.
    """
    from .curator.isolation.selftest import ollama_reachable, self_test

    try:
        isolation = select_backend_isolation(allow_reduced_isolation=allow_reduced_isolation)
    except SandboxUnavailable as exc:
        print(f"  sandbox: unavailable — fail-closed for network:none backends ({exc})")
        return True

    wt = Path(tempfile.mkdtemp(prefix="agora-doctor-wt-"))
    tmp = Path(tempfile.mkdtemp(prefix="agora-doctor-tmp-"))
    try:
        report = self_test(isolation, wt, tmp, [])
    except Exception as exc:  # noqa: BLE001 — doctor must never crash; report and move on.
        print(f"  sandbox ({isolation.name}): self-test ERROR — {exc}")
        return False
    finally:
        shutil.rmtree(wt, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    # The network-deny leg is only PROVABLE against a reachable target (the ADR uses host Ollama at
    # 127.0.0.1:11434): with nothing listening the probe gets ECONNREFUSED, not EPERM, so the
    # self-test cannot prove the deny. Treat that as "unproven", NOT a failure — health is the other
    # three legs plus a network deny only when a target was actually reachable.
    reachable = ollama_reachable()
    healthy = (
        report.write_inside_ok
        and report.write_outside_denied
        and report.apple_shim_ok
        and (report.network_denied or not reachable)
    )
    print(f"  sandbox: {report.mechanism} ({'ok' if healthy else 'FAILED'})")
    print(
        f"    write-inside={report.write_inside_ok} "
        f"write-outside-denied={report.write_outside_denied} "
        f"apple-shim={report.apple_shim_ok}"
    )
    net_note = "" if reachable else " (no reachable target — unproven, not a failure)"
    print(f"    network-denied={report.network_denied}{net_note}")
    return healthy


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
