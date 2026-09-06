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
source: claude-code | ... | manual | agent:<name> | web:<user> | harvest:<agent>   # see the table below
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
attachments:                               # optional: original bytes staged beside this event (below)
  - sha256: <hex of the raw bytes>         # content address; names the staged file AND its raw/_blob/ twin
    ext: pdf                               # one component, [a-z0-9]{1,16}, never `meta` (ADR-0041 D1.4)
    filename: 2026-q3-report.pdf           # DISPLAY only, sanitised; omitted when nothing safe survives
    media_type: application/pdf            # optional, bare `type/subtype`
    bytes: 481920
---
<the knowledge text to remember, or an extraction summary of raw_ref>
```

Rules: event contents are immutable and contain no mutable processing status. Lifecycle is represented
by location: `_kb/inbox/` (pending) → `_kb/processing/<run-id>/` (claimed) →
`_kb/processed/<date>/` or `_kb/failed/<date>/<run-id>/` (terminal). Recovery follows the run manifest
in the processing directory. `event_key` provides delivery idempotency; identical `content_sha256`
values are equivalent content whose distinct sources/writers must still be preserved and merged into
provenance.

**Attachments — the original bytes of a captured artefact** ([ADR-0041](adr/0041-stratum-kind-first-layout.md)
D4.2). A face that extracts a PDF to markdown captures the *text* in the event body; the PDF itself
rides along as an attachment. The bytes are staged **inside the writer's own namespace** at
`_kb/inbox/<writer>/_attach/<sha256>.<ext>` and written **before** the event that names them, so one
capture stays one delivery and an event is never visible citing bytes that are not on disk (the
rejected alternative — a staging area outside the inbox that APPLY reads by `raw_ref` — splits it
into two independently-failing writes). The staged file is content-addressed and immutable, like the
event: several events naming identical bytes share one file, `sha256` is the digest of the **raw
bytes** (never the §11.2 normalised-text `content_sha256`, which would not identify a binary), and
re-staging the same bytes is a no-op. `ext` is the ADR-0041 D1.4 grammar — exactly one component,
`[a-z0-9]{1,16}`, never `meta` — and a filename carrying none usable yields `bin`. Per-file size is
capped at the same bound as a web upload (`web.upload.max_bytes`, default 25 MiB) — the operator's
configured value, read by **both** capture surfaces (INGEST-CONTRACT §0.1 rule 3).

The attachment travels with its event through the spool (`_attach/` beside the event at each stage).
The move is a **rename when this is the last event citing the file**, and a **copy** while a sibling
event left behind still cites it — content-addressing means several events can name one staged file,
and taking it away from an event that still needs it is the one error that is not reversible. The
copy leaves no residue: once the last citing event has moved, the source is released.

APPLY — the sole writer of `raw/` — is what materialises the bytes into `raw/_blob/` (§2), **and it
does so only for a candidate the curator KEEPS.** A `DROP` (or `NOOP`) writes no note, so nothing
cites the artefact and nothing is materialised; the bytes drain to `_kb/processed/<date>/_attach/`
with their event, where they are never pruned but also never committed — `_kb/` is git-ignored, so
`agora sync` does not push them. This is the same rule a free-text capture has always followed (a
DROPped `kb_remember` writes no `raw/<domain>/<event_id>.md`), and it is stated here because handing
over a *file* reads as a stronger promise than typing a sentence into a tool.

The bytes themselves are opaque: they are never scanned, never redacted and never shown to a curator
brain, which sees only the `filename`/`media_type`/`bytes`/`sha256` summary.

**The `source` vocabulary.** `source` records *what kind of thing produced the capture*, and it is
deliberately open at the edges — the engine must never hold a blessed list of agent names
(invariant 6). Three of its forms are parametric, so a new agent, user or team needs no core change:

| form | meaning | notes |
|---|---|---|
| `claude-code` \| `codex` \| `qwen` \| `gemini` \| `opencode` \| `hermes` \| `manual` | the fixed names the engine shipped with | kept for BACK-COMPAT (events already on disk carry them); not the way a new agent is added |
| `agent:<name>` | any agent capturing under its own identity — `agent:aelix`, `agent:copilot` | a first-class `kind=capture`; `<name>` is `[A-Za-z0-9][A-Za-z0-9._-]*` and is stamped VERBATIM into provenance. A bare `aelix` (no prefix) is REJECTED, so a typo can never be silently blessed as a new agent |
| `web:<user>` | the web face, per authenticated/asserted user (#67) | |
| `harvest:<agent>` | the read-side harvester, per connector agent | enters GATED as `kind=candidate` (ADR-0007): the curator may `MERGE_INTO_THEME` / `MARK_CONTESTED` / `DROP` it, never create a note from it |

The distinction that matters is `agent:<name>` vs `harvest:<agent>`: the first is an agent
*asserting* something it wants remembered (a full capture), the second is Agora *pulling* from an
agent's memory or transcripts without being asked (a gated candidate). Both stay attributable.

**Back-edges.** Two transitions return an event to `_kb/inbox/`. Both are a single `os.replace` — the
bytes, the `id` and the frontmatter are preserved, so a back-edge never mints a second event for one
capture (writing a new event through `Inbox.write` would duplicate the knowledge and break the
immutability statement above):

| Back-edge | Who | When |
|---|---|---|
| `_kb/processing/<run-id>/` → `_kb/inbox/<writer>/` | the curator | a run that did not publish, for events still inside the `curator.max_attempts` retry budget (ADR-0011 §5.1), and the §9 crash-recovery path |
| `_kb/failed/<date>/<run-id>/` → `_kb/inbox/<writer>/` | `agora requeue` (#99) | an operator recovering a **terminal** failure, after fixing its cause |

`agora requeue` is the only non-curator process that mutates the spool. It does so under
`curator_lock` and inside the five-clause **spool-custodian rule** (ADR-0002 appendix): rename-only,
destination derived from the event's own frontmatter through the DESIGN §7 guards, never touching
`wiki/` / `raw/` / `log.md` / git / `_kb/state.json`, and non-destructive — an occupied inbox address
is reported and skipped, never overwritten.

**The requeued event's retry budget — and `_kb/requeued/`.** The budget is DERIVED from the
`failed/**/error.json` records that are still retained (ADR-0011 §5.1), never stored on the event, so
requeue's default of leaving those records in place is what gives a requeued event exactly **one**
more run: publish and the capture is saved, fail and it returns to `_kb/failed/` immediately instead
of burning three more ticks. That asymmetry is the loop break — the derived attempt count is strictly
monotone, so requeue↔fail cannot cycle. `agora requeue --reset-attempts` restores the full
`curator.max_attempts` budget by *archiving* the released records to
**`_kb/requeued/<date>/<run-id>/error.json`** — the exact `_kb/failed/` twin, with `<date>` still the
failure date. The archive **must** live outside `_kb/failed/`, because the budget derivation is
`failed_dir.rglob("error.json")` and `rglob` descends into dotted directories: an `_kb/failed/.archive/`
would still be counted and the reset would silently reset nothing. A record is released only once
**none** of the event ids it lists is still terminal, so no event that is still in `_kb/failed/` can
lose its budget; a retained record and the reason it was retained are both printed. The rule sees
only `_kb/failed/`, so it also releases a record whose events are already back in `_kb/inbox/` with
attempts spent — crash residue from an interrupted requeue and an event the curator is mid-retry are
byte-identical on disk, and releasing both is deliberate: the alternative makes crash residue
permanently un-reclaimable, and the error direction is the safe one (an event gets *more* attempts,
never fewer). Every released record is printed on an `archived:` line, and a `--run`/`--event`
selector that matched no events archives nothing at all.

**No disposition deletes an event (#124).** When the curator cannot return an event — the inbox
address is occupied, the frontmatter is unreadable, the `writer`/`id` is unaddressable, or the rename
raises `OSError` — the event is **preserved** on disk instead of being dropped. All three refusal
paths preserve it into the same `_kb/failed/<date>/<run-id>/` directory, byte-for-byte; they differ
only in how the run reports it: the retry-budget path counts it as one terminal `failed` (preserving
*is* a terminal disposition), while the CAS-conflict and crash-recovery paths add a distinct
`preserved` count, emitted only when non-zero so an ordinary run's counters are unchanged. A
preserved event is still counted by `failed_events` and still retrievable with `agora requeue`.

## 2. Raw source — `raw/<domain>/<date>-<slug>.<ext>` (+ sidecar for binaries)

Immutable original captured by an upload/harvest. Binaries get a `<file>.meta.yaml` sidecar; a
markdown source written by Agora carries **no** frontmatter — APPLY writes the event body bytes and
nothing else, so a `raw/<domain>/<event_id>.md` begins with the first byte of the captured text (the
capture facts live on the inbox event, and for a `raw/_blob/` artefact in its sidecar). The five
keys below are the **`raw/<domain>/` binary** sidecar — a
*re-ingest drift* record for a file that has a `source_url` and can be fetched again. The
content-addressed `raw/_blob/` sidecar is a different record with its own closed key set, given
further down ([ADR-0041](adr/0041-stratum-kind-first-layout.md) D1.4 addendum); which shape applies
is decided by which tree the file is in, and neither has ever been written into the other's.

```yaml
source_url: https://example.com/article    # if applicable
ingested: 2026-06-13
ingested_by: web:dochan
sha256: <hex of the body only>              # re-ingest: skip if unchanged, flag drift if changed
mime: application/pdf
```

**`raw/` did NOT move under KB wiki schema 2, and that is load-bearing** ([ADR-0041](adr/0041-stratum-kind-first-layout.md)
D1.4/D3.4). The shapes above are byte-identical to schema 1, so every `sources:` string written under
schema 1 stays resolvable verbatim and lint L1-7/L1-8/L1-8b keep working unmodified. The `<domain>`
segment survives as a **shard key only** — no code reads a subject out of it; the subject lives in
the note's `subjects:` frontmatter. This is also the one place ADR-0022's `domains[0]` catch-all
still applies: `raw/<domain>/<event_id>.md` needs a directory, so domain tokens stay ASCII
kebab-case even though they are no longer wiki path segments — a rule of the ADR and a convention
of every producer, not a gate any layer enforces today (§10).

Two prefixes are **reserved inside `raw/`** and share its namespace with `<domain>`:

| path | status today |
|---|---|
| `raw/_blob/<ab>/<sha256>.<ext>` + `<file>.meta.yaml` | A captured artefact's original bytes — content-addressed (`<ab>` = the first two hex chars of the digest), immutable, and written **only** by the deterministic APPLY pass: admission is membership in the APPLY `raw_writes` set *with matching bytes* (content-addressing is an extra self-check, **never** a substitute for that authorship check — a planted file whose name correctly hashes its own bytes still fails). `RepoLayout.blob_dir` resolves the path. The bytes reach APPLY as an **inbox attachment** (§1), which re-hashes them on the way in; a digest already present under `raw/_blob/` is cited, not rewritten. The sidecar name is the **full filename plus** `.meta.yaml`, which keeps L1-8b ("cite the artefact, not its sidecar") working unmodified, and a note cites the **blob**, never the sidecar. |
| `raw/_pages/` | **Reserved prefix only.** No writer, and the reservation grants no gate exception — a file appearing there fails the final diff like any other unauthored `raw/` path. It exists so the long-document contract (reserved ADR-0040, **unauthored**) can populate it later. |

The `raw/_blob/` sidecar has its **own closed key set** — the CAPTURE facts (which bytes, how many,
from whom, when, under which event), and never the extracted text (that lives in the event body and,
after curation, in the note). It is closed against *additions*; an absent optional is omitted rather
than emitted empty. It is deliberately NOT the five-key `raw/<domain>/` shape above: a captured
artefact has no `source_url` to re-fetch and is immutable by construction, so a drift record would
have nothing to detect drift against ([ADR-0041](adr/0041-stratum-kind-first-layout.md) D1.4
addendum, which supersedes D1.4's *"unchanged"* sentence for `raw/_blob/` only):

```yaml
sha256: <hex of the blob's bytes>           # == the basename: the integrity self-check
ext: pdf
media_type: application/pdf                 # optional; bare `type/subtype`
bytes: 481920
filename: 2026-q3-report.pdf                # optional; DISPLAY only, sanitised
captured_at: 2026-06-13T10:22:33Z
writer: dochan
source: web:dochan
event_id: 2026-06-13T10-22-33.481Z--a1b2c3  # the inbox event that delivered the bytes
```

Because both share one namespace with `raw/<domain>/`, a `_meta/taxonomy.yaml` `domains` entry
beginning with `_` is rejected by lint **L1-23** (schema 2 only), and the plan-side path composer
rejects a leading `_` in a path component independently — two layers over two different inputs,
neither covering the other.

A blob is written only where a note **cites** it, so a candidate the curator DROPs leaves nothing
under `raw/_blob/` (§1). And because a blob's filename *is* the hash of its bytes, git must never
rewrite them: `agora repo init` seeds a `.gitattributes` carrying `raw/_blob/** -text -diff -merge`
and every engine git call pins `core.autocrlf=false`, without which a CRLF, NUL-free
artefact (CSV/TXT/HTML/JSON) would be normalised to LF on commit and stop hashing to its own name.
`agora doctor` reports a repo created before that seed and prints the one-line remedy.

## 2a. Wiki note & KB identity (KB wiki schema 2)

The wiki layout and the note frontmatter are specified by [ADR-0010](adr/0010-kb-wiki-schema.md) as
amended/superseded by [ADR-0041](adr/0041-stratum-kind-first-layout.md), and emitted verbatim into
each repo as its `AGENTS.md`/`SCHEMA.md`. Recorded here only as the shapes the rest of this document
refers to.

**The path is a function of the KIND.** The first segment under `wiki/` *is* the kind and is
authoritative; `kind:` in frontmatter is a mirror of it, and lint hard-rejects a disagreement
(L1-11) or a `wiki/` sub-directory outside the closed set (L1-22).

| kind | path | notes |
|---|---|---|
| `concept` | `wiki/concepts/<slug>.md` | the v1 `type: theme` |
| `summary` | `wiki/summaries/<slug>.md` | **ships empty** — no producer (ADR-0040 unauthored; OD-7) |
| `note` | `wiki/notes/<yyyy>/<mm>/<yyyy>-<mm>-<dd>.md` | the v1 `type: daily`; ONE journal per `run_date`, repo-wide |
| `map` | `wiki/maps/<slug>.md` | the v1 `<domain>-moc.md`; children may be `concept`/`summary`/`map` (L1-24) |
| `entity` | `wiki/entities/<slug>.md` | **ships empty** — no day-1 producer (OD-8) |
| `person` | `wiki/people/<person>/**.md` | HUMAN-owned; the curator never writes it, lint never grades it |
| `index` | `index.md` (repo root) | exactly one; the root *of* the map tier, not a member of it |

Free sub-folders under a kind are legal and no code reads the intermediate segments; the exceptions
are the `notes/<yyyy>/<mm>` date shard (composed from the run date, never parsed out of a
model-supplied basename) and the `people/<person>` namespace.

**Frontmatter common base** every curator-written note carries (UTF-8 / LF / dates `YYYY-MM-DD`):
`title`, `kind`, `kb` (the `_meta/kb.yaml` `kb_id`, so a note copied out still names its origin),
`subjects` (0..n declared taxonomy domains — `[]` is a legal, honest value), `aliases`, `tags`,
`created`, `updated`, `status`, `summary`, `derived` (bool, default `false`) and `provenance`
(`writers:` = authenticated principals, TRUSTED; `agents:` = agent self-declarations, RECORDED and
NEVER trusted). Per-kind additions carry over from v1 unchanged in shape: `concept`/`summary` add
`sources`/`related`/`origin`/`confidence`/`body_status` + the contested triple — plus, since #169
wave B, a **derived** `source_links` (the `raw/` entries of `sources` rendered as `'[[raw/…]]'` so
Obsidian linkifies them; never naming a source `sources` does not carry, absent rather than empty,
re-derived on every write, never the provenance of record, and graded only by L1-25) — `note` adds
`date`/`run_id`/`sources`/`body_status`, `map`/`index` add `children`, `entity` adds
`sources`/`related`. APPLY additionally emits the ADR-0014 D2 OKF mirrors it already emitted —
`description` beside `summary`, `timestamp` beside `updated`, `okf_version` on the root index — plus
a derived `type:` mirroring `kind` (ADR-0041 OD-3): `type:` is retired **as the kind authority**
(nothing reads it on schema 2), not deleted from the emitted bundle.

**`_meta/kb.yaml` — the KB identity, a CLOSED key set, and no policy** (ADR-0041 D1.5):

```yaml
kb_id: 01J8Z...          # ULID, minted ONCE at `agora repo init`, never rewritten; mirrored into
                         # every note's `kb:` frontmatter
name: general            # display name
declared_kind: personal  # ADVISORY ONLY — never an authorisation input
```

Three keys and nothing else; `load_kb_identity` / `write_kb_identity` enforce the set on both sides
and reject a policy key. The *enforcing* `kind` stays in git-ignored `_kb/repo.yaml` (§3), which is
where `load_harvest_policy` reads it — a git-tracked enforcing `kind` would let an upstream author's
declaration unlock a downstream operator's personal-scope connectors. For a KB not created locally,
`kb_id` is a **self-claim**: join/display identity, never authorisation.

## 3. Repo metadata — `_kb/repo.yaml`

Per-repo configuration & identity.

```yaml
name: engineering
kind: team | personal                       # the ENFORCING kind (the advisory one is _meta/kb.yaml)
schema_version: 2                           # MIRROR of _meta/taxonomy.yaml (the canonical location,
                                            # ADR-0010 §5.1); `agora repo init` writes 2 for a new
                                            # repo, 1 stays readable but refuses every write
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
and under `curator:` — `backend`, `max_attempts`, `allow_reduced_isolation`, `triggers`, `language`
(#57), plus the Phase-3.5 tunables `limits.{body_byte_bound, related_k, max_candidates_per_run}` and
`lint.max_orphans` (ADR-0022 step 2 / ADR-0024 OD-3a — see §3.1 (a)/(a′)). The `harvest:` block is
read from the same file by its own loaders (`enabled`/`scope_lock` via `load_harvest_policy`,
`redact` via `load_redact_policy` — separate models on purpose, so the opt-in read-side policy never
couples into the `extra='forbid'` curator config). Unknown / not-yet-wired keys (e.g. the per-domain
`curator.domains.<domain>` block or top-level `health` from the ADR-0011 design) are **silently
ignored** — they neither take effect nor break loading.
### 3.1 Forward-looking config shapes (per-item status: WIRED or planned)

The following keys were designed here ahead of their implementation and follow the §3 convention
above (unknown keys load without effect and never break `load_repo_config`); they are recorded so an
implementer has a fixed target. Status is marked **per item** — a **WIRED** item documents the
settled, parsed shape; a *planned* item points to the ADR that governs the future behavior.

**(a) Bounded-batch claim cap** — RESOLVED by ADR-0024 **OD-3a** (#60, Phase 3.5): the
already-documented `curator.limits.max_candidates_per_run` (INGEST-CONTRACT §1.3, default 32) is now
**WIRED** — `load_repo_config` parses it and `claim()` enforces it at the FIFO claim, counting
DISTINCT tier-2 content groups (candidates, the unit that drives PASS-1 prompt size) and claiming
only the contiguous FIFO head that fits; the remainder stays in the inbox for the next trigger. This
is **intra-repo pipelining** (smaller, more frequent single-writer runs to bound prompt/context
cost), **NOT a second writer** — the per-repo single-writer CAS+flock remains the throughput ceiling
by design (#27/#60, ADR-0024; ROADMAP Phase 3.5). Per OD-3a a sibling pre-dedup
`curator.limits.max_events_per_run` was **NOT introduced** (one cap only; an event-level claim-cost
cap would come back only under ADR-0024 OD-3c's stated interaction if claim cost — not bundle cost —
is ever shown to dominate).

```yaml
curator:
  limits:
    max_candidates_per_run: 32      # wired (#60): FIFO-head claim cap in candidates, not a 2nd writer
```

**(a′) Repo-global curation thresholds — `curator.limits.body_byte_bound` / `curator.limits.related_k`
/ `curator.lint.max_orphans`** (WIRED in **Phase 3.5**, ADR-0022 step 2 — no longer silently ignored).
These promote three previously-hardcoded/inert tunables (ADR-0011 §1.3) to operator config, resolved
**repo-globally**, using the SAME nesting the full DATA-MODEL §3 example already shows:

- `curator.limits.body_byte_bound` (default `8192`) — the `{n_bytes}` body ceiling surfaced to the
  brain (authoritative §4.2 enforcement is unchanged).
- `curator.limits.related_k` (default `8`) — the `wiki.query_lexical(limit=…)` breadth for the
  bundle's related-notes fetch (the model-free oracle the write path is pinned to, #144).
- `curator.lint.max_orphans` (default **absent ⇒ check skipped**, byte-identical to today) — when set,
  `lint` emits **one `warning`-severity** `L2-1` finding if the whole-tree orphan count exceeds
  it; a warning never flips `LintResult.ok`, so it does not break the §4.4 gate or the dashboard.
  The *surface* is unchanged but the *population* moved with the schema: the orphan predicate is now
  the claim-bearing kinds (`concept`/`summary` — `core.layout.CLAIM_BEARING_KINDS`, the same object
  the dashboard's orphan count and the gold pack's eligibility set bind to), with `entity` and
  `person` exempt because both would be orphans by construction.

`L2-6` (stale `body_status`) is the other `warning`-severity finding `lint` emits — unconditional, with
no threshold to configure: one per note whose `body_status: pending` survives over a body with no
unauthored `agora:body` region. Like `L2-1` it never flips `LintResult.ok`, so it is debt rather than a
gate: a repo published by a pre-#119 build keeps curating normally and heals a note's flag whenever a
later run gives that note a `needs_prose` region. Bulk repair of notes the curator never re-touches
belongs to `agora repo upgrade` (#63) — invariant 2 forbids any other component writing `wiki/`.
Since #131 the curator can no longer MINT the shape that made a flag unclearable (an `APPEND_DAILY`
region nothing would ever author). The converse check stays deliberately unasserted because that
shape still exists at rest and `lint` grades the whole worktree: it survives in any pre-#131 daily
whose `APPEND_DAILY` dispositions never flagged `needs_prose` — a plan the reference brain cannot
emit — and in any hand-edited or imported note.
The debt is surfaced as `lint_findings` in `GET /api/dashboard/health` and on the dashboard's Lint stat,
which reads `ok · N warnings` while `lint_ok` stays true. It is deliberately NOT in the curator's
`failed_checks`: that channel carries only the error-severity findings that actually failed the §4.4
gate, so an unbounded warning cannot bury the one line saying why a run failed.

The nesting follows the established ADR-0011 namespaces — `curator.limits.*` for the two size/breadth
knobs, `curator.lint.*` for the orphan (lint) knob. **These are the repo-global bases that the
per-domain block (b) below overrides** — the per-domain block uses *flat* keys **inside** its
`curator.domains.<domain>` map, distinct from this repo-global nesting; do not copy the flat per-domain
shape into `curator.limits:` (mis-nested keys load without effect per §3).

```yaml
curator:
  limits:
    body_byte_bound: 8192   # WIRED (Phase 3.5)
    related_k: 8            # WIRED (Phase 3.5)
  lint:
    max_orphans: 5          # WIRED (Phase 3.5) — opt-in; absent ⇒ orphan check skipped
```

**(b) Per-domain curation override — `curator.domains.<domain>`** (planned, #24 — governed by
ADR-0022, *Accepted* 2026-07-05; per-domain custom processing). An override block layering onto the
**tuning surfaces only** (the existing deterministic tunables + default-brain selection); ADR-0022
pins that per-domain
config may NEVER alter the closed op vocabulary, the §4.0 allowlist, the fixed taxonomy, or the
§4.1/§4.4 validators — the integrity gate stays domain-agnostic. When wired it is **fail-loud** if
`<domain>` is absent from the fixed taxonomy.

```yaml
curator:
  domains:
    legal:    { body_byte_bound: 8192, max_orphans: 5, related_k: 6, backend: claude }  # planned (ADR-0022)
```

**(c) Structured domains entry** (planned, #23/#24 — the governed domain-auto-creation lane of
ADR-0022, *Accepted* 2026-07-05; the #23 no-loss floor + repo-global thresholds (a′) already
shipped). `domains` MAY be either the current list of strings (back-compat) OR a mapping carrying
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

**(d) Web-face operator policy — top-level `web:` block** (**WIRED** — ADR-0025 *Accepted*,
shipped via PR #33 (#29) and extended by the #66/#53/#67 appendix items; parsed by
`load_web_config`). repo.yaml is the established git-ignored operator-policy file (non-canonical
operator policy, invariant #1), so the graph caps, upload limits, and extension allowlist all land
here rather than in a parallel `web.yaml`, and the block is resolved **per-repo** in
`build_app(repo_path)` (never a global mutable — tenant-safe for Phase 4, invariant #5). The
knowledge graph itself shipped under ADR-0021 (*Accepted* — `GET /api/graph` + `GET /graph`,
vendored MIT `force-graph.min.js`, per-note ego-graph, PR #30); `web.graph` is the config seam that
lifted its two previously-hardcoded caps (`MAX_GRAPH_NODES`/`MAX_GRAPH_DEPTH`) into operator-local
policy. One deliberate exception to the tolerant-`.get()` convention: unknown keys under
`web.identity` fail **loud** (a typo'd security opt-in must never silently disable identity
threading — #67).

```yaml
web:                                # WIRED operator-local policy (ADR-0025; load_web_config)
  graph:    { max_nodes: 10000, max_depth: 3 }  # lifted MAX_GRAPH_NODES/MAX_GRAPH_DEPTH (defaults shown)
  upload:   { max_bytes: 26214400, max_files: 50, total_bytes: 209715200,
              max_uncompressed_bytes: 262144000,   # zip decompression-bomb cap (#53)
              url_enabled: true }                  # operator off-switch for the url extractor (#66)
  extensions: { allowed: [.md, .txt, .pdf, .docx, .html] }  # absent ⇒ extractor's built-in set
  features: { graph_enabled: true,
              raw_enabled: true }               # /raw + /api/raw + linked sources: kill switch (#169)
  identity: { trusted_header: X-Remote-User,   # opt-in reverse-proxy identity → web:<user> (#67)
              strip_domain: false }            # fail-loud block — these two are its ONLY accepted keys
```

## 4. Curator state — `_kb/state.json`

Mutable engine state. JSON (single small file; rewritten atomically under the lock).

```json
{
  "last_run": "2026-06-13T03:00:12Z",
  "last_commit": "705f4a4",
  "counters": { "ingested": 142, "merged": 38, "dropped": 11, "failed": 2 },
  "last_batch": { "claimed": 12, "candidates": 8, "cap": 8, "inbox_remaining": 4 },
  "last_attempt": "2026-06-13T04:10:00Z",
  "last_failure": {
    "when": "2026-06-13T04:10:00Z",
    "run_id": "2026-06-13T04-10-00.000Z--3f2a1b",
    "phase": "claimed",
    "reasons": ["TAXONOMY: unknown domain 'not-a-real-domain'"],
    "reasons_total": 1,
    "record_path": "_kb/failed/2026-06-13/2026-06-13T04-10-00.000Z--3f2a1b/error.json"
  },
  "event_keys": { "dochan:<event_key>": "<event-id>" },
  "published_runs": { "<run-id>": "<commit-sha>" }
}
```

`last_batch` (#60, ADR-0024 §3) records the last published run's claim/bundle shape — events
claimed, tier-2 candidates, the `max_candidates_per_run` cap in effect, and the inbox depth left
right after the claim — so the dashboard/`/metrics` can surface batch-size-vs-cap pressure without
re-reading run manifests (absent/`null` in a pre-#60 state.json; the field is additive). Crash
recovery does NOT recompute it: finalizing a run whose happy-path state save never landed CLEARS
`last_batch` back to `null` (the crashed run's shape is unknowable, so recovery clears rather than
mislabels the previous run's shape as the recovered run's — the same best-effort posture as the
un-replayed counters).
`last_attempt` / `last_failure` (#96) are the **failure surface**, and both are additive optionals
(absent/`null` in a pre-#96 state.json). `last_run` means "last successful PUBLISH" — `mark_run`
fires only on the publish path — so a curator that fails every run leaves it at `never` forever,
while a *non-terminal* failure returns its events to `inbox/` (depth unchanged) and bumps no counter
(`counters.failed` counts **terminal** failures only, at retry-budget exhaustion). Without these two
fields nothing an operator can read moves at all.

- **`last_attempt`** is stamped ONCE per run that actually CLAIMED work — success, failure, or
  crash — right after the claim, so it can never be older than `last_run`, and `last_attempt >>
  last_run` is the "the curator is trying and getting nowhere" signal. An idle tick (nothing to
  claim) writes nothing.
- **`last_failure`** records the most recent run that did not publish: `reasons` is a **bounded
  preview** (≤ 5 entries, each already flattened to one line and capped at 400 chars upstream — it
  is never re-clipped here, so `reasons_total`, the FULL count, cannot lie), and `record_path` is
  the repo-relative POSIX path of that run's `error.json`, which keeps the untruncated
  `failed_checks`. Repo-relative so the record survives a repo move and no host layout leaks into a
  repo-scoped file (invariant 5). `phase` is `claimed` for a failure BEFORE apply and `applied`
  after it. Requeue never rewrites this field (it is strictly rename-only and must not clear a
  failure nobody has fixed), so after `agora requeue --reset-attempts` the stored path names a file
  that has moved; the two renderers — `agora status`'s `last_failure:` line and `agora doctor`'s
  `failures:` line — follow it to the `_kb/requeued/` twin, and **only** when the original is gone
  and the twin exists. In every other case the stored string is printed verbatim.
- It is **sticky**: a later successful publish does NOT clear it (it is a historical fact, like
  `counters`). "Is it still broken?" is `CuratorState.failure_is_current` — a derived property,
  never serialized — which is true while `last_failure.when >= last_run` (or nothing has ever
  published). Crash recovery touches neither field (it replays no clock and no counters), and a CAS
  conflict is not a failure: it writes no `error.json` and no `last_failure`.
- Surfaces: `agora status` (`last_attempt:` / `last_failure:` / `failed_events:`), `agora doctor`'s
  `failures:` line, and `agora curate`'s `failed_record:` / `failed_checks:` lines. When a run
  actually left events terminal, `agora curate` (and each `agora watch` tick) adds a
  `failed_requeue: agora requeue --run <run-id>` line and `agora doctor` adds a `requeue:` line —
  the way BACK, gated on `counts["failed"] > 0` so a within-budget failure, whose events are back in
  `inbox/` already, is never sent to requeue (#99).

> **Downgrade note.** A `state.json` written by this version is **not readable by a pre-#96 agora**
> (the loader is deliberately fail-loud rather than dropping unknown keys). The remedy is to
> **re-upgrade agora** — never to delete `_kb/state.json`, which would discard the double-publish
> guard (`published_runs`) and the delivery-idempotency cache (`event_keys`). `_kb/` is git-ignored
> at `repo init`, so such a file cannot propagate to another machine or tenant.

`event_keys` is a bounded delivery-idempotency cache and may be rebuilt from retained events.
`published_runs` lets recovery finalize a committed run without invoking the backend twice. Separately,
`_kb/index/<repo>.notes.json` is the read-side query cache — **IMPLEMENTED in issue #26** (ADR-0012 §2
as-built): a parsed-note cache with its meta folded in, keyed on the curated-commit sha + a per-file
`source_digest` (a strict refinement of `content_sha256` over the exact parser input), always
reconstructable from the markdown at the curated commit and never canonical. Its candidate prefilter is
the exact in-memory inverted index (the §9 FTS5/ripgrep accelerators are deferred to a load-avoiding
reader, issue #28). It is written ONLY by deterministic worker-finalize + `agora index build` (never
the sandboxed curator backend; not in the ADR-0008 INGEST allowlist), and the read path opens it
read-only, falling back to a full pure-Python scan when it is absent/stale/schema-bumped/corrupt.

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
  "proposed": 24, "accepted": 17, "rejected": 7,
  "redacted": { "jwt": 2, "aws_access_key_id": 1 }
}
```

The cursor is a derived, git-ignored performance optimization (rebuildable from git + `processed/`),
never an integrity control: a missing/corrupt cursor loads fresh and the scan re-reads from scratch
(the candidate gate absorbs any re-flood). `last_content_sha256` is the whole-source fast no-op (an
unchanged file emits nothing); pending-delivery idempotency reuses the inbox `event_key`
(ADR-0017 §4). **Per-connector-type cursor semantics (ADR-0023 §8):** `file:` / `dir:` / `session:`
set `last_content_sha256` to the **whole-source hash** (re-read on any byte change; the candidate
gate + `event_key` idempotency absorb the re-flood), and `git:` hashes the concatenated since-cursor
commit payloads. There is **no** per-file-offset or per-commit-SHA cursor field — that would be a §6
schema change requiring its own ADR (ADR-0023 decision 8) and is premature for v1. **Counter ownership (ADR-0017 §7):** the harvester writes `connector` / `source_path`
/ `last_scan` / `last_content_sha256` / `proposed`; the curator owns `accepted` / `rejected`, bumped
at finalize from each run's harvested-candidate dispositions (ADR-0011 / ADR-0017 §7 — `accepted` +=
`MERGE_INTO_THEME`/`MARK_CONTESTED`, `rejected` += `DROP`, `NOOP` skipped, per harvested provenance
tuple attributed to its connector). The bump is happy-path-only (mirrors the `state.json` counter
bump, never replayed in recovery) so it is best-effort + rebuildable, never an integrity control;
each writer load-then-saves so neither clobbers the other. The connector name is sanitized to a safe filename
(`file:claude-code` → `file-claude-code.json`, path-traversal-guarded).

**`redacted` (ADR-0023 addendum §6, landed #25):** a `redacted: {<class>: <count>}` map — facts with
≥1 redaction, per secret/PII class — authorized as a decision-5-mandated observability field (the
one addition beyond the original §6 fields; the ADR redaction mandate is its authorizing ADR). It is
**harvester-owned**, bumped once per class per WRITTEN fact beside `proposed` at the connector
boundary (a deduped/pending fact is not re-counted, so it tracks new content like `proposed`), and
feeds the `agora_harvester_redacted{connector,class}` Prometheus family (dormant until #25 — #39
landed the redactor + the metric shell). The `file:` connector never redacts (its write path stays
byte-identical, #39), so its `redacted` stays `{}` — an honest 0. Metadata only: a class name +
count, never the secret. No `schema_version` impact (git-ignored `_kb/` state only).

## 6a. Gold context pack + meta sidecar — `_kb/gold/<pack>.md` + `_kb/gold/<pack>.meta.json`

A derived, git-ignored **context pack**: a small, token-budgeted, byte-stable slice of the wiki
assembled for injection into agents ([ADR-0027](adr/0027-gold-context-packs.md), #37). v1 ships one
implicit zero-config `default` pack. The pack **body** is a pure function of `(curated commit, pack
spec)` and carries ONLY the `curated_sha` in its header (byte-identical rebuild is a regression-tested
contract — prompt-cache economics depend on stable bytes), so the wall-clock `generated_at`/age and
all provenance live in the sidecar:

```json
{
  "schema_version": 1,
  "pack": "default",
  "curated_sha": "<full hex>",
  "spec_hash": "<hex over budget/estimator/weights/algorithm-version>",
  "generated_at": "2026-07-05T03:00:12Z",
  "estimator": "cjk-v1",
  "note_count": 42,
  "est_tokens": 1873,
  "budget_tokens": 2000,
  "reference_instant": "2026-07-05T02:59:40Z",
  "harvest_derived_share": 0.0,
  "inputs": [{"path": "wiki/…/x.md", "content_sha256": "<hex>", "score": 0.71}]
}
```

Like the harvester cursor (§6) and reader cache (ADR-0012 §2), the pack is **derived, rebuildable,
never an integrity control**: a missing/corrupt sidecar reads as "no pack" and degrades to no
injection (never a crash). The `(curated_sha, spec_hash)` pair is the invalidation key
(fresh iff `curated_sha` == the live curated tip). Selection is a NEW deterministic gold-score
(structural centrality + commit-anchored recency + status/confidence bucket + provenance density);
harvest-origin notes are **default-excluded** (anti-injection). `reference_instant` records the
curated commit's committer timestamp that anchored recency decay (determinism audit). The
curator alone rebuilds it in finalize (best-effort, happy-path only, swallow+log — never perturbs a
publish); faces gain their first non-inbox `_kb/` write here (the lazy read rebuild), bounded to
these derived bytes. Every emitted pack is wrapped in the normative `<!-- agora:pack … -->` …
`<!-- agora:pack:end … -->` sentinel (ADR-0027 §8) and the harvester drops the whole span.

## 7. Provenance & loop prevention

Every wiki note records where its claims came from (`sources:` in note frontmatter, per the KB schema),
and every inbox item carries `source`. The harvester additionally marks origin so KB-originated facts
are **never re-harvested** back into the KB:

- inbox `source=harvest:<agent>` → curator tags the resulting note region with `origin: harvest:<agent>`.
  The `origin` vocabulary is an EXACT copy of the `source` enum above (ADR-0010 D4), `agent:<name>`
  included — the L1-19 lint enum tracks §1 rather than freezing a subset of it.
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
  session:claude-code: { path: "~/.claude/projects/**/*.jsonl", scope: personal,
                         format: claude-code-jsonl }   # format optional; this is the default
  # letta:   { api: "...", scope: personal }
  # mem0:    { api: "...", scope: personal }
