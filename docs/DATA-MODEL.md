# Agora — Data Model

Concrete schemas for the operational data. Knowledge itself lives as markdown notes governed by the
**KB schema** (the repo's generated `AGENTS.md`); this doc covers the **engine's** structures: inbox
items, repo metadata, curator state, provenance, and the adapter config.

All on-disk formats are plain text (YAML/JSON/markdown) so they diff in git and are inspectable.

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
status: pending | processing | done | failed
confidence: high | medium | low           # low for harvested candidates (gated before promotion)
content_sha256: <hex>                      # idempotency key (dedup identical captures)
raw_ref: raw/ai-tech/2026-06-13-foo.pdf    # optional: link to an immutable source (uploads)
---
<the knowledge text to remember, or an extraction summary of raw_ref>
```

Rules: append-only, never edited or reordered. `status` transitions: `pending → processing →
done|failed`. A `processing` item older than `processing_timeout` is reset to `pending` (crash recovery).

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
  routing:                                  # optional per-task brain routing
    bulk_daily: qwen
    hard_merge: claude
  triggers:
    cron: "0 3 * * *"                       # 03:00 daily
    threshold: 10                           # consolidate when inbox depth ≥ 10
    idle_minutes: 30                        # or after 30 min of no writes with backlog > 0
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
  "seen": { "<content_sha256>": "2026-06-13T03:00:05Z" },
  "in_flight": []
}
```

`seen` is the idempotency index (dedup). It may be pruned/rotated; losing it only risks reprocessing,
never data loss (reprocessing an identical item is a no-op by sha).

## 5. Harvester cursor — `_kb/harvest/<connector>.json`

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

## 6. Provenance & loop prevention

Every wiki note records where its claims came from (`sources:` in note frontmatter, per the KB schema),
and every inbox item carries `source`. The harvester additionally marks origin so KB-originated facts
are **never re-harvested** back into the KB:

- inbox `source=harvest:<agent>` → curator tags the resulting note region with `origin: harvest:<agent>`.
- A connector skips any fact whose origin trace points back to Agora (breaks the KB→memory→KB loop).

## 7. Adapter config — `adapters.yaml`

The pluggable registry binding the three adapter families. Single source for swapping brains, adding
extractors, or enabling connectors.

```yaml
# WRITE adapters — the curator's brain (headless CLI agents). {prompt} is substituted.
backends:
  qwen:    { cmd: "qwen -p {prompt}",                         cwd: "{repo}" }
  claude:  { cmd: "claude -p {prompt} --allowedTools Read,Edit,Bash", cwd: "{repo}" }
  codex:   { cmd: "codex exec {prompt}",                      cwd: "{repo}" }
  gemini:  { cmd: "gemini -p {prompt}",                       cwd: "{repo}" }
  opencode:{ cmd: "opencode run {prompt}",                    cwd: "{repo}" }
  hermes:  { cmd: "hermes chat -q {prompt}",                  cwd: "{repo}" }
default_backend: qwen

# INPUT adapters — upload extractors by mime/scheme.
extractors:
  "text/html":        url        # trafilatura
  "application/pdf":  pdf        # pdfminer.six
  "application/vnd.openxmlformats-officedocument.*": office   # markitdown

# READ adapters — memory harvester connectors.
connectors:
  file:claude-code: { path: "~/.claude/**/MEMORY.md", scope: personal }
  file:hermes:      { path: "~/.hermes/MEMORY.md",    scope: personal }
  # letta:   { api: "...", scope: personal }
  # mem0:    { api: "...", scope: personal }
```

## 8. ID & naming conventions
- **Inbox id:** `YYYY-MM-DDTHH-MM-SS.mmmZ--<6 hex>` — sortable + unique; safe as a filename.
- **Note basenames are globally unique** within a repo (only the root `index.md` is named `index`),
  so `[[basename]]` resolves unambiguously in Obsidian/Logseq. Domain MOCs are `<domain>-moc.md`.
- **Tags** are kebab-case and must exist in the repo schema's taxonomy before use (prevents sprawl).
