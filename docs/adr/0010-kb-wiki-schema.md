# ADR-0010 — KB wiki schema v1 (the `AGENTS.md`/`SCHEMA.md` emitted into every repo)

**Status:** Accepted · 2026-06-13 · _amended by ADR-0014 (2026-06-17): body graph links are now standard markdown links `[Title](relative.md)`, not `[[ ]]`; the L1-2 resolution and L1-6 MOC child-bullet rule are updated accordingly. Frontmatter `related:`/`children:` stay `[[basename]]`; basename identity is retained._

## Context
The curator brain edits `wiki/` but the integrity gate that protects the repo is deterministic code
(ADR-0008 step 4) and the read path is deterministic retrieval (ADR-0009). For both to work, the
*content* the brain produces must obey a frozen, machine-checkable schema: the four note types,
their frontmatter, the link/MOC/citation conventions, the tag taxonomy, and a lint ruleset. That
schema is itself a document — the wiki-content `AGENTS.md`/`SCHEMA.md` (symlinked
`CLAUDE.md`/`QWEN.md`/`GEMINI.md`) emitted into each knowledge repo, which the brain MUST read
before every INGEST. This is the *editorial* schema for wiki content, distinct from the engine's own
source-code `AGENTS.md`.

We adopt the Karpathy three-layer LLM-wiki pattern: an immutable `raw/` verification baseline, a
curator-compiled `wiki/`, and this schema doc. Retrieval is navigation (`index.md` →
`<domain>-moc.md` → `themes/<slug>.md`), not vector search, which is what makes `core.read`
deterministic and testable without a model (ADR-0009). For that traversal to be a pure function, the
schema's identifiers and grammars must be frozen so two independent pure-Python implementations
cannot disagree.

Several forces pull against naive freezing. The curator runs in a no-network sandbox with only a
nondeterministic ambient clock, yet ADR-0008 recovery returns unpublished events to the inbox for a
*fresh* run; any wall-clock-derived date would break replay reproducibility. Graph facts such as
"orphan" or "stale" are tempting to persist in frontmatter, but doing so would either violate
Invariant 1 (indexes must be rebuildable from markdown) or require a second code pass that writes
wiki files after INGEST — contradicting ADR-0008's single-pass integrity line. Free-text
`kb_remember` captures have no uploaded file, yet the Karpathy guarantee demands every durable claim
trace back to `raw/`. Harvest loop-prevention (DATA-MODEL §7) requires a note's recorded origin to
round-trip byte-for-byte against the inbox `source` enum (DATA-MODEL §1). And an honest anti-sprawl
story must close the loophole where a model widens the controlled vocabulary while using it: the
taxonomy is therefore a FIXED read-only input to every INGEST run, with evolution confined to a
separate admin path.

This ADR freezes the schema to resolve all of these, so the deterministic linter can gate the commit
and `core.read` can navigate the graph reproducibly.

## Decision
Freeze **KB wiki schema v1** (canonical `schema_version: 1`) in `src/agora_kb/schema/`, emitted into
every repo as `AGENTS.md`/`SCHEMA.md` (+ symlinks). The schema is the editorial style guide *and*
the lint checklist. The deterministic linter (pure Python, no model, no wall clock) extends the
curator's post-INGEST diff validator (ADR-0008 step 4) and gates the commit. External editors
(Obsidian/Logseq/Foam) are read/browse-only while the curator owns the branch.

Six load-bearing decisions are frozen.

**D1 — Deterministic run clock.** Orchestration derives, before invoking the brain, `run_id` (the
run manifest id, DATA-MODEL §5) and `run_date` = `run_id[:10]` (the UTC calendar date,
`YYYY-MM-DD`), and injects both into the INGEST prompt. The brain MUST read no system clock and use
these — and only these — for every `daily.date`, the date portion of every daily basename, `created`
on new notes, every `updated` bump, and `run_id` on dailies. This makes a diff a pure function of
(`base_commit`, claimed events, `run_date`), so ADR-0008 recovery/replay (which mints a fresh
manifest and `run_date`) is reproducible and idempotent. L1-12/L1-14 enforce equality.