```

`scope` is the source's privacy class (enforced by the harvester's scope gate, ADR-0007/0017).
`follow_links` (optional, default `false`; ADR-0018) makes a `file:` connector follow a bullet's
`[Title](sibling.md)` link and harvest the sibling's content (frontmatter stripped) instead of the
thin one-line summary — opt-in, one hop, confined to the source file's own directory subtree.
`format` (optional, `session:` connectors only; default `claude-code-jsonl`) names the transcript
grammar the connector's files are parsed with. It is the config-visible half of the `SessionReader`
seam: without it every `session:<agent>` connector in the world was parsed as Claude Code JSONL,
which privileged one agent's format in the engine (invariant 6). The engine dispatches on this
DECLARED format, never on the `<agent>` half of the key; an unknown name is a hard config error, and
`harvester.session_sources.SESSION_READERS` is the registry a new grammar is added to.

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
    path: wiki/concepts/curator-concurrency.md
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
  so `[[basename]]` resolves unambiguously in Obsidian/Logseq. **Maps are `wiki/maps/<slug>.md`** —
  the `-moc` suffix is retired, because the kind marker moved from the filename into the directory
  (ADR-0041 D6 step 3 renames `<domain>-moc` to `<domain>` on conversion).
  Note: plan `links[]` carry basenames, but APPLY resolves each to a standard markdown body link
  `[Title](relative.md)` (the git+Obsidian+OKF-native form; ADR-0014 D3); only frontmatter
  `related:`/`children:` remain `[[basename]]`.
- **`wiki/people/**` is OUTSIDE the basename identity space** (ADR-0041 D3.3). People basenames are
  excluded from the curator's `live_basenames` (so a human file can never veto a curator name), from
  L1-1's duplicate check, and from L1-15's alias/basename union. The stated consequence: **a people
  note is addressed by path, never by `[[basename]]`** — a `[[ ]]` link into the tree does not
  resolve and is an L1-2 broken link, and the curator may not author one anyway.
- **Journal basenames are `<YYYY-MM-DD>`**, one per `run_date` repo-wide, at
  `wiki/notes/<yyyy>/<mm>/`. v1's `<domain>-<YYYY-MM-DD>` namespacing existed only because bare
  dates would collide across domains, and the domain is out of the path. The
  ADR-0011 §4.1 check-5 `(daily exempt)` clause is **kept**: it guards *pre-existence*, so retiring
  it would fail the second `agora curate` of any day. §10 itself has never carried a daily exemption
  and still does not.
- **Basenames and slugs are Unicode**, validated by `core/pathsafe.py`: a closed
  Unicode-**category** allowlist (`L`/`N`/`M` plus `-_.`), NFC-normalised, Windows reserved device
  stems rejected, capped at 180 UTF-8 bytes, leading `_` rejected at the composition site (the
  `raw/` reserved-prefix namespace). Case is not folded. Basename identity, alias uniqueness and
  link resolution compare NFC-normalised strings, so a macOS NFD directory read cannot make a name
  unequal to itself. The `note-<sha8>` fallback (#57) remains the last resort but fires far less
  often — a Korean title now yields a Korean component instead of an empty one. What stays **ASCII**
  is everything derived from `core/layout.py`'s writer regex — writer namespaces
  (`_kb/inbox/<writer>/`), harvest cursor stems, gold pack names and reader-cache stems, all
  validated by `layout.validate_writer` / `safe_path_component`. `subjects:`/domain tokens stay
  ASCII kebab-case too ([ADR-0041](adr/0041-stratum-kind-first-layout.md) D2.2 leg 3, because they
  are still `raw/` path segments) — but on a **different footing**, which matters to anyone reading
  this as a guarantee: a domain token never passes through that writer regex (`core/layout.py` says
  so at the composition site), and no layer gates its character class today. `curator/plan.py`
  grades a `Disposition.domain` with the same Unicode `pathsafe` rule as a basename plus the
  leading-`_` rejection, and `config._checked_domains` checks only the leading `_`. So ASCII kebab
  is the rule the ADR states and the convention every tool follows; it is not yet an enforced gate,
  and a taxonomy that declares a non-ASCII domain is admitted rather than refused.
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
`domain` (**singular, and it stays singular** — ADR-0041 D2.2/OD-9 deliberately did not widen the
plan wire to a `subjects` tuple, because that would bump the plan-envelope `schema_version`; its
meaning narrows to the `raw/<domain>/` shard key, and APPLY uses it to seed a ONE-element
`subjects:` on the note, or `subjects: []` when it is null. 0..n subjects are an APPLY-and-human
capability, not a model capability),
`basename` (the NEW note's basename for `CREATE_THEME`/`APPEND_DAILY`; null otherwise — and for
`APPEND_DAILY` it must equal the run date, which is what makes the `wiki/notes/<yyyy>/<mm>/` shard a
composed curator-owned fact rather than something parsed out of model output),
`target_basename` (the EXISTING claim-bearing note targeted by `MERGE_INTO_THEME`/`MARK_CONTESTED` —
kind `concept` or `summary`, derived from the DIRECTORY so a brain cannot falsify it; a journal, a
map, `index.md` or a `wiki/people/` note is rejected at the PLAN gate. Null otherwise)
— both null for `DROP`/`NOOP`, `title`, `summary`,
`status` (the C1 enum: `active | stub | contested | deprecated`), `aliases[]`, `tags[]` (each must already
exist in `_meta/taxonomy.yaml`, C5), `links[]` (wikilink basenames; APPLY resolves each to a standard
markdown body link `[Title](relative.md)` — the git+Obsidian+OKF-native form; only frontmatter `related:`/`children:`
remain `[[basename]]` — see ADR-0014 D3), `needs_prose` (whether PASS 2 authors a
body — and therefore whether APPLY places a region at all, for every prose op including
`APPEND_DAILY`: #131 / ADR-0011 §3.1 addendum), and `reason`. EXACTLY one disposition per candidate; the union of all `event_ids` equals the manifest
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
