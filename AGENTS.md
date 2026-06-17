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
  core/        single internal API: inbox(write) · wiki(read) · repo/tenant · state
  curator/     sleep-time consolidation worker + backends + triggers + isolation/ (OS sandbox)
  ingest/      input adapters: vault_import.py (Obsidian/markdown vault normalizer)
  faces/       mcp_server.py (the MCP face — agents)
  schema/      the KB wiki schema (AGENTS.md template emitted into each knowledge repo) + lint
  config.py    load config (adapters.yaml, repo.yaml, triggers)
  cli.py       `agora` entry point (repo init · import · status · curate · watch · serve · doctor)
  # --- not yet implemented (later phases) ---
  harvester/   (Phase 2+ — stub) read adapters: pull from other agents' memory → candidates
  faces/web/   (Phase 3+ — stub) FastAPI app: upload, search, dashboard
  auth/        (Phase 4+ — stub) authn/authz (tokens, OpenFGA/Forgejo delegation)
docs/          DESIGN, ARCHITECTURE, DATA-MODEL, ROADMAP, INGEST-CONTRACT, adr/
```

## Where to start (current phase)
The repo has **shipped Phase 1** (Personal MVP): the **core API**, the `agora` CLI, the **MCP face**
(four tools — `kb_remember` / `kb_query` / `kb_status` / `kb_curate`), the local-model curator
(Qwen via Ollama) with the ADR-0013 OS sandbox, `ingest/vault_import.py`, and the wiki schema are all
implemented, tested, and dogfooded on a real `~/knowledge` Obsidian vault (see
[`docs/ROADMAP.md`](docs/ROADMAP.md) Phase 1). Current work is **Phase 2** — a pluggable-brains
registry from `adapters.yaml` plus the harvester. Auth, web, and multi-tenancy remain deferred to
Phases 3–5.
