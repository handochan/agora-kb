# Agora — Roadmap

Phased delivery. Each phase is independently useful and builds on the last. The guiding rule: **prove
the core (capture → consolidate → query) on a single local repo before adding faces, auth, or scale.**

## Phase 0 — Design (complete)
- [x] Design docs (DESIGN, ARCHITECTURE, DATA-MODEL), ADRs, project scaffold.
- [x] Validate the wiki schema against a real existing KB (`~/knowledge`) for compatibility
      (validated via `agora import` on a `~/knowledge` clone → lint-clean; ADR-0014 D5).

## Phase 1 — Personal MVP (core + MCP face) — DONE (shipped, dogfooded on `~/knowledge`)
Goal: a single local markdown repo with capture, scheduled consolidation, and query — zero infra.
- [x] `core.inbox` (immutable events + event-key idempotency), deterministic `core.wiki`
      (`QueryResult`/`SearchHit`), `core.repo` (layout, git), `core.state`.
- [x] `curator.worker` transactional run loop (claim manifest → sandboxed worktree → validate → publish
      → finalize) with the **local-model**
      write-adapter (Qwen via Ollama) as default brain. OS sandbox per ADR-0013 (`agora doctor`
      self-test); the default Ollama brain does inference OUTSIDE the sandbox (env-scrubbed) by design.
- [x] `curator.triggers`: cron + threshold + idle (deterministic cron matcher + `agora watch`).
- [x] MCP face: `kb_remember`, `kb_query`, `kb_status`, `kb_curate` (FastMCP, stdio).
- [x] `agora` CLI: `serve`, `curate`, `repo init`, `status`, `doctor` (+ `watch`, `import`).
- [x] Register with Claude Code + Hermes; dogfood on `~/knowledge` (both clients ✔ connect to
      `agora serve`; the real `~/knowledge` is imported non-destructively to a curate-able KB and a
      live capture → curate → query loop runs on it).
**Exit:** capture from any MCP client → curator atomically files it → deterministic query returns it
with path/anchor citations; injected or failed backend output cannot modify the live wiki.

## Phase 2 — Pluggable brains + harvester (current/next)
Goal: tool-agnostic curator and autonomous accumulation, still single-user.
- [ ] `curator.backends` registry from `adapters.yaml`; verify ≥3 brains (qwen, claude, hermes) +
      per-task routing. (Per-act `plan`/`author` routing **shipped** — ADR-0015; live ≥3-brain
      verification still pending.)
- [ ] `harvester` with file connectors (Claude Code memory, Hermes `MEMORY.md`); candidate gate;
      provenance + loop prevention; personal-repo scope lock.
**Exit:** swap the curator brain via config with no data risk; harvested candidates flow safely.

## Phase 3 — Web face: upload + dashboard
Goal: humans contribute and observe via the browser.
- [ ] `ingest` extractors (url/pdf/office) → `raw/` + inbox.
- [ ] Web app: browse, search, upload.
- [ ] Dashboard: KB health + curator status + harvester status (reads existing metadata).
- [ ] Prometheus metrics export; optional Grafana dashboards.
**Exit:** upload a PDF/URL in the browser → it becomes a linked wiki note; dashboard shows the queue.

## Phase 4 — Small team (multi-tenant, network)
Goal: a few trusted people share repos over a private network.
- [ ] `core.repo` multi-repo + team/personal kinds; write routing + read scope.
- [ ] Single repo-owner service per working copy; gateways route captures to the owner (no competing clones).
- [ ] MCP **Streamable HTTP** transport.
- [ ] AuthN/AuthZ via **Forgejo delegation** (repos, teams, roles); token auth + Tailscale.
- [ ] Shared git remote (Forgejo); Quartz read-only web view.
**Exit:** two users, two teams + personal repos, hard-isolated, over the network.

## Phase 5 — Full team (governance & scale)
Goal: production-grade access control and review.
- [ ] **OpenFGA** for fine-grained, per-domain ACL.
- [ ] **OAuth 2.1 + PKCE** via Keycloak/Authentik.
- [ ] PR review mode (curator opens PRs instead of direct commits).
- [ ] API harvester connectors (Letta, mem0); horizontal curator sharding by repo.
- [ ] Horizontal face scaling with repo-affine owner routing and failover fencing.
- [ ] Packaging: Docker Compose, Helm (optional), published `agora-kb` (PyPI) + images.
**Exit:** self-hostable multi-team deployment with auth, review, and governance.

## Cross-cutting (every phase)
- Tests for the **core API** and **adapter contracts** (the stable surfaces).
- Keep core dependency-light and OSS-pure (ADR-0005); proprietary pieces stay behind adapters.
- Dogfood: Agora's own design/dev knowledge lives in an Agora repo as soon as Phase 1 lands.

## Explicitly deferred / out of scope (revisit later)
- Semantic/vector search layer over the markdown (only if navigation proves insufficient at scale).
- Real-time collaborative editing (CRDT/OT).
- Hosted/SaaS offering.