**D2 — Derived-only graph facts.** `orphan` and `stale` are NEVER stored in frontmatter; they are
computed at read/dashboard time from the link graph plus `run_date`, exactly like backlinks. The
persisted `status` enum is therefore `{active, stub, contested, deprecated}` — all brain-DECIDED.
"Single-pass gate / no post-INGEST code rewrite" means precisely this: NO code pass mutates a wiki
file *after* the brain authors prose, so derived facts (`orphan`/`stale`) are never written back.
This is FULLY CONSISTENT with the deterministic APPLY pass of ADR-0011 plan-apply-author: APPLY runs
*within* the same INGEST, materializing structure and ALL frontmatter from the brain's plan BEFORE
PASS-2 prose is authored between the worker-placed body sentinels — it is not a post-author rewrite.
This preserves ADR-0008's single-pass gate and Invariant 1.

**D3 — Provenance-complete free text.** `core.ingest` (deterministic engine code on the WRITE path,
NOT the sandboxed brain) persists every `kb_remember` body as an immutable
`raw/<domain>/<inbox-id>.md` at capture time, before the curator runs. It is the ONLY writer of
`raw/`; the curator brain still never writes `raw/` (ADR-0008 allowlist). A citable `raw/` artifact
therefore always exists, so L1-7 is satisfiable for every theme. `stub` themes are exempt from the
non-empty-`sources:` rule.

**D4 — Frozen `origin` enum.** `origin` is an exact copy of the inbox `source` enum (DATA-MODEL §1):
`{claude-code, codex, qwen, gemini, opencode, hermes, web:<user>, harvest:<agent>, manual}`. This
lets harvest loop-prevention (DATA-MODEL §7) round-trip byte-for-byte. (The prior `upload` value is
removed.)

**D5 — Deterministic lint grammar.** The MOC child-bullet grammar, the `[[ ]]` resolution/
normalization rule, alias uniqueness, and the heading-slug anchor algorithm are frozen as exact
regexes/algorithms so two independent linters and `core.read` agree byte-for-byte (ADR-0009).

**D6 — Taxonomy is a fixed, read-only INGEST input.** `_meta/taxonomy.yaml` is a FIXED input to
every INGEST run: the brain reads `allowed_tags` and `domains` but can NEVER add either. A plan whose
`tags` value is not in `allowed_tags`, or whose target `domain` is not in `domains`, is REJECTED at
PLAN validation (PASS-1, ADR-0011) — before any APPLY — and the run never reaches the worktree. The
taxonomy file is consequently NOT in the curator-writable allowlist (L1-9) and is NOT written during
INGEST; L1-5's declared-before-use check degenerates to "every tag already exists in the fixed
taxonomy". Taxonomy EVOLUTION (adding tags/domains) is a SEPARATE human/admin path — repo-init or a
future review-mode op — NOT part of INGEST, governed by `taxonomy_policy` (`open | review-only |
capped:<N>`) in `_meta/taxonomy.yaml` and audited by L1-18 on that admin path only. The integrity
boundary is intact: the only semantic judgement the brain proposes against the wiki is `contested`
(verified by the L1-10 contested-shape conjunction); it cannot widen the controlled vocabulary.

### The three layers (Karpathy LLM-wiki)

| Layer | Path | Mutability | Role |
|---|---|---|---|
| 1. Sources | `raw/<domain>/` | immutable; written by `core.ingest` only | verification baseline; every durable claim traces here |
| 2. Compiled wiki | `wiki/<domain>/`, `index.md`, `log.md`, `assets/` | curator-writable during INGEST (L1-9 allowlist) | navigable cross-linked knowledge |
| 3. Schema | this doc + `_meta/` | repo-init / admin only; READ-ONLY during INGEST | conventions + INGEST/QUERY/LINT rules + taxonomy |

### Note types (exactly four)

| `type:` | Path | Cardinality | Purpose |
|---|---|---|---|
| `index` | `index.md` (root) | exactly 1 | top Map-of-Content; navigation root; lists every domain MOC |
| `moc` | `wiki/<domain>/<domain>-moc.md` | 1 per active domain | domain MOC; created **lazily** at the domain's first theme |
| `theme` | `wiki/<domain>/themes/<slug>.md` | many | atomic, durable, citable concept page; primary QUERY target |
| `daily` | `wiki/<domain>/daily/<domain>-YYYY-MM-DD.md` | ≤1 per domain per `run_date` | dated consolidation journal that links into themes |

The schema doc and its symlinks are NOT notes: they are allowlisted but excluded from
`parse_all_notes()` and all L1 note rules (skipped by exact basename; symlinks skipped by identity).
A theme is the destination, a daily is the journal of how we got there. Atomicity = one idea per
note (with context), not one sentence.

