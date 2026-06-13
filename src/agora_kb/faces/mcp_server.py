"""MCP server face — the 4 agent tools over the core API (DESIGN §5.1).

This is the **agent-facing** face: a single FastMCP registration that exposes ``kb_remember`` /
``kb_query`` / ``kb_status`` / ``kb_curate`` to every MCP client (Claude Code, Codex, Qwen,
Hermes, …). It is a thin face: it adds no concurrency, provenance, or access-control logic of its
own — those are properties of the core (DESIGN §2.1). Each tool delegates straight to the core API:

- ``kb_remember`` → :meth:`agora_kb.core.inbox.Inbox.write` (the only write path; non-blocking).
- ``kb_query``    → :meth:`agora_kb.core.wiki.Wiki.query` (the deterministic, model-free read path).
- ``kb_status``   → :class:`agora_kb.core.state.StateStore` + live inbox/failed counts (meta face).
- ``kb_curate``   → :func:`agora_kb.curator.evaluate` (a trigger *probe* — see its note below).

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

from agora_kb.core import Inbox, Repo, StateStore, Wiki
from agora_kb.curator import TriggerConfig, evaluate

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
        """Number of terminal-failure entries under ``_kb/failed/`` (0 if the dir is absent)."""
        failed_dir = self._repo.layout.failed_dir
        if not failed_dir.is_dir():
            return 0
        return sum(1 for _ in failed_dir.glob("*.md"))

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
        """``kb_curate``: a trigger **probe** — report whether a consolidation run is due.

        It loads curator state, applies the default :class:`TriggerConfig`, and calls
        :func:`agora_kb.curator.evaluate` with ``cron_due=False`` (the MCP face is not the
        scheduler, so it does not evaluate the cron expression). ``force=True`` short-circuits to a
        ``"force"``-flavoured "should run" so an operator can request a drain on demand — an
        operator override, distinct from the backlog ``"threshold"`` signal, matching the sibling
        ``agora_kb.cli`` ``curate`` command.

        ``target`` defaults to ``"personal"`` for the Phase-1 single-repo MVP (the only repo until
        multi-tenancy lands); DESIGN §5.1 lists it as a required ``kb_curate(target, force?)`` arg.

        NOTE: the **actual** consolidation run (claim → worktree → backend INGEST → commit →
        compare-and-swap) is executed by ``agora_kb.curator.worker``, which is **not yet wired**.
        Until then ``kb_curate`` only *reports* whether a run is due; it never mutates the wiki.
        """
        state = self._state.load()
        depth = self._inbox.depth()
        config = TriggerConfig()

        if force:
            note = (
                f"force=True: a run was requested for target={target!r}; "
                "actual consolidation is performed by curator.worker (not yet wired)."
            )
            return {
                "should_run": True,
                "reason": "force",
                "inbox_depth": depth,
                "note": note,
            }

        decision = evaluate(
            inbox_depth=depth,
            now=_now(),
            last_write=None,
            last_run=state.last_run,
            config=config,
            cron_due=False,
        )
        note = (
            f"trigger probe for target={target!r}; actual consolidation is performed by "
            "curator.worker (not yet wired). kb_curate only reports whether a run is due."
        )
        return {
            "should_run": decision.should_run,
            "reason": decision.reason,
            "inbox_depth": depth,
            "note": note,
        }


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
        """Probe whether a consolidation run is due. ``force=True`` is an operator override.

        ``target`` defaults to ``"personal"`` — the only repo until multi-tenancy lands. The run
        itself is performed by ``curator.worker`` (not yet wired); this only reports.
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
