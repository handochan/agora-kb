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
from agora_kb.curator.constants import DEFAULT_BODY_BYTE_BOUND
from agora_kb.curator.subprocess_backend import (
    RoutedBackend,
    SubprocessBackend,
    build_routed_backend,
)
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

# Knowledge-graph viz bounds (faces/web /graph seam — ADR-0019 §7 / graph-plan). The global graph is
# soft-capped so a very large KB stays responsive in the browser; when the kept node set exceeds the
# cap it is truncated to the first MAX_GRAPH_NODES by sorted rel_path and ``truncated`` is set (the
# honest signal — node_total still reports the true pre-cap count, never silently dropped). These
# the CONFIGURABLE DEFAULTS (ADR-0025): the web face's :class:`WebConfig` graph caps override them
# per-call via ``graph(max_nodes=…, max_depth=…)``; the default is raised LARGE so a sizable KB
# renders out of the box (the honest-truncation behaviour is unchanged).
MAX_GRAPH_NODES = 10_000  # soft-cap default for the global graph; honest truncated flag (ADR-0025)
MAX_GRAPH_DEPTH = 3  # clamp default for the local ego-graph depth (ADR-0025)


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

    # --- browse (read; web face, ADR-0003/0019) -------------------------------------------------
    def browse(self) -> dict[str, object]:
        """List every wiki note for the browse face; return ``{notes: [...], domains: [...]}``.

        A thin shaping of :meth:`agora_kb.core.wiki.Wiki.list_notes` into JSON-serializable dicts —
        one ``{rel_path, basename, type, title, status, tags, domain}`` per note, plus the sorted
        unique non-``None`` ``domains``. ``title`` falls back to ``basename``; ``domain`` is the
        ``wiki/<domain>/`` path segment (``None`` for ``index.md``). Notes are sorted by
        ``rel_path`` for a deterministic listing. The body markdown is intentionally NOT included
        here — that is the per-note :meth:`note` payload.
        """
        rows = [self._note_summary(note) for note in self._wiki.list_notes()]
        rows.sort(key=lambda r: r["rel_path"])  # type: ignore[arg-type, return-value]
        domains = sorted({d for r in rows if (d := r["domain"]) is not None})  # type: ignore[comparison-overlap]
        return {"notes": rows, "domains": domains}

    def note(self, rel_path: str) -> dict[str, object] | None:
        """Return one note's full read payload by ``rel_path``, or ``None`` if not tracked.

        Shapes :meth:`agora_kb.core.wiki.Wiki.get_note` into the browse summary fields plus the raw
        ``frontmatter``, the raw markdown ``body`` (rendering to HTML is the web layer's job, never
        the core/face's), and ``links`` — the body graph-edge basenames via
        :func:`agora_kb.schema.notes.body_link_basenames`. Path-safety is the core's: an untracked
        or traversal path resolves to no note, so this returns ``None``.
        """
        note = self._wiki.get_note(rel_path)
        if note is None:
            return None
        from agora_kb.schema.notes import body_link_basenames

        payload = self._note_summary(note)
        payload.pop("type", None)  # the per-note shape carries the richer fields below instead
        payload["frontmatter"] = note.frontmatter
        payload["body"] = note.body
        payload["links"] = body_link_basenames(note.body)
        return payload

    @staticmethod
    def _note_summary(note: object) -> dict[str, object]:
        """Derive the shared browse summary fields from a :class:`~agora_kb.schema.notes.Note`.

        ``title`` ← ``frontmatter["title"]`` (else ``basename``); ``status`` ←
        ``frontmatter.status`` (may be ``None``); ``tags`` ← ``frontmatter.tags`` (``[]`` when
        absent/non-list); ``domain`` ← the ``wiki/<domain>/`` path segment (``None`` for
        ``index.md`` or any non-``wiki/`` path).
        """
        fm = note.frontmatter  # type: ignore[attr-defined]
        raw_title = fm.get("title")
        title = raw_title if isinstance(raw_title, str) and raw_title else note.basename  # type: ignore[attr-defined]
        raw_tags = fm.get("tags")
        tags = list(raw_tags) if isinstance(raw_tags, list) else []
        return {
            "rel_path": note.rel_path,  # type: ignore[attr-defined]
            "basename": note.basename,  # type: ignore[attr-defined]
            "type": note.type,  # type: ignore[attr-defined]
            "title": title,
            "status": fm.get("status"),
            "tags": tags,
            "domain": _wiki_domain(note.rel_path),  # type: ignore[attr-defined]
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
            "gold": self._gold_meta_row(),
        }

    def _gold_meta_row(self) -> dict[str, object]:
        """The ADR-0027 gold-pack row for :meth:`status` — a CHEAP meta-sidecar read, NO git.

        Reads only ``_kb/gold/<default>.meta.json`` (never assembles a pack, never resolves the
        curated tip), so ``kb_status`` and the metrics scrape stay cheap. ``present=False`` when the
        pack has never been built; freshness-vs-tip is intentionally NOT computed here (that needs a
        git call) — the dashboard panel (:meth:`gold_status`) and ``agora gold status`` do that.
        """
        from agora_kb.core.gold import DEFAULT_PACK, read_meta

        meta = read_meta(self._repo.layout, DEFAULT_PACK)
        if meta is None:
            return {"pack": DEFAULT_PACK, "present": False}
        return {
            "pack": meta.pack,
            "present": True,
            "note_count": meta.note_count,
            "est_tokens": meta.est_tokens,
            "budget_tokens": meta.budget_tokens,
            "generated_at": meta.generated_at,
            "curated_sha": meta.curated_sha,
            "harvest_derived_share": meta.harvest_derived_share,
        }

    # --- dashboard (meta; read-only — DESIGN §5.3 / ADR-0003) -----------------------------------
    # The three panels below are a pure READ aggregation over already-existing metadata: the wiki
    # notes (browse seam → core.wiki), the deterministic lint() (the SAME health-signal code path
    # the curator runs, so dashboard and curator can never disagree — DESIGN §5.3), the curator
    # state.json, log.md, and the harvester cursors. They add NO write path and touch no
    # integrity/curator/inbox code; every value is JSON-serializable, consistent with status().
    def health(self) -> dict[str, object]:
        """KB-health panel (DESIGN §5.3): note counts, status/tag distribution, lint signals.

        Counts are derived from :meth:`agora_kb.core.wiki.Wiki.list_notes` (the same core.wiki seam
        :meth:`browse` uses) — themes vs dailies by the ``type`` field, status split over the frozen
        vocabulary, and a tag-frequency map. ``broken_links`` and ``lint_findings`` come from the
        deterministic :func:`agora_kb.schema.lint.lint` reused VERBATIM (called WITHOUT ``run_date``
        — dashboard mode, so no historical note is flagged as a future date), so this never
        reimplements a health check. ``orphans`` is a small read-time link-graph derivation (L2-1:
        a theme nothing links TO — lint() emits no orphan finding, so it is NOT a lint signal).
        ``contested`` counts notes whose frontmatter ``status == 'contested'``;
        ``last_consolidation`` is from :meth:`status`. This panel runs lint() + a full note scan,
        so it is the heavy one — refreshed on load + a manual button, not a poll.
        """
        from agora_kb.config import load_repo_config
        from agora_kb.schema.lint import lint
        from agora_kb.schema.notes import body_link_basenames, wikilinks

        notes = self._wiki.list_notes()
        note_total = len(notes)
        themes = sum(1 for n in notes if n.type == "theme")
        dailies = sum(1 for n in notes if n.type == "daily")

        by_status = {"active": 0, "stub": 0, "contested": 0, "deprecated": 0}
        tag_distribution: dict[str, int] = {}
        for note in notes:
            fm = note.frontmatter
            status = fm.get("status")
            if isinstance(status, str) and status in by_status:
                by_status[status] += 1
            raw_tags = fm.get("tags")
            if isinstance(raw_tags, list):
                for tag in raw_tags:
                    if isinstance(tag, str):
                        tag_distribution[tag] = tag_distribution.get(tag, 0) + 1
        contested = by_status["contested"]

        # Reuse the curator's deterministic L1 lint VERBATIM (the dashboard health-signal source,
        # DESIGN §5.3). Dashboard mode = no run_date (the no-future-date half is a curator-run gate;
        # outside a run there is no canonical "today" to compare historical notes against).
        taxonomy = load_repo_config(self._repo.layout).taxonomy
        result = lint(self._repo.layout, taxonomy=taxonomy)
        lint_findings = len(result.findings)
        # Two DISTINCT health signals (DESIGN §5.3), in opposite link directions:
        #  - broken_links: dangling OUTBOUND references — L1-2 findings (a link whose target note
        #    does not exist). A hard-gate signal, counted straight from lint().
        #  - orphans: themes nothing links TO (L2-1) — read-time derived (lint() emits NO orphan
        #    finding). A theme is an orphan when its basename is referenced by no other note's body
        #    markdown link nor any frontmatter related:/children: [[ ]]; dailies and MOC/index roots
        #    (type != "theme") are exempt.
        broken_links = sum(1 for f in result.findings if f.code == "L1-2")
        referenced: set[str] = set()
        for n in notes:
            referenced.update(body_link_basenames(n.body))
            for fkey in ("related", "children"):
                fval = n.frontmatter.get(fkey)
                for item in fval if isinstance(fval, list) else [fval]:
                    if isinstance(item, str):
                        referenced.update(wikilinks(item))
        orphans = sum(1 for n in notes if n.type == "theme" and n.basename not in referenced)

        return {
            "note_total": note_total,
            "themes": themes,
            "dailies": dailies,
            "by_status": by_status,
            "tag_distribution": tag_distribution,
            "orphans": orphans,
            "broken_links": broken_links,
            "contested": contested,
            "lint_ok": result.ok,
            "lint_findings": lint_findings,
            "last_consolidation": self.status()["last_consolidation"],
        }

    def curator_status(self) -> dict[str, object]:
        """Curator panel (DESIGN §5.3): queue depth, throughput, active backend, work-log timeline.

        Reuses :meth:`status` for the inbox/consolidation meta and the cumulative ``counters``, adds
        the resolved ``active_backend`` label (the brain that runs the AUTHOR act — see
        :meth:`_active_backend`, which mirrors ``agora doctor``'s routing resolution), and tails
        ``log.md`` for the work-log timeline (:meth:`_recent_log`). All read-only — no curator
        interaction, the moving values (queue depth) make this a cheap, pollable panel.
        """
        base = self.status()
        return {
            "inbox_depth": base["inbox_depth"],
            "last_consolidation": base["last_consolidation"],
            "last_commit": base["last_commit"],
            "processed_today": base["processed_today"],
            "failed": base["failed"],
            "counters": base["counters"],
            "active_backend": self._active_backend(),
            "recent_log": self._recent_log(),
        }

    def harvester_status(self) -> dict[str, object]:
        """Harvester panel (DESIGN §5.3): connectors enabled + per-source scan / candidate tally.

        From :func:`agora_kb.config.load_harvest_policy` (``enabled``) and
        :func:`agora_kb.config.load_connector_specs` + :class:`agora_kb.harvester.CursorStore` per
        connector. ``accepted`` / ``rejected`` are the curator-owned cursor counters DEFERRED to
        ADR-0017 §7 — they are CURRENTLY 0 and are rendered as-is (never faked); they light up later
        without a redesign here. Tolerant: an unreadable ``repo.yaml`` / ``adapters.yaml`` degrades
        to ``enabled=False`` / no connectors rather than crashing the read-only panel.
        """
        from agora_kb.config import (
            ConfigError,
            load_connector_specs,
            load_harvest_policy,
        )
        from agora_kb.harvester import CursorStore

        try:
            policy = load_harvest_policy(self._repo.layout)
            enabled = policy.enabled
        except (ConfigError, ValueError):
            enabled = False

        adapters_path = self._repo.layout.root / "adapters.yaml"
        try:
            specs = load_connector_specs(adapters_path)
        except (ConfigError, ValueError):
            specs = None

        store = CursorStore(self._repo.layout)
        connectors: list[dict[str, object]] = []
        for spec in specs or []:
            cursor = store.load(spec.name)
            connectors.append(
                {
                    "name": spec.name,
                    "path": spec.path,
                    "scope": spec.scope,
                    "follow_links": spec.follow_links,
                    "last_scan": (None if cursor.last_scan is None else _iso_z(cursor.last_scan)),
                    "proposed": cursor.proposed,
                    "accepted": cursor.accepted,
                    "rejected": cursor.rejected,
                }
            )
        return {"enabled": enabled, "connectors": connectors}

    def gold_status(self) -> dict[str, object]:
        """Gold panel (DESIGN §5.3 medallion): the derived pack tier + its freshness vs silver.

        The bronze/silver/gold panel's gold row: reads the ADR-0027 pack meta sidecar and resolves
        the curated tip to mark the pack FRESH (``curated_sha`` == the live tip) or STALE. Unlike
        the cheap :meth:`status` gold row (meta-only, no git), this panel may do one ``git
        rev-parse``. Tolerant: an absent pack → ``present=False``; a git failure →
        ``fresh=None`` (unknown) rather than crashing the read-only panel. ``bronze`` carries the
        cheap tier context (inbox backlog already in :meth:`status`; silver lives in the KB-health
        panel)."""
        from agora_kb.core.gold import DEFAULT_PACK, read_meta

        base = self.status()
        meta = read_meta(self._repo.layout, DEFAULT_PACK)
        gold: dict[str, object] = {"present": meta is not None, "pack": DEFAULT_PACK}
        if meta is not None:
            try:
                commit: str | None = self._repo.branch_commit()
            except Exception:  # noqa: BLE001 — a git failure leaves freshness unknown, not fatal.
                commit = None
            gold.update(
                {
                    "pack": meta.pack,
                    "fresh": None if commit is None else meta.curated_sha == commit,
                    "note_count": meta.note_count,
                    "est_tokens": meta.est_tokens,
                    "budget_tokens": meta.budget_tokens,
                    "generated_at": meta.generated_at,
                    "curated_sha": meta.curated_sha[:12],
                    "harvest_derived_share": meta.harvest_derived_share,
                }
            )
        return {
            "bronze": {
                "inbox_depth": base["inbox_depth"],
                "processed_today": base["processed_today"],
            },
            "gold": gold,
        }

    # --- graph (read; web face /graph viz — ADR-0003/0019 §7 / graph-plan) -----------------------
    def graph(
        self,
        *,
        center: str | None = None,
        depth: int = 1,
        domain: str | None = None,
        max_nodes: int | None = None,
        max_depth: int | None = None,
    ) -> dict[str, object]:
        """Knowledge-graph data for the web ``/graph`` viz (DESIGN §5.3 / ADR-0019 §7, graph-plan).

        A thin, deterministic READ aggregation over :meth:`agora_kb.core.wiki.Wiki.list_notes` (the
        same core.wiki seam :meth:`browse` / :meth:`health` use) into a JSON-serializable
        node/edge graph — it adds NO write path and touches no integrity/curator/inbox code
        (ADR-0003 thin-face). It is the data seam the graph plan and ADR-0019 §7 prescribe; the
        rendering (force-graph layout) is the web layer's job, never this face's.

        **Nodes.** One per note via :meth:`_note_summary` (reused verbatim, NOT duplicated) for
        ``title`` / ``domain`` / ``status`` / ``type``, with ``id == rel_path`` — the canonical
        unique identity, which is exactly what ``/note/<id>`` navigates to — plus an ``orphan``
        flag.

        **Orphan.** Reuses :meth:`health`'s EXACT derivation: a referenced set is built over the
        FULL note set = the union of :func:`agora_kb.schema.notes.body_link_basenames` of each body
        plus, for ``related`` / ``children``, the :func:`agora_kb.schema.notes.wikilinks` of each
        str item (list-or-scalar tolerant, exactly as ``health()`` iterates). A node is an orphan
        iff ``type == "theme"`` and its basename is referenced by nothing; a non-theme is never an
        orphan. Orphan is a GLOBAL property, so it is computed over every note regardless of the
        ``center`` / ``domain`` filter (so the count matches ``health()["orphans"]``).

        **Edges.** Directed, deduped, no self-loops, both endpoints real nodes. A ``by_basename``
        map (basename → rel_path, ``setdefault`` over notes sorted by ``rel_path`` for determinism)
        resolves each source note's body links + ``related`` / ``children`` wikilinks to a target
        rel_path; an unresolved (dangling) basename is NOT an edge, and self-loops are dropped.
        Emitted as ``{"source", "target"}``, sorted by ``(source, target)``.

        **Scope.** GLOBAL (``center is None``): all notes, optionally filtered to one ``domain``
        (kept iff ``_wiki_domain(rel_path) == domain``); ``edge_total`` / ``node_total`` are the
        kept counts BEFORE the cap. If the kept node count exceeds :data:`MAX_GRAPH_NODES` the kept
        set is truncated to the first ``MAX_GRAPH_NODES`` by sorted rel_path, edges are recomputed
        on the survivors, and ``truncated`` is ``True`` — but ``node_total`` still reports the true
        pre-cap count (no silent truncation). LOCAL (``center`` is a rel_path): ``depth`` is clamped
        to ``[1, MAX_GRAPH_DEPTH]``; an unknown center returns an empty graph with ``center=None``;
        otherwise a BFS over the UNDIRECTED adjacency of the global edge set (both directions) from
        the center out to ``depth`` hops yields the reached notes (``domain`` is ignored for local),
        with edges induced on that reached set and ``truncated`` always ``False``.

        Returns ``{nodes, edges, node_total, edge_total, truncated, center, depth}`` — ``center``
        echoes the resolved center rel_path (``None`` for global or an unknown requested center) and
        ``depth`` echoes the clamped value; every value is JSON-serializable.
        """
        from agora_kb.schema.notes import body_link_basenames, wikilinks

        # ADR-0025: resolve the EFFECTIVE caps — a caller-supplied value (the web face threads its
        # WebConfig graph caps) wins, else the module default. Backward-compatible: a graph() call
        # with no caps (MCP/tests) keeps the module-default behaviour.
        eff_max_nodes = max_nodes if max_nodes is not None else MAX_GRAPH_NODES
        eff_max_depth = max_depth if max_depth is not None else MAX_GRAPH_DEPTH

        notes = self._wiki.list_notes()

        # Orphan derivation — reused VERBATIM from health() (a GLOBAL property over every note,
        # computed before any center/domain filtering so the count matches health()["orphans"]).
        referenced: set[str] = set()
        for n in notes:
            referenced.update(body_link_basenames(n.body))
            for fkey in ("related", "children"):
                fval = n.frontmatter.get(fkey)
                for item in fval if isinstance(fval, list) else [fval]:
                    if isinstance(item, str):
                        referenced.update(wikilinks(item))

        # Stable basename → rel_path resolver (setdefault over notes sorted by rel_path so a
        # duplicate basename deterministically resolves to the first path).
        sorted_notes = sorted(notes, key=lambda n: n.rel_path)
        by_basename: dict[str, str] = {}
        for n in sorted_notes:
            by_basename.setdefault(n.basename, n.rel_path)

        def _node(n: object) -> dict[str, object]:
            summary = self._note_summary(n)
            return {
                "id": summary["rel_path"],
                "title": summary["title"],
                "domain": summary["domain"],
                "status": summary["status"],
                "type": summary["type"],
                "orphan": (
                    n.type == "theme" and n.basename not in referenced  # type: ignore[attr-defined]
                ),
            }

        # The full directed, deduped, self-loop-free edge set over every real node — the basis for
        # both the global induced edges and the local undirected BFS adjacency.
        all_node_ids = {n.rel_path for n in notes}
        edge_pairs: set[tuple[str, str]] = set()
        for n in sorted_notes:
            targets = list(body_link_basenames(n.body))
            for fkey in ("related", "children"):
                fval = n.frontmatter.get(fkey)
                for item in fval if isinstance(fval, list) else [fval]:
                    if isinstance(item, str):
                        targets.extend(wikilinks(item))
            for base in targets:
                target_id = by_basename.get(base)
                if target_id is None or target_id == n.rel_path:
                    continue  # dangling link → not an edge; self-loop dropped
                edge_pairs.add((n.rel_path, target_id))

        def _induced_edges(node_ids: set[str]) -> list[dict[str, object]]:
            kept = sorted((s, t) for (s, t) in edge_pairs if s in node_ids and t in node_ids)
            return [{"source": s, "target": t} for (s, t) in kept]

        if center is None:
            # GLOBAL scope — all notes, optionally filtered to one domain.
            if domain is None:
                kept_notes = sorted_notes
            else:
                kept_notes = [n for n in sorted_notes if _wiki_domain(n.rel_path) == domain]
            kept_ids = {n.rel_path for n in kept_notes}
            node_total = len(kept_notes)
            edge_total = len(_induced_edges(kept_ids))
            truncated = node_total > eff_max_nodes
            if truncated:
                kept_notes = kept_notes[:eff_max_nodes]  # first N by sorted rel_path
                kept_ids = {n.rel_path for n in kept_notes}
            nodes = [_node(n) for n in kept_notes]
            edges = _induced_edges(kept_ids)
            return {
                "nodes": nodes,
                "edges": edges,
                "node_total": node_total,  # honest pre-cap count
                "edge_total": edge_total,
                "truncated": truncated,
                "center": None,
                "depth": depth,
            }

        # LOCAL scope — ego-graph around `center` out to a clamped depth.
        clamped_depth = max(1, min(depth, eff_max_depth))
        if center not in all_node_ids:
            # Unknown center → empty graph, center echoed as None (no node to seed the BFS).
            return {
                "nodes": [],
                "edges": [],
                "node_total": 0,
                "edge_total": 0,
                "truncated": False,
                "center": None,
                "depth": clamped_depth,
            }

        adjacency: dict[str, set[str]] = {}
        for s, t in edge_pairs:
            adjacency.setdefault(s, set()).add(t)
            adjacency.setdefault(t, set()).add(s)

        reached: set[str] = {center}
        frontier: set[str] = {center}
        for _ in range(clamped_depth):
            nxt: set[str] = set()
            for node_id in frontier:
                for neighbor in adjacency.get(node_id, ()):
                    if neighbor not in reached:
                        nxt.add(neighbor)
            reached.update(nxt)
            frontier = nxt
            if not frontier:
                break

        by_path = {n.rel_path: n for n in sorted_notes}
        local_notes = [by_path[rel] for rel in sorted(reached) if rel in by_path]
        nodes = [_node(n) for n in local_notes]
        edges = _induced_edges(reached)
        return {
            "nodes": nodes,
            "edges": edges,
            "node_total": len(nodes),
            "edge_total": len(edges),
            "truncated": False,
            "center": center,
            "depth": clamped_depth,
        }

    def _active_backend(self) -> str | None:
        """Resolve the label of the brain that runs the AUTHOR act (the prose writer), or ``None``.

        Mirrors ``agora doctor``'s routing resolution (cli.py ``_doctor_routing``):
        ``load_backend_registry(adapters.yaml).routed_backends(default=repo.yaml curator.backend)``
        under the ADR-0015 precedence (``routing[act]`` → repo default → adapters default). The
        AUTHOR act is the brain that actually materializes wiki prose, so it is the meaningful
        "active backend + model" the dashboard names (DESIGN §5.3). ``None`` when no
        ``adapters.yaml`` is configured (or it is unreadable) — the same "no backend" signal
        :meth:`curate` surfaces, never a crash on the read-only panel.
        """
        from agora_kb.config import load_backend_registry, load_repo_config

        adapters_path = self._repo.layout.root / "adapters.yaml"
        try:
            registry = load_backend_registry(adapters_path)
        except ValueError:
            return None
        if registry is None:
            return None
        default_backend = load_repo_config(self._repo.layout).default_backend
        routed = registry.routed_backends(default=default_backend)
        return routed.get("author") or default_backend

    def _recent_log(self, limit: int = 10) -> list[dict[str, object]]:
        """Tail the last ``limit`` structured ``log.md`` entries into a work-log timeline.

        Matches the EXACT format the curator's ``worker._append_log`` writes (one ``## <run_id>``
        section per run, followed by ``- base: \\`<base>\\``` / ``- dispositions: <op>=<n>, …`` and
        optional ``- contested:`` / ``- dropped:`` / ``- pending-body:`` lines). Each parsed entry
        is ``{run_id, base, ops:{op:count}, contested:[…], dropped:[…], pending_body:[…]}``, newest
        first. A pure read of the git-tracked ``log.md`` (the curator alone writes it, ADR-0002); a
        missing file yields ``[]``. Parsing is tolerant — a ``- dispositions: no-op`` line or an
        unrecognized bullet simply yields an empty ``ops`` / is ignored rather than raising.
        """
        log_path = self._repo.layout.log_file
        if not log_path.is_file():
            return []
        text = log_path.read_text(encoding="utf-8")
        entries: list[dict[str, object]] = []
        current: dict[str, object] | None = None
        for line in text.splitlines():
            if line.startswith("## "):
                current = {
                    "run_id": line[3:].strip(),
                    "base": None,
                    "ops": {},
                    "contested": [],
                    "dropped": [],
                    "pending_body": [],
                }
                entries.append(current)
                continue
            if current is None:
                continue
            stripped = line.strip()
            if stripped.startswith("- base:"):
                current["base"] = stripped[len("- base:") :].strip().strip("`") or None
            elif stripped.startswith("- dispositions:"):
                current["ops"] = _parse_ops(stripped[len("- dispositions:") :].strip())
            elif stripped.startswith("- contested:"):
                current["contested"] = _split_csv(stripped[len("- contested:") :])
            elif stripped.startswith("- dropped:"):
                current["dropped"] = _split_csv(stripped[len("- dropped:") :])
            elif stripped.startswith("- pending-body:"):
                current["pending_body"] = _split_csv(stripped[len("- pending-body:") :])
        # Newest first; cap at `limit`.
        entries.reverse()
        return entries[:limit]

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
            default_backend=cfg.default_backend,
            allow_reduced_isolation=cfg.allow_reduced_isolation,
            body_byte_bound=cfg.body_byte_bound,
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
            related_k=cfg.related_k,
        )
        return {
            "status": report.status,
            "published_commit": report.published_commit,
            "counts": report.counts,
            "recovered": recovered,
        }

    def _build_backend(
        self,
        *,
        default_backend: str | None = None,
        allow_reduced_isolation: bool = False,
        body_byte_bound: int = DEFAULT_BODY_BYTE_BOUND,
    ) -> RoutedBackend | SubprocessBackend | None:
        """Resolve the configured WRITE-adapter(s) into a worker backend, or ``None``.

        Loads ``adapters.yaml`` (DATA-MODEL §8) and delegates to the shared
        :func:`~agora_kb.curator.subprocess_backend.build_routed_backend`, honoring the optional
        per-act ``routing`` table (ADR-0015): ``plan`` and ``author`` may run on different brains.
        ``None`` (an absent file, or a ``network: 'none'`` act with no usable OS sandbox under
        ``allow_reduced_isolation=False``) is the caller's "no backend configured" signal — the face
        stays SILENT (``report=None``), unlike the CLI which prints to stderr. A missing executable
        surfaces later at invocation as a clear error from the backend itself.
        """
        adapters_path = self._repo.layout.root / "adapters.yaml"
        try:
            registry = load_backend_registry(adapters_path)
        except ValueError:
            # A malformed adapters.yaml (incl. an invalid ADR-0015 ``routing:`` block) is unusable;
            # the silent face reports it as "no_backend" rather than raising to the MCP client.
            return None
        if registry is None:
            return None
        return build_routed_backend(
            registry,
            allow_reduced_isolation=allow_reduced_isolation,
            default_backend=default_backend,
            body_byte_bound=body_byte_bound,
            report=None,
        )