### Frontmatter spec (YAML only; UTF-8 / LF / no BOM; dates `YYYY-MM-DD`)

Common base (all types) — `title` REQUIRED (no filename fallback):

```yaml
---
title: <human title>                 # REQUIRED
type: index | moc | theme | daily    # REQUIRED, exactly one
aliases: []                          # other names this note resolves under (powers QUERY)
tags: []                             # kebab-case; EACH must pre-exist in _meta/taxonomy.yaml
created: 2026-06-13                  # REQUIRED, == run_date when first created; never changed after
updated: 2026-06-13                  # REQUIRED, == run_date on every curator edit
status: active | stub | contested | deprecated   # REQUIRED
summary: <one-line precis>           # REQUIRED; single-line gist used by QUERY/Dashboard previews
---
```

> **Additive (ADR-0014 D2, 2026-06-17):** the curator also emits OKF-conformance frontmatter fields
> as an additive superset (no existing field changed): `description` (mirrors `summary`), a
> deterministic `timestamp` (`<updated>T00:00:00Z`), and `okf_version: "0.1"` on the bundle-root
> `index.md` only. See ADR-0014 and `kb_schema.md` §2.7.

`theme` adds:

```yaml
sources: []        # REQUIRED & non-empty UNLESS status == stub. raw/ paths; cite the source
                   #   artifact itself, NEVER a .meta.yaml sidecar (DATA-MODEL §2; L1-8b)
related: []        # array of "[[basename]]" — typed-edge substitute
origin: claude-code | codex | qwen | gemini | opencode | hermes | web:<user> | harvest:<agent> | manual
                   # present iff ANY provenance source is harvest:<agent>; EXACT copy of inbox
                   #   `source` enum (DATA-MODEL §1); loop-prevention (DATA-MODEL §7)
confidence: high | medium | low                        # mirrors inbox-item confidence (DATA-MODEL §1)
body_status: pending | absent   # present-as-`pending` only while PASS-2 prose not yet authored;
                                 #   `absent` (key omitted) once authored (ADR-0011 plan-apply-author)
# WHEN status == contested, ALSO REQUIRED (L1-10 / contested-shape):
contested_by: []   # non-empty list[str] of basenames whose claims disagree; set-union, never replaced
contested_at: 2026-06-13   # == run_date (D1; never a wall clock)
```

`daily` adds:

```yaml
date: 2026-06-13                            # REQUIRED, == run_date == basename date (L1-12, L1-14)
run_id: 2026-06-13T03-00-00.000Z--7f31ab    # REQUIRED, == injected run_id; back-link to the run
sources: []                                 # raw/ paths consolidated that day (may be empty)
body_status: pending | absent               # present-as-`pending` only while PASS-2 prose not yet
                                            #   authored; `absent` (key omitted) once authored (ADR-0011)
```

