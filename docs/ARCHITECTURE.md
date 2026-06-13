# Agora — Architecture

Component breakdown, data flow, and deployment topology. Read [DESIGN.md](DESIGN.md) first for the
conceptual model; this doc maps it to modules and runtime processes.

## 1. Module map (`src/agora_kb/`)

```
core/                 The single internal API. Everything else is a face or adapter over this.
  inbox.py            append-only write path: write(target, item) → _kb/inbox/<writer>/<id>.md
  wiki.py             deterministic retrieval: QueryResult with ordered SearchHit citations
  repo.py             Repo/tenant model: resolve repo, git ops (commit, push, PR), layout
  state.py            curator state (_kb/state.json): counters, event keys, published runs
  schema.py           emit/validate a repo's KB schema (AGENTS.md + symlinks, templates)

curator/              Sleep-time consolidation (one worker per repo).
  worker.py           claim → sandboxed worktree → validate → publish → finalize
  backends.py         WRITE adapters: registry of agent "brains" from adapters.yaml (headless CLIs)
  triggers.py         cron / threshold / idle trigger logic
  review.py           candidate gate + direct-commit vs PR (team/review mode)

harvester/            READ adapters: pull from other agents' memory systems → candidates.
  base.py             Connector protocol (scan since cursor → list[Candidate])
  connectors/
    file_connector.py    diff a markdown memory file (Claude/Codex/Hermes) since last hash
    letta_connector.py   Letta memory blocks via API
    mem0_connector.py    mem0 store via API

ingest/               INPUT adapters: uploads → markdown in raw/.
  base.py             Extractor protocol (bytes|url → markdown + metadata)
  extractors/
    url.py               trafilatura / readability
    pdf.py               pdfminer.six
    office.py            markitdown / pandoc (docx, xlsx, pptx)

faces/                The faces over core.
  mcp_server.py       FastMCP: kb_remember / kb_query / kb_status / kb_curate
  web/                FastAPI app: browse, search, upload, dashboard (read-only meta)

auth/                 AuthN/AuthZ.
  identity.py         token / OIDC (Keycloak) verification
  policy.py           AuthZ: Forgejo delegation or OpenFGA; resolve token → repos+roles+domains

cli.py                `agora` entry point: serve, curate, repo init, harvest, doctor
config.py             load config (adapters.yaml, repos, triggers)
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
web upload (file/url/text) → ingest extractor → markdown
  ├─ save verbatim original → raw/<domain>/<date>-<slug>.<ext>          (immutable + sha256)
  └─ core.write(target, item linking the raw source, source=web:<user>)
```

### 3.3 Harvest — pull candidates
```
harvester (scheduled) → connector.scan(since cursor)
  └─ for each new/changed memory fact:
       core.write(target=<scope-bound repo>, item{source=harvest:<agent>, kind=candidate, confidence=low})
```

### 3.4 Consolidate (curator) — single writer
```
trigger (cron/threshold/idle) → curator.worker.run(repo)
  ├─ flock curator.lock (else exit)
  ├─ atomically claim FIFO items → _kb/processing/<run-id>/ + manifest
  ├─ event_key idempotency; content hash equivalence retains/merges provenance
  ├─ candidate gate: harvested/low-confidence items require keep/merge/drop decision (curator/review)
  ├─ create temporary git worktree; run backend in sandbox with no network/credentials
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
web/dashboard → core.meta(scope)   reads: inbox count, state.json, log.md, git history, processed/failed
                                    + Prometheus time-series for trends
                                    (read-only; ACL-scoped)
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
  `_kb/`, git configuration, and hooks are unavailable to the backend (ADR-0008).
- **Rebuildable metadata:** any SQLite/PG index must be reconstructable from the markdown — markdown is
  the source of truth (ADR-0001).