def _wiki_domain(rel_path: str) -> str | None:
    """Return the ``<domain>`` segment of a ``wiki/<domain>/...`` note path, else ``None``.

    ``index.md`` (and any path not under ``wiki/<domain>/``) has no domain. POSIX-split on ``/``:
    a path is in a domain iff it is ``wiki/<domain>/<...>`` (at least three segments under
    ``wiki``).
    """
    parts = rel_path.split("/")
    if len(parts) >= 3 and parts[0] == "wiki":
        return parts[1]
    return None


def _parse_ops(value: str) -> dict[str, int]:
    """Parse a ``log.md`` ``dispositions`` value (``CREATE_THEME=2, DROP=1`` | ``no-op``) → counts.

    Mirrors the ``", ".join(f"{op}={n}" …)`` form ``worker._append_log`` writes. ``no-op`` (the
    empty-counts marker) and any malformed ``op=n`` token (a non-integer count) is skipped, so the
    dashboard tail never raises on a hand-edited or future log line.
    """
    ops: dict[str, int] = {}
    if not value or value == "no-op":
        return ops
    for token in value.split(","):
        op, sep, count = token.strip().partition("=")
        if not sep:
            continue
        try:
            ops[op.strip()] = int(count.strip())
        except ValueError:
            continue
    return ops


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated ``log.md`` list bullet (``contested``/``dropped``/…) into items."""
    return [item.strip() for item in value.split(",") if item.strip()]


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
