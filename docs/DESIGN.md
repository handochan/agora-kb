# Agora — Design

> Single source of truth for the architecture. Component/dataflow detail is in
> [ARCHITECTURE.md](ARCHITECTURE.md); concrete schemas in [DATA-MODEL.md](DATA-MODEL.md);
> rationale in [adr/](adr/); delivery sequence in [ROADMAP.md](ROADMAP.md).

## 1. Problem & goals

Agents and people accumulate knowledge constantly, but it scatters: chat logs, per-tool memories,
half-written notes, raw articles. Existing answers force a trade-off:

- **RAG / vector stores** re-derive knowledge per query; opaque indexes; infra-heavy.
- **Static wikis** require human bookkeeping (cross-links, dedup, indexing) that never scales.
- **Per-agent memories** (Claude Code memory, Hermes memory, Letta, mem0) are siloed per tool.

**Agora's thesis:** keep knowledge as **plain markdown in git** (human- and agent-readable, no DB),
and put a **background agent ("curator")** in charge of the bookkeeping. Many agents and people
**contribute**; the curator **consolidates** on a schedule. The result is a *compounding, shared,
self-organizing* knowledge base that any MCP-speaking tool can use.

### Goals
- **Compounding, navigable knowledge** (Karpathy "LLM wiki" pattern): `raw/` immutable sources +
  `wiki/` agent-maintained pages + indexes + backlinks. Retrieval = navigation, not vector search.
- **Tool-agnostic.** Any agent (Claude Code, Codex, Gemini, Qwen, OpenCode, Hermes, …) reads and
  writes via MCP; the curator's brain is itself a swappable agent.
- **Self-organizing.** A scheduled "sleep-time" curator ingests, summarizes, files, links, dedups.
- **Multi-tenant.** Team and personal knowledge repos, hard-isolated, with access control.
- **Self-hostable & fully OSS.** Every component has a permissive-OSS default; local-model path =
  zero API cost, no data leaves the host. Proprietary agents are optional plugins.

### Non-goals (v1)
- Not a real-time collaborative editor (git + curator handle merge, not OT/CRDT live cursors).
- Not a vector database (a thin semantic-search layer may be added later, over the markdown).
- Not a chat product; it is the *memory layer* other agents/products plug into.

## 2. Architectural spine

### 2.1 One core API, many faces
There is a single internal API; everything else is a **face** over it.

```
            ┌──────────── CORE API ────────────┐
            │  write(target, item)  → inbox     │   ← the only entry point.
            │  read(scope, query)   → wiki      │     enforces tenancy + access control here,
            │  meta(scope)          → status    │     so every face inherits it for free.
            └───────────────────────────────────┘
              ▲                ▲                ▲
        MCP server        Web app          Dashboard
        (agents)       (people, upload)   (status, read-only)
```

Because **every write goes to the inbox** and **every read goes through the wiki**, no face can
bypass the pipeline. Concurrency safety, provenance, and access control are properties of the core,
not of each face. (ADR-0003.)

### 2.2 CQRS + single-writer curator (the concurrency model)
Writes and the read-model are separated:

```
WRITE side (many writers, conflict-free)      READ-MODEL side (one writer, no races)
────────────────────────────────────────     ─────────────────────────────────────
kb_remember / upload / harvest                curator consolidates the inbox →
  → append ONE file to                          edits wiki/ + indexes + log.md
    _kb/inbox/<writer>/<id>.md                  (exactly one process per repo)
  (disjoint keys ⇒ no write conflicts)        → git commit (audit + OCC backstop)
```

- N concurrent writers touch **disjoint files** → zero write conflicts (event-sourcing append log).
- The **shared** files (`wiki/`, indexes, `log.md`) are edited by **exactly one** curator per repo
  → no races, no locks needed across the network. (ADR-0002.)
- git is the canonical-content audit log, rollback point, and publication boundary.
- **Consistency model:** *eventually consistent*. A captured item is durable immediately (in inbox,
  git-able) but becomes queryable only after consolidation. `kb_status` surfaces the pending backlog.

