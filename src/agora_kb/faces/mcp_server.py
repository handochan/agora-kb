"""MCP server face — the 4 agent tools over the core API (DESIGN §5.1).

This is the **agent-facing** face: a single FastMCP registration that exposes ``kb_remember`` /
``kb_query`` / ``kb_status`` / ``kb_curate`` to every MCP client (Claude Code, Codex, Qwen,
Hermes, …). It is a thin face: it adds no concurrency, provenance, or access-control logic of its
own — those are properties of the core (DESIGN §2.1). Each tool delegates straight to the core API:

- ``kb_remember`` → :meth:`agora_kb.core.inbox.Inbox.write` (the only write path; non-blocking).
- ``kb_query``    → :meth:`agora_kb.core.wiki.Wiki.query` (the deterministic, model-free read path).
- ``kb_status``   → :class:`agora_kb.core.state.StateStore` + live inbox/failed counts (meta face).
- ``kb_curate``   → :func:`agora_kb.curator.worker.recover` + :func:`agora_kb.curator.worker.run`
  (a real consolidation run against the configured ``adapters.yaml`` backend; see its note below).

**Testability split (the architecture this module is built around).** The cognitive part — turning
a tool call into a core-API call and shaping a JSON-serializable result — lives in
:class:`AgoraHandlers`, which has no dependency on a live MCP transport and is unit-testable
directly. :func:`build_server` is the only transport-aware part: it constructs the handlers and
registers four one-line tool wrappers that call them. So the handler logic is exercised by ordinary
unit tests with no server I/O, and the wiring is covered by a single smoke test.

The ``writer`` identity is **server configuration** (the connecting agent's identity), not a tool
argument — a client cannot spoof another writer's inbox namespace (tenant isolation, invariant 5).
For the local stdio MVP it defaults to ``"local"``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from agora_kb.config import load_backend_registry, load_repo_config
from agora_kb.core import Inbox, Repo, StateStore, Wiki
from agora_kb.curator.isolation import SandboxUnavailable, select_backend_isolation
from agora_kb.curator.subprocess_backend import SubprocessBackend
from agora_kb.curator.worker import recover, run

if TYPE_CHECKING:
    from datetime import datetime

    from fastmcp import FastMCP

__all__ = ["AgoraHandlers", "build_server", "main"]

# Default writer identity for the local stdio MVP. In team mode this comes from the authenticated
# connection, never from a tool argument (DESIGN §5.1 / §7).
DEFAULT_WRITER = "local"
# The MCP source tag used for captures that arrive without a more specific origin. ``manual`` is one
# of the FIXED_SOURCES the core accepts (DATA-MODEL §1).
DEFAULT_SOURCE = "manual"


class AgoraHandlers:
    """Transport-free handlers for the 4 MCP tools (DESIGN §5.1).

    Construct with the repo and the connecting writer identity; each method maps a tool call to a
    core-API call and returns a plain, JSON-serializable ``dict``. No FastMCP / transport types
    appear here, so these methods are unit-testable without a running server.
    """

    def __init__(self, repo: Repo, writer: str = DEFAULT_WRITER) -> None:
        self._repo = repo
        self._writer = writer
        self._inbox = Inbox(repo.layout)
        self._wiki = Wiki(repo.layout)
        self._state = StateStore(repo.layout)

    @property
    def writer(self) -> str:
        return self._writer

    # --- write ----------------------------------------------------------------------------------
    def remember(
        self,
        text: str,
        *,
        target: str = "personal",
        domain: str | None = None,
        tags: list[str] | None = None,
        source: str = DEFAULT_SOURCE,
    ) -> dict[str, object]:
        """``kb_remember``: append one immutable inbox event; return ``{id, queued, inbox_depth}``.

        Non-blocking — it only appends to the per-writer inbox (CQRS write side, DESIGN §2.2); the
        captured item becomes queryable only after the curator consolidates (eventual consistency).
        The ``writer`` is fixed by server config, never taken from the caller.
        """
        receipt = self._inbox.write(
            text=text,
            writer=self._writer,
            source=source,
            target=target,
            domain=domain,
            tags=tags,
        )
        return receipt.as_dict()

    # --- read -----------------------------------------------------------------------------------
    def query(self, question: str) -> dict[str, object]:
        """``kb_query``: deterministic evidence search; render the :class:`QueryResult` faithfully.

        Returns ``{query, status, hits}`` where ``status`` is ``'ok'`` or ``'not_found'`` and each
        hit carries ``{repo, path, anchor, line, excerpt, match_reason, score}`` citations.
        """
        result = self._wiki.query(question)
        return {
            "query": result.query,
            "status": result.status,
            "hits": [
                {
                    "repo": hit.repo,
                    "path": hit.path,
                    "anchor": hit.anchor,
                    "line": hit.line,
                    "excerpt": hit.excerpt,
                    "match_reason": hit.match_reason,
                    "score": hit.score,
                }
                for hit in result.hits
            ],
        }

    # --- meta -----------------------------------------------------------------------------------
    def status(self) -> dict[str, object]:
        """``kb_status``: the meta face — backlog + last consolidation + cumulative counters.

        Returns the documented §5.1 surface (``inbox_depth``, ``last_consolidation``,
        ``processed_today``, ``failed``) plus the richer ``last_commit`` / ``counters`` extras. All
        of it already exists in ``_kb/state.json`` and the live ``inbox/`` / ``processed/`` /
        ``failed/`` dirs (DESIGN §5.3), so this is a pure read with no curator interaction.
        """
        state = self._state.load()
        return {
            "inbox_depth": self._inbox.depth(),
            "last_consolidation": (None if state.last_run is None else _iso_z(state.last_run)),
            "processed_today": self._processed_today_count(),
            "last_commit": state.last_commit,
            "counters": {
                "ingested": state.counters.ingested,
                "merged": state.counters.merged,
                "dropped": state.counters.dropped,
                "failed": state.counters.failed,
            },
            "failed": self._failed_count(),
        }

    def _failed_count(self) -> int:
        """Number of terminal-failure events under ``_kb/failed/`` (0 if the dir is absent).

        The worker writes terminal failures NESTED at ``failed/<date>/<run-id>/<event>.md`` (with
        an ``error.json`` retry record alongside, ``worker._fail``), NOT as direct children of
        ``failed/`` — so this RECURSIVELY globs ``*.md`` to count the real on-disk layout. Events
        are the only ``.md`` under ``failed/`` (the retry record is ``error.json``), so the count
        tracks terminally-failed events exactly.
        """
        failed_dir = self._repo.layout.failed_dir
        if not failed_dir.is_dir():
            return 0
        return sum(1 for _ in failed_dir.rglob("*.md"))

    def _processed_today_count(self) -> int:
        """Number of items consolidated today under ``_kb/processed/<today-UTC>/`` (0 if absent).

        The processed spool is partitioned by UTC date (DESIGN §5.3: ``processed/<date>/``), so
        "today" is the current UTC calendar day, matching how the curator finalizes events.
        """
        today_dir = self._repo.layout.processed_dir / _utc_today()
        if not today_dir.is_dir():
            return 0
        return sum(1 for _ in today_dir.glob("*.md"))

    # --- admin ----------------------------------------------------------------------------------
    def curate(self, *, target: str = "personal", force: bool = False) -> dict[str, object]:
        """``kb_curate``: run ONE consolidation against the configured ``adapters.yaml`` backend.

        Order (ADR-0011 §9 then §0): :func:`agora_kb.curator.worker.recover` finalizes/returns any
        in-flight run first, then :func:`agora_kb.curator.worker.run` executes the transactional run
        (claim → bundle → PASS-1 PLAN → APPLY → PASS-2 AUTHOR → validate → commit → CAS publish).
        The backend is OUTSIDE the integrity boundary (ADR-0008/0011 §7): the deterministic gates
        decide success, so this face adds none of its own logic — it only loads the configured brain
        and shapes the :class:`~agora_kb.curator.worker.RunReport` into a JSON-serializable dict.

        Returns ``{status, published_commit, counts, ...}``. When no ``adapters.yaml`` backend is
        configured (or the configured brain is unknown), it returns ``{status: "no_backend", note:
        ...}`` rather than raising — a clear, actionable signal to the caller. ``force`` is accepted
        for signature stability (DESIGN §5.1 ``kb_curate(target, force?)``); the worker itself
        no-ops an empty/all-deduped inbox, so a forced call over an empty inbox simply reports
        ``noop``. ``target`` defaults to ``"personal"`` (the only repo until multi-tenancy lands).
        """
        cfg = load_repo_config(self._repo.layout)
        backend = self._build_backend(
            cfg.default_backend, allow_reduced_isolation=cfg.allow_reduced_isolation
        )
        if backend is None:
            return {
                "status": "no_backend",
                "published_commit": None,
                "counts": {},
                "inbox_depth": self._inbox.depth(),
                "note": (
                    f"no backend configured for target={target!r}: create an adapters.yaml with a "
                    "'backends:' mapping and 'default_backend' (DATA-MODEL §8) so curator.worker "
                    "can run consolidation."
                ),
            }

        recovered = [
            {"run_id": r.run_id, "status": r.status, "counts": r.counts}
            for r in recover(self._repo, state_store=self._state)
        ]
        report = run(
            self._repo,
            backend=backend,
            state_store=self._state,
            now=_now(),
            taxonomy=cfg.taxonomy,
            max_attempts=cfg.max_attempts,
        )
        return {
            "status": report.status,
            "published_commit": report.published_commit,
            "counts": report.counts,
            "recovered": recovered,
        }

    def _build_backend(
        self, backend_name: str, *, allow_reduced_isolation: bool = False
    ) -> SubprocessBackend | None:
        """Resolve the configured WRITE-adapter into a :class:`SubprocessBackend`, or ``None``.

        Loads ``adapters.yaml`` (DATA-MODEL §8) from the repo root. ``None`` (an absent file or an
        unknown configured brain) is the caller's "no backend configured" signal; a missing
        executable surfaces later at invocation as a clear error from the backend itself.

        ADR-0013: an OS-sandbox adapter is selected and injected ONLY for a ``network: 'none'``
        backend (its file-writing PASS-2 step is then confined). The default loopback Ollama brain
        does inference OUTSIDE the sandbox, so it needs none. A ``network: 'none'`` backend with no
        usable sandbox and ``allow_reduced_isolation=False`` fails closed (``None``), matching the
        CLI ``curate`` path, rather than running unconfined.
        """
        adapters_path = self._repo.layout.root / "adapters.yaml"
        registry = load_backend_registry(adapters_path)
        if registry is None:
            return None
        try:
            spec = registry.get(backend_name)
        except KeyError:
            return None
        isolation = None
        if spec.network == "none":
            try:
                isolation = select_backend_isolation(
                    allow_reduced_isolation=allow_reduced_isolation
                )
            except SandboxUnavailable:
                return None
        return SubprocessBackend(spec, isolation=isolation)


def _now() -> datetime:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _utc_today() -> str:
    """Current UTC calendar day as ``YYYY-MM-DD`` — the partition key for ``processed/<date>/``."""
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")


def _iso_z(value: datetime) -> str:
    """Render a UTC instant as ``2026-06-13T03:00:12Z`` (matches the state-file form)."""
    from datetime import UTC

    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_server(*, repo_path: Path, writer: str = DEFAULT_WRITER) -> FastMCP:
    """Construct the FastMCP server: build handlers over ``repo_path`` and register the 4 tools.

    The returned :class:`fastmcp.FastMCP` instance has ``kb_remember`` / ``kb_query`` /
    ``kb_status`` / ``kb_curate`` registered. Each tool is a one-line wrapper delegating to an
    :class:`AgoraHandlers`, keeping all logic transport-free and unit-testable.
    """
    from fastmcp import FastMCP

    repo = Repo.resolve(repo_path)
    handlers = AgoraHandlers(repo, writer=writer)
    mcp: FastMCP = FastMCP(name="agora-kb")

    @mcp.tool
    def kb_remember(
        text: str,
        target: str = "personal",
        domain: str | None = None,
        tags: list[str] | None = None,
        source: str = DEFAULT_SOURCE,
    ) -> dict[str, object]:
        """Capture knowledge: append it to the inbox (non-blocking). Returns id/queued/inbox_depth.

        The writer identity is the connecting agent (server config), not an argument.
        """
        return handlers.remember(text, target=target, domain=domain, tags=tags, source=source)

    @mcp.tool
    def kb_query(question: str) -> dict[str, object]:
        """Search the wiki for evidence. Returns {query, status, hits[...]} with path/anchor cites.

        Hits are ordered citations into ``wiki/`` markdown — navigation, not synthesis.
        """
        return handlers.query(question)

    @mcp.tool
    def kb_status() -> dict[str, object]:
        """Report backlog, last consolidation, processed-today/failed counts, and counters."""
        return handlers.status()

    @mcp.tool
    def kb_curate(target: str = "personal", force: bool = False) -> dict[str, object]:
        """Run one consolidation against the configured backend. Returns status/commit/counts.

        Recovers any in-flight run, then runs ``curator.worker`` over the ``adapters.yaml`` brain.
        ``target`` defaults to ``"personal"`` — the only repo until multi-tenancy lands. Returns
        ``status: "no_backend"`` (not an error) when no backend is configured.
        """
        return handlers.curate(target=target, force=force)

    return mcp


def main(argv: list[str] | None = None) -> int:
    """Build the server for a repo path and run it over stdio (the local MVP entry point)."""
    parser = argparse.ArgumentParser(prog="agora-mcp", description="Agora MCP server (stdio).")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Path to the knowledge repo (default: current directory).",
    )
    parser.add_argument(
        "--writer",
        default=DEFAULT_WRITER,
        help=f"Writer identity for captures (default: {DEFAULT_WRITER!r}).",
    )
    args = parser.parse_args(argv)
    server = build_server(repo_path=args.repo, writer=args.writer)
    server.run(transport="stdio")
    return 0
