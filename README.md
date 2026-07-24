# Agora

> A self-hostable, fully open-source, multi-tenant **shared memory hub for AI agents**.
> Plain-markdown knowledge that many agents and people write to, that **organizes itself**,
> and that any MCP-speaking tool can read and contribute to.

**Status:** **Phases 1–3 shipped** — a local markdown KB with capture (`kb_remember`), deterministic
navigation query (`kb_query`), and a scheduled curator (`kb_curate`), exposed over an MCP stdio face
and the `agora` CLI. The curator's brain is **pluggable/swappable via `adapters.yaml`** (a local
Ollama model or any headless CLI agent) with per-act `plan`/`author` routing (ADR-0015/0016), and an
opt-in **harvester** pulls other agents' on-disk memory into gated candidates (`agora harvest`,
ADR-0007). Phase 3 adds the **web face** (`agora web`): URL/PDF/office upload extractors, an API-first
FastAPI server with a server-rendered HTMX UI for browse/search/upload, an interactive **knowledge
graph** (`/graph` + a per-note local graph; vendored MIT force-graph, no Node — ADR-0021), a
read-only **dashboard** (KB health · curator · harvester), and a Prometheus `/metrics` exporter
(ADR-0019/0020/0021). Phases 4–5
(multi-tenant, auth, governance) are still ahead; see
[`docs/ROADMAP.md`](docs/ROADMAP.md). Package/distribution name: `agora-kb`.

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

## Quickstart (local, no cloud, no auth)

Runs entirely on your machine: a local markdown repo, a local model (Qwen via Ollama by
default), zero cloud, zero auth. Requires **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/).

```bash
# 1. Install the project
uv sync

# 2. Create a knowledge repo  (note: it is `agora repo init`, NOT `agora init`)
uv run agora repo init ~/my-kb --name my-kb --domain general

# 3. Health check — git, deps, and the curator OS-sandbox self-test (ADR-0013)
uv run agora doctor --repo ~/my-kb

# 4. (optional) Import an existing Obsidian/markdown vault — non-destructive: the
#    source is only READ; a new normalized Agora repo is written to the destination.
uv run agora import ~/existing-vault ~/my-kb --domain general

# 5. Run the MCP server face over stdio (what an MCP client connects to)
uv run agora serve --repo ~/my-kb
```

Register the stdio server with any MCP client and the seven tools — `kb_remember`, `kb_query`,
`kb_read`, `kb_neighbors`, `kb_context`, `kb_status`, `kb_curate` — appear to the agent (plus the
`agora://gold/{pack}` resource and the `gold_context` prompt for the gold pack, ADR-0027):

```bash
# Claude Code (other MCP clients: point them at `agora serve --repo ~/my-kb` over stdio)
claude mcp add agora-kb -- uv run --directory /path/to/agora-kb agora serve --repo ~/my-kb
```

The loop: an agent calls **`kb_remember`** to capture a fact (it lands in the append-only inbox),
**`kb_curate`** to consolidate the inbox into the wiki with the local model, and **`kb_query`** to
get cited evidence back — then **`kb_read`** to open a cited note and **`kb_neighbors`** to walk
its link neighborhood before re-querying (the agentic navigation loop, no filesystem access
needed). You can drive the same loop from the CLI without an agent —
`agora status` (inbox depth + curator state), `agora curate` (one consolidation run), and
`agora watch` (scheduler loop: cron + threshold + idle triggers).

**Phase 2 (pluggable brains + harvester).** To swap the curator's brain, add a `default_backend`
(and optional `routing: {plan, author}`) to `adapters.yaml` and run `agora curate --backend NAME`
(ADR-0015); any headless CLI agent can be a brain via `agora-cli-brain` (ADR-0016). To pull another
agent's on-disk memory (e.g. `~/.claude/**/MEMORY.md`) into the inbox as gated low-confidence
candidates, declare a `connectors:` block in `adapters.yaml`, enable `harvest.enabled` in
`_kb/repo.yaml`, then preview with `agora harvest --dry-run` and run `agora harvest` (ADR-0007/0018).
`agora doctor` prints the routing table and the configured connectors.

**Phase 3 (web face).** The optional web surface lives behind the `web`/`ingest`/`metrics` extras,
so the core stays dependency-light. Install them and launch the FastAPI + HTMX face over your repo:

```bash
# Install the web/upload/metrics extras (all permissive-OSS; lazy-imported)
uv sync --extra web --extra ingest --extra metrics

# Run the web face — localhost, single-user, no auth (ADR-0019)
uv run agora web --repo ~/my-kb            # → http://127.0.0.1:8000
```

