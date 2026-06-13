# ADR-0005 — Fully-OSS bill of materials; proprietary pieces are optional plugins

**Status:** Accepted · 2026-06-13

## Context
A self-hostable knowledge/memory product must be trustworthy and free of lock-in. The most capable
coding agents (Claude Code, Codex, Gemini) are proprietary clients, and some popular infra has
non-OSI licenses (Redis) or copyleft/AGPL terms (Grafana, pymupdf) that complicate redistribution.

## Decision
The **core must run with zero proprietary dependencies.** Every layer has a permissive-OSS default;
anything proprietary is an **optional plugin behind an adapter**, never required to run.
- Curator brain default: OSS agents (OpenCode/Goose/Hermes) + local open-weight model (Qwen via
  Ollama/vLLM). Proprietary agents are opt-in write-adapters.
- Storage/host: git + Forgejo/Gitea. Auth: Keycloak/Authentik/Ory + OpenFGA/Casbin. MCP: FastMCP.
- **License hygiene:** prefer MIT/Apache/BSD in the **core library**. Avoid AGPL/copyleft in
  redistributable code (use `pdfminer.six`, not AGPL `pymupdf`). Use **Valkey**, not (non-OSI) Redis.
  AGPL tools acceptable only as *separate self-hosted services* (e.g. Grafana for dashboards), never
  linked into the core. For a 100%-OSS editor, use **Logseq/Foam** instead of (non-OSS-core) Obsidian.
- Project license: **Apache-2.0** (permissive, patent grant, adoption-friendly).

## Consequences
- **+** Anyone can self-host the whole stack with no proprietary account or license.
- **+** Clear contribution boundary; proprietary integrations don't contaminate the core's license.
- **−** The default (local-model) curator may be less capable than a top proprietary agent; mitigated
  by per-task routing to an optional stronger backend when the user opts in.
- **−** Ongoing license vigilance on new dependencies (CI check recommended).
