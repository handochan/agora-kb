# Agora

> A self-hostable, fully open-source, multi-tenant **shared memory hub for AI agents**.
> Plain-markdown knowledge that many agents and people write to, that **organizes itself**,
> and that any MCP-speaking tool can read and contribute to.

**Status:** design phase. This repository currently contains the design documents; see
[`docs/DESIGN.md`](docs/DESIGN.md). Package/distribution name: `agora-kb`.

---

## What it is

Agora is a knowledge base that sits between you (and your team) and your raw sources, kept as
**interlinked markdown files in git** — no vector database. Retrieval is *navigation* (read an
index → follow `[[links]]` → grep), the way a coding agent already reads a codebase.

Unlike a static wiki, Agora has a **background curator**: an agent that, on a schedule, ingests
what was captured, summarizes it, files it under the right topic, updates indexes and backlinks,
and merges duplicates — the bookkeeping a human never sustains. This is the *sleep-time / "dreaming"*
pattern (cf. Letta sleep-time agents, Anthropic Dreaming) applied to a **markdown + local-LLM** store.

Agora is **tool-agnostic**: Claude Code, Codex, Gemini CLI, Qwen Code, OpenCode, Hermes, and any
MCP client can both **write** knowledge to it and **query** it. The curator's "brain" is itself
pluggable — any headless CLI agent + any model (default: a local open-weight model, zero API cost).

## Why

- **Compounding knowledge, not re-discovery.** RAG re-derives knowledge per query; Agora compiles
  it once and keeps it current. Cross-references and contradictions are already resolved.
- **Shared across agents.** One knowledge base any agent contributes to and reads from → agents
  (and teams) get smarter with use.
- **Yours.** Plain markdown on disk, versioned in git, self-hosted. No lock-in, no upload.
- **Open source, top to bottom.** Every component has a permissive-OSS default (see the BOM in
  [`docs/DESIGN.md`](docs/DESIGN.md)); proprietary agents are *optional plugins*.

## Core ideas (one screen)

- **One core API, many faces.** `write→inbox · read→wiki · meta` is the only entry point;
  the **MCP server** (agents), the **web app** (people, uploads), and the **dashboard** (status)
  are all faces over it. No face bypasses the pipeline → concurrency & access control are uniform.
- **CQRS + single-writer curator.** Many writers append to an immutable, per-writer inbox
  (conflict-free); exactly one curator process edits the shared wiki/index/log (no races).
  git is the audit log and the optimistic-concurrency backstop.
- **Three pluggable adapters** (the spine):
  - **input adapters** — upload extractors (URL, PDF, docx → markdown)
  - **read adapters** — *harvesters* that pull from other agents' memory systems → candidates
  - **write adapters** — the curator's *brain* (a headless CLI agent, swappable via config)
- **Repo = tenant boundary.** Team repos and personal repos are equal citizens, hard-isolated,
  with role- and (optionally) domain-level access control.

## Documentation

| Doc | What |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | Full design — core, tenancy, harvester, web/dashboard, OSS BOM |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, data flow, deployment topology |
| [`docs/DATA-MODEL.md`](docs/DATA-MODEL.md) | Concrete schemas: inbox item, repo meta, state, provenance |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Phased path: personal MVP → harvester → team server |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records (the *why* behind each choice) |

## License

Apache-2.0 (see [`LICENSE`](LICENSE)) — permissive, to maximize adoption.
