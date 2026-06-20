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
  routing:                                  # optional per-task brain routing + PRE-PLAN signals
    bulk_daily: qwen
    hard_merge: claude
    ambiguity_band: { low: 0.35, high: 0.70 } # PRE-PLAN: route by related/ top-hit score band
    top2_delta: 0.10                        # PRE-PLAN: route to a stronger brain when top-2 scores are close
    contradiction_regex:                    # PRE-PLAN: signals that may indicate a contested merge
      - "(?i)\\bbut\\b|\\bhowever\\b|\\bcontrary\\b"
  limits:                                   # deterministic caps that bound the local context window (ADR-0011 §1.3)
    body_byte_bound: 8192                   # max bytes a PASS-2 sentinel body region may write
    related_k: 8                            # top-k existing notes pre-fetched per candidate
    related_excerpt_bytes: 512              # per-hit excerpt budget (deterministic head-truncation)
    max_candidates_per_run: 32              # FIFO claim cap so a run never overflows context
    max_augmentation_regions: 4             # max MERGE_INTO_THEME augmentation sub-regions per run
  lint:
    max_orphans: 0                          # lint fails if derived orphan count exceeds this bound
  max_attempts: 3                           # per-event retry budget before move to failed/ (ADR-0011 §5.1)
  triggers:
    cron: "0 3 * * *"                       # 03:00 daily
    threshold: 10                           # consolidate when inbox depth ≥ 10
    idle_minutes: 30                        # or after 30 min of no writes with backlog > 0
health:                                     # derived-health thresholds (orphan/stale computed at read time, C1)
  stale_days: 90                            # a note whose updated is older than this is DERIVED stale
  stub_max_runs: 5                          # a stub un-promoted after this many runs is flagged
  max_note_bytes: 262144                    # soft cap on a single note's size
harvest:
  enabled: true
  scope_lock: personal                      # personal sources may only feed a personal repo
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
`_kb/index/` is a recognized read-side query cache (ADR-0012): it is the reader's rebuildable index, always
reconstructable from the markdown at the curated commit, and is never canonical (the sandboxed curator
backend does not write it).

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
/ `last_scan` / `last_content_sha256` / `proposed`; the curator owns `accepted` / `rejected` from
plan dispositions at finalize (ADR-0011) — currently **deferred**, so those two stay `0` and
round-trip untouched until that wiring lands. The connector name is sanitized to a safe filename
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
