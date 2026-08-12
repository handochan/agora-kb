"""``agora`` CLI entry point (DESIGN/ROADMAP Phase 1).

A dependency-light :mod:`argparse` front-end over the core API. Subcommands:

- ``agora repo init <path>`` — initialize a knowledge repo: ``Repo.init`` + emit the KB schema +
  taxonomy + a starter ``_kb/repo.yaml``, committed as one admin commit (the result lints clean).
- ``agora status [--repo PATH]`` — print inbox depth + curator state (last run/commit, counters).
- ``agora curate [--repo PATH] [--force]`` — recover any in-flight run, then execute ONE real
  consolidation run via :mod:`agora_kb.curator` ``worker`` against the configured ``adapters.yaml``
  backend, and print the resulting :class:`~agora_kb.curator.worker.RunReport`.
- ``agora requeue [--repo PATH] (--run ID | --event ID | --all) [--dry-run] [--force]
  [--reset-attempts]`` — return TERMINAL-failure events from ``_kb/failed/`` to the inbox (issue
  #99). Rename-only under ``curator_lock``: the events keep their bytes and their ids, an occupied
  inbox slot is reported rather than clobbered, and nothing is ever deleted.
- ``agora watch [--repo PATH] [--interval N] [--once]`` — the in-process scheduler: loop, evaluating
  the cron + threshold + idle triggers each tick and consolidating when due (``--once`` for a single
  evaluation, e.g. driven by an external system cron / launchd).
- ``agora sync [--repo PATH]`` — push-only git backup (issue #64): push the curated branch to the
  ``backup.remote`` configured in ``_kb/repo.yaml`` (fast-forward only, never ``--force``); with no
  remote configured it is a guided no-op. Pull/fetch/bidirectional is deferred to #46.
- ``agora serve [--repo PATH] [--writer W]`` — run the MCP stdio server face. The face is imported
  lazily so the rest of the CLI works even when an MCP transport dependency is missing.
- ``agora web [--repo PATH] [--host H] [--port P] [--writer W] [--user U]`` — run the FastAPI + HTMX
  web face (browse / search / upload; ADR-0019). Imported lazily (the optional ``web`` extra), so a
  missing fastapi gives a clean ``install agora-kb[web]`` message, not an import error.
- ``agora doctor`` — print a health report, headed by a paste-into-a-bug-report line carrying the
  agora + python versions (then git, key deps, repo init state, sandbox, routing, …).

``agora --version`` (top-level, no subcommand) prints ``agora <version>`` from the single source of
truth :data:`agora_kb.__version__` — never from install metadata, which a source checkout lacks
(issue #101).

The MCP and web faces are imported lazily (only inside ``serve`` / ``web``) on purpose: it keeps
``repo init`` / ``status`` / ``curate`` / ``doctor`` usable in environments where the transport /
web stack is not installed (invariant 4: every component has an OSS path, optional pieces stay
optional).
"""

from __future__ import annotations

import argparse
import functools
import importlib
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .config import (
    SUPPORTED_KB_SCHEMA_VERSIONS,
    ConfigError,
    RepoConfig,
    UnsupportedSchemaVersionError,
    guard_repo_schema_version,
    load_backend_registry,
    load_backup_policy,
    load_connector_specs,
    load_harvest_policy,
    load_index_policy,
    load_redact_policy,
    load_repo_config,
    read_kb_schema_version,
    write_default_adapters_yaml,
    write_default_repo_config,
)
from .core import (
    Inbox,
    InvalidWriterError,
    Repo,
    RepoLayout,
    StateStore,
    atomic_write_text,
    failed_event_count,
)
from .core.repo import GitError
from .core.state import CuratorState
from .curator import evaluate
from .curator.backends import _WORKTREE_TOKEN, BackendRegistry
from .curator.claim import LockHeld
from .curator.constants import DEFAULT_BODY_BYTE_BOUND
from .curator.cron import is_cron_due
from .curator.isolation import SandboxUnavailable, select_backend_isolation
from .curator.requeue import _KEEP_ERROR as _REQUEUE_KEEP_ERROR
from .curator.requeue import (
    MOVING_OUTCOMES,
    RequeueItem,
    RequeueOutcome,
    RequeueReport,
    Selector,
    StateUnreadable,
    rel_to_repo,
    run_requeue,
)
from .curator.subprocess_backend import (
    RoutedBackend,
    SubprocessBackend,
    build_routed_backend,
    resolve_program_on_path,
)
from .curator.worker import RunReport, recover, run
from .schema import Taxonomy, emit_schema, lint

__all__ = ["main", "build_parser"]

