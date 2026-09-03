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
               derived query reader cache, _kb/index/, issue #26) + gold.py (ADR-0027 pack
               assembler, _kb/gold/) + redact.py/sentinel.py (outbound redaction + the §8 sentinel,
               ADR-0023/0027) + rank_snapshot.py (the model-free deterministic ranking snapshot
               behind `agora eval`, #44) · repo/tenant · state
  curator/     sleep-time consolidation worker + backends (BackendRegistry, per-act plan/author
               routing — ADR-0015) + triggers + isolation/ (OS sandbox)
  adapters/    curator-brain shims invoked via adapters.yaml argv: ollama_brain.py
               (agora-ollama-brain, local Qwen) + cli_agent_brain.py (agora-cli-brain — any
               headless CLI agent as a pure text-gen brain, ADR-0016)
  harvester/   read adapters: connectors.py (Connector Protocol + FileConnector, opt-in
               link-following) + session_connector.py/session_sources.py (SessionConnector —
               transcript distillation + connector-boundary redaction, ADR-0023) + harvester.py
               (orchestrator, scope gate, cursor) — pull other agents' memory/sessions → gated
               candidates (ADR-0007/0017/0018/0023)
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
  cli.py       `agora` entry point (repo init · import · status · curate · harvest · index · gold ·
               eval · sync · watch · serve · web · doctor)
  # --- not yet implemented (later phases) ---
  auth/        (Phase 4+ — stub) authn/authz (tokens, OpenFGA/Forgejo delegation)
deploy/        launchd/systemd unit templates for always-on watch/web + harvest schedule (#65)
docs/          DESIGN, ARCHITECTURE, DATA-MODEL, ROADMAP, INGEST-CONTRACT, DEPLOY-TEAM, adr/
```

## Where to start (current phase)
The repo has **shipped Phases 1, 2, 3, 3.5, and 3.6**.

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

**Phase 3.5** (backlog pull-forward — ADRs 0022–0025 + 0027, all Accepted in the 2026-07-05 Step-0
session, #36): the no-loss catch-all floor (`domains[0]`) + repo-global curator thresholds (#23,
ADR-0022); the bounded-batch claim cap `max_candidates_per_run` (#60, ADR-0024 OD-3a); the web
`web:` operator config (`load_web_config`, per-repo in `build_app`) + multi-upload + broadened
extractor extensions (#29, ADR-0025); the ADR-0012 §2 derived reader cache (#26); the derived
**gold** context-pack tier `_kb/gold/` + the outbound sentinel/loop-break contract (#37, ADR-0027);
and the `session:` harvester connector with connector-boundary redaction via `core/redact.py`
(#25/#39, ADR-0023).

**Phase 3.6** (deployability + retrieval quality, 2026-07-24/25): Korean search + Korean no-loss
fixes (#56/#57 — CJK-bigram tokenizer/cache v2 + `note-<sha8>` slug fallback); `kb_read`/
`kb_neighbors` MCP tools (#58); gold Phase-C consumption channels — `kb_context` +
`agora://gold/{pack}` resource/prompt + `GET /api/gold/{pack}` (#40 agora half); `agora sync`
push-only remote backup (#64); `deploy/` launchd/systemd packaging (#65); upload hardening — SSRF
guard + zip-bomb cap + `.epub` (#66/#53); per-user identity threading `web.identity.trusted_header`
→ `web:<user>` (#67); and the team deployment guide [`docs/DEPLOY-TEAM.md`](docs/DEPLOY-TEAM.md) (#68).

Next is **Phase 4** (auth + multi-tenancy); the gating design debt is the authn/authz ADR (#69).
See [`docs/ROADMAP.md`](docs/ROADMAP.md) and the GitHub Project "agora dev" board (the live
backlog SSOT). Auth, multi-tenancy, original-binary-in-`raw/`, and Letta/mem0 API connectors remain
deferred to Phases 4–5.