You get a browse/search UI (markdown rendered XSS-safe via `markdown-it-py`), an **upload** page that
runs URL/PDF/office extractors and writes the result through the inbox (the curator stays the sole
writer of `raw/`; ADR-0020), a first-class JSON API under `/api/*`, an interactive **`/graph`**
knowledge graph (plus a per-note local/backlink graph; canvas drag/zoom/click → the note, vendored
MIT force-graph, no Node — ADR-0021), a read-only **`/dashboard`**
(KB health · curator · harvester · gold-pack status, HTMX-polled), and a Prometheus **`/metrics`**
endpoint for external scraping. It binds to `127.0.0.1` by default and ships no authentication — keep
it local. The upload surface is hardened for the day you do share it (the team-deployment guide,
[`docs/DEPLOY-TEAM.md`](docs/DEPLOY-TEAM.md)): server-side URL fetches are SSRF-guarded (private/loopback/link-local/metadata targets
and redirects into them are refused) and can be disabled outright with `web.upload.url_enabled:
false`, and zip-based uploads (docx/xlsx/pptx/epub) are capped against decompression bombs via
`web.upload.max_uncompressed_bytes` (ADR-0025 appendix). Behind an **authenticating reverse proxy**,
set `web.identity.trusted_header` (e.g. `X-Remote-User`) so each teammate's uploads stamp their own
`web:<user>` provenance instead of one shared `web:local` — opt-in, header forced/stripped by the
proxy only (issue #67; snippets in `deploy/README.md`).

**Gold context packs (ADR-0027).** Above the searchable wiki, Agora assembles a **gold** tier: a
small, token-budgeted, byte-stable slice of your highest-value theme notes, meant to be injected at
agent session start (the CLAUDE.md-style standing context every CLI agent wants). It is a *pure,
deterministic function of (curated commit, pack spec)* — reader-class code assembles verbatim summary
lines; no LLM runs, nothing writes the wiki. The curator rebuilds it after each consolidation (it is
never staler than the wiki), or build it explicitly:

```bash
uv run agora gold build --repo ~/my-kb      # → _kb/gold/default.md (+ .meta.json sidecar)
uv run agora gold status --repo ~/my-kb     # presence + freshness vs the curated tip
uv run agora gold build --check --repo ~/my-kb   # CI: verify byte-identical rebuild (exit 1 if drifted)
```

**Consume it pull-only** — add a one-line include to your agent config (the include itself is your
standing consent; Agora never writes agent config dirs):

```
@~/my-kb/_kb/gold/default.md
```

Every pack is wrapped in an `<!-- agora:pack … -->` sentinel span that the harvester drops whole, so
an injected pack round-trips to **zero** re-harvested facts, and harvest-origin notes are excluded
from packs — gold can never become a prompt-injection amplifier (ADR-0027 §8). Agents without
filesystem access consume the same pack over MCP — the **`kb_context`** tool (plus the
`agora://gold/{pack}` resource and `gold_context` prompt) — and any HTTP client over
**`GET /api/gold/{pack}`** on the web face; every channel serves the built pack **byte-identically**
and answers a not-yet-built pack with actionable build guidance — the tool and prompt return it in
the response, the resource and web route carry it in an explicit error (`ResourceError` / HTTP 404)
(Phase C, #40). All of it is pull-only: nothing auto-injects a pack anywhere.

**Backup (`agora sync`, push-only).** The KB is a plain git repo, but nothing pushes it anywhere by
default — a lost disk would be a lost KB. Configure a backup remote in `_kb/repo.yaml` and push the
curated branch explicitly (or automatically after each successful `agora watch` consolidation):

```yaml
# _kb/repo.yaml
backup:
  remote: git@github.com:you/my-kb-backup.git  # a git remote name or URL (default: none — off)
  auto: true                                   # push after each successful watch-tick curation
```

```bash
uv run agora sync --repo ~/my-kb   # push the curated branch — fast-forward only, never --force
```

This is **strictly push-only, one direction**: Agora never pulls or fetches. A non-fast-forward
rejection (the remote is ahead — another machine has pushed) is reported as a clear error and left
alone; multi-machine topology / bidirectional sync is deferred to issue #46. An auto push is
best-effort **and non-interactive**: credential/host-key prompts are disabled and the push is
time-bounded, so an unattended scheduler can never hang on one — use an ssh agent or a credential
helper (not a prompting flow) with `auto: true`. Its failure never fails the curation that
triggered it (the result surfaces on the `agora doctor` backup line). **Coverage limits:** `_kb/` is git-ignored, so *uncurated inbox
events, harvest cursors, and gold packs are NOT protected by the push* — knowledge captured but not
yet curated can still be lost with the disk (a residual capture→curate window; a separate spool
backup is a later decision). Run `agora curate` before you rely on a fresh `agora sync`.

**Always-on (launchd / systemd).** `agora watch`, `agora web`, and `agora harvest` are plain
foreground processes; [`deploy/`](deploy/) ships example launchd LaunchAgents and systemd user
units that start them at login/boot, restart them when they exit, and capture logs — see
[`deploy/README.md`](deploy/README.md) for the install steps. Two rules baked into the examples:
the web unit binds `127.0.0.1` **only** (the web face has no auth/TLS), and **harvest needs its
own scheduled unit** — the `agora watch` loop evaluates only the curation triggers and never runs
harvest. Sharing one KB with a small team (single hub, reverse proxy, SSH MCP writes, read-only
clones) is covered by [`docs/DEPLOY-TEAM.md`](docs/DEPLOY-TEAM.md) (issue #68).

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
