# Agora — Architecture

Component breakdown, data flow, and deployment topology. Read [DESIGN.md](DESIGN.md) first for the
conceptual model; this doc maps it to modules and runtime processes.

## 1. Module map (`src/agora_kb/`)

```
core/                 The single internal API. Everything else is a face or adapter over this.
  inbox.py            append-only write path: write(target, item) → inbox/<repo>/<writer>/<id>.md
  wiki.py             read path: navigate MOC → [[links]] → grep; returns answers + citations
  repo.py             Repo/tenant model: resolve repo, git ops (commit, push, PR), layout
  state.py            curator state (_kb/state.json): cursors, counters, seen-hashes, last_run
  schema.py           emit/validate a repo's KB schema (AGENTS.md + symlinks, templates)

curator/              Sleep-time consolidation (one worker per repo).
  worker.py           the run loop: lock → snapshot → dedup → delegate → log → move → commit
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
| **MCP gateway** | serves the MCP face (stdio locally, Streamable HTTP for teams); enforces auth | 1..N behind LB |
| **Web/dashboard** | FastAPI app (browse, upload, dashboard) | 1..N |
| **Curator worker(s)** | one logical curator **per repo**; consolidates that repo's inbox | 1 per repo (serialized) |
| **Harvester** | scheduled scans of configured memory sources → candidates | 1 |
| **git remote** | Forgejo/Gitea — source-of-truth distribution + repo ACL | 1 (or external) |
| **Auth** | Keycloak/Authentik (team mode) | 1 (or external) |
| **Metrics** | Prometheus scrape + Grafana (optional) | optional |

Key constraint: **per-repo curator singleton.** Concurrency safety depends on exactly one writer to a
repo's wiki. Enforced by `curator.lock` (flock) + a single scheduled worker; if scaled horizontally,
repos are sharded so each repo is owned by one worker.

## 3. Data flow

### 3.1 Write (capture) — non-blocking
```
agent/web → core.write(target, item)
  ├─ auth: caller may write target repo?            (auth/policy)
  ├─ resolve repo + writer namespace                (core/repo)
  ├─ compute content_sha256                         (idempotency)
  └─ append inbox/<repo>/<writer>/<id>.md           (core/inbox)  ── O(1), returns immediately
```

### 3.2 Upload — capture with extraction
```
web upload (file/url/text) → ingest extractor → markdown
  ├─ save verbatim original → raw/<repo>/<domain>/<date>-<slug>.<ext>   (immutable + sha256)
  └─ core.write(target, item linking the raw source, source=web:<user>)
```

### 3.3 Harvest — pull candidates
```
harvester (scheduled) → connector.scan(since cursor)
  └─ for each new/changed memory fact:
       core.write(target=<scope-bound repo>, item{source=harvest:<agent>, status=candidate, confidence=low})
```

### 3.4 Consolidate (curator) — single writer
```
trigger (cron/threshold/idle) → curator.worker.run(repo)
  ├─ flock curator.lock (else exit)
  ├─ snapshot inbox (FIFO), dedup by sha256
  ├─ candidate gate: harvested/low-confidence items require keep/merge/drop decision (curator/review)
  ├─ delegate INGEST batch → write-adapter (headless agent + model)   ← edits wiki/, MOC, backlinks
  ├─ append log.md ; move processed→processed/ , failed→failed/
  ├─ git commit (or open PR in review mode)
  └─ update state.json ; release lock ; export metrics
```

### 3.5 Query (read)
```
agent/web → core.read(scope, question)
  ├─ auth: filter to readable repos+domains            (auth/policy)
  ├─ navigate: read <domain>-moc.md → follow [[links]] → grep synonyms   (core/wiki)
  └─ answer with citations to note paths (or "not found" — never invent)
```

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
 │  [storage] knowledge repos (git working copies)   ·   SQLite/PG (metadata)     │
 │  [observ.] Prometheus → Grafana (optional)                                     │
 └────────────────────────────────────────────────────────────────────────────────┘
        │ git push/pull (source of truth)            │ read-only static build
        ▼                                            ▼
   Forgejo/Gitea (shared remote, repo ACL, PRs)   Quartz site (team read view)
        │
        ▼  clone
   Obsidian / Logseq (personal power-editing)
```

**Progression** (same code, more pieces — see [ROADMAP.md](ROADMAP.md)):
1. **Personal:** MCP stdio + filesystem inbox + local-model curator + Obsidian. No auth, no web.
2. **+ Harvester:** file connectors (Claude/Hermes memory), personal-repo scope.
3. **Small team:** MCP-HTTP + Forgejo (repos/roles) + Tailscale + Quartz web + web upload/dashboard.
4. **Full team:** Keycloak + OpenFGA (domain ACL) + PR review mode + API connectors.

## 5. Failure & recovery (operational invariants)
- **Crash mid-consolidation:** items not yet moved remain in inbox → reprocessed next run. Items
  marked `processing` past a timeout are reset to `pending`. (Idempotent via `content_sha256`.)
- **Atomicity:** inbox→processed uses same-filesystem `rename` (atomic). One git commit per run is the
  rollback unit.
- **Lock:** `curator.lock` (flock) prevents concurrent curators; the write path never touches shared
  wiki files, so captures stay safe even mid-consolidation.
- **Distribution conflicts:** git pull --rebase before push; disjoint files auto-merge; the curator is
  the only writer so same-file conflicts are rare and resolvable.
- **Rebuildable metadata:** any SQLite/PG index must be reconstructable from the markdown — markdown is
  the source of truth (ADR-0001).