### 2.3 Three pluggable adapter families (the spine)
Everything cognitive or format-specific is an adapter; the core stays small.

| Adapter family | Role | Examples |
|---|---|---|
| **Input adapters** (`ingest/`) | turn any upload into markdown in `raw/` | URL→trafilatura, PDF→pdfminer, docx→markitdown |
| **Read adapters** (`harvester/`) | pull from other agents' memory systems → inbox *candidates* | file (Claude/Hermes memory), API (Letta, mem0) |
| **Write adapters** (`curator/backends`) | the curator's *brain*: run INGEST over a batch | `claude -p`, `codex exec`, `qwen -p`, `hermes chat -q`, local model |

New format / new agent-memory source / new curator brain = one adapter, no core change. (ADR-0004.)

## 3. Storage layout (per knowledge repo)

Each repo is a git repository the curator maintains. Layout (the wiki schema is emitted as the
repo's own `AGENTS.md`/`SCHEMA.md`, tool-agnostic via symlinks `CLAUDE.md`/`QWEN.md`/`GEMINI.md`):

```
<repo>/
  AGENTS.md / SCHEMA.md / CLAUDE.md…   the KB schema (conventions: frontmatter, links, INGEST/QUERY/LINT)
  index.md                             top Map-of-Content (navigation hub)
  log.md                               append-only action log
  raw/<domain>/                        Layer 1: immutable sources (+ sha256 for re-ingest drift)
  wiki/<domain>/
    <domain>-moc.md                    domain Map-of-Content
    daily/                             dated notes (briefings/captures)
    themes/                            durable, atomic concept pages
  assets/                              binary assets (images), referenced from notes
  _templates/                          note templates
  _kb/                                 git-ignored operational area (NOT wiki content)
    inbox/<writer>/                    WRITE path: append-only captures
    processing/<run-id>/               atomically claimed immutable items + run manifest
    processed/<date>/                  consolidated items (audit trail)
    failed/                            terminal failures + separate error records
    state.json                         curator state: counters, last_run, published runs
    curator.lock                       flock held during a consolidation run
```

`raw/` + `wiki/` + indexes are the **knowledge** tracked in git. `_kb/` is the engine's git-ignored
operational spool; it is durable on the repo owner's storage but is not canonical knowledge. Its
indexes/state are rebuildable from retained events and git history. Schemas are in
[DATA-MODEL.md](DATA-MODEL.md).

## 4. The curator (sleep-time consolidation)

A background worker, one per repo, triggered by **cron** + **threshold** (inbox depth ≥ N) + **idle**
(no writes for M minutes and backlog > 0). Run loop:

1. **Acquire `curator.lock`** (flock, non-blocking; if held, exit — a run is already in progress).
2. **Claim** a FIFO snapshot by atomically moving unchanged events from `_kb/inbox/` to
   `_kb/processing/<run-id>/`; write a run manifest and ignore later arrivals.
3. **Classify duplicates:** `event_key` handles delivery idempotency; `content_sha256` finds equivalent
   content whose provenance must still be merged rather than silently discarded.
4. **Create an isolated temporary git worktree** at the current curated revision.
5. **Delegate INGEST** to the configured **write adapter** inside an OS sandbox. The backend has no
   network or credentials by default and can write only the temporary repo content paths. Harvested
   *candidates* are gated (see §6).
6. **Validate** the diff deterministically: reject path escapes, symlinks, malformed schema, and any
   backend change to `_kb/`, git configuration, hooks, or other non-allowlisted paths.
7. **Commit and publish:** commit the validated worktree and compare-and-swap the curated branch from
   the manifest's base commit to the new commit — or publish that commit as a PR in review mode.
8. **Finalize** events to `processed/<date>/`; terminal failures go to `failed/` with a separate error
   record. Update `state.json`, remove the worktree, and release the lock. An interrupted run is
   recovered from its manifest without rerunning a commit that was already published.

The orchestration (lock, queue, dedup, git) is **deterministic code**; only the *cognitive* INGEST
step is delegated to a swappable agent. Transactional worktrees, validation, and sandboxing keep the
backend outside the integrity boundary. (ADR-0004, ADR-0008, ADR-0013.) Routing is supported: bulk/simple →
local open-weight model (free); hard merges /
contradiction resolution → a stronger (optional, possibly proprietary) backend.

## 5. Faces

### 5.1 MCP server (agents)
FastMCP over stdio (local) or Streamable HTTP (team). Tools:

| Tool | Side | Behavior |
|---|---|---|
| `kb_remember(text, target?, domain?, tags?, source?)` | write | append inbox item, return `{id, queued, inbox_depth}` — **non-blocking** |
| `kb_query(question, scope?)` | read | return ordered evidence hits with path/anchor citations; optional later synthesis may use only those hits |
| `kb_status(scope?)` | meta | `{inbox_depth, last_consolidation, processed_today, failed}` |
| `kb_curate(target, force?)` | admin | trigger a consolidation run now (also invoked by triggers) |

One MCP registration = every MCP client (Claude Code, Codex, Qwen, Hermes, …) automatically gains
these tools. No per-tool plugin needed.

### 5.2 Web app (people)
A FastAPI + lightweight-frontend face for humans:
- **Browse & search** the wiki (read path).
- **Upload** text / files (PDF, docx, …) / URLs → stored verbatim in `raw/` (+ provenance, sha256),
  then an inbox candidate is enqueued; the curator turns it into a wiki note. Upload reuses the
  `raw/` + inbox path exactly — no new concept, same concurrency & access control. (ADR-0003.)
- Binary assets → `assets/`, referenced from notes (outside the navigation graph).

### 5.3 Dashboard (status, read-only)
A read-only meta face. **All data it needs already exists** in `_kb/state.json`, the live `inbox/`
count, `log.md`, git history, and `processed/`+`failed/`. Panels:
- **KB health (per team / per person):** note counts, themes vs daily, tag distribution, growth,
  orphan/contested notes (lint signals), last consolidation.
- **Curator/agent status:** queue depth, in-flight, throughput, success/fail, active backend+model,
  cost (if a paid backend), and a work-log timeline (what was ingested/merged/dropped).
- **Harvester status:** connectors enabled, last scan per source, candidates proposed/accepted/rejected.

Observability splits in two: operational time-series (queue depth, throughput) via
**Prometheus + Grafana** (the curator exports metrics); content/health views via the web app.
Scope is filtered by access control — a viewer sees only their teams/personal repos.

## 6. Memory harvester (autonomous accumulation — optional)

The mirror of the curator: **read adapters** that periodically scan other agents' memory systems and
propose candidate knowledge.

```
agent memory sources                          candidate flow
─────────────────────                          ──────────────
Claude Code  ~/.claude/.../MEMORY.md  (file)   diff vs cursor → new/changed only
Codex        ~/.codex                 (file)   → inbox item:
Hermes       ~/.hermes/MEMORY.md      (file)      source=harvest:<agent>, kind=candidate,
Letta        memory blocks            (API)       confidence=low
mem0         vector store             (API)   → curator review gate: keep / merge / drop
```

Three safety mechanisms are mandatory (without them this feature poisons the base):

| Risk | Mitigation |
|---|---|
| **Feedback loop** (KB → agent memory → harvest → KB …) | provenance tags (`harvest:<agent>`) + origin marking; never re-harvest KB-originated facts |
| **Noise pollution** | harvested items are `kind=candidate` / `confidence=low` → must pass the curator review gate before promotion to `wiki/`; never written directly |
| **Privacy leakage** | scope enforcement: a personal agent-memory source feeds **only** the personal repo, never a team repo; team harvest requires explicitly-designated team sources; consent-based |

Effect: Agora becomes the **shared long-term memory of all the user's/team's agents** — the
"memory of memories." (ADR-0007.)

## 7. Multi-tenancy & access control

**Repo = tenant boundary.** (ADR-0006.) Each repo is an independent git repository + inbox + curator
state + schema. **Team repos** (shared) and **personal repos** (private to one user) are equal.

- A user belongs to multiple teams and owns ≥1 personal repo.
- **Write routing:** `target="team:engineering" | "personal"` selects the repo's inbox (default: personal).
- **Read scope:** `scope=[...]` queries across the repos the caller may read.
- **Roles:** `owner > editor > reader` per repo; optionally per-domain ACL (`wiki/<domain>`).
- **Isolation is hard:** writes land in that repo's `_kb/inbox/<user>/…`; a repo's curator only ever touches
  that repo's files. Cross-repo leakage is structurally impossible (separate git repos).

Team mode initially uses one **repo-owner process** per repo working copy. Gateways route captures to
that owner; they do not mount or mutate independent clones. Horizontal scale later uses repo-affine
sharding, still with one owner and one curated branch writer per repo.

External editors are read/browse tools by default. Direct edits to `wiki/` are unsupported while a
curator owns the branch; human contributions use `kb_remember`/upload, or a review-mode PR that the
repo owner imports. This preserves the single-writer invariant.

**Access control, OSS, two tiers:**
1. **Delegate to the git host (Forgejo/Gitea):** repos, teams, roles, and PRs already exist there;
   Agora authorizes via the host's tokens/API → no ACL to reinvent. Best starting point.
2. **OpenFGA (relationship-based / Zanzibar-style):** when fine-grained, per-domain rules are needed
   (`user editor team/repo`, `domain:finance reader-only`).

Transport for teams: **MCP Streamable HTTP** with **OAuth 2.1 + PKCE** (or tokens + Tailscale for
small private teams). AuthN via Keycloak/Authentik/Ory.

## 8. Fully-OSS bill of materials

Core must run with **zero proprietary dependencies**; proprietary agents are optional plugins.

| Layer | OSS default | License | Optional proprietary |
|---|---|---|---|
| MCP server | FastMCP | Apache/MIT | — |
| Storage / SSOT | git + Forgejo (or Gitea) | MIT/GPL | GitHub/GitLab |
| Curator brain | OpenCode · Goose · Hermes (OSS agents) | Apache/MIT | Claude Code · Codex · Gemini |
| Model | local Qwen via Ollama/vLLM | MIT/Apache | Claude · GPT APIs |
| AuthN | Keycloak · Authentik · Ory | Apache/MIT | Auth0 |
| AuthZ | OpenFGA · Casbin | Apache | — |
| Web view | Quartz (read) · Tolaria (edit) | MIT | Notion |
| Personal editor | Obsidian (free, non-OSS core) → Logseq/Foam for pure-OSS | — | — |
| Queue (if needed) | filesystem · NATS / Valkey | Apache/BSD | Redis (non-OSI → use Valkey) |
| Metadata DB | SQLite · Postgres | PD/OSS | — |
| Deploy | Docker / Podman | Apache | — |

**Purity notes:** Obsidian's core is non-OSS (swap for Logseq for a 100%-OSS stack); Redis is no
longer OSI (use Valkey); Grafana and pymupdf are AGPL — fine for self-host, avoid in redistributable
core (we use pdfminer.six instead of pymupdf). The fully-OSS curator path is
*OpenCode/Goose/Hermes + Qwen via Ollama* — autonomous consolidation with not one proprietary line.

## 9. Prior art & differentiation

Agora's pattern matches what the industry converged on in 2026 — *async write-behind + background
("sleep-time"/"dreaming") consolidation + MCP connector* — seen in Letta sleep-time agents, Anthropic
Dreaming, Amazon Bedrock AgentCore Memory, and `mcp-memory-service`. On the storage side it shares the
markdown-first philosophy of Basic Memory, Khoj, and the Karpathy LLM-wiki pattern.

**The gap Agora fills:** no mature OSS project combines *markdown wiki × queue + sleep-time
consolidation + MCP × local-LLM × multi-tenant teams + memory harvesting*. That intersection — a
self-hostable, fully-OSS, multi-tenant shared-memory hub — is Agora.
