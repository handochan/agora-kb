# Agora — Data Model

Concrete schemas for the operational data. Knowledge itself lives as markdown notes governed by the
**KB schema** (the repo's generated `AGENTS.md`); this doc covers the **engine's** structures: inbox
items, repo metadata, curator state, provenance, and the adapter config.

All on-disk formats are plain text (YAML/JSON/markdown). Knowledge paths diff in git; `_kb/` is a
git-ignored, inspectable operational spool whose derived state is rebuildable.

## 1. Inbox item — `_kb/inbox/<writer>/<id>.md`

The unit of capture (the "event" in the append-only log). Markdown with YAML frontmatter:

```yaml
---
id: 2026-06-13T10-22-33.481Z--a1b2c3      # time-sortable ISO + short random ⇒ globally unique, FIFO-ordered
source: claude-code | codex | qwen | gemini | opencode | hermes | web:<user> | harvest:<agent> | manual
writer: dochan                            # who/what captured it (namespacing + provenance)
cwd: /Users/handochan/dev/analytics/psa   # where captured, if applicable (provenance)
target: personal | team:engineering       # which repo this routes to (default: personal)
domain: ai-tech | economy | general       # hint; the curator may reclassify
tags: [hint, ...]                          # optional hints
created: 2026-06-13T10:22:33Z
kind: capture | candidate                    # harvested facts are candidates
confidence: high | medium | low           # low for harvested candidates (gated before promotion)
event_key: <optional caller-scoped key>    # retries with the same writer+key create no new event
content_sha256: <hex>                      # content equivalence; never discards new provenance
raw_ref: raw/ai-tech/2026-06-13-foo.pdf    # optional: link to an immutable source (uploads)
---
<the knowledge text to remember, or an extraction summary of raw_ref>
```

Rules: event contents are immutable and contain no mutable processing status. Lifecycle is represented
by location: `_kb/inbox/` (pending) → `_kb/processing/<run-id>/` (claimed) →
`_kb/processed/<date>/` or `_kb/failed/`. Recovery follows the run manifest in the processing
directory. `event_key` provides delivery idempotency; identical `content_sha256` values are equivalent
content whose distinct sources/writers must still be preserved and merged into provenance.

## 2. Raw source — `raw/<domain>/<date>-<slug>.<ext>` (+ sidecar for binaries)

Immutable original captured by an upload/harvest. Markdown sources carry frontmatter; binaries get a
`<file>.meta.yaml` sidecar:

```yaml
source_url: https://example.com/article    # if applicable
ingested: 2026-06-13
ingested_by: web:dochan
sha256: <hex of the body only>              # re-ingest: skip if unchanged, flag drift if changed
mime: application/pdf
```

## 3. Repo metadata — `_kb/repo.yaml`

Per-repo configuration & identity.

```yaml
name: engineering
kind: team | personal
schema_version: 1
domains: [ai-tech, economy, general]
git_remote: https://forgejo.internal/agora/engineering.git
review_mode: direct | pr                    # curator commits directly, or opens PRs
curator:
  backend: qwen                             # default write-adapter (see adapters.yaml)
  max_attempts: 3                           # per-event retry budget before move to failed/ (ADR-0011 §5.1)
  allow_reduced_isolation: false            # ADR-0013 fail-closed opt-in (see below)
  triggers:
    cron: "0 3 * * *"                       # 03:00 daily
    threshold: 10                           # consolidate when inbox depth ≥ 10
    idle_minutes: 30                        # or after 30 min of no writes with backlog > 0
harvest:                                    # ADR-0007 — opt-in; disabled by default
  enabled: true
  scope_lock: personal                      # personal sources may only feed a personal repo
```

**Per-task brain routing** is configured in `adapters.yaml` (`routing: {plan, author}`, ADR-0015 —
see §8), NOT in `repo.yaml`. The earlier `curator.routing` PRE-PLAN-signal design
(`ambiguity_band`/`top2_delta`/`contradiction_regex`) and per-op keys were **not adopted** in v1
(per-op / per-tier routing reserved as future work).

What `load_repo_config` actually parses today: `name`, `kind`, `domains`/`schema_version` (taxonomy),
and under `curator:` only `backend`, `max_attempts`, `allow_reduced_isolation`, and `triggers`; plus
the `harvest:` block (`enabled`, `scope_lock`). Any other keys (e.g. a forward-looking
`curator.limits` / `curator.lint` / top-level `health` from the ADR-0011 design) are **silently
ignored** — they are not yet wired, so they neither take effect nor break loading.
### 3.1 Forward-looking config shapes (silently-ignored-until-wired)

The following keys are **planned, not yet parsed** — they follow the §3 convention above (unknown
keys load without effect and never break `load_repo_config`) and are recorded here so an implementer
has a fixed target. Each points to the ADR that governs the behavior; none is presented as settled
v1 behavior.

**(a) Bounded-batch claim cap — `curator.limits.max_events_per_run`** (default: unbounded,
back-compat). Caps how many events `claim()` pulls per run (today `claim()`/`_fifo_snapshot` is
whole-inbox, uncapped); the remainder stays in the inbox for the next trigger. This is **intra-repo
pipelining** (smaller, more frequent single-writer runs to bound prompt/context cost), **NOT a
second writer** — the per-repo single-writer CAS+flock remains the throughput ceiling by design
(#27, ADR-0024 *Proposed*; ROADMAP Phase 3.5; formalizes the existing `curator.limits` stub
referenced in `bundle.py`/`apply.py`). It pairs with the already-documented sibling
`curator.limits.max_candidates_per_run` (INGEST-CONTRACT §1.3, default 32) — note BOTH are currently
**documented-but-not-yet-wired** (`load_repo_config` parses no `curator.limits` today), so neither is
enforced pre-implementation; ADR-0024 must reconcile them (event cap slices the FIFO head pre-dedup;
candidate cap bounds the post-dedup bundle).

```yaml
curator:
  limits:
    max_events_per_run: 32          # planned; default unbounded — caps the per-run claim, not a 2nd writer
```

**(b) Per-domain curation override — `curator.domains.<domain>`** (planned, ADR-0022 *Proposed* —
per-domain custom processing, #24). An override block layering onto the **tuning surfaces only**
(the existing deterministic tunables + default-brain selection); ADR-0022 pins that per-domain
config may NEVER alter the closed op vocabulary, the §4.0 allowlist, the fixed taxonomy, or the
§4.1/§4.4 validators — the integrity gate stays domain-agnostic. When wired it is **fail-loud** if
`<domain>` is absent from the fixed taxonomy.

```yaml
curator:
  domains:
    legal:    { body_byte_bound: 8192, max_orphans: 5, related_k: 6, backend: claude }  # planned (ADR-0022)
```

**(c) Structured domains entry** (planned, ADR-0022 *Proposed* — governed domain auto-creation,
#23). `domains` MAY be either the current list of strings (back-compat) OR a mapping carrying
per-domain metadata. `allowed_tags` demonstrates the list-or-mapping PATTERN the loader already
tolerates, but `domains` is currently **list-only in all three readers** (`config._load_taxonomy`
reads it via `_str_list`; `ollama_brain.parse_taxonomy` accepts list/tuple/set; `schema/lint`), so a
normalizing reader that also accepts the mapping form is **net-new work added + tested in each** of
the three — not an existing-tolerance freebie. The mapping is **additive**: it does NOT bump
`schema_version` (L1-17 untouched), `_load_taxonomy` would normalize both forms, and the bare list
stays valid indefinitely, so no migration command is needed for already-dogfooded repos. The mapping
lets an auto-created domain be marked provisional/audited. Domain *creation itself* is a governed lane
(ADR-0022 cross-refs ADR-0010 D6 / ADR-0011 §4.0/§6.1 / ADR-0007 gate): the sandboxed brain still
may never widen `_meta/taxonomy.yaml` directly, and `taxonomy_policy` (`open | review-only |
capped:<N>`) governs whether new domains are committed, proposed, or capped per run.

```yaml
domains:                            # planned superset of the list form (ADR-0022)
  ai-tech: { status: active }
  fintech: { status: proposed, created: 2026-06-24, created_by: curator, source_run_id: 2026-06-24T03-00-00.000Z--7f31ab }
```

**(d) Web-face operator policy — top-level `web:` block** (planned, ADR-0025 *Proposed* — web
config / multi-upload / extensions, #29 — parsed by a future `load_web_config`). repo.yaml is the
established git-ignored operator-policy file (non-canonical operator policy, invariant #1), so the
graph caps, upload limits, and extension allowlist all land here rather than in a parallel
`web.yaml`, and the block is resolved **per-repo** in `build_app(repo_path)` (never a global mutable
— tenant-safe for Phase 4, invariant #5). The knowledge-graph itself is already shipped under
ADR-0021 (*Accepted*, branch `feat/web-knowledge-graph-viz` — `GET /api/graph` + `GET /graph`,
vendored MIT `force-graph.min.js`, per-note ego-graph) and hardcodes its caps
(`MAX_GRAPH_NODES`/`MAX_GRAPH_DEPTH`, `faces/mcp_server.py:59-60`); this `web.graph` block is the
documented config seam that lifts those two constants into operator-local policy (a post-merge
follow-up — ADR-0025 adds the config the graph's caps will consume; it does NOT re-build the graph).

```yaml
web:                                # planned operator-local policy (ADR-0025); silently ignored until wired
  graph:    { max_nodes: 500, max_depth: 2 }   # lifts MAX_GRAPH_NODES/MAX_GRAPH_DEPTH (mcp_server.py:59-60)
  upload:   { max_bytes: 26214400, max_files: 20, allowed_extensions: [.md, .txt, .pdf, .docx, .html] }
  features: { graph_enabled: true }
```

## 4. Curator state — `_kb/state.json`

Mutable engine state. JSON (single small file; rewritten atomically under the lock).

```json
{
  "last_run": "2026-06-13T03:00:12Z",
  "last_commit": "705f4a4",
  "counters": { "ingested": 142, "merged": 38, "dropped": 11, "failed": 2 },
  "event_keys": { "dochan:<event_key>": "<event-id>" },
  "published_runs": { "<run-id>": "<commit-sha>" }
}
```

`event_keys` is a bounded delivery-idempotency cache and may be rebuilt from retained events.
`published_runs` lets recovery finalize a committed run without invoking the backend twice. Separately,
`_kb/index/<repo>.notes.json` (+ an OPTIONAL `_kb/index/<repo>.fts.sqlite` FTS5 prefilter) is the
read-side query cache — **IMPLEMENTED in issue #26** (ADR-0012 §2 as-built): a parsed-note cache with
its meta folded in, keyed on the curated-commit sha + a per-file `source_digest` (a strict refinement
of `content_sha256` over the exact parser input), always reconstructable from the markdown at the
curated commit and never canonical. It is written ONLY by deterministic worker-finalize + `agora index
build` (never the sandboxed curator backend; not in the ADR-0008 INGEST allowlist), and the read path
opens it read-only, falling back to a full pure-Python scan when it is absent/stale/schema-bumped/corrupt.

## 5. Curator run manifest — `_kb/processing/<run-id>/run.json`

```json
{
  "run_id": "2026-06-13T03-00-00.000Z--7f31ab",
  "base_commit": "705f4a4",
  "event_ids": ["2026-06-13T02-40-10.000Z--a1b2c3"],
  "phase": "claimed",
  "prose_complete": false,
  "schema_version": 1,
  "published_commit": null,
  "started": "2026-06-13T03:00:00Z"
}
```

The orchestrator atomically rewrites the manifest under the lock as `claimed → applied → published
→ finalized`, with `prose_complete: bool` distinguishing the two recovery entry points at `applied`.
Event files remain byte-for-byte unchanged throughout the lifecycle. **Recovery:** a crash at `applied`
with `prose_complete=false` re-enters PASS 2 (re-authoring prose, or re-running PASS 1 if the worktree
was dropped); a `published` run is finalized with no backend call (the CAS commit is the durable publish
point).

## 6. Harvester cursor — `_kb/harvest/<connector>.json`

Per-connector position, so each scan only emits new/changed facts.

```json
{
  "connector": "file:claude-code",
  "source_path": "/Users/handochan/.claude/.../MEMORY.md",
  "last_scan": "2026-06-13T02:00:00Z",
  "last_content_sha256": "<hex>",
  "proposed": 24, "accepted": 17, "rejected": 7
}
```

The cursor is a derived, git-ignored performance optimization (rebuildable from git + `processed/`),
never an integrity control: a missing/corrupt cursor loads fresh and the scan re-reads from scratch
(the candidate gate absorbs any re-flood). `last_content_sha256` is the whole-source fast no-op (an
unchanged file emits nothing); pending-delivery idempotency reuses the inbox `event_key`
(ADR-0017 §4). **Counter ownership (ADR-0017 §7):** the harvester writes `connector` / `source_path`
/ `last_scan` / `last_content_sha256` / `proposed`; the curator owns `accepted` / `rejected`, bumped
at finalize from each run's harvested-candidate dispositions (ADR-0011 / ADR-0017 §7 — `accepted` +=
`MERGE_INTO_THEME`/`MARK_CONTESTED`, `rejected` += `DROP`, `NOOP` skipped, per harvested provenance
tuple attributed to its connector). The bump is happy-path-only (mirrors the `state.json` counter
bump, never replayed in recovery) so it is best-effort + rebuildable, never an integrity control;
each writer load-then-saves so neither clobbers the other. The connector name is sanitized to a safe filename
(`file:claude-code` → `file-claude-code.json`, path-traversal-guarded).

## 7. Provenance & loop prevention

Every wiki note records where its claims came from (`sources:` in note frontmatter, per the KB schema),
and every inbox item carries `source`. The harvester additionally marks origin so KB-originated facts
are **never re-harvested** back into the KB:

- inbox `source=harvest:<agent>` → curator tags the resulting note region with `origin: harvest:<agent>`.
- A connector skips any fact whose origin trace points back to Agora (breaks the KB→memory→KB loop).

## 8. Adapter config — `adapters.yaml`

The pluggable registry binding the three adapter families. Single source for swapping brains, adding
extractors, or enabling connectors.

```yaml
# WRITE adapters — argv arrays avoid shell interpolation; execution is sandboxed in {worktree}.
backends:
  qwen:   { argv: ["qwen", "--headless"], cwd: "{worktree}", prompt: stdin, sandbox: strict }
  claude: { argv: ["claude", "--headless"], cwd: "{worktree}", prompt: stdin, sandbox: strict }
  codex:  { argv: ["codex", "exec"], cwd: "{worktree}", prompt: stdin, sandbox: strict }
  hermes: { argv: ["hermes", "chat"], cwd: "{worktree}", prompt: stdin, sandbox: strict }
default_backend: qwen
routing:                 # OPTIONAL per-act routing (ADR-0015); omit → default_backend everywhere
  plan:   qwen           # PASS-1 plan() brain
  author: claude         # PASS-2 author() brain

# INPUT adapters — upload extractors by mime/scheme.
extractors:
  "text/html":        url        # trafilatura
  "application/pdf":  pdf        # pdfminer.six
  "application/vnd.openxmlformats-officedocument.*": office   # markitdown

# READ adapters — memory harvester connectors.
connectors:
  file:claude-code: { path: "~/.claude/**/MEMORY.md", scope: personal, follow_links: true }
  file:hermes:      { path: "~/.hermes/MEMORY.md",    scope: personal }
  # letta:   { api: "...", scope: personal }
  # mem0:    { api: "...", scope: personal }
```

`scope` is the source's privacy class (enforced by the harvester's scope gate, ADR-0007/0017).
`follow_links` (optional, default `false`; ADR-0018) makes a `file:` connector follow a bullet's
`[Title](sibling.md)` link and harvest the sibling's content (frontmatter stripped) instead of the
thin one-line summary — opt-in, one hop, confined to the source file's own directory subtree.

The exact argv is backend/version-specific and validated by the adapter. The registry stores an argv
array rather than a shell command, and prompt data travels over stdin or a read-only file. Backend
adapters receive no shell, network, git credentials, or writable paths outside the temporary worktree.
Deterministic validation remains mandatory even when the backend advertises its own sandbox.

The optional `routing` map (ADR-0015) pins a brain per cognitive act — the closed set `plan`
(PASS-1) and `author` (PASS-2), the only two points a brain is invoked. An omitted act or an absent
block falls back to `default_backend`; routing to an unknown act or an undefined backend is a hard
config error. Routing only chooses *which* brain runs an act, never how its output is validated, so
the deterministic integrity boundary is unchanged (`plan` and `author` may use different brains, even
with different `network` postures).

A backend's `argv` may shell a **brain shim** rather than a raw model: `agora-ollama-brain` drives a
local Ollama model, and `agora-cli-brain` (ADR-0016) drives ANY headless CLI agent as a pure text
generator — the CLI argv follows a `--` separator. Both shims read the bundle and normalize the
output; the agent only generates text (no file tools, no elevated permissions):

```yaml
backends:
  qwen:   { argv: [agora-ollama-brain, --model, "qwen3.6:35b-a3b"], network: loopback }
  claude: { argv: [agora-cli-brain, --, claude, -p],                 network: loopback }
  codex:  { argv: [agora-cli-brain, --, codex, exec, --skip-git-repo-check, --sandbox, read-only], network: loopback }
  gemini: { argv: [agora-cli-brain, --, gemini, -p, ""],             network: loopback }
default_backend: qwen
```

## 9. Query result

```yaml
query: "How is curator concurrency controlled?"
status: ok | not_found
hits:
  - repo: personal
    path: wiki/ai-tech/themes/curator-concurrency.md
    anchor: "curator-concurrency"
    line: 1
    excerpt: "Exactly one curator advances the curated branch..."
    match_reason: linked-theme | heading | lexical
    score: <illustrative>
```

`SearchHit` fields are `{repo, path, anchor, line, excerpt, match_reason, score}`; `match_reason` is one
of `linked-theme | heading | lexical`, and `anchor` MAY be `""` for a pre-heading lexical match (ADR-0012).
`SearchHit` ordering and citation fields are part of the stable core contract. Optional synthesis may
consume this result but may not replace or hide the underlying evidence.

## 10. ID & naming conventions
- **Inbox id:** `YYYY-MM-DDTHH-MM-SS.mmmZ--<6 hex>` — sortable + unique; safe as a filename.
- **Note basenames are globally unique** within a repo (only the root `index.md` is named `index`),
  so `[[basename]]` resolves unambiguously in Obsidian/Logseq. Domain MOCs are `<domain>-moc.md`.
  Note: plan `links[]` carry basenames, but APPLY resolves each to a standard markdown body link
  `[Title](relative.md)` (the git+Obsidian+OKF-native form; ADR-0014 D3); only frontmatter
  `related:`/`children:` remain `[[basename]]`.
- **Tags** are kebab-case and must exist in the repo schema's taxonomy before use (prevents sprawl).

## 11. Curator plan & content hash (PLAN-APPLY-AUTHOR)

The two on-disk artefacts of the INGEST contract (ADR-0011): the PASS-1 `plan.json` (the only thing the
model writes in pass 1) and the canonical `content_sha256` normalization used for tier-2 dedup (§1).

### 11.1 `plan.json` — PASS 1 output (`_agora_scratch/plan.json`, git-ignored)

A closed-vocabulary JSON plan; the backend writes no wiki files in PASS 1. Deterministic APPLY
materializes ALL structure and ALL frontmatter from it (C7): the model DECIDES, the worker MATERIALIZES.

```json
{
  "schema_version": 1,
  "run_id": "2026-06-13T03-00-00.000Z--7f31ab",
  "finished": true,
  "dispositions": [
    { "candidate_id": "c1",
      "event_ids": ["2026-06-13T02-40-10.000Z--a1b2c3"],
      "op": "CREATE_THEME",
      "domain": "ai-tech",
      "basename": "curator-concurrency",
      "title": "Curator concurrency model",
      "summary": "One curator advances the curated branch under a per-repo lock.",
      "status": "active",
      "aliases": [],
      "tags": ["curator","concurrency"],
      "links": ["single-writer-invariant"],
      "needs_prose": true,
      "reason": "New concept; no related note above threshold." },
    { "candidate_id": "c2",
      "event_ids": ["2026-06-13T02-41-00.000Z--d4e5f6","2026-06-13T02-41-09.000Z--999aaa"],
      "op": "MERGE_INTO_THEME",
      "target_basename": "cqrs",
      "summary": "Adds flock detail.",
      "status": "active",
      "links": [],
      "needs_prose": true,
      "reason": "Overlaps related/c2 cqrs; union provenance." },
    { "candidate_id": "c3",
      "event_ids": ["2026-06-13T02-42-00.000Z--beef01"],
      "op": "DROP",
      "target_basename": null,
      "needs_prose": false,
      "reason": "Unsupported gated candidate; default drop." }
  ]
}
```

Disposition fields the model DECIDES: `candidate_id`, `event_ids[]`, `op` ∈ {CREATE_THEME, APPEND_DAILY, MERGE_INTO_THEME, MARK_CONTESTED, DROP, NOOP} (closed vocabulary; ADR-0011 §2),
`domain`, `basename` (the NEW note's basename for `CREATE_THEME`/`APPEND_DAILY`; null otherwise),
`target_basename` (the EXISTING theme note targeted by `MERGE_INTO_THEME`/`MARK_CONTESTED`; null otherwise)
— both null for `DROP`/`NOOP`, `title`, `summary`,
`status` (the C1 enum: `active | stub | contested | deprecated`), `aliases[]`, `tags[]` (each must already
exist in `_meta/taxonomy.yaml`, C5), `links[]` (wikilink basenames; APPLY resolves each to a standard
markdown body link `[Title](relative.md)` — the git+Obsidian+OKF-native form; only frontmatter `related:`/`children:`
remain `[[basename]]` — see ADR-0014 D3), `needs_prose` (whether PASS 2 authors a
body), and `reason`. EXACTLY one disposition per candidate; the union of all `event_ids` equals the manifest
set, each exactly once (the manifest is the sole coverage universe). Contested judgments are NOT expressed by
setting `status: contested` on a normal disposition. They use the dedicated `MARK_CONTESTED` op against an
existing `target_basename`; deterministic APPLY then materializes the `status: contested` frontmatter plus
non-empty `contested_by`/`contested_at` (== run_date) and the templated `> [!contested]` callout (ADR-0011
§2.1, constraint C3). A plain `CREATE_THEME`/`MERGE_INTO_THEME` disposition declaring `status: contested` is
rejected by the STATUS validator (plan.py) — the model never writes frontmatter or callouts itself.

### 11.2 `content_sha256` canonical normalization (tier-2 dedup, §1)

So identical knowledge from different writers/sources collapses to one candidate reproducibly across
implementations, the hash input is canonically normalized:

1. **body text only** — the YAML frontmatter is excluded;
2. **UTF-8, NFC** Unicode normalization;
3. **LF** newlines (CRLF/CR → LF);
4. **trailing whitespace stripped** per line (Unicode whitespace, i.e. Python ``str.rstrip()``);
5. **single trailing newline**.

`content_sha256 = sha256(those bytes)`. Two byte-equivalent bodies therefore always collide; distinct
`{event_id, source, writer, cwd, raw_ref, created}` provenance tuples are still preserved and unioned (§1, §7).
