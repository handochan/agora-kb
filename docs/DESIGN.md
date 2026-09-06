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
  index.md                             the ROOT MAP (kind: index) — exactly one, at the repo root
  log.md                               append-only action log
  raw/                                 Layer 1: immutable sources — NEVER MOVED by schema 2
    <domain>/                          (+ sha256 sidecar for binaries); <domain> is a SHARD KEY only
                                       — no code reads a subject out of it
    _blob/                             content-addressed original bytes (ADR-0041 D1.4/D4.2) — written
                                       by APPLY from inbox attachments since wave W2.5, never by a brain
    _pages/                            RESERVED PREFIX ONLY (ADR-0040, unauthored) — no writer, no
                                       gate exception; `_`-prefixed domain names are rejected (L1-23)
  wiki/                                the FIRST segment under wiki/ IS the note's KIND (ADR-0041 D1)
    concepts/<slug>.md                 kind: concept — durable, atomic concept pages (was type: theme)
    summaries/<slug>.md                kind: summary — SHIPS EMPTY (no producer; ADR-0040/OD-7)
    notes/<yyyy>/<mm>/<yyyy-mm-dd>.md  kind: note — ONE journal per run_date, repo-wide (was daily/)
    maps/<slug>.md                     kind: map — Map-of-Content (was <domain>-moc.md)
    entities/<slug>.md                 kind: entity — SHIPS EMPTY (no day-1 producer; OD-8)
    people/<person>/**.md              kind: person — HUMAN-OWNED; the curator never writes it
  assets/                              binary assets (images), referenced from notes
  _meta/                               repo-init/admin inputs, read-only during INGEST
    taxonomy.yaml                      schema_version, domains, allowed_tags, taxonomy_policy
    kb.yaml                            KB identity: {kb_id, name, declared_kind} — no policy (D1.5)
  _templates/                          note templates for hand editing (schema 2: concept.md, note.md)
  _kb/                                 git-ignored operational area (NOT wiki content)
    inbox/<writer>/                    WRITE path: append-only captures
    processing/<run-id>/               atomically claimed immutable items + run manifest
    processed/<date>/                  consolidated items (audit trail)
    failed/                            terminal failures + separate error records
    harvest/<connector>.json           per-connector harvester cursor (last scan, hash, proposed;
                                       DATA-MODEL §6, ADR-0007)
    index/<repo>.notes.json            derived query READER cache (parsed-note + inverted index);
                                       rebuildable, never canonical (ADR-0012 §2, #26)
    gold/<pack>.md                     derived GOLD context pack (verbatim summary lines, budgeted)
                                       + <pack>.meta.json sidecar; a pure fn of (commit, spec),
                                       rebuildable, never canonical (ADR-0027 §3, #37)
    state.json                         curator state: counters, last_run, published runs
    curator.lock                       flock held during a consolidation run
```

`raw/` + `wiki/` + indexes are the **knowledge** tracked in git. `_kb/` is the engine's git-ignored
operational spool; it is durable on the repo owner's storage but is not canonical knowledge. Its
indexes/state are rebuildable from retained events and git history. Schemas are in
[DATA-MODEL.md](DATA-MODEL.md).

**KB wiki schema 2 — directory is the kind** ([ADR-0041](adr/0041-stratum-kind-first-layout.md),
Proposed; the layout above). The two axes of the v1 layout were inverted: the *path* carried the
subject (`wiki/<domain>/themes/<slug>.md`) while a closed four-value `type:` enum carried the kind.
Schema 2 flips them. The first segment under `wiki/` is the kind and is **authoritative** — a
directory outside the closed set `{concepts, summaries, notes, maps, entities, people}` is a hard
lint reject (L1-22) — while `kind:` in frontmatter is only a mirror of it. The subject moves into a
`subjects:` list whose values must already exist in `_meta/taxonomy.yaml`, and `subjects: []` is a
legal, honest value: a concept lands at `wiki/concepts/<slug>.md` whether or not its subject is
known, so nothing is dropped for lack of a domain. Free sub-folders under a kind are permitted and
no code reads the intermediate segments; the three exceptions are `notes/<yyyy>/<mm>/` (a date shard
composed from the run date), `people/<person>/` (the person namespace) and the kind segment itself.
`raw/` is **never moved** — its `<domain>` survives as a shard key only — which is what keeps every
`sources:` string written under schema 1 resolvable verbatim.

`config.SUPPORTED_KB_SCHEMA_VERSIONS` is `{1, 2}`, and the two versions are **not** symmetric:
`agora repo init` creates schema 2 by default, and on a schema-1 repo reads keep working
(`query`/`status`/`browse`/`doctor`, the MCP read tools, the web read routes — `agora doctor` runs
and reports, though its overall verdict on such a repo is `status: unhealthy`, exit 1, since a KB
that can accept nothing new is not a healthy deployment) while every write path **refuses** — `agora curate`/`watch`/`requeue`/`harvest`, `kb_curate`, and `Inbox.write` itself, so
`kb_remember` and the web upload are covered by one gate. That is a deliberate hardening of the §10
V9 "write-warns" posture for this bump only: writing schema-2 paths and frontmatter into a schema-1
tree would produce a repo that is neither, and the damage would be a commit. There is **no in-place
migrator** (no `agora repo upgrade`, no dual-layout reader); the one sanctioned crossing is a
conversion into a NEW repo, `agora import --from-kb <old-repo> <new-repo>`, which never modifies the
source — see [LIMITATIONS.md](LIMITATIONS.md) §6a for the conversion rules.

**Medallion tiers (vocabulary, ratified 2026-07-05).** In data-platform terms Agora is a three-tier
pipeline: **bronze** = the ingress concept (the append-only `_kb/inbox/` spool + the immutable
`raw/`/`assets/` captures), **silver** = the curated `wiki/` SSOT, **gold** = derived, always-fresh
context packs assembled from the wiki for injection into agents ([ADR-0027](adr/0027-gold-context-packs.md)).
Medallion words are a docs/vocabulary overlay; the code keeps the `inbox`/`raw`/`wiki`/`gold` names.
A gold pack is a **pure, deterministic function of (curated commit, pack spec)** — reader-class code
(`core/gold.py`'s `PackAssembler`) assembles verbatim summary lines from validator-gated
claim-bearing notes (`GOLD_KINDS` — `concept` and `summary`, the schema-2 successors of `type: theme`;
`wiki/people/**` and `derived: true` notes are excluded from every pack)
into a small, token-budgeted, byte-stable slice; no LLM runs, no new curator op, nothing writes
`wiki/`/the inbox/indexes. It is built best-effort in the curator's finalize (never staler than
silver), on the explicit `agora gold build`, and lazily on read. **Consumption is pull-only** and
agent-neutral: the documented file channel is a CLAUDE.md-style include `@<repo>/_kb/gold/default.md`
— the one-line include IS the standing human consent (Agora never writes agent config dirs). Every
emission is wrapped in the normative `<!-- agora:pack … -->` … `<!-- agora:pack:end … -->` sentinel
and the harvester drops that whole span, so an emitted pack round-trips to **zero** harvested facts
(the outbound loop-break contract, ADR-0027 §8). Harvest-origin notes are default-excluded from packs
so gold can never become a prompt-injection amplifier. The Phase-C consumption channels are
**shipped** (#40 agora half): the MCP `kb_context` tool, the `agora://gold/{pack}` resource +
`gold_context` prompt, and web `GET /api/gold/{pack}` — all pull-only wrappers over one read-only
handler (§5.1).
**Source assets are never-lossy:** an original captured file (pdf/pptx/…) is retained under
`raw/`/`assets/` and linked from every note it feeds (`raw_ref`/`sources:`), and the curator's
provenance union keeps that link even when the note's *content* deduplicates — no asset is ever
dropped from the KB (ADR-0020 amend, tracked separately).

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
**No-drop on unclassifiable durable captures (ADR-0022, Accepted; no-loss floor SHIPPED — Phase 3.5).**
A durable, non-gated capture must **not** be silently dropped merely because it matches no existing
`_meta/taxonomy.yaml` domain. The local path used to do exactly that (`ollama_brain.py` cascading
downgrade-to-DROP on a missing valid domain; `plan.py` check-4 TAXONOMY reject of any
`domain ∉ domains`) — that behavior is **superseded** by the policy below:

- **Fallback, not loss (SHIPPED).** An unclassifiable non-gated basename op is routed to the
  deterministic catch-all — the **first declared domain `domains[0]`** (for a default repo, exactly
  the `general` domain `agora repo init` seeds) — instead of `op = "DROP"`, so the fact survives to
  the wiki and stays navigable. The floor lives in `ollama_brain.normalize_plan` step 3 (a pure
  post-process keyed on the injected taxonomy, so the model-independent §4.1 gate is unchanged); a
  gated candidate keeps its gate and can never originate the catch-all. Shipped alongside the
  repo-global `curator.limits.{body_byte_bound,related_k}` / `curator.lint.max_orphans` threshold
  wiring (ADR-0022 step 2). `domains[0]` was chosen over a literal `general` because `general` is
  only the `repo init` default, not a guarantee (`repo init --domain foo,bar` has none).
  **What schema 2 changed here, and what it did not** (ADR-0041 D2.2): the floor no longer decides a
  *path* — a concept lands at `wiki/concepts/<slug>.md` regardless of subject, so nothing can be
  dropped for lack of a domain because nothing needs a domain to have a path. APPLY files a
  disposition carrying no domain with `subjects: []`, which asserts nothing rather than possibly
  asserting a false subject. The catch-all itself is **still in the wire**: `normalize_plan` step 3
  still substitutes `domains[0]` and the §4.1 BASENAME check still rejects a domain-less
  `CREATE_THEME`/`APPEND_DAILY`, because `raw/<domain>/<event_id>.md` still needs a shard directory
  (`raw/` did not move). So the `subjects: []` path exists and is exercised, but no producer reaches
  it end to end — recorded as ADR-0041 **OD-10**, deferred deliberately rather than claimed.
- **Governed new-domain proposal (separate lane).** The same capture **MAY** additionally trigger a
  *governed* domain-creation proposal. Domain creation is a deliberately-closed decision (ADR-0010
  D6 / ADR-0011 §6.1) and stays so: the sandboxed brain may **never** directly widen
  `_meta/taxonomy.yaml`. A new domain is born only via the governed lane — a deterministic worker
  step driven by a closed-vocabulary plan field — gated by `taxonomy_policy` (open = deterministic
  auto-create, solo/MVP; review-only = emit a proposal artifact/PR instead of committing;
  capped:&lt;N&gt; = at most N new domains per run). The inert `taxonomy_policy` + L1-18 machinery is
  what #23 wires; it governs the creation lane only, never plain INGEST. A **hard prerequisite** of
  that change: the same edit that lands worker-applied `CREATE_DOMAIN` must flip the effective
  default to be repo-kind-aware (personal → `open`; team → `review-only`/`capped:1`), since the code
  default is unconditionally `"open"` today (`emit.py` `Taxonomy.taxonomy_policy="open"`, `lint.py`
  `loaded.get("taxonomy_policy","open")`) — so a team repo never auto-mints a domain in-INGEST. L1-18
  is likewise **extended**, not merely activated: today it diffs only `allowed_tags(after) −
  allowed_tags(before)` (`schema/lint.py`, `kb_schema.md`) and never inspects `domains`; the creation
  lane needs a `domains` set-difference branch added. The whole proposal (taxonomy write + lazy MOC +
  theme) is **one atomic worktree diff** published by a single CAS — a failed publish discards the
  worktree, leaving no half-created domain, and the fact falls back to the catch-all floor.

The taxonomy entry shape is planned to widen from a bare string list to a list-**or**-mapping
(`{<domain>: {created, created_by, status: proposed|active, …}}`) so auto-created domains can be
marked provisional/audited and per-domain custom handling (#24) can attach additively. `allowed_tags`
already demonstrates this list-or-mapping pattern (`config.py` `_load_taxonomy`), but `domains` is
**list-only in all three readers today** (`config._load_taxonomy`, `ollama_brain.parse_taxonomy`,
`schema/lint`), so the normalizer is **net-new** in each. The mapping form is additive: it does NOT
bump `schema_version` (L1-17 untouched), the bare list stays valid indefinitely, and no migration
command is needed. See ADR-0022 (Accepted 2026-07-05, #36; this widening is the still-planned
leg on the open card #24 — issue #23 itself is closed, its floor shipped) and DATA-MODEL §3.


## 5. Faces

### 5.1 MCP server (agents)
FastMCP over stdio (local) or Streamable HTTP (team). Tools:

| Tool | Side | Behavior |
|---|---|---|
| `kb_remember(text, target?, domain?, tags?, source?)` | write | append inbox item, return `{id, queued, inbox_depth}` — **non-blocking** |
| `kb_query(question, scope?)` | read | return ordered evidence hits with path/anchor citations; optional later synthesis may use only those hits |
| `kb_read(path)` | read | open one wiki note **or** one cited `raw/` artefact (a `sources:` string): a note answers with raw markdown body + frontmatter + outgoing link basenames; a raw answer is marked `resource: "raw"` + `raw_kind` (`text`\|`blob`) and a blob returns its capture facts only — bytes are never served over MCP (#169 D4/D5); unknown/escaping path → `{error: "not_found"}` (#58, ADR-0012 rider) |
| `kb_neighbors(path, depth?)` | read | the ADR-0021 link ego-graph around a note — nodes (`id` = rel_path, feeds `kb_read`) + directed edges; `depth` server-clamped and echoed (#58) |
| `kb_context(pack?)` | read | the ADR-0027 gold context pack — the built `_kb/gold/<pack>.md` served **byte-identically** (standing context, not retrieval); not-built → `status: "not_built"` + build guidance (#40; a `scopes` param is reserved for federation, ADR-0030) |
| `kb_status(scope?)` | meta | `{inbox_depth, last_consolidation, processed_today, failed}` |
| `kb_curate(target, force?)` | admin | trigger a consolidation run now (also invoked by triggers) |

One MCP registration = every MCP client (Claude Code, Codex, Qwen, Hermes, …) automatically gains
these tools. No per-tool plugin needed. The same registration also exposes the gold pack as an
`agora://gold/{pack}` **resource** and a `gold_context` **prompt** (ADR-0027 decision 7, #40) —
tool, resource, and prompt all wrap the same read-only handler the web `GET /api/gold/{pack}`
serves, and all are pull-only (nothing is auto-injected).

### 5.2 Web app (people)
Implemented (Phase 3 — ADR-0019/0020). `faces/web/app.py` is a **FastAPI** face that is
**API-first**: a first-class JSON API (`GET /api/{status,search,notes,notes/{path},graph,gold/{pack},raw/{path}}`,
`POST /api/upload`) plus a thin **server-rendered HTMX + Jinja2** UI over it (`/`, `/search`,
`/note/{path}`, `/upload`, `/graph`, `/raw/{path}`). Run it with `agora web` (the optional `web` extra). Localhost, no-auth
for now (auth is Phases 4–5). Both layers call the same core read helpers (`Wiki.list_notes` /
`Wiki.get_note`, surfaced as `AgoraHandlers.browse` / `AgoraHandlers.note`) and the same inbox
write path the MCP face uses — the web face never mutates `wiki/` / git / `raw/`, and it never
composes a `raw/` path itself: since #169 wave A it READS `raw/` only through `core.rawstore`'s
validated `RawRef` (the `AgoraHandlers.raw()` seam), which is the sole door into the capture tier.
- **Browse & search** the wiki (read path). Note bodies render via `markdown-it-py` with raw HTML
  disabled (`html=False`, XSS-safe) and intra-wiki `[Title](relative.md)` links rewritten to UI URLs.
- **Upload** text / **one-or-many** files / URLs — drag-and-drop **multi-upload** is supported, and
  **each file is its own independent inbox capture** (fan-out, not a merge): the face runs the
  matching **input extractor** (§2.3) per file to produce markdown + provenance, then writes one
  inbox item per file — exactly the `kb_remember` write path, N times, no new concept, same
  concurrency & access control (ADR-0003). The single-writer curator invariant (ADR-0002) is
  unchanged: a batch is just N appends to per-writer inboxes, never N writers. The endpoint returns
  a **per-file batch receipt** (`{id, queued}` per file, partial-success tolerant, not atomic) and
  each item is `queued` — **eventually consistent**, searchable only after the next curator run.
  Supported input formats are an **extensible, registry-routed set of pure extractors** (ADR-0004)
  behind the lazy optional `ingest` extra (`.txt`/`.md` dependency-free passthroughs; richer formats
  — PDF, docx, url, … — via the pinned dispatcher, §2.3); broadening the accepted extension/MIME set
  (e.g. html/csv/json/epub through the already-pinned dispatcher) is a localized change at that one
  dispatch seam — image/OCR/audio stay deferred behind their own opt-in extra + ADR-0005 license
  vetting. The **curator remains the sole writer of `raw/`** (ADR-0002): it materializes `raw/` from
  each capture body during consolidation; the *face* never writes `raw/`. (ADR-0020.) Staging the
  **original binary** verbatim now happens (Stratum wave W2.5, ADR-0041 D1.4/D4.2): the face
  attaches the bytes to the inbox event (`Inbox.write(..., attachments=[...])`, staged
  content-addressed under `_kb/inbox/<writer>/_attach/` before the event that names them), and APPLY
  materialises `raw/_blob/<ab>/<sha256>.<ext>` plus its `<file>.meta.yaml` sidecar under the
  bytes-mode gate — the brain's bundle carries a text summary and never the bytes. The derived note
  cites the blob in `sources:`. **Serving them back landed with #169 wave A**: `core/rawstore.py` is
  the read primitive (three gates — a textual `raw/` allowlist, containment against `raw_dir`, and a
  symlink-identity refusal), `AgoraHandlers.raw()` is the one shared seam, and the faces over it are
  `GET /raw/{path}` + `GET /api/raw/{path}` (behind `web.features.raw_enabled`, default on),
  `kb_read` on a `sources:` string, and `agora read`. A blob's BYTES leave only through the web
  route, always as `application/octet-stream` + `nosniff` + `attachment` and never as the sidecar's
  uploader-supplied `media_type`; over MCP a blob returns its capture facts and no bytes at all.
  **The citation itself became clickable with #169 wave B**, in the frontmatter rather than the
  body: APPLY stamps a derived `source_links:` mirror of the `raw/` half of `sources:` on concepts
  and summaries (`'[[raw/…]]'` — the one form Obsidian linkifies inside a list property), absent
  rather than empty, re-derived on every write and never authoritative, with lint `L1-25` (a
  warning) reporting any citation `sources:` does not carry. A curator-emitted body footnote was
  **rejected on measurement, not deferred** — CommonMark and Obsidian parse `[^n]:` differently, and
  a body citation moves the `query_lexical` merge oracle — see the ADR-0041 `source_links:`
  addendum. What is still deferred: a size-tier/LFS policy (#48), and image/OCR extraction. Per-batch max-files / total-bytes caps shipped with the
  multi-upload surface (**ADR-0025**, Accepted); the untrusted-input hardening landed as the
  extractor-layer SSRF guard (#66) + zip decompression-bomb cap (#53) — see the ADR-0025 appendix —
  while SVG/HTML XSS stays covered at render time (markdown-it `html=False`).
- **Knowledge graph** (read-only) at `/graph`: a derived, Obsidian-like view of the wiki's
  link/relation structure — global plus a per-note local ego-graph (node click → the note, a
  **subject** filter, contested/orphan accents). It is purely derived from the
  markdown (reuses the §5.3 dashboard helpers — `Wiki.list_notes` + body link basenames + frontmatter
  `related`/`children`), holds no canonical state (invariant 1), and never writes. Rendering is a
  single vendored MIT force-graph lib over a JSON graph endpoint (`GET /api/graph` + the `/graph`
  page) — the extended "single bolt-on charting" precedent, the first firing of the ADR-0019 §7
  escape hatch (a vendored client-side renderer with no Node/SPA toolchain) — **ADR-0021** (Accepted).
  The render layer (route, template, vendored `force-graph.min.js`, per-note local-graph link)
  shipped under ADR-0021. The node/depth caps, upload limits, the accepted-extension allowlist,
  feature toggles, and the opt-in reverse-proxy identity header (#67) are **operator-configurable**
  via the optional `web:` block in (git-ignored, non-canonical — invariant 1) `_kb/repo.yaml` —
  parsed by `load_web_config` and resolved **per-repo in `build_app(repo_path)`** (never a global
  mutable — tenant-safe for Phase 4, invariant 5); the graph caps were its first consumers —
  **ADR-0025** (Accepted, shipped; see DATA-MODEL §3.1(d) for the settled shape).
- Binary assets → `assets/`, referenced from notes (outside the navigation graph).

### 5.3 Dashboard (status, read-only)
Implemented (Phase 3 — part of the web face). A read-only meta face served at `/dashboard` with
`GET /api/dashboard/*` JSON endpoints and HTMX-polling fragments, backed by
`AgoraHandlers.health` / `curator_status` / `harvester_status`. **All data it needs already exists**
in `_kb/state.json`, the live `inbox/` count, `log.md`, git history, `processed/`+`failed/`, and the
harvest cursors. Panels:
- **KB health (per team / per person):** note counts (a TOTAL `by_kind` census over the ADR-0041
  kind vocabulary plus an `unknown` residue bucket, with `concepts`/`journals` lifted out as the two
  headline scalars that replace v1's `themes`/`dailies`), tag distribution, growth, orphan notes vs
  broken links (distinct lint signals — `health` reuses the existing `lint()` path verbatim; the
  orphan population is the claim-bearing kinds only, and `wiki/people/**` is never graded and is
  reported instead as `by_kind['person']`), last consolidation.
- **Curator/agent status:** queue depth, in-flight, throughput, success/fail, active backend+model,
  cost (if a paid backend), and a work-log timeline (what was ingested/merged/dropped).
- **Harvester status:** connectors enabled, last scan per source, candidates proposed/accepted/rejected
  (the curator now wires the accepted/rejected cursor counters at finalize — ADR-0017 §7).

Observability splits in two: operational time-series (queue depth, throughput) via a **Prometheus
exporter** (`faces/web/metrics.py`, `GET /metrics`; `prometheus_client` is the optional `metrics`
extra, returning 503 if absent). The exporter is a cheap read of current state — it **never** runs
`lint()` or a whole-tree scan on scrape; the content/health views stay in the dashboard above.
Grafana remains an external (AGPL) sidecar for graphing. Scope is filtered by access control — a
viewer sees only their teams/personal repos.

## 6. Context harvester (autonomous accumulation — optional)

The mirror of the curator: **read adapters** that periodically scan agent memory *and* work-context
sources and propose candidate knowledge. Originally a *memory* harvester (agent `MEMORY.md` files); the
seam generalizes to any **work-context source** behind the **same triad** — candidate gate +
fail-closed scope lock + provenance — with **no core change** (each source is an ADR-0004 read-adapter).

> **Status (shipped — ADR-0007/0017/0018/0023).** Implemented and CLI-exposed via
> `agora harvest [--dry-run] [--connector NAME]` over a `FileConnector` and a `SessionConnector`.
> Opt-in: disabled unless `harvest.enabled` is set in `_kb/repo.yaml`, with `connectors:` declared in
> `adapters.yaml`; `agora doctor` lists the configured connectors. Opt-in **link-following** follows a
> pointer bullet's `[Title](sibling.md)` to harvest the sibling's content (ADR-0018). The **`session:`
> connector** (agent transcripts → deterministically-distilled candidates, with connector-boundary
> secret redaction before the immutable inbox write) is **shipped** (#25, ADR-0023 Accepted). The API
> connectors (Letta, mem0) remain Phase 5; the remaining work-context classes below
> (`dir:`/`git:`/`mail:`/`chat:`/`calendar:`) are **planned** (#28) under the same (Accepted)
> ADR-0023 taxonomy / OSS-paths / safety envelope.

**Source classes** — all flow through the one triad and map to `source=harvest:<agent>` (no new inbox
`source` enum member; the parametric `harvest:<agent>` form already covers every class):

| Source class | Connector key | OSS path | Status |
|---|---|---|---|
| Agent memory files | `file:<agent>` (`letta:`/`mem0:` API) | `MEMORY.md`-shaped markdown glob | **shipped** (file); Letta/mem0 Phase 5 |
| Local working folders | `dir:<agent>` | filesystem walk | planned (#28, ADR-0023) |
| Git repos | `git:<agent>` | plain git (commit/diff) | planned (#28, ADR-0023) |
| Agent sessions | `session:<agent>` | transcript glob (e.g. `~/.claude/projects/**/*.jsonl`) | **shipped** (#25, ADR-0023) |
| Mail | `mail:<agent>` | IMAP/JMAP (Gmail/Graph optional) | planned (#28, ADR-0023) |
| Chat | `chat:<agent>` | Matrix (Slack/Teams optional) | planned (#28, ADR-0023) |
| Calendar | `calendar:<agent>` | CalDAV (Google/MS optional) | planned (#28, ADR-0023) |

Connector-type key grammar is `<type>:<agent>` — `session:`/`dir:`/`git:`/`mail:`/`chat:`/`calendar:`
join the existing `file:`/`letta:`/`mem0:`. `build_connectors` dispatches on the type prefix and
remains fail-loud on an unknown type; reserving the namespace now avoids a future collision.
`file:claude-code` and `file:hermes` are the examples emitted in the default `adapters.yaml`.

Three safety mechanisms are mandatory for **every** source class (without them this feature poisons
the base):

| Risk | Mitigation |
|---|---|
| **Feedback loop** (KB → agent memory → harvest → KB …) | provenance tags (`harvest:<agent>`) + origin marking; a best-effort, verbatim-only origin-marker skip avoids re-harvesting unchanged KB-originated facts. The candidate gate (below) is the *primary* loop break; the reworded KB→memory→KB loop is a documented residual risk (ADR-0017) |
| **Noise pollution** | harvested items are `kind=candidate` / `confidence=low` → must pass the curator review gate before promotion to `wiki/`; never written directly |
| **Privacy leakage** | scope enforcement: a personal source feeds **only** the personal repo, never a team repo; team harvest requires explicitly-designated team sources; consent-based |

Three additional rules govern the noisier, higher-volume context sources (ADR-0023, Accepted;
rule (a) is live in the shipped `session:` connector, the #28 classes remain planned):

- **(a) Connector-boundary distillation + redaction.** Noisier sources (sessions/mail/chat) are
  reduced by **deterministic, model-free** distillation and **PII/secret redaction** at the connector
  boundary *before* anything reaches the immutable, append-only inbox — never a raw firehose, and
  never retro-scrubbed (invariant #3). ANY PII-bearing source (local OR networked) runs redaction
  before persistence; the high-PII `session:` connector is the **first concrete trigger** for the
  redaction policy (a shared `core/redact.py` is a hard dependency of its merge), not the first
  networked one.
- **(b) Opt-in pre-curation digest.** High-volume sources MAY route through an opt-in pre-curation
  *digest* stage (summarize/cluster into fewer candidate facts) so the per-run bundle caps and the
  curator gate are not overwhelmed; this is a read-side transform only — **the gate still adjudicates**
  keep / merge / drop and the integrity boundary is unchanged.
- **(c) Team/corporate-shared scope deferred.** Any team- or corporate-*shared* source is **scope-
  deferred to Phase 4** (multi-tenancy + auth); fail-closed scope-lock keeps personal sources personal
  until then. Session→**skill-suggestion** write-back (#25) is deferred to its own ADR (**ADR-0026**,
  reserved; opt-in dry-run/staging only, never auto-writes back to an agent).

Effect: Agora becomes the **shared long-term memory of all the user's/team's agents and work
context** — the "memory of memories." (ADR-0007; broadened by the Accepted ADR-0023.)

## 7. Multi-tenancy & access control

**Repo = tenant boundary.** (ADR-0006.) Each repo is an independent git repository + inbox + curator
state + schema. **Team repos** (shared) and **personal repos** (private to one user) are equal.

- A user belongs to multiple teams and owns ≥1 personal repo.
- **Write routing:** `target="team:engineering" | "personal"` selects the repo's inbox (default: personal).
- **Read scope:** `scope=[...]` queries across the repos the caller may read.
- **Roles:** `owner > editor > reader` per repo; optionally a per-subject convenience filter. (The
  path basis that clause once named — a `wiki/<domain>` subtree — no longer exists under schema 2:
  the subject lives in `subjects:` frontmatter, not in a directory. ADR-0036 had already demoted the
  per-domain ACL to a convenience filter; ADR-0041 records that its mechanism is gone too. The repo
  stays the only security boundary.)
- **Isolation is hard:** writes land in that repo's `_kb/inbox/<user>/…`; a repo's curator only ever touches
  that repo's files. Cross-repo leakage is structurally impossible (separate git repos).

Team mode initially uses one **repo-owner process** per repo working copy. Gateways route captures to
that owner; they do not mount or mutate independent clones. Horizontal scale later uses repo-affine
sharding, still with one owner and one curated branch writer per repo.

External editors are read/browse tools by default. Direct edits to `wiki/` are unsupported while a
curator owns the branch; human contributions use `kb_remember`/upload, or a review-mode PR that the
repo owner imports. This preserves the single-writer invariant.

**Personal + team compose at read time, not by merging** (the local read profile + declaration-order
bands = [ADR-0037](adr/0037-local-multi-kb-federation.md), Proposed; outbound team-audience *pack*
composition = [ADR-0030](adr/README.md) — reserved, not yet authored, Phase-4-coupled).
Connecting to a team does not spin up a second Agora or merge repos: personal and team stay separate
git repos, each with its own single-writer curator. A client-side scope profile
(`$AGORA_HOME/profile.yaml`, default `~/.agora/`, outside every repo) lists the repos the caller
reads, and **queries** are *composed* at read time as declaration-order bands (ADR-0037, Proposed) —
so the agent experiences one memory while writes never cross a tenant boundary. Outbound
**gold-pack** composition across an audience remains ADR-0030's and is not part of the local read
profile.

**Deployment topology (V12).** Exactly **one curation home per repo** — the machine or always-on host
that runs its curator/`watch`; every other environment (laptops, cloud sessions, CI) is a *face* that
reaches the repo over the network (MCP / HTTP) or via a fetch-only clone with lazy gold rebuild (safe
because a pack is a pure function of the curated commit). The Phase-4 HTTP inbox API doubles as the
personal multi-device write path.

**Access control, OSS, two tiers:**
1. **Delegate to the git host (Forgejo/Gitea):** repos, teams, roles, and PRs already exist there;
   Agora authorizes via the host's tokens/API → no ACL to reinvent. Best starting point.
2. **OpenFGA (relationship-based / Zanzibar-style):** when fine-grained, per-domain rules are needed
   (`user editor team/repo`, `domain:finance reader-only`).

Transport for teams: **MCP Streamable HTTP** with **OAuth 2.1 + PKCE** (or tokens + Tailscale for
small private teams). AuthN via Keycloak/Authentik/Ory.

> The concrete authn/authz decisions — Phase-4 Forgejo-delegated identity + the repo-permission
> mirror (repo = the **only** security boundary; per-domain ACL demoted to a convenience filter),
> the Phase-5 OAuth 2.1/OpenFGA triggers, and the per-surface rollout — are specified in
> [ADR-0036](adr/0036-authn-authz.md) (**Proposed**, #69); where this section's prose and that ADR
> differ, the ADR governs once Accepted.

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

**~~The gap Agora fills:~~** ~~no mature OSS project combines *markdown wiki × queue + sleep-time
consolidation + MCP × local-LLM × multi-tenant teams + memory harvesting*.~~ **RETRACTED 2026-09-02**
— every clause of that sentence was falsified clause-by-clause by the 2026-08 market survey
(`docs/STRATEGY-2026-08.md` §3, re-issued §14), including *memory harvesting*, which OpenViking ships
as `ingest`. The only clause that survived is *multi-tenant teams*, which Agora has **not** shipped
(`src/agora_kb/auth/__init__.py` is a two-line docstring).

**What Agora actually claims instead — custody, not coverage:** Agora is a **system of record for what
your agents learned**. Every fact enters through an append-only, redacted, provenance-stamped inbox;
exactly one writer edits the wiki; every change is a git commit you can diff and revert — whatever
compiler, graph, or index you bolt on. See `docs/STRATEGY-2026-08.md` §14.4 for the judgment behind
this wording and §14.9 for what it deliberately does not claim.

## 10. Vision extensions & ecosystem posture (Step-0 ratified 2026-07-05)

Ratified in the Step-0 session (issue #36) and recorded here as SSOT; each item is realized by an ADR
in the spine below. The framing: Agora is a **knowledge orchestrator + knowledge platform** — a
medallion pipeline (§3) whose curator is the transform stage, whose harvester connectors are the
extract stage, and whose gold packs are the serving layer that keeps every agent's memory fresh.

**ADR spine.** [0027](adr/0027-gold-context-packs.md) gold packs + the single normative outbound
sentinel/loop-break contract (Accepted). Reserved: **0028** LLM `DISTILL` curator act
(evidence-triggered); **0029** connector ecosystem — the exec "CWP" wire + registration UX + injection
re-consent (evidence-triggered); **0030** federation / team composition + promotion airlock
(Phase-4-coupled); **0031** retention / right-to-delete (a hard prerequisite for `mail:`/`chat:`
connectors). Reserved-exploratory (2026-07, [docs/notes/retrieval-vs-vectordb.md](notes/retrieval-vs-vectordb.md)):
**0032** semantic embedding tier as a strictly-additive tail (would supersede 0012 §11); **0033**
pending-read / read-your-own-write overlay; **0034** bulk map-parallel curation (extends 0024);
**0035** hybrid tri-signal fusion (layers on 0032) — all evidence-triggered, not yet authored.

**Plane law (V7).** Agora is the **memory plane**; a companion execution runtime (e.g. aelix) is the
**execution plane** (task boards, self-improving skills); "hermes" is a flagship **distribution
profile**, not a codebase. Agora never stores mutable task state; an execution runtime never stores
canonical knowledge. Integrations are bridges — the first is `agora-bridge-<runtime>`, *first among
equals, never privileged* (invariant 6).

**Single-registry rule (V8).** `adapters.yaml` is the one registry for brains and connectors. A
runtime's extension marketplace distributes *authoring* artifacts only — never a parallel runtime
registry.

**Schema/versioning rule (V9).** Every vision ADR carries a `schema_version` impact clause
(none / additive / bump); an `agora repo upgrade` command ships before the first feature that adds
curator-visible vocabulary. Posture: new binary on an old repo = read-works / write-warns; old binary
on a new repo = fail-loud.

*As built (issue #98):* the fail-loud half is `config.SUPPORTED_KB_SCHEMA_VERSIONS` +
`guard_repo_schema_version`, asserted at the CLI dispatch (`main`) and both face constructors
(`build_server` / `build_app`). It judges the canonical `_meta/taxonomy.yaml` value (ADR-0010 §5.1);
cross-location drift stays lint `L1-17`'s finding. The loader deliberately never raises, so
`agora doctor` — the one exempt command, alongside `--version` and `repo init` — can still READ a
skewed repo to diagnose it, reporting `schema: repo=<n> supported=[…] (UNSUPPORTED …)` and
`status: unhealthy`.

*The first bump landed (ADR-0041 D6), and it HARDENED the write half rather than implementing it.*
`SUPPORTED_KB_SCHEMA_VERSIONS` is now `{1, 2}`, so `guard_repo_schema_version` — a membership test —
passes for a schema-1 repo, which is exactly the point: reads keep working. Writability is a
**separate, stricter predicate**, `config.assert_writable_kb_schema_version`, which requires equality
with `MAX_SUPPORTED_KB_SCHEMA_VERSION` and raises `ReadOnlySchemaVersionError` (a subclass of
`UnsupportedSchemaVersionError`, so existing handlers keep working) so a caller can tell *"this build
cannot read your repo"* from *"this build will not write your repo"*. It is asserted per WRITE PATH,
not per entry point: `agora curate` / `watch` (per tick) / `requeue` / `harvest` / `repo upgrade` in
`cli.py`, the `kb_curate` MCP handler, and `Inbox.write` itself — one call there covers
`kb_remember`, the web upload, and every future writer. `agora doctor` keeps its diagnostic exemption. So the posture for
this bump is read-works / **write-REFUSES**, not write-warns: a warn assumes the write is merely
suboptimal, and here it would be corrupting. `agora repo upgrade` (#63) exists as a verb but
MIGRATES nothing and is not a prerequisite — with no flag it reports the schema version, and its
`--restamp` leg is an additive maintenance run *within* a schema (it refuses on a schema-1 repo like
every other write path). The crossing is a conversion into a new repo, not an in-place migration.

**Consumption reaches restricted environments (Q3).** Because corporate hosts often block MCP, the
file-`@include` channel is first-class: a per-agent memory-path map (e.g. `CLAUDE.md`,
`.github/copilot-instructions.md`, `.cursor/rules`) receives a sentinel-fenced, refresh-only gold-pack
region written only on the user's explicit `agora link <agent>` (standing consent; never a silent
write into an agent config dir — the reserved-[ADR-0026](adr/README.md) posture holds). Tier-1 target agents: Claude
Code, Codex, Antigravity CLI, OpenCode, GitHub Copilot, Pi, Qwen, aelix — tested integrations, not
hard-coded (invariant 6).

**Ratified defaults.** Gold pack default budget = **2000 tokens** (V3; a reversible config default);
harvest-origin and contested notes are default-excluded from packs. Corporate-source harvesters
(`mail:`/`chat:` against employer tenants) default to a **team/org repo**, not a personal one, and the
OSS no-admin-consent path (`imap:` / local export) is the default route (V5). Gold freshness =
curation cadence (eventual consistency, §2.2); tighter freshness is bought by running the curator more
often, decided after a one-day curator-economics measurement (V4). Bridge repo location/name is
deferred to bridge Phase A (issue #38).
