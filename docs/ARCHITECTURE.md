# Agora — Architecture

Component breakdown, data flow, and deployment topology. Read [DESIGN.md](DESIGN.md) first for the
conceptual model; this doc maps it to modules and runtime processes.

## 1. Module map (`src/agora_kb/`)

> **Phases 1–3 ship:** `core/`, `curator/` (incl. `isolation/` + the `backends` registry with per-act
> routing), `adapters/` (the curator-brain shims), `harvester/` (file connectors), `ingest/vault_import.py`
> + `ingest/extractors/` (url/pdf/office), `faces/mcp_server.py`, `faces/web/` (FastAPI JSON API + HTMX UI,
> dashboard, `/metrics`), `schema/`, `cli.py`, `config.py`. Only the `auth/` package below remains a
> later-phase (Phase 4–5) stub, shown here for the full target map.

```
core/                 The single internal API. Everything else is a face or adapter over this.
  inbox.py            append-only write path: write(target, item) → _kb/inbox/<writer>/<id>.md
  wiki.py             deterministic retrieval: QueryResult with ordered SearchHit citations
  gold.py             GOLD context packs (ADR-0027, #37): deterministic PackAssembler — verbatim
                      summary lines from validator-gated theme notes → _kb/gold/<pack>.md + meta
                      sidecar; a pure fn of (curated commit, spec); commit-anchored gold-score +
                      CJK-aware estimator + §8 outbound sentinel wrap/neutralization + loop shingle
  repo.py             Repo/tenant model: resolve repo, git ops (commit, push, PR), layout
  state.py            curator state (_kb/state.json): counters, event keys, published runs

curator/              Sleep-time consolidation (one worker per repo).
  worker.py           claim → sandboxed worktree → validate → publish → finalize (run loop)
  claim.py            atomic FIFO claim of inbox items → _kb/processing/<run-id>/ + manifest
  manifest.py         per-run manifest (unpublished/published/finalized state for recovery)
  plan.py             candidate gate + consolidation plan (keep/merge/drop, schema-validated)
  apply.py            apply the validated plan to the worktree; allowlisted-diff enforcement
  bundle.py           assemble the curated change set for publish
  backends.py         WRITE-adapter registry: agent "brains" from adapters.yaml + per-act
                      `plan`/`author` routing (ADR-0015), no-shell argv invocation primitive
  subprocess_backend.py  generic no-shell brain runner (SubprocessBackend) — shells the configured
                      adapters.yaml brain argv over stdin for PASS-1 plan + PASS-2 author; RoutedBackend
                      dispatches each act to its routed brain (ADR-0015)
  isolation/          OS sandbox for the backend (ADR-0013): seatbelt (macOS) · bwrap (Linux)
                      · restricted (fallback) · selftest (`agora doctor`); profiles/
  triggers.py         cron / threshold / idle trigger logic
  cron.py             deterministic cron matcher for `agora watch`
  # Phase 5 (planned, not yet shipped): PR-review mode — curator opens PRs vs direct commit

adapters/             WRITE-adapter brain shims invoked via the adapters.yaml argv.
  ollama_brain.py     `agora-ollama-brain`: default local Qwen via Ollama (two-pass plan+author)
  cli_agent_brain.py  `agora-cli-brain` (ADR-0016): drives ANY headless CLI agent (claude/codex/…)
                      as a pure text generator; the CLI argv follows a `--` separator

harvester/            READ adapters: pull from other agents' memory + working-context sources → gated candidates (ADR-0007/0017/0018/0023).
  connectors.py       Connector protocol + FileConnector (segment a markdown memory file, scan since
                      cursor, opt-in link-following — ADR-0018) + shared path-safe glob resolver
  session_sources.py  SessionReader seam + ClaudeCodeJsonlReader (parse a transcript → flat-role turns; ADR-0023)
  session_connector.py SessionConnector: distill session transcripts (assistant marker reflections),
                      redact secrets at the connector boundary before the hash (ADR-0023, #25)
  harvester.py        orchestrator: fail-closed scope gate (privacy) + §6 cursor (incl. redacted) + write kind=candidate/confidence=low
  # Phase 5 (planned, not yet shipped): letta/mem0 API connectors; dir:/git:/mail:/chat:/calendar: (#28, DATA-MODEL §8)

ingest/               INPUT adapters: uploads → markdown in raw/.
  base.py             Extractor protocol (bytes|url → markdown + metadata)
  extractors/
    url.py               trafilatura / readability
    pdf.py               pdfminer.six
    office.py            markitdown / pandoc (docx, xlsx, pptx)

faces/                The faces over core.
  mcp_server.py       FastMCP: kb_remember / kb_query / kb_read / kb_neighbors / kb_context /
                      kb_status / kb_curate (kb_read/kb_neighbors reuse the note()/graph() read
                      handlers, #58) + the agora://gold/{pack} resource and gold_context prompt —
                      all three gold channels wrap the same gold_pack() handler, serving the built
                      _kb/gold/ pack byte-identically (ADR-0027 Phase C, #40)
  web/                FastAPI web face (ADR-0019): API-first JSON API (GET /api/{status,search,notes,
                      notes/{path},graph,gold/{pack}}, POST /api/upload) + server-rendered
                      HTMX/Jinja2 UI (browse, search, upload, knowledge graph, read-only dashboard);
                      localhost no-auth
    app.py            build_app: the JSON API + HTMX routes; markdown-it-py render (XSS-safe), upload
                      extract→inbox write path (ADR-0020 — curator stays sole raw/ writer); GET /graph
                      + /api/graph + per-note local ego-graph (ADR-0021)
    static/           vendored MIT JS (no Node/CDN): htmx.min.js, force-graph.min.js + graph.js (the
                      knowledge-graph viz, ADR-0021), app.css
    metrics.py        Prometheus /metrics exporter — cheap per-scrape operational metrics (NEVER runs
                      lint on scrape); prometheus_client is the optional `metrics` extra

auth/                 AuthN/AuthZ.
  identity.py         token / OIDC (Keycloak) verification
  policy.py           AuthZ: Forgejo delegation or OpenFGA; resolve token → repos+roles+domains

schema/               KB wiki schema emitted into each knowledge repo.
  emit.py             emit AGENTS.md + symlinks/templates into a repo
  lint.py             validate a repo's KB schema / notes (deterministic L1 rules)
  notes.py            note-type helpers (index/moc/theme/daily) + link resolution
  templates/kb_schema.md   the AGENTS.md schema template

cli.py                `agora` entry point: repo init · import · status · curate · harvest · index · gold · watch · serve · web · doctor
config.py             load config (adapters.yaml backends/routing + connectors, repo.yaml, triggers, harvest policy)
```