_PROG = "agora"


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with one subparser per command."""
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Agora — markdown + git shared-memory hub for AI agents.",
    )
    # `--version` hangs on the TOP-LEVEL parser, not a subcommand: the question "what am I running?"
    # is asked before the user knows any subcommand, and `agora --version` must answer it on a bare
    # invocation (issue #101). argparse's `version` action prints to stdout and raises
    # `SystemExit(0)` from inside `parse_args`, so it never reaches `main`'s dispatch — deliberate:
    # it is the one flag that must work even if every subcommand's imports are broken.
    parser.add_argument("--version", action="version", version=f"{_PROG} {__version__}")
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
    # Positional repo path — declared so the #98 guard covers a RE-init onto an existing repo.
    p_repo_init.set_defaults(func=_cmd_repo_init, schema_guard_attr="path")
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
    # `dest` may already BE an Agora repo (import does not refuse one), and import writes
    # wiki/ + _meta/ and commits — so it must be guarded (#98).
    p_import.set_defaults(func=_cmd_import, schema_guard_attr="dest")

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

    # requeue — the _kb/failed/ → _kb/inbox/ back-edge (issue #99, ADR-0002 spool-custodian
    # appendix). A SELECTOR IS REQUIRED: nothing else in this CLI moves files en masse on a bare
    # invocation, `agora status` / `agora doctor` already serve discovery, and `agora curate` hands
    # the operator the exact `--run <id>` command — so the mass move stays a deliberate act.
    p_requeue = sub.add_parser(
        "requeue",
        help="return terminal-failure events from _kb/failed/ to the inbox (rename-only; #99)",
    )
    p_requeue.add_argument("--repo", default=".", help="repo root (default: .)")
    sel = p_requeue.add_mutually_exclusive_group(required=True)
    sel.add_argument(
        "--run",
        default=None,
        metavar="RUN_ID",
        help="requeue only the events of this failed run (the id 'agora curate' prints as "
        "failed_requeue)",
    )
    sel.add_argument("--event", default=None, metavar="EVENT_ID", help="requeue only this event")
    sel.add_argument(
        "--all",
        action="store_true",
        help="requeue every terminal-failure event under _kb/failed/",
    )
    p_requeue.add_argument(
        "--dry-run",
        action="store_true",
        help="report what WOULD move without changing one byte of the filesystem",
    )
    p_requeue.add_argument(
        "--force",
        action="store_true",
        help="requeue events whose event_key is already in state.event_keys (the claim will still "
        "drop them); never overwrites an existing inbox event",
    )
    # OUTSIDE the mutually-exclusive group on purpose: the budget flag is orthogonal to WHICH
    # events are selected, and criterion 8 requires it to work with every selector.
    p_requeue.add_argument(
        "--reset-attempts",
        action="store_true",
        help="also archive to _kb/requeued/ every retry record none of whose events is still in "
        "_kb/failed/, so they get the full curator.max_attempts budget again (by default a "
        "requeued event keeps the attempts it already spent, so it gets exactly one more run); "
        "every archived record is printed",
    )
    p_requeue.set_defaults(func=_cmd_requeue)

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

    # sync — push-only git backup of the curated branch (issue #64).
    p_sync = sub.add_parser(
        "sync",
        help="push the curated branch to the configured backup remote (push-only, issue #64)",
    )
    p_sync.add_argument("--repo", default=".", help="repo root (default: .)")
    p_sync.set_defaults(func=_cmd_sync)

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
        "--user",
        default="local",
        help=(
            "identity stamped into source=web:<user> (default: local; the fallback when "
            "web.identity.trusted_header is unset or the header is absent, issue #67)"
        ),
    )
    p_web.set_defaults(func=_cmd_web)

    # doctor
    p_doctor = sub.add_parser("doctor", help="print a health report")
    p_doctor.add_argument("--repo", default=".", help="repo root to check (default: .)")
    p_doctor.add_argument(
        "--skip-probe",
        action="store_true",
        help="skip the brain-availability probe (no daemon or PATH lookups; the health "
        "verdict then ignores brain reachability)",
    )
    # `skip_schema_guard` EXEMPTS doctor from the #98 KB-schema support gate — the ONLY command that
    # names a repo with `--repo` and is allowed to run against one this build does not support.
    # Diagnosing the skew is doctor's entire job; a guard that killed it would take out the tool the
    # failure message sends the operator to. Doctor reports the skew on its `schema:` line and goes
    # `status: unhealthy` instead (see `_doctor_schema`).
    p_doctor.set_defaults(func=_cmd_doctor, skip_schema_guard=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (wired as ``[project.scripts] agora``).

    Returns a process exit code. With no subcommand (or an unknown one) the parser help is written
    to stderr and ``2`` is returned. Between parsing and dispatch sits the #98 KB-schema support
    gate (:func:`_schema_version_guard`) — ONE central check rather than a call every command has to
    remember, because a guard a future ``agora foo`` forgets to make is the same silent-misread bug
    the guard exists to fix.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 2
    guard_rc = _schema_version_guard(args)
    if guard_rc is not None:
        return guard_rc
    return func(args)


def _schema_version_guard(args: argparse.Namespace) -> int | None:
    """The DESIGN §10 V9 fail-loud gate, applied ONCE for every command (#98).

    Returns ``None`` to let dispatch proceed, or an exit code the caller returns immediately.

    The rule is deliberately structural rather than a per-command list: a command is guarded IFF it
    names a repo with ``--repo`` and has not opted out with ``skip_schema_guard``. So a future
    ``agora foo --repo PATH`` is guarded the day it is added, with no edit here — the failure mode
    that made this issue P0 is a check somebody forgets to wire.

    A command whose repo argument is POSITIONAL declares which attribute holds it, with
    ``set_defaults(schema_guard_attr="...")``. That covers ``agora import <src> <dest>`` and
    ``agora repo init <path>``, which an earlier draft exempted on the premise that they "CREATE
    the repo they name". That premise is FALSE and the exemption was a hole: neither command
    refuses an ALREADY-INITIALIZED destination, and both are DIRECT writers of ``wiki/`` and
    ``_meta/`` plus their own git commit — i.e. exactly the old-binary-writes-into-a-newer-repo
    damage this issue exists to prevent, on the flow the README quickstart documents. Creating a
    FRESH destination still passes silently, because :func:`read_kb_schema_version` yields the
    default ``1`` for a directory that is not an Agora repo — so the exemption's real intent
    survives without the hole.

    Two deliberate non-firings, each an "act on a repo?" answer of no:

    * ``agora --version`` — argparse exits from inside ``parse_args``, never reaching dispatch;
    * ``agora repo`` / ``index`` / ``gold`` with no subcommand — usage stubs that touch no repo.

    ``agora doctor`` is the one command that DOES name a repo and is still exempt (see
    ``build_parser``). ``tests/test_schema_version_guard.py`` walks the whole parser tree and fails
    if any other command slips into the unguarded category without a decision being made.
    """
    if getattr(args, "skip_schema_guard", False):
        return None
    repo = getattr(args, "repo", None)
    if repo is None:
        attr = getattr(args, "schema_guard_attr", None)
        repo = getattr(args, attr, None) if attr is not None else None
    if repo is None:
        return None
    try:
        guard_repo_schema_version(RepoLayout(Path(repo)))
    except UnsupportedSchemaVersionError as exc:
        # A clean one-liner + non-zero exit — never a traceback. Ordered BEFORE the ValueError arm
        # below on purpose: UnsupportedSchemaVersionError is a ConfigError, hence a ValueError.
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        # An unusable `--repo` string (NUL byte, vanished cwd, unreadable path). Not the guard's
        # verdict to render: the command itself reports it with the context only it has.
        return None
    return None


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
    try:
        state = StateStore(layout).load()
    except (ValidationError, OSError, ValueError) as exc:
        # StateStore.load RAISES on a corrupt/invalid file BY DESIGN — it never silently discards
        # published_runs/event_keys. `agora status` is the command an operator reaches for when
        # "nothing is happening", so it must REPORT that, not traceback. rc is unchanged (an
        # uncaught exception already exits 1); only the traceback is removed (#96/#97).
        #
        # ALL THREE classes, because `load` reads before it validates: invalid JSON/schema raises
        # ValidationError, a non-UTF-8 (truncated / half-written / FS-corrupted) file raises
        # UnicodeDecodeError, and an unreadable one raises OSError. Catching only the first would
        # leave `agora status` tracebacking on the same corruption that `agora doctor` and
        # `agora watch` both report cleanly.
        print(f"repo: {layout.root}")
        print(f"inbox depth: {depth}")
        print(
            f"{_PROG} status: _kb/state.json is unreadable — {_one_line(str(exc), 200)}",
            file=sys.stderr,
        )
        return 1
    c = state.counters
    print(f"repo: {layout.root}")
    print(f"inbox depth: {depth}")
    print(f"last_run: {_fmt_dt(state.last_run)}")
    print(f"last_commit: {state.last_commit or '-'}")
    print(
        f"counters: ingested={c.ingested} merged={c.merged} dropped={c.dropped} failed={c.failed}"
    )
    # #96: the failure surface. `last_run` is "last successful PUBLISH", so a repo whose curator
    # fails every run shows `never` forever — these three lines are the only place a non-terminal
    # failure (which returns its events to inbox/ and burns no counter) is visible from the CLI.
    print(f"last_attempt: {_fmt_dt(state.last_attempt)}")
    print(f"last_failure: {_fmt_last_failure(state, layout)}")
    print(f"failed_events: {failed_event_count(layout)}")
    # `agora status` deliberately does NOT advertise `agora requeue` (#99 §5.3): every line here is
    # `key: <machine-readable value>` and a remediation sentence has no value slot, but the real
    # reason is ordering — status has no CAUSE information, so it would send an operator to requeue
    # BEFORE `agora doctor`, inverting the one sequencing the risk section demands. Locked by
    # `test_status_does_not_advertise_requeue`.
    return 0


def _fmt_last_failure(state: CuratorState, layout: RepoLayout) -> str:
    """One line for the last curator failure, or ``none`` (#96).

    The VERDICT WORD comes FIRST so `agora status | grep UNRESOLVED` works and the operator can
    tell "currently broken" from "already superseded by a later successful run" at a glance. The
    reason is already whitespace-collapsed and length-capped upstream, so this stays ONE line.

    ``layout`` is here only for :func:`_record_pointer`: ``last_failure`` is never cleared by a
    later success, so after ``agora requeue --reset-attempts`` the stored ``record=`` would point
    at a file that has moved to ``_kb/requeued/`` — indefinitely (#99).
    """
    lf = state.last_failure
    if lf is None:
        return "none"
    verdict = "UNRESOLVED" if state.failure_is_current else "superseded"
    first = lf.reasons[0] if lf.reasons else "-"
    return (
        f"{verdict} {_fmt_dt(lf.when)} run={lf.run_id} phase={lf.phase} "
        f"reasons={lf.reasons_total} record={_record_pointer(layout, lf.record_path)} "
        f"first={first}"
    )


def _record_pointer(layout: RepoLayout, record_path: str) -> str:
    """The rendered ``record=`` value: the requeued twin when the original is gone (#99).

    ``agora requeue --reset-attempts`` ARCHIVES a run's ``error.json`` to the ``_kb/requeued/``
    twin instead of rewriting ``state.json`` (requeue is strictly rename-only and must never clear
    a failure the operator has not fixed — that is the blind spot #96 closed). The cost is a stored
    pointer to a moved file, and the fix belongs in the two renderers, not in the state.

    Deliberately conservative — the twin is returned ONLY when (1) the stored string has the exact
    ``_kb/failed/<date>/<run-id>/error.json`` shape, (2) the original is absent, and (3) the twin
    exists. In every other case — including a record deleted by hand, which already dangles today —
    the stored string is returned UNCHANGED, so existing output stays byte-identical.

    OSError-GUARDED: ``record_path`` is an unconstrained ``str`` in an operator-editable
    ``state.json`` and ``Path.exists()`` RAISES ``ENAMETOOLONG`` (it is not in pathlib's
    ignored-errno set). :func:`_fmt_last_failure` is called OUTSIDE ``_cmd_status``'s try, so an
    unguarded stat would re-introduce exactly the traceback #96 removed. Shape first, filesystem
    second. The value is swapped with NO suffix: ``_kb/requeued/`` is self-describing, and a space
    inside the value would break the ``record=… first=…`` grammar scripts already split on.
    """
    parts = record_path.split("/")
    if len(parts) != 5 or parts[0] != "_kb" or parts[1] != "failed" or parts[4] != "error.json":
        return record_path
    try:
        if (layout.root / record_path).exists():
            return record_path
        twin = layout.requeued_record_path(date=parts[2], run_id=parts[3])
        if twin.exists():
            return twin.relative_to(layout.root).as_posix()
    except (OSError, InvalidWriterError):
        return record_path
    return record_path


def _cmd_curate(args: argparse.Namespace) -> int:
    """``agora curate``: recover in-flight runs, then run ONE real consolidation if due (or forced).

    Order (ADR-0011 §9 then §0): recover() first so any crashed run is finalized/returned before a
    fresh run; then evaluate the configured triggers (``--force`` overrides). When a run is due,
    load the ``adapters.yaml`` WRITE-adapter registry, build a :class:`SubprocessBackend` over the
    configured default brain, and execute :func:`agora_kb.curator.worker.run`. The integrity verdict
    is the worker's (the backend is outside the boundary), so this prints the resulting RunReport
    (status / published commit / per-op counts). A missing/absent ``adapters.yaml`` is a clear
    error, not a crash.

    EXIT CODE: a ``status: failed`` run still returns **0** — deliberately, do not "fix" it (#96).
    ``failed`` is not a synonym for "something is wrong": a CAS conflict and a within-budget retry
    both leave the events valid and back in ``inbox/``, i.e. normal self-healing operation. Making
    those non-zero would fire cron mail on every benign retry and trip ``Restart=on-failure`` on a
    supervising unit — manufacturing exactly the crash loop #97 removes. The setup failures that
    ARE the operator's problem (no/unknown backend) already return 1 above; the CAUSE of a failed
    run is now on stdout via :func:`_print_run_diagnostics`.
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
        language=cfg.language,
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
        max_candidates=cfg.max_candidates_per_run,
        max_orphans=cfg.max_orphans,
    )
    counts = ", ".join(f"{op}={n}" for op, n in sorted(report.counts.items())) or "-"
    print(f"status: {report.status}")
    print(f"published_commit: {report.published_commit or '-'}")
    print(f"counts: {counts}")
    _print_run_diagnostics(report)
    return 0


def _print_run_diagnostics(report: RunReport, *, prefix: str = "") -> None:
    """Render ONE run's operator-facing diagnostics — the single #115 channel, extended by #96.

    Two message classes, two streams, ONE call per face, so no face can render one and forget the
    other:

    * a FATAL cause (``report.failure``) → STDOUT, in the same ``key: value`` grammar as the
      ``status:``/``published_commit:``/``counts:`` lines it follows. Exactly two deterministic
      lines: WHERE the durable record is (repo-relative), and the head of WHAT it says. This
      EXTENDS the machine-readable stdout summary rather than breaking it — ``status: failed`` was
      already there; the missing piece was any way to get from that verdict to the cause (#96
      criterion 6, which names stdout explicitly).
    * NON-FATAL diagnostics (``report.warnings``) → STDERR, byte-identical to #115: free-form,
      unbounded-count prose that must never sit inside the machine-readable stdout summary. A run
      that publishes placeholder bodies is still ``status: published`` by design (§4.2), so this
      line is the only thing between an operator and a silently-empty KB.

    ``prefix`` stamps the stdout lines for the ``agora watch`` tick, whose whole log obeys a
    ``{stamp} <verb>:`` grammar so ``journalctl -u agora-watch`` reads as ONE stream. The stderr
    ``warning:`` lines are deliberately NOT prefixed: their bytes are the #115 contract and are
    already test-locked, and prefixing them would buy nothing on a stream that carries no timeline.

    Since #99 a THIRD stdout line rides the same channel — the way BACK from a terminal failure —
    gated on ``counts["failed"] > 0``, i.e. on this run having actually left events in
    ``_kb/failed/``. That gate is the whole point: a WITHIN-BUDGET failure reports
    ``retried=1 failed=0`` with the events back in ``inbox/``, where advertising requeue would not
    be noise but WRONG ADVICE, and the CAS-conflict path (``record_path=None``, ``retried=N``) is
    correctly silent for the same reason. ``failure.run_id`` IS the ``_kb/failed/<date>/<run-id>/``
    directory name (``worker._fail``), so ``--run <that id>`` selects exactly the events this run
    lost — the narrow selector doing the mass-move footgun mitigation structurally. Run ids contain
    only ``[A-Za-z0-9.-]``, so the line pastes into any shell unquoted.
    """
    if report.failure is not None:
        print(f"{prefix}failed_record: {report.failure.record_path or '-'}")
        print(f"{prefix}failed_checks: {report.failure.summary()}")
        if report.counts.get("failed", 0) > 0:
            print(f"{prefix}failed_requeue: {_PROG} requeue --run {report.failure.run_id}")
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _build_backend(
    layout: RepoLayout,
    *,
    default_backend: str | None = None,
    override: str | None = None,
    allow_reduced_isolation: bool = False,
    body_byte_bound: int = DEFAULT_BODY_BYTE_BOUND,
    language: str | None = None,
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
        language=language,
        report=lambda msg: print(f"{_PROG} curate: {msg}", file=sys.stderr),
    )


# --- requeue (issue #99) -------------------------------------------------------------------------
#: The outcomes the report counts as a DECLINE — everything that neither moved nor errored. Kept as
#: one frozenset so the `skipped:` count, its per-reason tally and the exit code cannot disagree.
_REQUEUE_DECLINED: frozenset[RequeueOutcome] = frozenset(
    {
        RequeueOutcome.already_delivered,
        RequeueOutcome.destination_exists,
        RequeueOutcome.unreadable,
    }
)

#: Printed only when at least one event moved (or would): requeueing into a still-broken curator is
#: a non-destructive round trip, but it is pure noise, and the ordering fix → requeue → curate is
#: the whole recovery procedure (`deploy/README.md`).
_REQUEUE_NOTE = (
    "note: fix the cause before curating again — 'agora status' shows the last failure, "
    "'agora doctor' checks the brain"
)


def _cmd_requeue(args: argparse.Namespace) -> int:
    """``agora requeue``: return TERMINAL-failure events from ``_kb/failed/`` to the inbox (#99).

    The face is a thin renderer over :func:`~agora_kb.curator.requeue.run_requeue`, mirroring
    ``_cmd_curate`` → ``worker.run``: every decision (the lock span, the tier-1 pre-check, the
    drain rule) belongs to the curator domain, so a future ``kb_requeue`` MCP tool gets one call
    site instead of a re-implementation.

    Uses ``RepoLayout(Path(args.repo))`` and NOT ``Repo.resolve``: requeue publishes nothing and
    must work when git is broken — a plausible co-morbidity of the outage that produced the
    failures in the first place.

    EXIT CODES (#99 crit 4/9): 0 when anything moved, when everything was legitimately declined, or
    when ``--all`` found an empty spool (a decline means the command DID its job and correctly
    refused — the ``_cmd_curate`` ruling that rc 1 means "could not do its job"); 1 for a held
    lock, an unreadable ``state.json`` without ``--force``, a named selector that matched nothing
    (it asserts the existence of a specific thing, unlike ``--all``), or any event that could not
    be moved; 2 from argparse for a missing/duplicated selector.
    """
    layout = RepoLayout(Path(args.repo))
    selector = _requeue_selector(args)
    # A WARNING, never a guard (#99 §4.3): `failure_is_current` is True on 100% of legitimate
    # recoveries — `last_run` is the last successful PUBLISH and the canonical order is fix →
    # requeue → curate — so a "refuse unless --force" gate would fire every single time, train
    # reflexive --force, and collide with criterion 6's use of the same flag. Silent for the narrow,
    # advertised `--run`/`--event` selectors. Handed to `run_requeue` as a preflight hook so it
    # prints BEFORE the batch moves, off the state the batch itself loaded under the lock: a second
    # unlocked `StateStore.load()` here would both widen the pre-lock access budget and report a
    # value the batch never saw. A held lock never reaches it — the lock is taken first, so crit 4's
    # "one clean line and nothing else" survives.
    preflight = (
        functools.partial(_warn_unresolved_failure, layout) if selector.kind == "all" else None
    )
    try:
        report = run_requeue(
            layout,
            selector=selector,
            dry_run=args.dry_run,
            force=args.force,
            reset_attempts=args.reset_attempts,
            preflight=preflight,
        )
    except LockHeld:
        # ADR-0008 step 1: the lock is non-blocking, so contention is a REFUSAL, not a wait and
        # not a traceback. Nothing was touched — run_requeue takes the lock before anything else.
        print(
            f"{_PROG} requeue: a curator run is in progress (_kb/curator.lock is held); "
            "nothing was changed — try again when it finishes",
            file=sys.stderr,
        )
        return 1
    except StateUnreadable as exc:
        print(
            f"{_PROG} requeue: _kb/state.json is unreadable — {_one_line(str(exc), 200)} "
            "(use --force to requeue without the tier-1 pre-check)",
            file=sys.stderr,
        )
        return 1

    if not report.items and selector.kind != "all":
        # A named selector ASSERTS that a specific run/event exists; `--all` asserts nothing, so an
        # empty spool is a clean 0 there (cron-safe) while this is an error. The decision rests on
        # the SELECTION alone: `--reset-attempts` scopes RECORDS rather than events, so letting a
        # drained record suppress this line would turn a mistyped run id into a silent rc 0 whose
        # exit code depended on unrelated repo state. `run_requeue` correspondingly archives
        # nothing when a named selector matched nothing, so there is never anything to suppress.
        noun = "run" if selector.kind == "run" else "event"
        print(
            f"{_PROG} requeue: no failed {noun} {selector.value!r} under _kb/failed/",
            file=sys.stderr,
        )
        return 1

    if report.precheck_skipped:
        print(
            "warning: _kb/state.json could not be read — the already-delivered pre-check was "
            "SKIPPED (--force); an event whose key is already published becomes an inbox zombie",
            file=sys.stderr,
        )

    print(f"repo: {layout.root}")
    _print_requeue_report(layout, report, reset_attempts=args.reset_attempts)

    forced = [item for item in report.items if item.outcome is RequeueOutcome.forced]
    if forced:
        carry = "would carry" if report.dry_run else "carry"
        print(
            f"warning: {len(forced)} forced event(s) {carry} an event_key already in "
            "state.event_keys — the claim will drop them again",
            file=sys.stderr,
        )
    rc = _warn_reset_attempts(report, reset_attempts=args.reset_attempts)
    stuck = [
        item
        for item in report.items
        if item.outcome in (RequeueOutcome.unreadable, RequeueOutcome.error)
    ]
    if stuck:
        tail = (
            "cannot be requeued and would be left in place"
            if report.dry_run
            else "could not be requeued and were left in place"
        )
        print(f"warning: {len(stuck)} event(s) under _kb/failed/ {tail}", file=sys.stderr)
        rc = 1
    return rc


def _warn_reset_attempts(report: RequeueReport, *, reset_attempts: bool) -> int:
    """Say what ``--reset-attempts`` actually did when it did not archive; return the exit code.

    Three states hide behind ``archived=0``, and naming the wrong one sends the operator away from
    the fix: records genuinely shared with a still-terminal event (use a wider selector), no
    records at all (nothing to reset — there is no wider selector to try), and records the
    filesystem REFUSED (a read-only or full disk; the budget was NOT reset). Only the first is the
    #99 §3.2 loud-null sentence, so it is now gated on the ``kept:`` lines it tells the operator to
    read. The third is the one that also changes the exit code: `deploy/README.md` documents this
    command inside a scripted recovery, and a wrapper must not see success when the budget it asked
    for is still spent. A PARTIAL failure (some archived, some errored) reports too — the count of
    archived records would otherwise read as a complete reset.
    """
    if not reset_attempts:
        return 0
    errored = [record for record in report.kept if record.reason.startswith(_REQUEUE_KEEP_ERROR)]
    if errored:
        print(
            f"warning: {len(errored)} retry record(s) could not be archived — the budget of the "
            "events they list was NOT reset (see the kept: lines for the reason)",
            file=sys.stderr,
        )
        return 1
    if report.archived or not report.moved:
        return 0
    # The LOUD null outcome: the flag ran, moved events, and reset nothing. Silence here would send
    # an operator into the next run believing the budget was restored.
    if report.kept:
        print(
            "warning: --reset-attempts reset nothing — every attempt record of the requeued "
            "events is shared with an event that is still terminal (see the kept: lines); "
            "use --run or --all to reset the whole set",
            file=sys.stderr,
        )
    else:
        print(
            "warning: --reset-attempts reset nothing — the requeued events have no retry records "
            "under _kb/failed/, so there was no spent budget to restore",
            file=sys.stderr,
        )
    return 0


def _requeue_selector(args: argparse.Namespace) -> Selector:
    """Turn the mutually-exclusive flags into the ONE selector value the engine filters on."""
    if args.run is not None:
        return Selector(kind="run", value=args.run)
    if args.event is not None:
        return Selector(kind="event", value=args.event)
    return Selector(kind="all")


def _print_requeue_report(
    layout: RepoLayout, report: RequeueReport, *, reset_attempts: bool
) -> None:
    """Render one :class:`RequeueReport` in the house grammar (#99 §4.3 — these bytes are locked).

    ``key: value`` on stdout with a two-space indent for per-item lines (``_cmd_harvest``), paths
    repo-relative POSIX (``worker._fail``'s ``record_path`` convention, so the output can be pasted
    into an issue and matched against ``agora status``), and ``[dry-run]`` on the header.

    The two summary lines print even at 0: a constant, greppable shape is what lets a script read
    this report without parsing prose (the ``counters:`` precedent). ``failed_events:`` is the
    PREDICTED post-count in BOTH modes — never the pre-count — because it is the number the dry run
    is promising to produce, which is what makes the preview comparable to the result (crit 5).
    """
    dry = report.dry_run
    print(
        f"requeue{' [dry-run]' if dry else ''}: "
        f"selector={report.selector.label} matched={len(report.items)}"
    )
    for item in report.items:
        print(f"  {item.label}: {_requeue_item_line(layout, item, dry_run=dry)}")

    moved = report.moved
    print(f"{'would requeue' if dry else 'requeued'}: {len(moved)}")
    declined = [item for item in report.items if item.outcome in _REQUEUE_DECLINED]
    tally = ""
    if declined:
        counts = Counter(str(item.outcome) for item in declined)
        tally = " (" + " ".join(f"{slug}={counts[slug]}" for slug in sorted(counts)) + ")"
    print(f"{'would skip' if dry else 'skipped'}: {len(declined)}{tally}")
    errors = sum(1 for item in report.items if item.outcome is RequeueOutcome.error)
    if errors:
        print(f"errors: {errors}")

    if reset_attempts:
        # The header carries the mode; the INDENTED lines are byte-identical in both modes, which
        # turns "the dry-run list matches the result" into a literal string equality (crit 5 ∩ 8).
        print(
            f"reset_attempts{' [dry-run]' if dry else ''}: "
            f"archived={len(report.archived)} kept={len(report.kept)}"
        )
        for archived in report.archived:
            print(f"  archived: {archived.source} -> {archived.dest}")
        for kept in report.kept:
            print(f"  kept: {kept.source} ({kept.reason})")

    print(f"failed_events: {report.failed_events_after}")
    if moved:
        print(_REQUEUE_NOTE)


def _requeue_item_line(layout: RepoLayout, item: RequeueItem, *, dry_run: bool) -> str:
    """One event's verdict, after the ``  <label>: `` prefix.

    ``forced`` says WHY a known-doomed event moved rather than hiding it behind a plain
    ``requeued``, and an ``error`` is deliberately NOT rendered as a skip: a skip is a correct
    refusal, an error is work that could not be done.
    """
    if item.outcome in MOVING_OUTCOMES:
        verb = "would requeue" if dry_run else "requeued"
        why = " (already-delivered; forced)" if item.outcome is RequeueOutcome.forced else ""
        return f"{verb}{why} -> {rel_to_repo(layout, item.dest)}"
    if item.outcome is RequeueOutcome.error:
        return f"error ({item.detail})"
    return f"{'would skip' if dry_run else 'skipped'} ({item.outcome}: {item.detail})"


def _warn_unresolved_failure(layout: RepoLayout, state: CuratorState) -> None:
    """Warn that ``--all`` is requeueing into a curator whose last failure is still UNRESOLVED.

    ``run_requeue``'s preflight hook, so it lands on stderr BEFORE the batch moves, off the very
    state the batch used (a corrupt ``state.json`` means no state and therefore no preflight — not
    this command's problem to report, since ``agora status`` and ``agora doctor`` both say so
    loudly, and the whole point of requeue is to work in a broken repo). Never raises and never
    changes the exit code.
    """
    lf = state.last_failure
    if lf is None or not state.failure_is_current:
        return
    print(
        f"warning: the last curator failure is still UNRESOLVED (run={lf.run_id} "
        f"record={_record_pointer(layout, lf.record_path)}) — run 'agora doctor' first; "
        "a requeued event goes terminal again on the next failing run",
        file=sys.stderr,
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


def _cmd_sync(args: argparse.Namespace) -> int:
    """``agora sync``: push the curated branch to the configured backup remote (issue #64).

    STRICTLY PUSH-ONLY, one direction: local curated tip → ``backup.remote`` (a git remote name or
    URL from ``_kb/repo.yaml``). No pull/fetch code exists in this slice — a non-fast-forward
    rejection (the remote is ahead: another machine has likely pushed) is reported as a clear error
    naming issue #46, never forced. With NO remote configured — on an INITIALIZED repo — this is a
    guided NO-OP success (exit 0), so the command is safe to script unconditionally. A missing or
    uninitialized ``--repo`` path exits 1 FIRST: a typoed cron path or an unmounted volume must
    never read as "no remote configured" and report success (that would be silently NOT backing up
    — the exact failure mode the ``backup:`` config parsing refuses to allow). The outcome
    (ok/failed + instant) is recorded best-effort in the non-canonical ``_kb/backup.json`` for the
    ``agora doctor`` line. A push failure exits 1 with a clean message (an explicit operator action
    reports honestly — the best-effort swallow belongs to the watch tick, not here) but never a
    traceback.
    """
    repo = Repo.resolve(args.repo)
    layout = repo.layout
    print(f"repo: {layout.root}")
    # BEFORE the guided no-op below: a nonexistent/uninitialized path must fail loudly, or a
    # --repo typo in a cron line reports exit-0 success forever while nothing is ever pushed.
    if not repo.is_initialized():
        print(f"{_PROG} sync: repo not initialized (run 'agora repo init')", file=sys.stderr)
        return 1
    try:
        policy = load_backup_policy(layout)
    except ConfigError as exc:
        print(f"{_PROG} sync: invalid config — {exc}", file=sys.stderr)
        return 1

    if policy.remote is None:
        print(
            "sync: no backup remote configured — nothing to push. Set backup.remote (a git "
            "remote name or URL) in _kb/repo.yaml to enable push-only backup (issue #64)."
        )
        return 0

    now = datetime.now(UTC)
    try:
        sha = repo.push_backup(policy.remote)
    except (GitError, ValueError) as exc:
        _record_backup_result(layout, remote=policy.remote, ok=False, when=now, error=str(exc))
        print(f"{_PROG} sync: push failed — {exc}", file=sys.stderr)
        return 1
    _record_backup_result(layout, remote=policy.remote, ok=True, when=now, commit=sha)
    print(f"sync: pushed {repo.branch} @ {sha[:12]} -> {policy.remote}")
    return 0


def _record_backup_result(
    layout: RepoLayout,
    *,
    remote: str,
    ok: bool,
    when: datetime,
    commit: str | None = None,
    error: str | None = None,
) -> None:
    """Best-effort record of the last backup push into ``_kb/backup.json`` (issue #64).

    The same derived-state posture as the ``_kb/harvest/`` cursors (DATA-MODEL §6): git-ignored,
    non-canonical, expendable — losing it only blanks the ``agora doctor`` backup line. Written
    atomically; NEVER raises (an unrecordable result must not turn a successful push — or a
    best-effort watch push — into a failure).
    """
    doc = {
        "remote": remote,
        "ok": ok,
        "at": when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": commit,
        "error": error,
    }
    try:
        path = layout.backup_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(doc, indent=2) + "\n", exclusive=False)
    except OSError:
        pass


def _auto_backup_push(repo: Repo, *, stamp: str) -> None:
    """Best-effort post-consolidation backup push for the ``agora watch`` tick (issue #64).

    Runs ONLY when ``backup.auto: true`` AND ``backup.remote`` is set — otherwise it returns
    without a single side effect, keeping the default watch path byte-identical. A push failure
    NEVER fails (or retries) the curation that just published — the wiki commit is already durable
    locally; this prints one warning line, records the outcome for ``agora doctor``, and moves on.
    The push is NON-INTERACTIVE and time-bounded (``interactive=False`` + a tight timeout): an
    unattended scheduler must never sit on a credential/host-key prompt or a network blackhole —
    a hang here would stall the ``agora watch`` loop itself, which is the one way a best-effort
    push could break curation.
    """
    layout = repo.layout
    try:
        policy = load_backup_policy(layout)
    except ConfigError as exc:
        # Surface the typo without disturbing the scheduler; `agora sync` reports it loudly.
        print(f"{stamp} backup config invalid (skipping push): {exc}")
        return
    if not policy.auto or policy.remote is None:
        return
    now = datetime.now(UTC)
    try:
        # interactive=False: prompts fail fast into the record/warn path below (never a scheduler
        # stall); 120s bounds even a prompt-free hang (blackhole) to a fraction of push_backup's
        # operator-facing default.
        sha = repo.push_backup(policy.remote, interactive=False, timeout=120.0)
    except (GitError, ValueError) as exc:
        _record_backup_result(layout, remote=policy.remote, ok=False, when=now, error=str(exc))
        print(f"{stamp} backup push failed (best-effort; curation unaffected): {exc}")
        return
    _record_backup_result(layout, remote=policy.remote, ok=True, when=now, commit=sha)
    print(f"{stamp} backup pushed: {repo.branch} @ {sha[:12]} -> {policy.remote}")


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

    A tick that RAISES no longer kills the scheduler (issue #97): it is reported on stderr as one
    bounded line and the loop backs off exponentially instead of exiting into a supervisor's
    restart policy. ``--once`` keeps the single-shot contract — it reports the same line and
    returns 1, so an external scheduler still sees the failure in the exit code.
    """
    repo = Repo.resolve(args.repo)
    interval = max(1, args.interval)
    # Python block-buffers stdout whenever it is not a TTY, and BOTH shipped units capture it to a
    # file/journal (deploy/systemd/agora-watch.service, deploy/launchd/com.agora.watch.plist) — so
    # by default an operator running `journalctl -u agora-watch` sees NOTHING until 8 KB of tick
    # lines accumulate, which at one short line per minute is hours. Every stdout line this loop
    # emits is a timeline event that is worthless late: the `idle:`/`ran (…)` log and, since #96,
    # the `failed_record:`/`failed_checks:` pair. stderr is already line-buffered, which is why the
    # #115 `warning:` lines and #97's `tick failed:` line were the only ones that ever showed up.
    # Line-buffering ONLY here: `agora watch` is the one long-running command — every other command
    # exits promptly and flushes on exit, so this stays a daemon concern, not a global one.
    sys.stdout.reconfigure(line_buffering=True)
    mode = " [once]" if args.once else f" (interval={interval}s)"
    print(f"agora watch: {repo.layout.root}{mode}")
    failures = 0
    try:
        while True:
            try:
                productive = _watch_tick(repo)
            except Exception as exc:  # noqa: BLE001 — one bad tick must never kill the scheduler
                # Issue #97: a DETERMINISTIC per-tick raise (a corrupt state.json — StateStore.load
                # is fail-loud by design; a repo.yaml typo — load_repo_config raises ConfigError)
                # would otherwise exit non-zero into `Restart=on-failure`/`RestartSec=10`
                # (deploy/systemd/agora-watch.service) or launchd `KeepAlive`
                # (deploy/launchd/com.agora.watch.plist), turning a 60s scheduler into a 10s crash
                # loop that never fixes the YAML. Report once, back off, stay alive.
                _print_tick_failure(exc)
                if args.once:
                    return 1
                failures += 1
            else:
                if args.once:
                    break
                # A tick that could not build a backend did NOT raise — `_build_backend` catches the
                # ConfigError, prints it, and returns None — so without this it counted as success
                # and RESET the backoff. Reproduced: a malformed `adapters.yaml` at `--interval 1`
                # emitted 18 multi-line parse errors in 8 seconds, forever, with zero `tick failed:`
                # lines. That is #97's own disease (one typo amplified into a log flood) wearing a
                # different hat, so it gets #97's own cure. It is NOT reported as a failed tick —
                # the tick already printed its own `due (…) but no usable backend` line, and calling
                # it a raise would be a lie — it only feeds the backoff.
                failures = 0 if productive else failures + 1
            # OUTSIDE the guard on purpose: a FAILED tick must still sleep. Sleeping only in the
            # `else` branch would busy-spin at 100% CPU on a permanent failure — strictly worse
            # than the crash loop this removes.
            _watch_sleep(_watch_backoff_delay(interval, failures))
    except KeyboardInterrupt:
        # NOT re-raised from the inner guard: `KeyboardInterrupt` is a `BaseException`, so the
        # `except Exception` above provably cannot see it. Ctrl-C during a tick OR during a sleep
        # lands here, exactly as before #97.
        print("agora watch: stopped")
    return 0


# Issue #97. How much of a tick exception rides on the ONE stderr line. Larger than
# _doctor_backup's 140 because we COLLAPSE whitespace rather than truncate at the first newline
# (see _one_line): the two exceptions that actually reach this path — pydantic's ValidationError
# and a ConfigError wrapping a yaml.YAMLError — both put the diagnosis on LINE 2.
_TICK_DETAIL_CHARS = 200

# Opt-in traceback for bug reports. Same posture and naming family as AGORA_BRAIN_DEBUG
# (adapters/ollama_brain.py) — a debug hook, NOT a supported CLI surface: the one-line summary is
# the operator's test-locked contract, whereas a traceback's content is a CPython/pydantic
# implementation detail we must not promise. An env var also leaves `p_watch` untouched, which is
# what keeps #97's "nothing else changes" criterion structural rather than a claim.
_WATCH_TRACEBACK_ENV = "AGORA_WATCH_TRACEBACK"
# Explicit falsey set (case-folded) rather than bare truthiness: `AGORA_WATCH_TRACEBACK=0` meaning
# "on" is a footgun.
_FALSEY = frozenset({"", "0", "false", "no", "off"})


def _tick_failure_detail(exc: BaseException) -> str:
    """One bounded single-line rendering of a tick exception: ``Type: message``."""
    return f"{type(exc).__name__}: {_one_line(str(exc), _TICK_DETAIL_CHARS)}"


def _print_tick_failure(exc: BaseException) -> None:
    """Report ONE failed ``agora watch`` tick on STDERR — the #115 channel, extended (issue #97).

    Same sink and same discipline as :func:`_print_run_diagnostics`' warnings half: one bounded
    line, no traceback, stdout's machine-readable tick log untouched. It CANNOT ride on
    ``RunReport.warnings`` — a tick that raises produces no ``RunReport`` at all, because the raise
    routinely PRECEDES ``worker.run`` entirely (``load_repo_config`` is the tick's FIRST statement
    and ``recover()`` its second).

    The stamp is taken HERE, not from the tick: :func:`_watch_tick` computes its own stamp one line
    AFTER the config load that is the most likely thing to raise, so on the failure path no tick
    stamp exists. The loop owns this one, in the tick log's ``{stamp} <verb>:`` grammar so
    `journalctl -u agora-watch` stays ONE stream.
    """
    print(f"{_fmt_dt(datetime.now(UTC))} tick failed: {_tick_failure_detail(exc)}", file=sys.stderr)
    if os.environ.get(_WATCH_TRACEBACK_ENV, "").strip().lower() not in _FALSEY:
        traceback.print_exception(exc, file=sys.stderr)


# Issue #97. 15 minutes: at the 60s default this bounds a permanently-broken repo to ~96 stderr
# lines/day (vs ~1440 unbounded, vs ~8640 under the RestartSec=10 crash loop this replaces), while
# staying short enough that an operator who fixes repo.yaml sees recovery within one coffee.
_WATCH_BACKOFF_CAP_S = 900

# 2**n with unbounded n is a real hazard (Python ints are arbitrary-precision: a month-long broken
# loop would allocate a multi-megabyte integer every tick). Clamp the SHIFT, not just the product.
# 10 is provably sufficient: the smallest interval is 1 (`max(1, args.interval)`) and 1*2**10 =
# 1024 > 900 = the cap, so no larger shift can ever change the result.
_WATCH_BACKOFF_MAX_SHIFT = 10


def _watch_backoff_delay(interval: int, consecutive_failures: int) -> int:
    """Seconds to sleep before the next ``agora watch`` tick — PURE, no clock, no sleep (#97).

    ``interval * 2**n`` capped, ``n`` = consecutive failed ticks so far (1-based on the FIRST
    failure, per #97). ``n == 0`` (a clean tick) returns ``interval`` unchanged — that is what keeps
    the happy path byte-identical AND lets the loop hold exactly ONE sleep expression.

    The cap is ``max(interval, _WATCH_BACKOFF_CAP_S)``, never the bare constant: backoff must only
    ever make the loop SLOWER. With ``--interval 3600`` a bare 900s cap would SPEED THE SCHEDULER UP
    4x on failure — a change nobody asked for, and pointless besides (an hourly loop cannot flood).
    """
    shift = min(max(consecutive_failures, 0), _WATCH_BACKOFF_MAX_SHIFT)
    return min(interval * (2**shift), max(interval, _WATCH_BACKOFF_CAP_S))


def _watch_sleep(seconds: float) -> None:
    """Indirection so the ``agora watch`` loop is drivable in tests (#97 criteria 1-3, 5).

    ``time.sleep`` is called ONLY here; a test replaces this module attribute to record the schedule
    and to terminate the loop deterministically. Same idiom as
    ``monkeypatch.setattr(cli_mod, "run", …)`` in tests/test_cli.py.
    """
    time.sleep(seconds)


def _watch_tick(repo: Repo) -> bool:
    """One scheduler iteration: recover, evaluate the triggers, and run ONE consolidation if due.

    Loads the repo config fresh, evaluates ``threshold``/``idle``/``cron`` (``cron_due`` derived
    from the configured schedule + ``last_run``), and — when a signal fires — builds the configured
    sandbox-confined backend and executes :func:`agora_kb.curator.worker.run`. Prints one concise,
    timestamped status line per tick (idle / ran / due-but-no-backend). The integrity verdict is the
    worker's; this only decides *when* to wake it.

    Returns whether the tick was PRODUCTIVE — ``False`` only for the one non-raising dead end, a due
    tick with no usable backend (issue #97). A `ConfigError` in ``adapters.yaml`` never reaches the
    loop's guard because ``_build_backend`` catches and prints it, so that state would otherwise
    repeat at the full interval forever with no backoff; the loop feeds this flag to
    :func:`_watch_backoff_delay` instead. ``True`` for every other outcome INCLUDING ``idle:`` — an
    idle tick is a correct decision on an empty queue, i.e. the steady state, not a degradation.
    """
    layout = repo.layout
    cfg = load_repo_config(layout)
    # #98 re-asserted PER TICK, not just at dispatch. `watch` is the one always-on command (the
    # launchd/systemd units) and it hosts the single writer of `wiki/`, so a process that started
    # on a v1 repo would keep curating onto a repo that became v2 under it — a `git pull` of a
    # newer schema, or a future `agora repo upgrade`, is exactly how that happens. The tick already
    # re-reads the config every pass so operator edits take effect without a restart; the schema is
    # part of what may have changed. The raise lands in the loop's own guard (#97), so it surfaces
    # as one bounded tick error rather than a crash loop.
    guard_repo_schema_version(layout)
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
        return True

    backend = _build_backend(
        layout,
        default_backend=cfg.default_backend,
        allow_reduced_isolation=cfg.allow_reduced_isolation,
        body_byte_bound=cfg.body_byte_bound,
        language=cfg.language,
    )
    if backend is None:
        print(f"{stamp} due ({decision.reason}) but no usable backend — skipping this tick")
        return False

    report = run(
        repo,
        backend=backend,
        state_store=StateStore(layout),
        now=now,
        taxonomy=cfg.taxonomy,
        max_attempts=cfg.max_attempts,
        related_k=cfg.related_k,
        max_candidates=cfg.max_candidates_per_run,
        max_orphans=cfg.max_orphans,
    )
    counts = ", ".join(f"{op}={n}" for op, n in sorted(report.counts.items())) or "-"
    commit = report.published_commit or "-"
    print(
        f"{stamp} ran ({decision.reason}): status={report.status} commit={commit} counts={counts}"
    )
    # An always-on watch loop is exactly where a silently prose-less run would accumulate unnoticed
    # (#115) — the per-tick line above says "published" either way. The #96 failure lines are
    # STAMPED so they stay inside the tick log's `{stamp} <verb>:` grammar; the `warning:` lines
    # keep their unprefixed #115 bytes.
    _print_run_diagnostics(report, prefix=f"{stamp} ")
    # Issue #64: best-effort backup push, ONLY after a curation that actually published and ONLY
    # when backup.auto is opted in (default off → this call is side-effect-free, byte-identical).
    if report.status == "published":
        _auto_backup_push(repo, stamp=stamp)
    # A run that FAILED is still a productive tick: the backend answered, the retry budget moved,
    # and `_print_run_diagnostics` just pointed at the error record. Backoff is for the dead ends
    # that repeat identically forever, not for work that is progressing badly.
    return True


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


# agora doctor's brain probe budget (#96 criterion 5). Deliberately SHORTER than the shim's own 10s
# runtime default: 10s is the right budget for a real curate run, 3s for a yes/no reachability
# question an operator is waiting on. 3.0 (the top of #96's 2-3s band) because a loopback daemon
# mid model-load can take >2s to answer /api/tags and a false UNREACHABLE is the expensive error.
_BRAIN_PROBE_TIMEOUT_S = 3.0

# Remediation hint reused across the probe's failure renderings. The example model mirrors the one
# the shim's OWN no-models BrainError already names (ollama_brain.select_model). A HINT only — never
# routing (invariant 6: the model used always comes from select_model / adapters.yaml).
_OLLAMA_PULL_HINT = "ollama pull qwen3.6:35b-a3b"


def _cmd_doctor(args: argparse.Namespace) -> int:
    ok = True

    # The header doubles as the bug-report banner (issue #101): ONE line an operator can paste that
    # answers the two questions every report starts with, "which build?" and "which interpreter?".
    # Both facts also appear below in verdict form (the `python:` line owns the >= 3.12 check); the
    # duplication is intentional — a header that a reporter can copy in isolation is worth more than
    # the saved line, and the `agora doctor` prefix is preserved so existing greps still match.
    v = sys.version_info
    print(f"agora doctor (agora {__version__}, python {v.major}.{v.minor}.{v.micro})")

    git_path = shutil.which("git")
    if git_path:
        print(f"  git: ok ({git_path})")
    else:
        print("  git: MISSING (required for the curated source of truth)")
        ok = False

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
    cfg = _doctor_repo_config(layout)

    # #98 / DESIGN §10 V9: the schema-skew line. Doctor is EXEMPT from the guard that stops every
    # other command, precisely so this line can exist — and it is the one `_doctor_*` line about a
    # FILE that still moves the verdict, because a repo this build cannot read makes every line
    # below it a guess.
    ok = _doctor_schema(layout) and ok

    # Loaded BEFORE the sandbox probe (#129): the `sandbox:` block reports not just "the mechanism
    # works on this host" but "does it confine THIS repo's brains", and that second answer is the
    # resolved backends' `network` posture. Still ONE parse shared with routing/brains below.
    registry, registry_error = _doctor_backend_registry(layout)

    ok = (
        _doctor_sandbox(
            cfg.allow_reduced_isolation, registry=registry, default_backend=cfg.default_backend
        )
        and ok
    )

    # ADR-0015: observability — which brain runs each cognitive act (default or routed). Reporting
    # only; never affects the health verdict.
    _doctor_routing(registry, registry_error, cfg.default_backend)

    # Issue #96: is that brain actually ANSWERABLE? The first verdict contributor that asks the
    # question a first-run failure actually turns on. Same AND-form and operand ORDER as
    # _doctor_sandbox above — the probe must run even when `ok` is already False.
    ok = (
        _doctor_brains(
            registry, cfg.default_backend, registry_error=registry_error, skip_probe=args.skip_probe
        )
        and ok
    )

    # ADR-0007: observability — the harvester policy + configured connectors with cursor state.
    # Reporting only; never affects the health verdict and never crashes on malformed config.
    _doctor_connectors(layout)

    # ADR-0012 §2 / issue #26: observability — the derived reader-cache state (present/fresh/stale).
    # Reporting only; never affects the health verdict and never crashes on malformed config.
    _doctor_index(layout)

    # ADR-0027 / issue #37: observability — the derived gold-pack state + the §8 scan exclusion.
    # Reporting only; never affects the health verdict and never crashes on malformed config.
    _doctor_gold(layout)

    # Issue #64: observability — push-only backup config + the last recorded push outcome.
    # Reporting only; never affects the health verdict and never crashes on malformed config.
    _doctor_backup(layout)

    # Issue #96: observability — the failure backlog + the last attempt/failure the curator
    # recorded. Reporting only; never affects the health verdict and never crashes.
    _doctor_failures(layout)

    print(f"status: {'healthy' if ok else 'unhealthy'}")
    return 0 if ok else 1


def _doctor_schema(layout: RepoLayout) -> bool:
    """Print ``schema: repo=<n> supported=[...]``; False when this build does not support it.

    Issue #98 / DESIGN §10 V9. Unlike the purely observational ``_doctor_*`` lines, this one FEEDS
    THE VERDICT: `agora status` / `curate` / `harvest` / `serve` / `web` all refuse on an
    unsupported repo, so reporting `status: healthy` for a tree where nothing but doctor can run
    would be a lie. Doctor stays the exempt command — it must reach this line to explain WHY the
    others exited 1.

    Takes the LAYOUT, not the loaded :class:`RepoConfig`, and reads the canonical value through
    :func:`~agora_kb.config.read_kb_schema_version` — the SAME narrow read the guard makes, so the
    two provably cannot disagree. Reading it off ``cfg`` was a real defect: ``_doctor_repo_config``
    substitutes DEFAULTS whenever ``repo.yaml`` fails to load for ANY reason, so a schema-2 repo
    with an unrelated ``repo.yaml`` typo made doctor print ``repo=1 supported=[1]`` and vote
    ``healthy`` — asserting a version it never read, about the one repo this line exists for.

    An indeterminate version is reported as such and FAILS the verdict: doctor's whole job here is
    to answer the question the other commands refuse on, and "I could not tell" is not a pass.
    Never crashes — the reader swallows every I/O and parse error into ``None``.
    """
    supported = sorted(SUPPORTED_KB_SCHEMA_VERSIONS)
    version = read_kb_schema_version(layout)
    if version is None:
        print(f"  schema: repo=? supported={supported} (UNREADABLE — cannot verify)")
        return False
    if version in SUPPORTED_KB_SCHEMA_VERSIONS:
        print(f"  schema: repo={version} supported={supported}")
        return True
    print(f"  schema: repo={version} supported={supported} (UNSUPPORTED — upgrade agora)")
    return False


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


def _doctor_backup(layout: RepoLayout) -> None:
    """Print the push-only backup state (issue #64). Observability only — never crashes.

    Shows whether a ``backup.remote`` is configured (+ the ``auto`` flag) and the last recorded
    push outcome from the non-canonical ``_kb/backup.json`` (absent → ``never``). Never affects
    the health verdict: an unconfigured backup is a valid — if single-copy — setup, surfaced as
    information, not ill health; ``agora sync`` reports real push failures loudly.
    """
    try:
        policy = load_backup_policy(layout)
    except Exception as exc:  # noqa: BLE001 — doctor must never crash on a malformed repo.yaml.
        print(f"  backup: repo.yaml present but unreadable ({exc})")
        return
    if policy.remote is None:
        print("  backup: no remote configured (push-only backup off — set backup.remote, #64)")
        return
    last = "last_push=never"
    try:
        raw = json.loads(layout.backup_state_path.read_text(encoding="utf-8"))
        at = raw.get("at") or "?"
        if raw.get("ok"):
            last = f"last_push={at} ok @ {(raw.get('commit') or '')[:12]}"
        else:
            # One-line doctor rendering: the FULL error stays in _kb/backup.json; here only its
            # first line, truncated, so a multi-line git stderr cannot flood the health report.
            first = str(raw.get("error") or "unknown error").splitlines()[0]
            if len(first) > 140:
                first = first[:140].rstrip() + "…"
            last = f"last_push={at} FAILED ({first})"
    except OSError:
        pass  # absent/unreadable file → "never"
    except (ValueError, AttributeError, IndexError, TypeError):
        # TypeError included: a corrupted state file (e.g. a non-string "commit") must degrade to
        # "unreadable", never crash doctor — doctor's whole job is diagnosing broken repos.
        last = "last_push=unreadable"
    print(f"  backup: remote={policy.remote} auto={policy.auto} {last}")


def _doctor_failures(layout: RepoLayout) -> None:
    """Print the failure backlog + last attempt/last failure (issue #96: doctor half).

    Observability only — never affects the health verdict and never crashes. A corrupt
    ``state.json`` is REPORTED here rather than raised: it is exactly the state in which an
    operator runs `agora doctor`, and `agora status`/`agora watch` already tell them loudly.

    Since #99 a backlog also gets a ``requeue:`` line. Doctor is the right PULL surface for it: it
    is the only place that holds the CAUSE (#96's brain probe) and the backlog in one output, and
    this helper runs LAST among the ``_doctor_*`` helpers, so "fix the cause above" is literally
    true — the doctor-first ordering the #99 risk section demands. Still observability only: a
    recoverable backlog is not ill health, so the verdict is untouched, and the count comes from
    the SHARED ``failed_event_count`` (#99 crit 7), never a second glob.
    """
    try:
        events = failed_event_count(layout)
    except Exception as exc:  # noqa: BLE001 — doctor must never crash.
        print(f"  failures: unreadable ({_one_line(str(exc), 140)})")
        return
    try:
        state = StateStore(layout).load()
    except Exception as exc:  # noqa: BLE001 — a corrupt state.json is a REPORT, not a crash.
        print(f"  failures: events={events} state.json unreadable ({_one_line(str(exc), 140)})")
        # The backlog line still prints: a corrupt state.json is precisely the repo where requeue
        # (with `--force`, which relaxes the tier-1 pre-check's state load) is the next step, and
        # `events` is already known. Only the "fix the cause above" prefix is dropped — there is no
        # `failure_is_current` to consult.
        _print_requeue_backlog(events, fix="")
        return
    lf = state.last_failure
    tail = (
        "none"
        if lf is None
        else (
            f"{'UNRESOLVED' if state.failure_is_current else 'superseded'} run={lf.run_id} "
            f"record={_record_pointer(layout, lf.record_path)}"
        )
    )
    print(
        f"  failures: events={events} last_attempt={_fmt_dt(state.last_attempt)} "
        f"last_failure={tail}"
    )
    _print_requeue_backlog(
        events, fix="fix the cause above, then " if state.failure_is_current else ""
    )


def _print_requeue_backlog(events: int, *, fix: str) -> None:
    """The doctor's ``requeue:`` backlog line — ONE spelling for both ``_doctor_failures`` exits."""
    if events <= 0:
        return
    plural = "" if events == 1 else "s"
    print(
        f"  requeue: {events} terminal event{plural} in _kb/failed/ — "
        f"{fix}'{_PROG} requeue --all' returns the backlog to the inbox"
    )


def _doctor_repo_config(layout: RepoLayout) -> RepoConfig:
    """Load ``_kb/repo.yaml`` for doctor, falling back to DEFAULTS on a malformed file.

    `agora doctor` must ALWAYS reach its ``status:`` line — a machine consuming the report needs a
    verdict, and a YAML typo is precisely the state an operator runs doctor in. `agora curate`
    still fails loudly on the same file. Loaded ONCE here for both the sandbox probe and the
    routing/brains lines, so a config problem is reported once, in one place.

    Catches BROADLY, like every sibling ``_doctor_*`` helper: ``load_repo_config`` wraps only
    ``yaml.YAMLError`` in ``ConfigError``, so a file that is unreadable or not UTF-8 (a CP949
    editor writing a Korean ``name:`` — epic #85 / issue #57) still raises straight through and
    would kill doctor BEFORE the verdict line, which is the exact failure this guard exists to
    remove.
    """
    try:
        return load_repo_config(layout)
    except Exception as exc:  # noqa: BLE001 — doctor must never crash on a malformed repo.yaml.
        print(f"  repo.yaml: unreadable ({_one_line(str(exc), 140)}) — using defaults")
        return RepoConfig()


def _doctor_backend_registry(layout: RepoLayout) -> tuple[BackendRegistry | None, str | None]:
    """Load ``adapters.yaml`` ONCE for both the routing table and the brain probe. Never raises.

    ``(None, None)`` = file ABSENT; ``(None, "<msg>")`` = present but unreadable;
    ``(registry, None)`` otherwise. One parse means the two lines cannot contradict each other and
    a config problem is reported once (#96: "reuse the set _doctor_routing already resolved").
    """
    try:
        return load_backend_registry(layout.root / "adapters.yaml"), None
    except Exception as exc:  # noqa: BLE001 — doctor must never crash on a malformed adapters.yaml.
        return None, str(exc)


def _doctor_routing(
    registry: BackendRegistry | None,
    load_error: str | None,
    default_backend: str | None = None,
) -> None:
    """Print the ADR-0015 per-act routing table (which brain runs ``plan`` / ``author``).

    Observability only — never affects the health verdict and never crashes: an absent
    ``adapters.yaml`` is noted and a malformed one is skipped (``agora curate`` surfaces the real
    config error loudly). ``default_backend`` (the repo's ``curator.backend``) is threaded so the
    table reflects the SAME precedence a real run uses. Showing each act's ``network`` posture lets
    an operator see BEFORE a run that e.g. routing an act to a ``network: 'none'`` brain on a
    sandbox-less host will fail closed (ADR-0013), or that routing ``author`` to a metered API
    multiplies cost (PASS-2 runs per region).

    A PURE RENDERER since #96: the parse itself moved to :func:`_doctor_backend_registry` so this
    line and the ``brains:`` line below it are two views of ONE registry and cannot disagree.
    """
    if load_error is not None:
        print(f"  routing: adapters.yaml present but unreadable ({load_error})")
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


def _routed_brain_names(registry: BackendRegistry, default_backend: str | None) -> list[str]:
    """Unique backend names across the routable acts, in FIRST-SEEN act order (plan, then author).

    ``routed_backends`` is a dict comp over ``_ROUTABLE_ACTS = ("plan", "author")``
    (curator/backends.py), so plan=X/author=X yields ``["X"]`` (ONE probe) and plan=X/author=Y
    yields ``["X", "Y"]``. ``dict.fromkeys`` is the dedupe: insertion-ordered and deterministic —
    the output is test-locked, so an unordered ``set`` would be a flaky test.
    """
    return list(dict.fromkeys(registry.routed_backends(default=default_backend).values()))


def _ollama_argv_tail(argv: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return the shim's OWN argument tail when ``argv`` invokes the Ollama brain, else ``None``.

    Two ACCEPTED shapes, both unambiguous:

    * ``argv[0]``'s basename (minus a Windows ``.exe``) == ``ollama_brain.CONSOLE_SCRIPT`` —
      covers the bare name ``repo init`` emits (config.write_default_adapters_yaml) AND any path to
      the installed console script. Tail = ``argv[1:]``.
    * an adjacent ``-m <ollama_brain.__name__>`` pair — the ``python -m`` form under ANY
      interpreter path. Tail = everything after the module name.

    Every other shape returns ``None`` ON PURPOSE — in particular wrappers (``uv run …``,
    ``sh -c …``, ``env FOO=1 …``, ``docker run …``). For a wrapper the flags belong to the WRAPPER,
    and feeding them to the shim's parser yields a confidently WRONG model/host report, which is
    strictly worse than the honest generic ``argv[0]``-on-PATH fallback (#96 criterion 4). No
    heuristic scan for the script name anywhere in argv: guessing where the shim's own arguments
    begin is exactly the guess that produces the wrong answer.
    """
    from .adapters import ollama_brain

    # BackendSpec.argv is validated non-empty (backends.BackendSpec._check_argv), so argv[0] cannot
    # IndexError; Path("").name == "" simply falls through to the generic probe.
    program = Path(argv[0]).name
    if program.lower().endswith(".exe"):
        program = program[: -len(".exe")]
    # Case-INSENSITIVE: the `.exe` shape only exists on Windows, whose filesystem is
    # case-insensitive, so `Agora-Ollama-Brain.exe` is the same program (epic #85). A case-folded
    # match on POSIX costs nothing — a differently-cased file there would be a different program,
    # but treating it as the shim only means parsing its tail with the shim's own parser.
    if program.casefold() == ollama_brain.CONSOLE_SCRIPT.casefold():
        return tuple(argv[1:])
    module = ollama_brain.__name__
    for index, token in enumerate(argv[:-1]):
        if token == "-m" and argv[index + 1] == module:
            return tuple(argv[index + 2 :])
    return None


def _probe_ollama(tail: tuple[str, ...], *, timeout: float) -> tuple[str, bool]:
    """Probe an Ollama-shim backend; return ``(rendering, healthy)``.

    Mirrors :func:`ollama_brain._resolve_model` EXACTLY **on model selection** so doctor's answer
    and the run's answer cannot diverge:

    * an explicit ``--model`` short-circuits ``/api/tags`` ENTIRELY (#96 criterion 3) — doctor does
      not list models either, because "which models are installed" is a fact the run never
      establishes on this path. It DOES check liveness (``ping_ollama``, #129): the mirror is about
      model SELECTION, while every run — pinned or not — still does ``POST /api/generate``, so a
      dead daemon fails it. Checking a precondition the run really has cannot diverge from the run;
      skipping it made doctor report ``healthy`` on a repo where ``agora curate`` could not run;
    * otherwise ``list_ollama_models(host)`` runs FIRST and its result is fed VERBATIM to
      ``select_model(None, $AGORA_OLLAMA_MODEL, available)``. Note the consequence, which is why
      #96 mandates calling the real functions: an ENV-pinned model does NOT short-circuit the
      daemon call (argument evaluation order in ``_resolve_model``), so a down daemon fails a run
      even with the model pinned in the environment — and doctor reproduces that.

    A model named by ``--model`` / ``$AGORA_OLLAMA_MODEL`` is reported but NOT verified to be
    installed: ``/api/tags`` returns fully-qualified ``name:tag`` while Ollama resolves an
    unqualified name to ``:latest``, so exact membership is not a sound existence test and would
    paint a working host UNHEALTHY. The blind spot is stated in the rendered line itself.
    """
    from .adapters import ollama_brain

    args = ollama_brain.parse_shim_args(list(tail))
    if args is None:
        return (
            "ollama shim argv UNPARSEABLE by the shim's own parser — check the adapters.yaml argv",
            False,
        )
    host = args.host
    if args.model and args.model.strip():
        # #129: the /api/TAGS short-circuit stays (a pinned run lists no models, and doctor must
        # not answer a question the run never asks), but LIVENESS is checked — a real run does
        # POST /api/generate, so a dead daemon fails it. Before this, a pinned repo ran ZERO
        # reachability checks and doctor said `healthy` while `agora curate` could not run at all.
        pin = args.model.strip()
        try:
            ollama_brain.ping_ollama(host, timeout=timeout)
        except ollama_brain.BrainError as exc:
            return (f"ollama {host} UNREACHABLE ({exc}) [model pinned to {pin!r}]", False)
        except ValueError as exc:
            # A PORTLESS host (`AGORA_OLLAMA_HOST=localhost`) makes urlopen raise a bare ValueError
            # that ping_ollama does NOT wrap — verified, and NOT the same shape as
            # `localhost:11434`, which urlopen reads as scheme `localhost` and reports as a
            # URLError. Same first-run typo family, same rendering as the auto-select branch below.
            return (
                f"ollama {host} UNREACHABLE ({type(exc).__name__}: {exc}) "
                f"[model pinned to {pin!r}]",
                False,
            )
        return (
            f"ollama {host} reachable, model pinned to {pin!r} by adapters.yaml argv "
            "(no /api/tags probe — the run lists no models either; the pin is NOT verified "
            "installed)",
            True,
        )
    try:
        available = ollama_brain.list_ollama_models(host, timeout=timeout)
    except ollama_brain.BrainError as exc:
        return (f"ollama {host} UNREACHABLE ({exc})", False)
    except ValueError as exc:
        # A host with no URL scheme (`AGORA_OLLAMA_HOST=localhost:11434`) makes urlopen raise a
        # bare ValueError, which list_ollama_models does NOT wrap — the exact first-run config
        # typo #96 exists to diagnose. Rendered as UNREACHABLE (with the remediation block) rather
        # than as `probe ERROR`, which reads as an internal defect and offers no fix.
        return (f"ollama {host} UNREACHABLE ({type(exc).__name__}: {exc})", False)
    env = os.environ.get(ollama_brain.MODEL_ENV)
    try:
        model = ollama_brain.select_model(None, env, available)
    except ollama_brain.BrainError:
        # The ONLY way select_model raises: no env pin AND an empty install list. The daemon
        # answered, so this is not a reachability problem — the curator simply has nothing to run.
        return (
            f"ollama {host} reachable, 0 models installed (the curator has no model to run)",
            False,
        )
    if env and env.strip():
        # Pinned by env: report which pin decided it, and (the honest half) that an env pin is
        # never checked against the install list — see the "not verified" note above.
        if not available:
            return (
                f"ollama {host} reachable, 0 models installed, would use {model!r} "
                f"(pinned by ${ollama_brain.MODEL_ENV} — NOT installed here)",
                True,
            )
        return (
            f"ollama {host} reachable, {len(available)} models, would use {model!r} "
            f"(pinned by ${ollama_brain.MODEL_ENV})",
            True,
        )
    if "qwen" not in model.lower():
        # PASSES with a WARNING: the brain answers, and model quality is not a health binary —
        # reddening a working llama-only host has no action short of a multi-GB pull. Mirrors the
        # sandbox probe's "unproven, not a failure" precedent.
        return (
            f"ollama {host} reachable, {len(available)} models, would use {model!r} — WARNING no "
            f"qwen model installed, this is the alphabetical fallback (run '{_OLLAMA_PULL_HINT}' "
            "for the probed-good family)",
            True,
        )
    return (f"ollama {host} reachable, {len(available)} models, would use {model!r}", True)


def _probe_program(program: str) -> tuple[str, bool]:
    """Probe ANY backend's ``argv[0]``: is it there and runnable? ``(rendering, healthy)``.

    Resolution is :func:`~agora_kb.curator.subprocess_backend.resolve_program_on_path` — the very
    function the spawn uses — plus ONE extra check the spawn does not do up front: a path-ish
    ``argv[0]`` passes through resolution UNCHECKED, and that is exactly where a real run discovers
    the problem only at ``execvp`` time. An argv[0] carrying the
    :data:`~agora_kb.curator.backends._WORKTREE_TOKEN` placeholder is resolved per-run against a
    directory that does not exist yet, so it is unprobeable: reported, and PASSED (an inability to
    probe is never a verdict). The token is IMPORTED, never re-spelled: a second copy would
    silently stop matching if backends renamed it, and every placeholder argv would then read
    ``NOT FOUND on PATH`` on a perfectly good repo.
    """
    if _WORKTREE_TOKEN in program:
        return (
            f"not probed (argv[0] {program!r} is resolved per-run from {_WORKTREE_TOKEN})",
            True,
        )
    resolved = resolve_program_on_path(program)
    if resolved is None:
        return (f"{program!r} NOT FOUND on PATH — install it or fix the adapters.yaml argv", False)
    if not (Path(resolved).is_file() and os.access(resolved, os.X_OK)):
        return (f"{program!r} at {resolved} is NOT EXECUTABLE — fix the adapters.yaml argv", False)
    return (f"{program!r} on PATH ({resolved})", True)


def _argv_yaml(argv: tuple[str, ...]) -> str:
    """Render an argv tuple as the YAML flow list an operator pastes into ``adapters.yaml``.

    Only an EMPTY token is quoted (``gemini -p ""``), which is the one token in
    :data:`~agora_kb.adapters.cli_agent_brain.KNOWN_CLI_AGENTS` that plain flow style would lose.
    ONE renderer: the doctor remediation snippet and the anti-drift docstring lock share it.
    """
    return "[" + ", ".join(token if token else '""' for token in argv) + "]"


def _print_brain_remediation() -> None:
    """Print the TOOL-AGNOSTIC recovery block for an unusable brain (#96, owner ruling A1).

    Agora is brain-agnostic (invariant 6): ``repo init`` writing ``curator.backend: qwen`` is a
    DEFAULT, not a requirement, and ADR-0016's ``agora-cli-brain`` drives any headless CLI agent as
    a pure text generator. Telling a beta user whose FIRST run just failed to install a daemon and
    pull a ~20 GB model is the most expensive recovery available — and usually an unnecessary one,
    because the developer hitting this very likely already has ``claude`` or ``codex`` on PATH. So
    the agent path is printed FIRST and Ollama is demoted to "instead".

    Kept OFF the diagnosis line (a separate indented block) so every byte-locked ``brain X:`` line
    is unchanged and the fix has room to be copy-pasteable. Pure ``shutil.which`` + string building:
    it cannot raise, and the caller prints it at most ONCE per doctor run.
    """
    from .adapters.cli_agent_brain import KNOWN_CLI_AGENTS

    installed = [(name, argv) for name, argv in KNOWN_CLI_AGENTS if shutil.which(argv[2])]
    if installed:
        names = ", ".join(repr(name) for name, _ in installed)
        verb = "is" if len(installed) == 1 else "are"
        first_name, first_argv = installed[0]
        # The parent keys are printed WITH the entries on purpose: `adapters.yaml` nests the
        # backends under a `backends:` mapping and `repo.yaml` nests the default under `curator:`,
        # so the bare entry alone is not paste-able — appending it verbatim makes adapters.yaml
        # unparseable, and a literal top-level `curator.backend:` key is silently IGNORED by
        # load_repo_config. A fix an operator can follow to no effect is worse than no fix.
        print(f"    fix (no download — {names} {verb} already installed): add to adapters.yaml")
        print("      backends:          # merge into the existing key, do not add a second one")
        print(f"        {first_name}: {{ argv: {_argv_yaml(first_argv)}, network: loopback }}")
        print(f"      then set  curator.backend: {first_name}  in _kb/repo.yaml (ADR-0016) — i.e.")
        print("      curator:           # merge into the existing key")
        print(f"        backend: {first_name}")
    else:
        known = ", ".join(name for name, _ in KNOWN_CLI_AGENTS)
        print(f"    fix (no download): install a headless CLI agent ({known}) and drive it via")
        print("      agora-cli-brain — see ADR-0016")
    print(f"    fix (local model instead): ollama serve  &&  {_OLLAMA_PULL_HINT}")


def _doctor_brains(
    registry: BackendRegistry | None,
    default_backend: str | None,
    *,
    registry_error: str | None = None,
    skip_probe: bool = False,
    timeout: float = _BRAIN_PROBE_TIMEOUT_S,
) -> bool:
    """Probe each routed brain's AVAILABILITY and contribute the result to doctor's verdict (#96).

    GOVERNING RULE: the probe fails the verdict only on a probe RESULT — a fact established about
    the configured brain — never on an inability to probe. The one deliberate exception is an
    unexpected exception inside the probe, which fails, exactly as :func:`_doctor_sandbox`'s own
    catch-all does.

    Cannot raise: ``routed_backends`` is a pure dict comp, ``registry.get`` raises only ``KeyError``
    (caught), ``parse_shim_args`` swallows ``SystemExit``/``ArgumentError`` internally, the probe
    body is inside a per-brain ``except Exception``, and the remediation block — which sits outside
    it — carries its own. ``KeyboardInterrupt`` still propagates (correct for an interactive
    command).
    Cannot hang: the only blocking call is the ``/api/tags`` GET under ``timeout``, and the per-name
    dedupe caps a two-different-brains repo at 2 × that. Doctor never EXECUTES a brain.
    """
    if registry_error is not None:
        # FAILS the verdict, like the UNKNOWN-backend row and unlike the absent-file row below.
        # This is an established FACT, not an inability to probe: `adapters.yaml` is the only
        # definition of every brain, so a malformed one means `load_backend_registry` raises out of
        # `_build_backend`, which returns None, and `agora curate` exits 1 (verified live). The
        # brain is not merely unprobeable — it does not exist. Reporting `healthy` for a repo where
        # curation is structurally impossible is verbatim issue #96's opening complaint, so the
        # same reasoning that fails an UNKNOWN backend applies here a fortiori.
        print("  brains: NOT CONFIGURED (adapters.yaml unreadable — see the routing line above)")
        _print_brain_remediation()
        return False
    if registry is None:
        # PASSES, deliberately: an ABSENT file is a setup-not-done state, not a typo in work the
        # operator believes they finished. Failing it would redden `agora doctor` in any directory
        # that is not an agora repo — noise, not diagnosis — and `repo init` always writes the file
        # (config.write_default_adapters_yaml), so this row means "not wired yet", which the line
        # itself says.
        print("  brains: not probed (no adapters.yaml — no backend configured)")
        return True
    if skip_probe:
        print("  brains: probe skipped (--skip-probe)")
        return True

    ok = True
    remediated = False
    for name in _routed_brain_names(registry, default_backend):
        try:
            spec = registry.get(name)
        except KeyError:
            # An ESTABLISHED fact, not an inability to probe: build_routed_backend catches this
            # same KeyError and returns None → _build_backend returns None → `agora curate` exits
            # 1. A `healthy` verdict on a repo that cannot curate is #96's opening complaint.
            rendering, healthy = (
                "UNKNOWN backend — not defined in adapters.yaml; 'agora curate' cannot run",
                False,
            )
        else:
            try:
                # The program question comes FIRST for EVERY shape, including the Ollama shim
                # (#96 criterion 4). `agora-ollama-brain` is a console script: a `pip install
                # --user` whose bin dir is off the curator's PATH leaves it unspawnable while the
                # daemon is perfectly up — `agora curate` dies at execvp with
                # BackendUnavailableError while a daemon-only probe reports `healthy`, which is
                # verbatim #96's opening complaint. Only when argv[0] resolves (or is the
                # unprobeable {worktree} shape, which PASSES) does the daemon rendering replace it,
                # so every byte-locked PASS line in the verdict matrix is unchanged.
                tail = _ollama_argv_tail(spec.argv)
                rendering, healthy = _probe_program(spec.argv[0])
                if healthy and tail is not None:
                    rendering, healthy = _probe_ollama(tail, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 — doctor must never crash on a probe.
                # A defect in the PROBE, not a fact about the brain: it fails the verdict but gets
                # NO remediation block — any fix we could suggest here would be a guess (A1 §A1.5).
                print(f"  brain {name}: probe ERROR — {type(exc).__name__}: {exc}")
                ok = False
                continue
        print(f"  brain {name}: {rendering}")
        if not healthy:
            ok = False
            if not remediated:
                # Once per doctor run: a repo with two dead brains must not print the fix twice.
                remediated = True
                try:
                    _print_brain_remediation()
                except Exception as exc:  # noqa: BLE001 — the HINT must never crash doctor.
                    # A1.5 asserts the block is contained; it sits OUTSIDE the probe's own guard
                    # (that one `continue`s), so the containment has to be written here. A fix hint
                    # that fails is a footnote, never a reason to lose the `status:` line.
                    print(f"    fix: unavailable ({type(exc).__name__}: {_one_line(str(exc), 80)})")
    return ok


# Only PASS-2 can ever be confined: SubprocessBackend.plan() passes confine=False unconditionally
# (curator/subprocess_backend.py:279), so PASS-1 runs outside the kernel sandbox even for a
# `network: none` spec. Spelled once, here, because every rendering below turns on it.
_CONFINABLE_ACT = "author"

# The mechanisms that confine at the KERNEL level. `restricted` — the ADR-0013
# allow_reduced_isolation fallback — is deliberately absent: it blocks neither network egress nor
# out-of-worktree writes, so counting it here would be the overclaim #129 exists to remove.
_KERNEL_SANDBOX_MECHANISMS = frozenset({"seatbelt", "bwrap"})


def _sandbox_confinement(
    registry: BackendRegistry | None,
    default_backend: str | None,
    *,
    mechanism: str | None,
    proven: bool = True,
) -> str | None:
    """Render whether the sandbox actually confines THIS repo's brains, or ``None`` if unknowable.

    The `sandbox:` self-test proves the MECHANISM works on this host. It says nothing about whether
    any brain goes through it, because :meth:`SubprocessBackend._spawn` confines ONLY a
    ``network: 'none'`` spec — and ``repo init`` writes ``network: loopback`` (the local Ollama
    daemon needs the loopback socket), so the DEFAULT repo runs its brain UNCONFINED. Reading
    ``sandbox: seatbelt (ok)`` as "my brain is sandboxed" is therefore the natural and WRONG
    reading; #129 exists because that misreading nearly reached SECURITY.md.

    Reporting only — never a verdict. An unconfined loopback brain is the designed default, not a
    fault; what was faulty was leaving an operator to infer the opposite.

    Resolution is PER ACT, over ``routed_backends`` — the same mapping the `routing:` line renders,
    so the two cannot disagree. Per-act is not a refinement, it is the correctness condition:
    ``spec.network == 'none'`` is a *request* for confinement, and three further facts decide
    whether it is granted.

    * **PASS-1 is never confined.** :meth:`SubprocessBackend.plan` passes ``confine=False``
      UNCONDITIONALLY (``curator/subprocess_backend.py:279``); only
      :meth:`~SubprocessBackend.author` passes ``confine=True``, and ``SECURITY.md`` states the
      same rule in prose. So a ``network: none`` brain
      reached through ``routing.plan`` is not confined at all, and an unqualified "yes" for a repo
      is never true — which is why this function cannot emit one.
    * **The mechanism must exist and be proven.** ``SandboxUnavailable`` means no act can even be
      BUILT with ``network: none`` (``subprocess_backend.py:615-620`` returns ``None`` and
      ``agora curate`` exits 1), and a self-test that FAILED is a confinement that lies.
    * **``restricted`` is not kernel confinement.** The ``allow_reduced_isolation`` fallback blocks
      neither network egress nor out-of-worktree writes, so calling it confined would re-introduce
      exactly the overclaim this line exists to remove.
    """
    if registry is None:
        return None
    kernel_ok = proven and mechanism in _KERNEL_SANDBOX_MECHANISMS
    confined: list[str] = []
    unconfined: list[str] = []
    blocked: list[str] = []
    for act, name in registry.routed_backends(default=default_backend).items():
        try:
            network = registry.get(name).network
        except KeyError:
            # repo.yaml curator.backend names no defined brain — `routing:` and the brain probe
            # both surface that; saying anything about its confinement would be invention.
            continue
        label = f"{act}={name} (network: {network})"
        if network != "none":
            unconfined.append(label)
        elif mechanism is None:
            blocked.append(label)
        elif act != _CONFINABLE_ACT or not kernel_ok:
            unconfined.append(label)
        else:
            confined.append(label)
    if not (confined or unconfined or blocked):
        return None

    head = "confines this repo's brains:"
    if blocked:
        tail = f"; outside: {', '.join(unconfined)}" if unconfined else ""
        return (
            f"{head} NO — no usable kernel sandbox on this host, so {', '.join(blocked)} "
            f"cannot run at all ('agora curate' fails closed){tail}"
        )
    if not confined:
        return (
            f"{head} NO — outside: {', '.join(unconfined)} ({_why_unconfined(mechanism, proven)})"
        )
    return (
        f"{head} PARTIAL — confined: {', '.join(confined)}; outside: {', '.join(unconfined)} "
        "(PASS-1 is never confined on any path)"
    )


def _why_unconfined(mechanism: str | None, proven: bool) -> str:
    """The one-clause reason nothing in this repo is confined — the actionable half of a `NO`."""
    if mechanism is not None and mechanism not in _KERNEL_SANDBOX_MECHANISMS:
        return f"the {mechanism} fallback is not kernel confinement, ADR-0013"
    if not proven:
        return "the sandbox self-test FAILED on this host"
    return "only a network: none author is confined; PASS-1 never is"


def _doctor_sandbox(
    allow_reduced_isolation: bool,
    *,
    registry: BackendRegistry | None = None,
    default_backend: str | None = None,
) -> bool:
    """Run the ADR-0013 sandbox self-test and print its report; return whether the host is healthy.

    Selects the OS-appropriate :class:`~agora_kb.curator.isolation.BackendIsolation` and runs the
    hardened self-test against a throwaway worktree + a SEPARATE throwaway tmp (the ADR's
    EPERM-specific probes: write-inside OK, write-outside denied, network denied to a reachable
    target, Apple-shimmed binary runs). Returns ``False`` when a sandbox is present but its
    self-test FAILS (a confinement that lies is worse than none), and — since #129 — when the host
    has NO kernel sandbox while a routed act declares ``network: none``. That second case is the
    issue's own headline restated: ``build_routed_backend`` refuses to build such an act
    (``subprocess_backend.py:615-620``) so ``agora curate`` exits 1, and doctor reporting
    ``healthy`` over it is precisely "green on a repo that cannot curate". A sandbox-less host
    whose acts are all ``loopback`` still returns ``True`` — that is the case the fail-closed note
    was always about, and nothing there needs a sandbox.

    ``registry`` / ``default_backend`` are threaded in for both of those: the #129 sub-line
    (:func:`_sandbox_confinement`), and the verdict above. The self-test alone answers "does the
    mechanism work HERE", which an operator reads — wrongly — as "is my brain sandboxed". Both stay
    optional so the self-test remains callable on its own; omitting them drops the sub-line and
    restores the pre-#129 always-``True`` behavior on a sandbox-less host.
    """
    from .curator.isolation.selftest import ollama_reachable, self_test

    try:
        isolation = select_backend_isolation(allow_reduced_isolation=allow_reduced_isolation)
    except SandboxUnavailable as exc:
        print(f"  sandbox: unavailable — fail-closed for network:none backends ({exc})")
        _print_sandbox_confinement(registry, default_backend, mechanism=None)
        return not _has_sandboxed_act(registry, default_backend)

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
    _print_sandbox_confinement(
        registry, default_backend, mechanism=report.mechanism, proven=healthy
    )
    return healthy


def _has_sandboxed_act(registry: BackendRegistry | None, default_backend: str | None) -> bool:
    """Does any routed act ask for the sandbox (``network: none``)? Never raises.

    The question is per ACT, not per backend, and it covers ``plan`` too: ``_build_one`` selects
    isolation for ANY act whose spec says ``network: none``, so a sandboxed PLAN act fails to build
    on a sandbox-less host just like a sandboxed AUTHOR one — even though PLAN would then run
    unconfined. Confinement and buildability are different questions about the same flag.

    Answers ``False`` on anything it cannot resolve (no registry, an undefined backend name, a
    malformed registry): the verdict this feeds must never turn RED on doctor's own uncertainty.
    """
    if registry is None:
        return False
    try:
        for name in registry.routed_backends(default=default_backend).values():
            try:
                if registry.get(name).network == "none":
                    return True
            except KeyError:
                continue
    except Exception:  # noqa: BLE001 — doctor's verdict never turns on a crash in this probe.
        return False
    return False


def _print_sandbox_confinement(
    registry: BackendRegistry | None,
    default_backend: str | None,
    *,
    mechanism: str | None,
    proven: bool = True,
) -> None:
    """Print the #129 confinement sub-line when it is knowable. Never raises, never a verdict.

    Contained like every other doctor helper: a defect here must not cost the operator the
    `sandbox:` block that was already printed, let alone the `status:` line.
    """
    try:
        line = _sandbox_confinement(registry, default_backend, mechanism=mechanism, proven=proven)
    except Exception as exc:  # noqa: BLE001 — an observability line never crashes doctor.
        print(f"    confines this repo's brains: unknown ({type(exc).__name__}: {exc})")
        return
    if line is not None:
        print(f"    {line}")


# --- helpers ------------------------------------------------------------------------------------
def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "never"
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(text: str, limit: int) -> str:
    """Collapse ``text`` to ONE whitespace-normalized line, clipped to ``limit`` chars.

    Whitespace-COLLAPSED, not first-line-truncated (the ``_doctor_backup`` shape): the exceptions
    that actually reach these call sites carry a useless first line — pydantic's ``1 validation
    error for CuratorState`` and ConfigError's ``malformed YAML in <path>: while scanning for the
    next token`` — with the diagnosis on line 2. Verified against real exception objects from this
    tree. Elision is U+2026, matching the backup line's own truncation.
    """
    flat = " ".join(str(text).split())
    if not flat:
        return "<no message>"
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def _can_import(module: str) -> bool:
    try:
        importlib.import_module(module)
    except ImportError:
        return False
    return True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
