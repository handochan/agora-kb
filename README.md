# Agora

> A self-hostable, fully open-source, multi-tenant **shared memory hub for AI agents**.
> Plain-markdown knowledge that many agents and people write to, that **organizes itself**,
> and that any MCP-speaking tool can read and contribute to.

> **Beta — read the limits before you rely on it.** Start at
> [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) (prerequisites → your first curated note);
> read [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) (the data-safety contract) before you put
> anything you cannot afford to re-create into a repo.

**Status:** **Phases 1–3.6 shipped** · version **`0.1.0b1`** (pre-release; the `v0.1.0b1` tag is
**not cut yet** — install from `main` and expect it to move; see
[`CHANGELOG.md`](CHANGELOG.md) for what is in it, what is still gating the tag, and the
**known limitations**) — a local markdown KB with capture (`kb_remember`), deterministic
navigation query (`kb_query`), and a scheduled curator (`kb_curate`), exposed over an MCP stdio face
and the `agora` CLI. The curator's brain is **pluggable/swappable via `adapters.yaml`** (a local
Ollama model or any headless CLI agent) with per-act `plan`/`author` routing (ADR-0015/0016), and an
opt-in **harvester** pulls other agents' on-disk memory into gated candidates (`agora harvest`,
ADR-0007). Phase 3 adds the **web face** (`agora web`): URL/PDF/office upload extractors, an API-first
FastAPI server with a server-rendered HTMX UI for browse/search/upload, an interactive **knowledge
graph** (`/graph` + a per-note local graph; vendored MIT force-graph, no Node — ADR-0021), a
read-only **dashboard** (KB health · curator · harvester), and a Prometheus `/metrics` exporter
(ADR-0019/0020/0021). Phases 3.5/3.6 then added the retrieval and deployability floor: Korean
search + no-loss capture, the derived `_kb/index` reader cache and `_kb/gold` context packs,
`kb_read`/`kb_neighbors`/`kb_context`, bounded curator batches, `agora sync` push-only backup, and
launchd/systemd units (ADR-0022/0024/0025/0027). In progress on top of that is **Stratum**
(ADR-0041, Accepted): **KB wiki schema 2**, where the first directory under `wiki/` is the note's
*kind* and its *topic* moves into `subjects:` frontmatter — `agora repo init` now creates schema 2,
and a repo on the old schema stays readable but refuses every write
([`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) §6a). Phases 4–5
(multi-tenant, auth, governance) are still ahead — **this release has no authentication**; see
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
- **Yours.** Plain markdown on disk, versioned in git, self-hosted. No lock-in, and nothing is
  uploaded by default — the default brain is a local model and `agora sync` only pushes to a backup
  remote *you* configure. (Routing the brain to a hosted CLI agent is supported and does send KB
  content to that vendor; it is an explicit opt-in — [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) §9.)
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
  with role- and (optionally) subject-level access control.
- **Directory is the kind; frontmatter carries the subject.** A repo is a plain folder you can open
  in Obsidian:

  ```
  <repo>/
    index.md                             the root map
    wiki/concepts/<slug>.md              durable, atomic concept pages
        summaries/<slug>.md              (ships empty — no producer yet)
        notes/<yyyy>/<mm>/<date>.md      one dated journal per curator run date
        maps/<slug>.md                   maps of content, one per subject
        entities/<slug>.md               (ships empty — no producer yet)
        people/<person>/**.md            yours — the curator never writes here
    raw/<domain>/…                       immutable captured sources (unchanged, never moved)
    _meta/{taxonomy.yaml,kb.yaml}        the closed vocabulary + this KB's identity
    _kb/                                 git-ignored engine spool (inbox, state, caches, packs)
  ```

  Every note carries `kind:` (a mirror of its directory, which wins if they disagree) and
  `subjects:` (0..n topics from the declared taxonomy — an empty list is legal, so nothing is ever
  dropped for lack of a topic). ADR-0041.

## Quickstart (local, no cloud, no auth)

> **New here? Read [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) instead.** It is this block
> with the prerequisites spelled out, the model's real disk/RAM cost measured, and every step's
> output pasted from a real run. The block below assumes you already have a curator brain.

Runs entirely on your machine: a local markdown repo, a local model (Qwen via Ollama by
default), zero cloud, zero auth. Requires **Python 3.12+**, [`uv`](https://docs.astral.sh/uv/),
**`git`**, and **a curator brain** — `agora curate` does not think for itself, so an LLM is a
required runtime dependency, not an optional enhancement. That is either a local Ollama model
(~23 GB on disk, ~29 GB resident — the default, fully offline) or any headless CLI agent already on
your PATH (`claude`/`codex`/`gemini`, no download, but **hosted** — your KB content leaves the
machine on every run). Both paths, with the numbers, are
[`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) §1.2.

**There is no PyPI release — a source checkout is the only install path.** `pip install agora-kb`
resolves to nothing (the distribution name is unclaimed; the unrelated `agora` on PyPI is someone
else's package), so every command below runs out of the clone through `uv run`. Reserving the name
is [#102](https://github.com/handochan/agora-kb/issues/102).

**Supported platforms.** macOS and Linux are the developed and tested targets — the two matrix legs
CI runs *without* `continue-on-error` (`.github/workflows/ci.yml:33,40`; note that nothing gates a
merge today — branch protection is not enabled, [`SECURITY.md`](SECURITY.md) §4.7) — and the
curator's OS sandbox exists only for them (Apple `sandbox-exec` on macOS, `bubblewrap` on Linux —
ADR-0013, `src/agora_kb/curator/isolation/`; on Linux `bwrap` is a separate
`apt-get install bubblewrap`, see [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) §1.1).
**Native Windows does not run at all** — no command works, `agora --help` included.
`src/agora_kb/curator/claim.py:30` imports `fcntl` at module scope and the CLI imports that module
at `src/agora_kb/cli.py:86`, so the CLI cannot be imported there in the first place. What a Windows
user gets is a refusal rather than a crash: the `agora` console script points at a stdlib-only shim
(`src/agora_kb/_entry.py`) that checks `sys.platform` *before* that import and writes three lines to
stderr — what is supported, that WSL2 may work but is unverified, and where the port is tracked —
then exits 2 ([#103](https://github.com/handochan/agora-kb/issues/103)). The shim is a stopgap and
is deleted the day the import blocker [#86](https://github.com/handochan/agora-kb/issues/86) lands.
`windows-latest` runs in CI as `continue-on-error` for exactly that reason — a progress signal, not
a gate — and deleting that line is what marks the port done (`.github/workflows/ci.yml:33`). The
port is epic [#85](https://github.com/handochan/agora-kb/issues/85). Windows deployment units and an
interim WSL2 path are not documented — [#92](https://github.com/handochan/agora-kb/issues/92).

```bash
# 0. Get the source (no PyPI release — the clone is the install)
git clone https://github.com/handochan/agora-kb.git
cd agora-kb

# 1. Install the project  (bare `uv sync` installs NO extras and PRUNES any already there —
#    add --extra web --extra ingest --extra metrics --extra dev if you want them)
uv sync

# 1.5 You need a curator brain before step 3 will pass. Either:
#       ollama pull qwen3.6:35b-a3b        # local, offline, ~23 GB — the default wiring
#     or wire a CLI agent you already have (hosted; see docs/GETTING-STARTED.md §1.2 Path B).
#     On Linux also: sudo apt-get install -y bubblewrap  (only needed for `network: none` backends)

# 2. Create a knowledge repo  (note: it is `agora repo init`, NOT `agora init`)
uv run agora repo init ~/my-kb --name my-kb --domain general

# 3. Health check — git, deps, the curator OS-sandbox self-test (ADR-0013), and a brain
#    reachability probe. Exits 1 if no brain is configured or reachable (#96).
uv run agora doctor --repo ~/my-kb

# 4. (optional) Import an existing Obsidian/markdown vault — non-destructive: the source is
#    only READ, and a normalized schema-2 Agora repo is written to the destination. A
#    destination that is ALREADY a knowledge base is refused, of either layout: the old one so
#    two layouts never end up in one tree, the current one because the importer mints a fresh
#    kb_id and rebuilds the root map. To ADD to a KB you have, capture through the inbox.
uv run agora import ~/existing-vault ~/imported-kb --domain general

# 4b. (only if you have a KB from an earlier Agora release) Convert it ONCE into a new repo.
#     The old wiki layout is read-only to this build and there is no in-place upgrade; the
#     source repo is never modified. See docs/LIMITATIONS.md §6a.
uv run agora import --from-kb ~/old-kb ~/converted-kb

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
it local; what that posture does and does not defend against, and how to report a hole privately,
is [`SECURITY.md`](SECURITY.md). A loopback bind is a *network* boundary, and a browser walks
through it, so the face also
defends the browser-mediated paths (issue #94, ADR-0025 appendix): a **Host allowlist**
(`web.security.allowed_hosts`, loopback-only by default) rejects DNS-rebinding reads with 400, an
**Origin/Referer guard** rejects any upload whose origin is not the deployment's own host:port with
403 before anything reaches the inbox (a request with no `Origin` — `curl`, CI — still passes unless
you set `web.security.require_origin: true`), and every response **denies framing**. Behind a
reverse proxy, pass the client's `Host` through verbatim (`proxy_set_header Host $http_host`) and
add your public hostname to `allowed_hosts` — an explicit list replaces the loopback default rather
than extending it. The upload surface is hardened for the day you do share it (the team-deployment guide,
[`docs/DEPLOY-TEAM.md`](docs/DEPLOY-TEAM.md)): server-side URL fetches are SSRF-guarded (private/loopback/link-local/metadata targets
and redirects into them are refused) and can be disabled outright with `web.upload.url_enabled:
false`, and zip-based uploads (docx/xlsx/pptx/epub) are capped against decompression bombs via
`web.upload.max_uncompressed_bytes` (ADR-0025 appendix). Behind an **authenticating reverse proxy**,
set `web.identity.trusted_header` (e.g. `X-Remote-User`) so each teammate's uploads stamp their own
`web:<user>` provenance instead of one shared `web:local` — opt-in, header forced/stripped by the
proxy only (issue #67; snippets in `deploy/README.md`).

**Gold context packs (ADR-0027).** Above the searchable wiki, Agora assembles a **gold** tier: a
small, token-budgeted, byte-stable slice of your highest-value concept notes, meant to be injected at
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
from packs — gold can never become a prompt-injection amplifier (ADR-0027 §8). Two further
exclusions came with schema 2: your human-owned `wiki/people/**` notes and any note marked
`derived: true` never leave through a pack. Note the scope — that is the *push* surface;
`kb_query`/`kb_read` will still return a people note to an agent that asks
([`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) §6b). Agents without
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
triggered it (the result surfaces on the `agora doctor` backup line). **Coverage limits:** `_kb/` is git-ignored, so *nothing under it is
protected by the push* — uncurated inbox events, harvest cursors, gold packs, **`_kb/failed/`
(terminal events awaiting `agora requeue`), the curator's `_kb/state.json`, and your `_kb/repo.yaml`
policy**. Knowledge captured but not yet curated can still be lost with the disk (a residual
capture→curate window; a separate spool backup is a later decision). Run `agora curate` before you
rely on a fresh `agora sync`, and read the full twelve-entry table in
[`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) §1.

**Always-on (launchd / systemd).** `agora watch`, `agora web`, and `agora harvest` are plain
foreground processes; [`deploy/`](deploy/) ships example launchd LaunchAgents and systemd user
units that start them at login/boot, restart them when they exit, and capture logs — see
[`deploy/README.md`](deploy/README.md) for the install steps. Two rules baked into the examples:
the web unit binds `127.0.0.1` **only** (the web face has no auth/TLS), and **harvest needs its
own scheduled unit** — the `agora watch` loop evaluates only the curation triggers and never runs
harvest. If an unattended loop ever curates against a brain that has stopped answering, three
consecutive failures move those captures to `_kb/failed/` — `agora requeue` moves them back, by
rename, once you have fixed the cause ([`deploy/README.md`](deploy/README.md) → "Recovering terminal
failures", the procedure's SSOT; the retry-budget trap and the `--reset-attempts` cost are worked
through in [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) §7). Sharing one KB with a small team
(single hub, reverse proxy, SSH MCP writes, read-only clones) is covered by
[`docs/DEPLOY-TEAM.md`](docs/DEPLOY-TEAM.md) (issue #68; written in Korean).

## Documentation

| Doc | What |
|---|---|
| [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) | The first 30 minutes: prerequisites → first curated note |
| [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) | The data-safety contract — what this beta does *not* protect |
| [`docs/DESIGN.md`](docs/DESIGN.md) | Full design — core, tenancy, harvester, web/dashboard, OSS BOM |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, data flow, deployment topology |
| [`docs/DATA-MODEL.md`](docs/DATA-MODEL.md) | Concrete schemas: inbox item, repo meta, state, provenance |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Phased path: personal MVP → harvester → team server |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records (the *why* behind each choice) |
| [`docs/DEPLOY-TEAM.md`](docs/DEPLOY-TEAM.md) | Sharing one KB with 2–10 people: hub topology, proxy auth, footguns (**Korean**) |
| [`docs/STRATEGY-2026-08.md`](docs/STRATEGY-2026-08.md) | Scope review: what is defensible vs commoditized, the verified defects, roadmap verdicts (**Korean**) |
| [`deploy/README.md`](deploy/README.md) | Always-on packaging — launchd / systemd units + install steps |
| [`SECURITY.md`](SECURITY.md) | Threat model, supported scope, private vulnerability reporting |

## License

Apache-2.0 (see [`LICENSE`](LICENSE)) — permissive, to maximize adoption.