Dependency rule: **faces and adapters depend on `core`; `core` depends on nothing above it.**
`core` never imports a specific agent, model, or face. (Enforces ADR-0004 + ADR-0001.)

## 2. Runtime processes

A deployment is a small set of long-lived processes (containers):

| Process | Role | Scale |
|---|---|---|
| **MCP gateway** | serves the MCP face (stdio locally, Streamable HTTP for teams); enforces auth | 1 through Phase 4 |
| **Web/dashboard** | FastAPI app (browse, upload, dashboard) | 1 through Phase 4 |
| **Repo owner** | owns each repo working copy and accepts routed captures | exactly 1 per repo |
| **Curator worker(s)** | one logical curator **per repo**; consolidates that repo's inbox | 1 per repo (serialized) |
| **Harvester** | scheduled scans of configured memory sources → candidates | 1 |
| **git remote** | Forgejo/Gitea — source-of-truth distribution + repo ACL | 1 (or external) |
| **Auth** | Keycloak/Authentik (team mode) | 1 (or external) |
| **Metrics** | Prometheus scrape + Grafana (optional) | optional |

Key constraint: **per-repo curator singleton.** Concurrency safety depends on exactly one writer to a
repo's wiki. Enforced by `curator.lock` (flock) + a single scheduled worker; if scaled horizontally,
repos are sharded so each repo is owned by one repo-owner/worker pair. Gateways never write to
independent clones; they route each capture to the owner of the target repo.

