# AGENTS.md — guide for agents working on the Agora codebase

This file orients any AI coding agent (Claude Code, Codex, Qwen Code, Gemini CLI, Hermes, …)
working **on this repository**. It is symlinked as `CLAUDE.md` / `QWEN.md` / `GEMINI.md`.

> Not to be confused with the *KB schema* `AGENTS.md` that Agora generates inside each knowledge
> repo — that one governs wiki content. This one governs the Agora source code.

## What this project is
Agora (`agora-kb`) is a self-hostable, OSS, multi-tenant shared-memory hub for AI agents built on
markdown + git. Read [`docs/DESIGN.md`](docs/DESIGN.md) first; it is the single source of truth for
the architecture. Decisions are recorded in [`docs/adr/`](docs/adr/) — read the relevant ADR before
changing a load-bearing choice, and add a new ADR if you change one.

## Architecture in one paragraph
One **core API** (`write→inbox`, `read→wiki`, `meta`) with three **faces** (MCP server, web app,
dashboard) and three **adapter** families (input/extractors, read/harvesters, write/curator-brains).
**CQRS + single-writer curator**: many writers append to an immutable per-writer inbox; one curator
edits the shared wiki. Repo = tenant boundary. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Non-negotiable invariants (do not violate without an ADR)
1. **Markdown + git is the source of truth.** No database holds canonical knowledge; DBs are for
   metadata/indexes only and must be rebuildable from the markdown.
2. **All writes go through the inbox.** No face writes the wiki directly. Only the curator writes
   `wiki/`, indexes, and `log.md`.
3. **The inbox is append-only and per-writer-namespaced.** Never edit or reorder inbox items.
4. **Every component has an OSS path.** Proprietary services are optional plugins behind adapters,
   never required for the core to run. Avoid copyleft/AGPL deps in the core library.
5. **Tenant isolation is hard.** A curator/face for one repo must never touch another repo's files.
6. **Tool-agnostic.** Never hard-code a single agent or model; go through the adapter registry.

## Conventions
- **Language:** Python 3.12+. Package `agora_kb` under `src/` layout. `uv` for env/deps.
- **Style:** ruff (format + lint). Type hints required on public functions.
- **MCP:** FastMCP for the server face.
- **Tests:** pytest under `tests/`. Prefer testing the core API and adapter contracts.
- **Commits:** conventional-ish (`feat:`, `fix:`, `docs:`, `refactor:`). Small, focused.

## Layout
```
src/agora_kb/
  core/        single internal API: inbox(write) · wiki(read) + index_cache.py (the ADR-0012 §2
               derived query reader cache, _kb/index/, issue #26) · repo/tenant · state
  curator/     sleep-time consolidation worker + backends (BackendRegistry, per-act plan/author
               routing — ADR-0015) + triggers + isolation/ (OS sandbox)
  adapters/    curator-brain shims invoked via adapters.yaml argv: ollama_brain.py
               (agora-ollama-brain, local Qwen) + cli_agent_brain.py (agora-cli-brain — any
               headless CLI agent as a pure text-gen brain, ADR-0016)
  harvester/   read adapters: connectors.py (Connector Protocol + FileConnector, opt-in
               link-following) + harvester.py (orchestrator, scope gate, cursor) — pull other
               agents' memory → gated candidates (ADR-0007/0017/0018)
  ingest/      input adapters: vault_import.py (Obsidian/markdown vault normalizer) +
               extractors/ (url/pdf/office → ExtractedDoc; lazy optional `ingest` extra)
  faces/       mcp_server.py (the MCP face — agents; AgoraHandlers also hosts the read-only web
               aggregations: browse/note/health/curator_status/harvester_status + graph, ADR-0021)
               + web/ (the web face — humans):
               web/app.py (API-first FastAPI: JSON /api/* incl. /api/graph + server-rendered
               HTMX/Jinja2 UI incl. /graph, ADR-0019/0020/0021), web/metrics.py (Prometheus /metrics
               exporter), web/templates/ (graph.html), web/static/ (vendored MIT htmx + force-graph +
               graph.js — no Node/CDN)
  schema/      the KB wiki schema (AGENTS.md template emitted into each knowledge repo) + lint
  config.py    load config (adapters.yaml, repo.yaml, triggers + harvest policy + connector specs)
  cli.py       `agora` entry point (repo init · import · status · curate · harvest · index · watch ·
               serve · web · doctor)
  # --- not yet implemented (later phases) ---
  auth/        (Phase 4+ — stub) authn/authz (tokens, OpenFGA/Forgejo delegation)
docs/          DESIGN, ARCHITECTURE, DATA-MODEL, ROADMAP, INGEST-CONTRACT, adr/
```

## Where to start (current phase)
The repo has **shipped Phases 1, 2, and 3**.

**Phase 1** (Personal MVP): the **core API**, the `agora` CLI, the **MCP face** (four tools —
`kb_remember` / `kb_query` / `kb_status` / `kb_curate`), the local-model curator (Qwen via Ollama)
with the ADR-0013 OS sandbox, `ingest/vault_import.py`, and the wiki schema — all tested and
dogfooded on a real `~/knowledge` Obsidian vault.

**Phase 2** (pluggable brains + harvester): the `curator.backends` `BackendRegistry` from
`adapters.yaml` with per-act `plan`/`author` routing (ADR-0015), the generic `agora-cli-brain`
CLI-agent shim (ADR-0016), and the opt-in **harvester** with file connectors, the candidate gate,
provenance, fail-closed scope lock, and link-following (ADR-0007/0017/0018) — surfaced via
`agora harvest`, `agora curate --backend`, and the `agora doctor` routing/connectors tables.

**Phase 3** (ingest extractors + web face): the `ingest/extractors/` pure transforms (url via
trafilatura, pdf via pdfminer.six, office via markitdown → `ExtractedDoc`, behind the lazy optional
`ingest` extra), and the **web face** (`faces/web/app.py`, `agora web`) — an API-first FastAPI app
(JSON `GET /api/{status,search,notes,notes/{path}}` + `POST /api/upload`) with a server-rendered
HTMX/Jinja2 UI and XSS-safe markdown-it-py rendering (ADR-0019); the upload write-path runs an
extractor then `Inbox.write` (the curator remains the sole writer of `raw/`, ADR-0020); a read-only
**dashboard** (`/dashboard`, `AgoraHandlers.health()/curator_status()/harvester_status()`) that
reuses `lint()` verbatim; and a cheap Prometheus exporter (`faces/web/metrics.py`, `GET /metrics`,
which never runs lint on scrape). The curator now also wires the harvest cursor `accepted`/`rejected`
counters at finalize (ADR-0017 §7), so the dashboard/metrics surface real values. An interactive
**knowledge graph** (`GET /graph` + `GET /api/graph` + a per-note local ego-graph on `/note`,
`AgoraHandlers.graph()`) reuses `Wiki.list_notes` + `schema.notes` + `health()`'s orphan derivation
and is drawn by a vendored MIT force-graph lib (`web/static/force-graph.min.js` + `graph.js`, no
Node/build/CDN) — the first firing of the ADR-0019 §7 per-route-viz escape hatch (ADR-0021; a graph
DB such as Neo4j was rejected on license/SSOT/overkill grounds).

Next is **Phase 4** (auth + multi-tenancy); see [`docs/ROADMAP.md`](docs/ROADMAP.md). Auth,
multi-tenancy, original-binary-in-`raw/`, and Letta/mem0 API connectors remain deferred to Phases 4–5.