(`title` and `summary` from the common base are likewise REQUIRED on a `daily`; `summary` is the
one-line precis of the run's consolidation.)

`moc` and `index` add:

```yaml
children: []       # array of "[[basename]]"; MUST exactly equal the child-bullet basename set
                   #   in the body (L1-6, grammar below). index → domain MOCs; moc → THEMES ONLY.
```

`status` vocabulary: `active` (normal), `stub` (forward-declared target — the file exists with full
frontmatter but no body; exempt from L1-7), `contested` (≥2 sources disagree; both claims recorded
with their sources; proposed by brain, verified by linter L1-10), `deprecated` (superseded; delisted
from MOCs). `orphan`/`stale` are derived (D2), never persisted — there is no `orphan`/`stale`/
`canonical`/`verified`/`draft` status value.

**Contested representation (frozen, ONE convention).** `status: contested` is the canonical flag —
there is NO separate `contested: true` boolean. A contested theme MUST carry, together: `status:
contested`; a non-empty `contested_by:` list of the basenames whose claims disagree (set-union with
any prior value, never replaced); `contested_at:` == `run_date` (D1); ≥2 entries in `sources:`; and a
body callout whose first line matches the regex `^> \[!contested\]` recording both claims with their
citations. The linter (L1-10 / contested-shape) verifies all of these as a single conjunction.

### Link, MOC & citation conventions (frozen)

**Wikilinks.** `[[basename]]` only — no path, no extension. `[[basename|display]]` allowed for
display; resolution keys off `basename` (left of `|`). Normalization: take the substring left of `|`
(or whole), strip leading/trailing ASCII whitespace; **NO case folding, NO unicode normalization, NO
slugging**; match byte-for-byte against a basename or an `aliases:` entry. Resolution precedence:
(1) exact basename match wins over any alias; (2) else an exact alias match resolves to its owner.
The union of all basenames + all `aliases:` must be globally unique (L1-15), so `[[X]]` is
single-valued. Basename uniqueness is scoped to wiki notes (raw/ is outside the wikilink graph).

> **Scope note (ADR-0014 D3, 2026-06-17):** `[[basename]]` is now the form for FRONTMATTER
> `related:`/`children:` arrays only. BODY graph links (note bodies, MOC/index child bullets) use
> standard markdown links `[Title](relative.md)`; the unique-basename resolver maps the link
> target path (filename minus directory minus `.md`) to the same basename identity. See ADR-0014
> and the "MOC child grammar" section below.

**MOC child grammar (frozen).** A child bullet is a body line matching EXACTLY this regex at indent
0:

> **Amended by ADR-0014 D3 (2026-06-17):** the BODY child-bullet grammar is now the STANDARD
> MARKDOWN LINK form, not the `[[ ]]` wikilink shown below. The shipped grammar is
> `^- \[(?P<text>[^\]\r\n]*)\]\((?P<path>[^)\r\n]+)\)(?:\s.*)?$`, with the child basename parsed
> from the link target path (filename minus directory minus `.md`). Frontmatter `related:`/`children:`
> arrays STAY `[[basename]]`. See `kb_schema.md` §3.2/L1-2 and `src/agora_kb/schema/notes.py`
> `_CHILD_BULLET_RE`/`_basename_from_link_path`.

```
^- \[\[(?P<base>[a-z0-9][a-z0-9-]*)(\|[^\]\r\n]+)?\]\](?:\s.*)?$
```

Marker is `- ` (hyphen-space) only; exactly one `[[ ]]`, the first token of the bullet; `base` is
the child. Any `[[ ]]` in prose, nested bullets, `related:`, or a second link on the line is ignored
for L1-6. L1-6 fails iff the captured child set ≠ the `children:` basename set (set equality;
duplicates collapse).

**Backlinks/orphan/stale** are computed by scanning all `[[ ]]` occurrences plus `run_date`; none
are persisted (Invariant 1).

**Citing `raw/`.** Two ways that must agree: `sources:` frontmatter (authoritative; non-empty for
non-stub themes) and inline citation markers (footnote `[^n]` recommended) whose referenced files
SHOULD be a subset of `sources:`. L1-7/L1-8/L1-8b enforce: non-stub themes have non-empty
`sources:`; every listed `raw/...` path exists; no entry ends in `.meta.yaml` (cite the artifact,
not its sidecar; the sidecar schema is DATA-MODEL §2 and is not redefined here).

**Free-text provenance (D3).** `core.ingest` persists each `kb_remember` body as
`raw/<domain>/<inbox-id>.md` (basename = inbox `id`, DATA-MODEL §1) carrying the capture's
`source`/`writer`/`created`/`content_sha256`, git-tracked. The brain cites it. The brain never
writes `raw/`.

**Assets.** Images/binaries in `assets/`, referenced with standard markdown image links
(`![alt](assets/foo.png)`) — deliberately outside the `[[ ]]` graph; never counted for
orphan/backlink.

**Dailies** are transient: a MOC's `children:` is themes only (dailies MUST NOT appear); dailies may
be listed in MOC prose only via a non-child-bullet form (indented, or an inline `Recent: [[a]],
[[b]]` line, or a table) so the L1-6 grammar does not miscount them; dailies are exempt from orphan
computation (L2-1).

**Forward-declaration via stubs.** To link a not-yet-written page, the brain MUST create the target
as a real note with `status: stub` and full frontmatter in the SAME commit. "Exists" in L1-2 means
the file exists, not merely referenced; there is no dangling `[[ ]]`. Stubs are exempt from L1-7.

### Folder & naming rules

```
<repo>/
  AGENTS.md|SCHEMA.md (+symlinks CLAUDE.md/QWEN.md/GEMINI.md)   this schema (NOT a note; parse-exempt)
  index.md                          the ONLY note basenamed "index"
  log.md                            append-only action log (curator-only; not a note)
  raw/<domain>/<YYYY-MM-DD>-<slug>.<ext>   immutable uploads/harvests (+ <file>.meta.yaml, DATA-MODEL §2)
  raw/<domain>/<inbox-id>.md        immutable free-text captures persisted by core.ingest (D3)
  wiki/<domain>/
    <domain>-moc.md                 e.g. ai-tech-moc.md
    themes/<slug>.md                <slug> = globally-unique basename
    daily/<domain>-YYYY-MM-DD.md    domain-namespaced; date == run_date
  assets/                           binaries placed by the upload/raw path (curator only LINKS them)
  _templates/                       one note template per type (repo-init/admin; READ-ONLY in INGEST)
  _meta/taxonomy.yaml               allowed_tags + domains + canonical schema_version
                                    #   (repo-init/admin; FIXED read-only input to INGEST)
  _kb/                              git-ignored engine spool — NEVER a wiki write target
```

Filenames are kebab-case `.md` slugs; basenames globally unique; only `index.md` is named `index`.
Domain MOC basename `<domain>-moc`; theme basename = its slug; daily basename `<domain>-YYYY-MM-DD`
(bare dates would collide across domains, so dailies are domain-namespaced). Domains are lowercase
kebab tokens declared in `_meta/taxonomy.yaml`. Avoid for portability: Obsidian block-refs `^id`,
transclusions `![[..]]`, `cssclass`; Logseq `A___B___C` namespacing and outliner-structure bullets;
Basic-Memory `- [category]` observation grammar. Body is plain markdown prose + YAML frontmatter +
`[[basename]]` only.

**Encoding/size/anchors.** UTF-8 no-BOM, LF endings (CRLF/BOM → L1-16). Soft note size ≤ ~64 KiB
body; hard cap `_kb/repo.yaml: max_note_bytes` (default 262144) is L2 health. Heading-slug anchor
(frozen, so `SearchHit.anchor` is reproducible — ADR-0009): (1) heading text after `#`s; (2) strip
inline markdown (`[[x|y]]`→`y`, `` `code` ``→`code`, `**b**`→`b`, links→text); (3) lowercase (ASCII
fold); (4) replace any run of non-`[a-z0-9]` with a single `-`; (5) trim leading/trailing `-`; (6)
on duplicate anchors in a file, append `-1`, `-2`, … in document order (GitHub/Quartz convention).

### Tag taxonomy & schema_version

```yaml
# _meta/taxonomy.yaml — machine source of truth (YAML; git-tracked; FIXED read-only input to INGEST)
schema_version: 1                    # CANONICAL
taxonomy_policy: open                # open | review-only | capped:<N>  (governs ADMIN evolution only)
domains: [ai-tech, economy, general]
allowed_tags:
  architecture: { desc: "system structure & design" }
  concurrency:  { desc: "locking, races, single-writer" }
  macro:        { desc: "macroeconomics" }
```

The file is named `_meta/taxonomy.yaml` everywhere (never `taxonomy.json`); its keys are
`allowed_tags` and `domains`. Every note `tags:` value must already be a key under `allowed_tags`,
and every target domain must already be in `domains`, in the worktree the brain reads (L1-5). Because
the taxonomy is a FIXED INGEST input (D6), the brain does NOT add either during a run — `_meta/` is
git-tracked (unlike `_kb/`) but is NOT in the INGEST curator-writable allowlist (L1-9). **Canonical
`schema_version`** lives in `_meta/taxonomy.yaml` because the worktree at the curated revision cannot
see git-ignored `_kb/repo.yaml`; `_kb/repo.yaml` (DATA-MODEL §3) and this doc's header MIRROR it
(L1-17 asserts equality across locations present in the worktree). **Taxonomy evolution gate (D6):**
on the SEPARATE admin/human path that edits the taxonomy, L1-18 reads `taxonomy_policy` — `open` (no
protection; solo-MVP default), `review-only` (a commit ADDING an `allowed_tags`/`domains` key is
allowed only in review/PR mode, ADR-0008), `capped:<N>` (≤ N new keys per admin change). New tags =
`taxonomy.allowed_tags(after) − taxonomy.allowed_tags(before)`. This gate does NOT run during INGEST,
where the taxonomy is read-only.

### LINT ruleset (deterministic; gates the commit; reads no wall clock)

Runs after INGEST, before commit, extending ADR-0008 step 4. Parses every `.md` note except the
schema doc + symlinks. Two tiers.

**L1 — STRUCTURAL (hard reject → events to `_kb/failed/`):**

| # | Rule |
|---|---|
| L1-1 | Duplicate basename anywhere in repo |
| L1-2 | Broken/dangling wikilink (after alias resolution; a stub must already exist as a file) — checked in the note BODY **and** in the frontmatter `related:` and `children:` `[[ ]]` arrays, every entry of which must resolve to a known basename |
| L1-3 | Ambiguous wikilink (belt-and-suspenders; only if L1-1/L1-15 broke uniqueness) |
| L1-4 | Missing required frontmatter for `type` |
| L1-5 | Tag/domain not in the FIXED taxonomy: a `tags:` value absent from `_meta/taxonomy.yaml.allowed_tags`, or a target domain absent from `domains` (taxonomy is read-only during INGEST; evolution is L1-18's separate admin path) |
| L1-6 | MOC `children:` ≠ child-bullet set (frozen grammar) |
| L1-7 | Non-stub theme with empty `sources:` |
| L1-8 | `sources:` path absent under `raw/` in the worktree |
| L1-8b | `sources:` entry ends in `.meta.yaml` (sidecar cited) |
| L1-9 | Path escape / disallowed symlink / off-allowlist add or modify. INGEST curator-writable allowlist (frozen, ADR-0008 step 4): `wiki/**`, `index.md`, `<domain>-moc.md`, `log.md`, `assets/**`. REJECT any add/modify to anything else — `_meta/`, `_templates/`, `raw/`, `_kb/`, git config, hooks, and the schema doc (`AGENTS.md`/`SCHEMA.md`) + its symlinks (`CLAUDE.md`/`QWEN.md`/`GEMINI.md`, diff-scoped: may EXIST unchanged, any add/modify/delete FAILS the run) |
| L1-10 | Contested-shape: `status: contested` MUST imply ALL of {`len(sources) ≥ 2`, non-empty `contested_by:`, `contested_at` == `run_date`, a body callout matching `^> \[!contested\]`}; any missing element fails |
| L1-11 | Unknown `type` or `status` value |
| L1-12 | `created`/`updated`/`date` not `YYYY-MM-DD`, or any `> run_date` (no future dates) |
| L1-13 | Second note basenamed `index` |
| L1-14 | Daily: `date` ≠ basename date, or ≠ `run_date`, or `run_id` ≠ injected `run_id` |
| L1-15 | Alias/basename collision (union not globally unique) |
| L1-16 | Not UTF-8-no-BOM with LF endings |
| L1-17 | `schema_version` drift across `_meta/taxonomy.yaml` / `_kb/repo.yaml` / schema-doc header |
| L1-18 | `taxonomy_policy` violation on the SEPARATE admin/human evolution path (`review-only` change made in direct mode, or `capped:<N>` exceeded). NOT evaluated during INGEST, where the taxonomy is a fixed read-only input |
| L1-19 | `theme` with `origin` ∉ inbox `source` enum (DATA-MODEL §1) |

**L2 — HEALTH (soft; DERIVED at read/dashboard time; never written; feeds Dashboard, DESIGN §5.3):**
L2-1 orphan (theme with 0 inbound `[[ ]]` AND not in any MOC `children:`; dailies exempt) · L2-2
stale (`updated` older than `stale_days` before `run_date`) · L2-3 stub unfilled for `stub_max_runs`
runs · L2-4 contested lingering · L2-5 body exceeds `max_note_bytes` · L2-6 stale `body_status`
(the key is present but every `agora:body` region in the note is authored — warning only, so it
never flips `LintResult.ok`; promote to a hard L1 rule only once `agora repo upgrade` can repair an
existing repo). Thresholds from
`_kb/repo.yaml` (`health: {stale_days: 90, stub_max_runs: 5, max_note_bytes: 262144}`), computed
relative to `run_date`.

```python
def lint_l1(worktree, taxonomy, run_date, run_id, review_mode, base_taxonomy):
    notes = parse_all_notes(worktree)          # EXCLUDES schema doc + its symlinks
    errors, by_basename = [], collect_basenames(notes)
    for base, paths in by_basename.items():
        if len(paths) > 1: errors.append(DuplicateBasename(base, paths))          # L1-1
        if base == "index" and not is_root_index(paths[0]):                       # L1-13
            errors.append(SecondIndex(paths[0]))
    name_space = set(by_basename)
    for n in notes:                                                               # L1-15
        for a in n.fm.aliases:
            if a in name_space: errors.append(AliasCollision(n.path, a))
            name_space.add(a)
    known = set(by_basename) | all_aliases(notes)
    new_tags = set(taxonomy.allowed_tags) - set(base_taxonomy.allowed_tags)        # for L1-18 (admin path)
    for n in notes:
        if not is_utf8_lf_no_bom(n.path): errors.append(BadEncoding(n.path))      # L1-16
        # L1-2/L1-3: resolve body links AND frontmatter related:/children: arrays
        for link in resolve_links(n.body, known) + resolve_wikilinks(n.fm.related, known) \
                                                + resolve_wikilinks(n.fm.children, known):
            if link.unresolved: errors.append(BrokenLink(n.path, link))
        errors += check_required_frontmatter(n)          # L1-4, L1-11 (incl. title/summary/status)
        errors += check_dates(n, run_date)               # L1-12 (no future dates)
        for tag in n.fm.tags:                                                     # L1-5 (fixed taxonomy)
            if tag not in taxonomy.allowed_tags: errors.append(UnknownTag(n.path, tag))
        if note_domain(n.path) not in taxonomy.domains:                           # L1-5 (fixed domains)
            errors.append(UnknownDomain(n.path))
        if n.fm.type in ("moc", "index"):                                         # L1-6
            if set(n.fm.children) != child_bullet_set(n.body):
                errors.append(MocChildrenMismatch(n.path))
        if n.fm.type == "theme":
            if n.fm.origin not in INBOX_SOURCE_ENUM: errors.append(BadOrigin(n.path))   # L1-19
            if n.fm.status != "stub" and not n.fm.sources:
                errors.append(EmptySources(n.path))                              # L1-7
            for s in n.fm.sources:                                               # L1-8 / L1-8b
                if s.endswith(".meta.yaml"): errors.append(SidecarCited(n.path, s))
                elif not (worktree / s).exists(): errors.append(MissingSource(n.path, s))
            if n.fm.status == "contested":                                       # L1-10 contested-shape
                if not (len(n.fm.sources) >= 2 and n.fm.contested_by
                        and n.fm.contested_at == run_date
                        and has_contested_callout(n.body)):
                    errors.append(ContestedShape(n.path))
        if n.fm.type == "daily":                                                 # L1-14
            if not (n.fm.date == basename_date(n.path) == run_date
                    and n.fm.run_id == run_id):
                errors.append(DailyDateMismatch(n.path))
    errors += validate_paths_and_symlinks(worktree)      # L1-9 (allowlist: wiki/** index.md
                                                         #   <domain>-moc.md log.md assets/**)
    errors += check_schema_version(worktree, taxonomy)   # L1-17
    if taxonomy_evolution_change:                        # L1-18 ONLY on the admin/human path;
        errors += check_taxonomy_policy(taxonomy, new_tags, review_mode)  #  NOT during INGEST
    return errors
# commit only if not lint_l1(...); else -> _kb/failed/ (ADR-0008 step 5)
```

### INGEST workflow (per run) — plan-apply-author (ADR-0011)
The brain performs EXACTLY two cognitive acts; deterministic worker code (APPLY) writes all structure
and all frontmatter from the plan. **PASS 1 (plan):** read this schema + the FIXED read-only
`_meta/taxonomy.yaml`; note injected `run_date`/`run_id` (use ONLY these; no system clock). For each
claimed item confirm `domain`, decide the semantic ops in `plan.json` — `status`, `tags` (only those
already in `allowed_tags`; a plan with an unknown tag or domain is REJECTED at PLAN validation),
`sources` event ids, `related`/`children` links, `summary`, `aliases`, and contested judgments
(`status: contested` with `contested_by` when sources disagree). Route capture text into
`daily/<domain>-<run_date>.md` (its body already exists as `raw/<domain>/<inbox-id>.md`, D3). Distill
durable claims into `theme` pages (new when a claim recurs or warrants standalone; else merge,
preserving all `sources:`), citing the originating `raw/` file. To link a not-yet-written page, plan
a `status: stub` theme in the same run. Plan MOC maintenance (lazy create at first theme; add to
`index.md`; `children:` == child-bullet set, themes only). **APPLY (worker):** materializes files,
all frontmatter (dates from injected values; `summary`; contested fields; `body_status: pending`) and
the body sentinels. **PASS 2 (author):** the brain writes prose ONLY between the worker-placed
`<!-- agora:body:start/end id=<candidate_id> -->` sentinels; once authored, `body_status` is dropped.
*As built:* the brain cannot drop it (the §4.2 AUTHOR diff requires frontmatter byte-identity), so the
WORKER retracts the key immediately after that gate and before the L1 lint, for every `needs_prose`
note left holding no unauthored region; a region an earlier run left at the placeholder keeps the flag.
The worker appends one line to `log.md`. The brain never adds a tag/domain, never writes
`_meta/`/`_templates/`/`raw/`/`_kb/`, git config, hooks, or any off-allowlist path (L1-9).

### QUERY alignment (ADR-0009)
Globally-unique basenames + the frozen resolver make `[[basename]]` a total function, so the link
graph is traversable in pure Python — that traversal IS `match_reason: linked-theme`. `index.md` +
MOCs give every page a navigational entry point. `aliases`/`related`/`children` are structured
edges, so `core.read` ranks linked-theme > heading > lexical (DATA-MODEL §9). Atomic themes keep
each `SearchHit.excerpt` self-contained; the frozen heading-slug algorithm makes `SearchHit.anchor`
reproducible. The markdown IS the index; any SQLite/backlink/orphan cache is rebuildable (Invariant
1). At optional Quartz publish time, `status: deprecated` and derived `orphan` map to `draft: true`;
Quartz ignores unknown frontmatter, so no core schema change is needed to be publish-ready.

## Consequences
- **+** The wiki-content contract is frozen and machine-checkable: the pure-Python linter gates the
  commit (extending ADR-0008 step 4) with no model in the loop, and `core.read` navigates the graph
  reproducibly (ADR-0009).
- **+** Diffs are a pure function of (`base_commit`, claimed events, `run_date`): injecting the run
  clock from the manifest (D1) makes ADR-0008 recovery/replay idempotent.
- **+** Invariant 1 holds end-to-end: graph facts (`orphan`/`stale`/backlinks) are derived, never
  persisted (D2), so the commit stays a single brain-authored pass and indexes remain rebuildable.
- **+** Every durable claim traces to `raw/`, including free-text captures, because `core.ingest`
  persists each `kb_remember` body as `raw/<domain>/<inbox-id>.md` (D3) — the Karpathy provenance
  guarantee is total.
- **+** Harvest loop-prevention round-trips byte-for-byte: `origin` is an exact copy of the inbox
  `source` enum (D4, DATA-MODEL §7), so connectors never re-harvest KB-originated facts.
- **+** Two independent linters cannot disagree: the MOC grammar, link normalization, alias
  uniqueness, and heading-slug anchor are frozen exactly (D5).
- **−** Derived graph facts mean a tool reading raw frontmatter cannot see `orphan`/`stale` without
  running the link-graph computation; the rejected alternative (a second code pass rewriting
  `status: orphan`) was discarded for adding a second writer over the diff, against ADR-0008.
- **−** Run-clock injection loses sub-day date precision and keys a daily to the consolidation RUN
  date, not the human capture date; fine-grained order is recovered from `run_id` and git history,
  and the capture's own `created` survives in `raw/<inbox-id>.md`.
- **−** Persisting every free-text capture adds one immutable file per `kb_remember` and slightly
  larger repos, preferred over relaxing L1-7 to accept synthetic non-`raw/` source tokens.
- **−** Freezing `origin` to the inbox enum couples this schema to DATA-MODEL §1: adding an agent
  source requires editing both enums together (intentional — divergence is exactly what would break
  re-harvest prevention).
- **+** A model can never widen the controlled vocabulary: the taxonomy is a FIXED read-only INGEST
  input (D6), so a plan with an unknown tag/domain is rejected at PLAN validation before APPLY, and
  `_meta/taxonomy.yaml` is off the L1-9 allowlist — junk tags cannot land in a curation commit.
- **−** Because INGEST cannot extend the taxonomy, a genuinely new tag/domain requires a separate
  admin/human edit before the relevant claim can be curated; the `open` default `taxonomy_policy`
  keeps that admin path frictionless for the solo MVP, with `review-only`/`capped` as opt-in
  enforcement on that path only.
- **−** Forward-declared links must materialize a `status: stub` file in the same commit, so the
  brain emits placeholders it may abandon; stubs are exempt from L1-7 to keep this cheap, and L2-3
  surfaces long-unfilled stubs.
- **−** Frozen grammars are rigid: only `- [[x]]` bullets at indent 0 count as MOC children, so a
  stylistically valid but differently-formatted MOC can be rejected. Rigidity is the price of a
  reproducible hard-reject gate.