## 3. Data flow

### 3.1 Write (capture) — non-blocking
```
agent/web → core.write(target, item)
  ├─ auth: caller may write target repo?            (auth/policy)
  ├─ resolve repo + writer namespace                (core/repo)
  ├─ accept optional event_key; compute content_sha256
  └─ append _kb/inbox/<writer>/<id>.md              (core/inbox)  ── O(1), returns immediately
```

### 3.2 Upload — capture with extraction
```
web upload (file/url/text) → ingest extractor → markdown          (ADR-0020)
  ├─ provenance-stamp the extracted markdown (extractor, content_sha256, source=web:<user>)
  └─ core.write(target, item)                       (the inbox is the only write path — no face
                                                     writes raw/; the curator alone materializes it)
```
Storing the verbatim original binary in `raw/` (+ the re-ingest-drift sha256 sidecar) is a
curator-side staging step **deferred** past Phase 3 (ADR-0020); Phase 3 carries provenance in the
capture body, not a separate raw/ write.

### 3.3 Harvest — pull candidates
```
harvester (agora harvest) → for each connector (file: memory | session: transcript, ADR-0017/0023):
  ├─ check_scope (HARD pre-write gate: personal source → personal repo only, fail-closed)
  └─ connector.scan(since cursor) → for each new/changed fact (distilled + sentinel-neutralized;
       session: also redacts secrets at the connector boundary BEFORE fact_key — invariant #3):
       core.write(target=<scope-bound repo>, item{source=harvest:<agent>, kind=candidate, confidence=low})
       (bump §6 cursor: proposed; redacted{class} for facts a connector redacted)
```

### 3.4 Consolidate (curator) — single writer
```
trigger (cron/threshold/idle) → curator.worker.run(repo)
  ├─ flock curator.lock (else exit)
  ├─ atomically claim FIFO items → _kb/processing/<run-id>/ + manifest
  ├─ event_key idempotency; content hash equivalence retains/merges provenance
  ├─ candidate gate: harvested/low-confidence items require keep/merge/drop decision (curator/plan)
  ├─ create temp git worktree; run backend in OS sandbox (macOS seatbelt / Linux bwrap / restricted
  │   fallback — ADR-0013) with no network/credentials (default Ollama brain: inference runs OUTSIDE
  │   the sandbox, env-scrubbed; the file-writing step is confined when network:none)
  ├─ validate allowlisted diff + schema; discard invalid/partial changes
  ├─ commit and compare-and-swap curated branch ref (or publish validated commit as PR)
  └─ finalize processed/failed; update state; remove worktree; release lock
```

### 3.5 Query (read)
```
agent/web → core.read(scope, question) -> QueryResult
  ├─ auth: filter to readable repos+domains            (auth/policy)
  ├─ navigate: read <domain>-moc.md → follow [[links]] → grep synonyms   (core/wiki)
  └─ return ordered SearchHit{repo,path,anchor,excerpt,reason,score}
```

Phase 1 `kb_query` renders these evidence hits directly. Optional prose synthesis is a later adapter
and may use only the returned hits; `not_found` is explicit and never synthesized around.

### 3.6 Dashboard (meta)
```
web/dashboard → AgoraHandlers.{health,curator_status,harvester_status,gold_status}  (read-only panels)
   reads: inbox count, state.json, log.md, processed/failed, harvest cursors, deterministic lint(),
   the gold-pack meta sidecar (ADR-0027 medallion panel, #37)
   (HTMX-polled fragments; lint reused VERBATIM for KB-health signals — Phase 3c)

GET /metrics  →  web/metrics.py            Prometheus exporter (separate surface — Phase 3d):
   cheap per-scrape operational counters/gauges; NEVER runs lint on scrape; Grafana stays an
   external (optional, AGPL) sidecar for time-series trends

GET /api/graph → AgoraHandlers.graph(center,depth,domain)   (read-only graph data — ADR-0021):
   nodes = Wiki.list_notes; edges = body_link_basenames + frontmatter related/children; orphan flag
   reuses health()'s derivation. Global /graph page + per-note local ego-graph; the canvas is the
   vendored MIT force-graph (static/graph.js), the first firing of the ADR-0019 §7 per-route viz.
```

## 4. Deployment topology

```
 team agents (MCP-HTTP)          browsers (team/personal, authed)
        │                              │  browse · search · upload · dashboard
        ▼                              ▼
 ┌──────────────────── Agora server (Docker Compose / Podman) ────────────────────┐
 │  [faces]   MCP gateway (FastMCP)   ·   Web+Dashboard (FastAPI)                  │
 │  [core]    write→inbox · read→wiki · meta      (auth + tenancy enforced here)   │
 │  [adapters] ingest extractors · harvester connectors · curator backends        │
 │  [workers]  per-repo curator (singleton)   ·   harvester (scheduled)           │
 │  [auth]    Keycloak/Authentik (OIDC)   ·   OpenFGA / Forgejo (ACL)             │
 │  [storage] curated git refs/worktrees · git-ignored _kb spools · SQLite/PG cache │
 │  [observ.] Prometheus → Grafana (optional)                                     │
 └────────────────────────────────────────────────────────────────────────────────┘
        │ git push/pull (source of truth)            │ read-only static build
        ▼                                            ▼
   Forgejo/Gitea (shared remote, repo ACL, PRs)   Quartz site (team read view)
        │
        ▼  clone
   Obsidian / Logseq (browse/read; contributions use inbox or reviewed PR)
```

**Progression** (same code, more pieces — see [ROADMAP.md](ROADMAP.md)):
1. **Personal:** MCP stdio + filesystem inbox + local-model curator + Obsidian browse/read. No auth/web.
2. **+ Harvester:** file connectors (Claude/Hermes memory), personal-repo scope.
3. **Small team:** MCP-HTTP + Forgejo (repos/roles) + Tailscale + Quartz web + web upload/dashboard.
4. **Full team:** Keycloak + OpenFGA (domain ACL) + PR review mode + API connectors.

## 5. Failure & recovery (operational invariants)
- **Crash mid-consolidation:** the run manifest distinguishes unpublished, published, and finalized
  runs. Unpublished events return unchanged to inbox; published runs finalize without backend replay.
- **Atomicity:** claiming/finalization use same-filesystem `rename`; publication compare-and-swaps the
  curated branch ref. Readers resolve published commits and never inspect a partial backend worktree.
- **Lock:** `curator.lock` (flock) prevents concurrent curators; the write path never touches shared
  wiki files, so captures stay safe even mid-consolidation.
- **Distribution conflicts:** one repo owner advances the curated branch. Human edits arrive as inbox
  events or reviewed PRs; gateways do not push competing working-copy commits.
- **Backend isolation:** the temporary worktree is the only writable mount; network, credentials,
  `_kb/`, git configuration, and hooks are unavailable to the backend (ADR-0008 transaction;
  ADR-0013 OS-sandbox mechanism — macOS seatbelt / Linux bwrap / restricted fallback, self-tested by
  `agora doctor`). The default Qwen-via-Ollama brain runs model inference outside the sandbox
  (env-scrubbed); the confined PASS-2 step applies when `network: none`.
- **Rebuildable metadata:** any SQLite/PG index must be reconstructable from the markdown — markdown is
  the source of truth (ADR-0001).
