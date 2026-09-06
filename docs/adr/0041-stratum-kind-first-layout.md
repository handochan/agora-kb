# ADR-0041 — Stratum: the wiki axis flips (directory is the KIND, frontmatter carries the SUBJECT) — KB wiki schema 2

**Status:** Accepted · 2026-09-05 (proposed 2026-09-04) · _authored as normative text so acceptance was a status flip, nothing else._

**ACCEPTANCE RECORD (owner judgement, 2026-09-05).** The open sub-decisions were ratified as recommended, **OD-1 through OD-10** — note the count: the day-1 handoff said *OD-1..9* and OD-10 is real. OD-1..OD-9 were accepted as *already-shipped* (each recommendation is wired and test-locked on the branch this ADR ships with); **OD-10 was accepted as a deliberate deferral**, which means this ADR now records, normatively, that the curator lane keeps asserting a possibly-false subject through ADR-0022's catch-all floor, exactly as schema 1 did — the `subjects: []` PATH exists and is exercised, the PRODUCER does not, and closing it stays the three coupled changes OD-10 names. Two things are ratified as **known gaps, not as done**: D3.3's repo-internal `file:`-connector fence has no implementation (`harvester/` carries no `people` rule), and the `wiki/people/**` pull surface (`kb_query`/`kb_read`/`kb_neighbors`) stays open by design — residual risk R1. Both are tracked as issues rather than left implicit here: **#165** (the D3.3 connector fence) and **#166** (the R1 pull boundary, which ADR-0037 must answer for `role: reader` KBs before federation ships). Two ratified-but-deferred rows carry tickets too: **#167** for OD-6's cache-stem follow-up (the `#108` residual) and **#168** for OD-10's three coupled changes.

**AMENDED (append-only) — three mechanical details of D1.4 and D4.2 are REVISED by the addendum at the end of this file** (*Addendum — as-built: the capture transport and the `raw/_blob/` sidecar (#153, landed 2026-09-04)*): the staging path is `_kb/inbox/<writer>/_attach/<sha256>.<ext>` with an `attachments:` list, **not** `<id>.blob` with `raw_ref`; the `raw/_blob/` sidecar has its own nine-key CAPTURE key set, **not** the five-key DATA-MODEL §2 re-ingest shape (which is unchanged for `raw/<domain>/` binaries); and `<ext>` is the D1.4 grammar with a `bin` fallback, deliberately **not** narrowed to the extractor's accepted set. Every normative property — APPLY as the sole `raw/` writer, `raw_writes` membership **with matching bytes** as the admission rule, content-addressing as an additional self-check and never a substitute for it — is untouched. The D1.4/D4.2 prose below is retained verbatim for history.

Supersedes the layout, note-type, and folder-naming sections of [ADR-0010](0010-kb-wiki-schema.md)
(KB wiki schema v1) and the "no new `type:` values" conclusion of
[`STRATEGY-2026-08.md` §12](../STRATEGY-2026-08.md) (see the triage section below, which also
overrides §12's *entities-as-a-tag-facet* verdict). Amends
[0002](0002-cqrs-single-writer-curator.md), [0006](0006-repo-as-tenant-boundary.md),
[0008](0008-transactional-sandboxed-curation.md), [0011](0011-curator-ingest-contract.md),
[0012](0012-deterministic-query-ranking.md), [0014](0014-okf-obsidian-interoperability.md),
[0017](0017-harvester-file-connector-mechanics.md)/[0018](0018-harvester-link-following.md),
[0019](0019-web-face-stack.md), [0020](0020-web-upload-write-path.md),
[0021](0021-knowledge-graph-viz.md), [0022](0022-curator-taxonomy-governance.md),
[0024](0024-bulk-processing-horizontal-curator-scale.md),
[0025](0025-web-config-multiupload-extensions.md) and [0027](0027-gold-context-packs.md), plus
`docs/DATA-MODEL.md` §1/§2/§10 and `docs/INGEST-CONTRACT.md` — the per-ADR triage is the **ADR
triage** section, the non-ADR amendments are listed immediately after it, and every amended ADR
carries a banner pointing here.

Design record: [`docs/notes/stratum-target-architecture.md`](../notes/stratum-target-architecture.md)
(Draft, non-normative) and [`STRATEGY-2026-08.md` §14.8](../STRATEGY-2026-08.md). This ADR is the
normative form of that draft; where the two differ, this ADR governs.

---

## Context

**The diagnosis, in one paragraph.** Agora's wiki schema has its two axes inverted: the *path*
carries the **subject** (`wiki/<domain>/themes/<slug>.md`) while a closed four-value `type:` enum
carries the **kind** (`index | moc | theme | daily`). That single inversion is the mechanical cause
of six unrelated-looking blockers — a long document has no home (`type: Summary` is a hard L1-11
reject); the evidence tier has no place in the corpus; entities have no node kind; the no-loss floor
of [ADR-0022](0022-curator-taxonomy-governance.md) §A must **assert a possibly-false subject**
(`domains[0]`) in order to have somewhere to put the file at all; a genuinely new *kind* of
knowledge demands a lint rewrite because the kind vocabulary is closed and enumerated; and the
subject is recorded **nowhere but the path segment**, so flattening the domain partition destroys
information no other field holds. Inverting the axis fixes all six at once, and it fixes them
because the kind is what code needs to branch on (it is finite, engine-owned, and structural) while
the subject is what humans and retrieval need to *filter* on (it is open, plural, and editorial).
The one principle worth importing from the nearest competing tree (OpenKB) is exactly this and
nothing else — **directory is the kind** — and matching OpenKB's directory *shape* byte-for-byte was
already rejected on blast-radius grounds in [`STRATEGY-2026-08.md` §10](../STRATEGY-2026-08.md),
where cohabitation in one repo is **forbidden**: `openkb add`'s rollback snapshots
`wiki/concepts`/`wiki/entities` wholesale and `unlink()`s live files absent from its backup.

**Forces.** Four constraints shape the decision and are non-negotiable inputs to it.

1. **There are no released users.** The two live knowledge bases are the owner's, both schema 1, and
   `agora repo upgrade` (#63) does not exist. This removes the usual reason to build an in-place
   migrator and the usual reason to phase a schema change.
2. **`raw/` cannot move.** No code in `src/` reads a domain *out of* a `raw/` path, but that path
   string is stored in the `sources:` frontmatter of **every note in every repo**, and lint L1-7 /
   L1-8 / L1-8b verify those references resolve. Re-pathing `raw/` is a rewrite of the provenance
   chain of every note — a second point of no return that nobody had named.
3. **The integrity boundary must not be re-opened.** [ADR-0011](0011-curator-ingest-contract.md)
   §4.0/§4.1 and [ADR-0008](0008-transactional-sandboxed-curation.md) §4 make success a pure function
   of `(plan.json, git_diff, manifest, bundle, lint)` with a model-independent validator. A layout
   change that also loosens that gate would leave any later failure unattributable.
4. **The layout change is entangled with two ASCII-only controls.** Byte-first capture forces a
   rewrite of `curator/worker.py`'s `_is_engine_written_raw` (a UTF-8 `read_text` equality check
   cannot admit a binary blob), and Unicode slugs collide with
   `curator/plan.py:92` `_SAFE_TOKEN_RE_PATTERN`, which is a path-**escape** control, not a style
   rule. Both were split out and gated ahead of the layout (see "Gate A" below).

**Gate A is already half-landed.** Its *layout-invariant* half shipped as PR #160 (commit
`1a9ea8a`): `raw/` admission now compares **bytes** (`raw_writes: dict[str, bytes]`,
`full.read_bytes() == raw_writes[path]`), containment (`resolve()` + `is_relative_to`) guards every
curator path-composition site, the final-diff gate grades the **source** path of a rename, and git
output is decoded as UTF-8 with `-z` name-status streams read as raw bytes. `core/pathsafe.py`
landed alongside it (commit `5ba7bd5`) — a closed Unicode-**category** allowlist
(`unicodedata.category(ch)[0] in ("L","N","M")` plus `-_.`), with Windows reserved-device rejection,
a UTF-8 byte cap, and **no call sites yet**, deliberately, so a later failure is attributable to the
swap and not to the validator. What this ADR authorises is the *layout-defined* half of gate A: the
`raw/_blob/` capture channel, the `raw/_pages/` reservation, the pathsafe swap plus its slugger
mirrors, and the `wiki/people/` carve-out in the allowlist.

**Gate B is a deterministic golden, and it is already pinned.** The original gate B (an n=24
five-arm search harness) never existed as code. It was re-scoped and shipped as PR #161
(commit `c1c3d9e`): `tests/rank_golden/` records what `Wiki.query` returns *today* over a synthetic
46-note repo built in the v1 layout — 44 probes, both `fm` modes, every hit keyed on **basename**
rather than path precisely so the record survives the flip it has to measure through. `_is_moc_path`
reads the MOC out of the path and seeds `d_moc` for the whole corpus, so ranking moves when the
layout moves, silently, unless something records the before.

> **Branch note (authoring-time fact, not a decision).** At the time of writing, `main` carries
> neither PR #160/#161 nor the Stratum design note: `core/pathsafe.py` lives on
> `feat/stratum-unit1-integrity-boundary` (commit `5ba7bd5`); `tests/support/kb_builder.py` and
> `tests/rank_golden/` live on `feat/stratum-unit3-rank-golden` (commit `c1c3d9e`);
> `docs/notes/stratum-target-architecture.md` and `STRATEGY-2026-08.md` §14 live on
> `docs/strategy-14-four-way-synthesis` (commit `5da5d03`); and the **ADR-0005 T0–T4 licence-tier
> addendum** — the framework this ADR's dependency and cohabitation arguments actually turn on
> (OD-1, D3.5) — lives on that same branch at commit `66fa455` (#157). All of them land before the
> flip PR; this ADR cites them by commit so the citations stay checkable.

---

## Decision

Adopt **KB wiki schema 2**: `wiki/`'s first path segment is the note's **kind**, the subject leaves
the path and becomes the `subjects:` frontmatter list, and `type:` is retired as the kind authority.
The whole format lands **at once** — axis flip, `raw/_blob`, Unicode slugs, `_meta/kb.yaml` — reached
by a **clean break** (`agora export` / `agora import --from-kb`), never by an in-place migrator.

### D1 — The schema-2 on-disk layout

```text
<repo>/                                  one git repo = one tenant (ADR-0006, unchanged)
  index.md                               CANONICAL · the ROOT MAP · kind: index · exactly one
  log.md                                 CANONICAL · append-only curator log (worker-only)
  AGENTS.md (+ SCHEMA.md/CLAUDE.md/…)    CANONICAL · the emitted wiki schema (parse-exempt)

  wiki/                                  CANONICAL · the FIRST segment under wiki/ IS the kind
    concepts/[<free>/…/]<slug>.md        kind: concept    (was type: theme)
    summaries/[<free>/…/]<slug>.md       kind: summary    (NEW; SHIPS EMPTY — contract is ADR-0040, OD-7)
    notes/<yyyy>/<mm>/<slug>.md          kind: note       (was type: daily)
    maps/[<free>/…/]<slug>.md            kind: map        (was type: moc)
    entities/[<free>/…/]<slug>.md        kind: entity     (NEW; SHIPS EMPTY — no day-1 producer, OD-8)
    people/<person>/**.md                kind: person (DERIVED) · HUMAN-OWNED · curator NEVER writes

  assets/**                              CANONICAL · attachments; outside the note graph (unchanged)

  raw/                                   CANONICAL · evidence tier — NEVER MOVED
    <domain>/<event_id>.md               unchanged (ADR-0010 D3) — <domain> is now ONLY a shard key
    <domain>/<date>-<slug>.<ext>         unchanged (DATA-MODEL §2)
    _blob/<ab>/<sha256>.<ext>            NEW · original bytes · content-addressed · immutable
    _blob/<ab>/<sha256>.<ext>.meta.yaml  NEW · capture sidecar (the DATA-MODEL §2 shape, verbatim)
    _pages/                              RESERVED PREFIX ONLY (ADR-0040). Nothing writes it on day 1.

  _meta/                                 CANONICAL · repo-init/admin · READ-ONLY during INGEST
    taxonomy.yaml                        unchanged: schema_version, domains, allowed_tags, policy
    kb.yaml                              NEW · CLOSED key set {kb_id, name, declared_kind}
  _templates/                            unchanged · one template per KIND now, not per type

  _kb/                                   DERIVED · git-ignored · rebuildable · never a write target
```

**Canonical** = must not change silently. **Derived** = reconstructible byte-identically. The only
derived artefact this ADR permits inside the canonical tree is `raw/_pages/`, and it is reserved,
not populated (D1.4).

**D1.1 — Free sub-folders under a kind, and the three places that is not true.** A note may sit at
any depth under its kind directory (`wiki/concepts/engineering/team/foo.md`) and **no code reads the
intermediate segments.** A person who organises by folder in Obsidian keeps doing so; Agora reads
frontmatter. Three exceptions are stated rather than discovered later:

- `wiki/notes/<yyyy>/<mm>/` — the first two sub-segments are a **date shard** and MUST equal the
  year and month of the note's frontmatter `date:` (the successor to lint L1-14).
- `wiki/people/<person>/` — the first sub-segment is the **person namespace** and is read by the
  read-side facet (D3.3). Below it, depth is free.
- `wiki/<kind>/` itself — segment index 1 is the kind and is authoritative (D2.1).

**D1.2 — `index.md` is the root map and keeps its own kind.** It sits at the repo root, not under
`wiki/maps/`, so the directory rule cannot name it; it carries `kind: index`, is the only note
basenamed `index` (lint L1-13, unchanged), and has cardinality exactly one. It is the *root of* the
map tier, which is why `maps/` hangs off it; it is not a member of it.

**D1.3 — `maps/<slug>.md` and the frozen child grammar.** The v1 MOC child-bullet grammar is
retained **byte-for-byte** — `^- \[(?P<text>[^\]\r\n]*)\]\((?P<path>[^)\r\n]+)\)(?:\s.*)?$` at
indent 0, child basename parsed from the link target path (ADR-0014 D3), frontmatter `children:`
staying `[[basename]]` and required to equal the child-bullet set exactly (lint L1-6, unchanged).
What changes is only the **admitted child set**:

| child kind | may appear in a map's `children:` | why |
|---|---|---|
| `concept` | **YES** | the v1 rule (a MOC's children are themes only), carried forward |
| `summary` | **YES** | a summary is a navigable destination with a body and `sources:`; it is what the map tier exists to reach |
| `map` | **YES** | a map may be a child of `index.md` (the v1 index→MOC edge) and of another map (nesting was already reachable and is now nameable) |
| `note` | **NEVER** | verbatim carry-over of the v1 "dailies MUST NOT appear in `children:`" rule; a dated journal churns the map every run |
| `entity` | **NO — day 1** | see below (and moot on day 1: `wiki/entities/` ships empty, OD-8) |
| `person` | **NEVER** | `people/` is outside the curated wiki (D3.3); the curator cannot author a bullet pointing into a tree it may not write |

The rule is enforced, not merely stated: v1's *"dailies MUST NOT appear in `children:`"* lives in
ADR-0010 prose and has **no lint rule** — L1-6 is pure set equality (`declared != child_bullets(body)`,
`schema/lint.py`) and never inspects a child's kind. Schema 2 gives the table teeth as **L1-24**
(D3.1's rule numbering).

**Entities are deliberately excluded from `children:` on day 1, and the reason is a ranking
property, not taste.** ADR-0012 stage 1 makes every map child a `d_moc = 0` seed, and
[ADR-0012's #146 addendum](0012-deterministic-query-ranking.md) records that the **thin-page half**
of #146 is *still open* — pinned as a `strict=True` xfail in
`tests/core/test_wiki_lexical_evidence_146.py`. An entity page is by design "registered, with gated
filling": a large population of thin, structurally-perfect pages is precisely the husk shape that
addendum was written about, and admitting them as children would inject that population directly
into the seed set. Two further costs: L1-6's set equality would make every entity registration a
map-churn event, and `indeg_norm`'s denominator would move for reasons unrelated to knowledge.
Nothing is lost by the exclusion — entities stay reachable by lexical match, by body links from
concepts, and in the graph face. **Widening the child set later is additive and cheap; narrowing it
later is not**, which is the asymmetry that decides day 1. Recorded as open sub-decision OD-2.

> **`wiki/entities/` has no day-1 producer, and this ADR does not invent one.** The kind, its
> frontmatter shape (D2) and its directory are defined; **nothing writes into it**. No op creates an
> entity: this ADR adds no op (see the ADR-0011 triage row), `_implied_note_path` derives a path only
> for `CREATE_THEME` and `APPEND_DAILY` (`curator/plan.py`), and `agora import --from-kb` has no v1
> antecedent to convert (D2.5). So `wiki/entities/` ships **empty**, exactly as `wiki/summaries/`
> does under OD-7, and for the same reason: shipping the *container* before the contract avoids a
> second migration, while inventing a producer here would create a population no ADR governs. The
> producer is **open sub-decision OD-8**. Until it lands, the D1.3 entity-child debate is a decision
> taken in advance about a population that does not yet exist — which is the cheap moment to take it.

**D1.4 — `raw/` is never moved; two new prefixes are reserved inside it.**

- `raw/<domain>/…` is **byte-identical to v1**. The `<domain>` segment survives as a *shard key* and
  nothing more: no code reads a subject out of it, and `sources:` strings written under schema 1
  remain resolvable verbatim, which is the entire reason this ADR does not touch `raw/`.
- `raw/_blob/<ab>/<sha256>.<ext>` — the original bytes of a captured artefact, where `<ab>` is the
  first two hex characters of the sha256 (fan-out shard) and `<sha256>` is the hex digest of the
  file's bytes. **Immutable and content-addressed.** It is written **only** by the deterministic
  APPLY pass and admitted **only** by the bytes-mode gate landed in PR #160 — that is, by membership
  in `raw_writes` with matching bytes.
  **`<ext>` grammar (normative, and it is the path composer's obligation, not lint's):** exactly one
  component — `[a-z0-9]{1,16}`, lowercased, containing **no `.`** — drawn from the extractor's
  accepted extension set (ADR-0025's broadened list). A dotted compound extension is forbidden, and
  `meta` is **excluded** outright. Without that exclusion the composer can mint
  `…/<sha256>.meta.yaml`-shaped names for a legitimate artefact, and lint **L1-8b** is a pure suffix
  test (`if s.endswith(".meta.yaml")`, `schema/lint.py`) with no check that the file really is a
  sidecar — so a citable artefact would become permanently uncitable. The single-component rule
  closes that by construction: `.yaml` is a legal `<ext>`, `.meta.yaml` is not a legal `<ext>` at all.
  > **Content-addressing is an ADDITIONAL self-check, never a substitute for `path in raw_writes`.**
  > `hash(bytes) == basename` is an **integrity** check. `path in raw_writes` is an **authorship**
  > check. They close different holes. A PASS-2 brain that plants
  > `raw/_blob/ab/<correct-sha>.bin` whose name correctly hashes its own bytes **still fails**,
  > because the path is not in `raw_writes` — the planting path #135 closed must not be re-opened
  > wearing the face of a simplification. This sentence is normative and any future
  > "content-addressing makes the admission gate redundant" proposal must cite and overturn it.
- `raw/_blob/<ab>/<sha256>.<ext>.meta.yaml` — the capture sidecar, carrying the DATA-MODEL §2 shape
  (`source_url`, `ingested`, `ingested_by`, `sha256`, `mime`) unchanged. The sidecar name is
  `<file>.meta.yaml`, i.e. the **full filename plus** `.meta.yaml`, not `<sha256>.meta.yaml`: this
  keeps the single sidecar-naming rule the repo already has, stays unambiguous when identical bytes
  are admitted under two extensions, and leaves lint L1-8b (a `sources:` entry may not end in
  `.meta.yaml` — cite the artefact, not its sidecar) working **unmodified**.
- `raw/_pages/` — **reserved prefix only.** Nothing writes it on day 1 and the gate grants it no
  special power: a file appearing there fails the final diff exactly like any other unauthored
  `raw/` path. What the reservation buys is (a) that [ADR-0040](README.md) (long-document contract,
  reserved, not yet authored) can populate it without re-arguing an exception, and (b) the
  namespace safety below.
- **`_blob` and `_pages` are RESERVED domain names, and the reservation must be enforced in TWO
  places because the pathsafe swap removes the layer that enforces it today.** `raw/<domain>/` and
  `raw/_blob/` share one namespace, so a taxonomy declaring a domain literally named `_blob` would
  make APPLY write `raw/_blob/<event_id>.md` into the content-addressed tree.
  - **Today's plan-derived immunity comes from ONE thing: `_SAFE_TOKEN_RE_PATTERN`**
    (`curator/plan.py:92`, `\A[A-Za-z0-9][A-Za-z0-9._-]*\Z`), whose leading character class excludes
    `_`. **`core/pathsafe.py` does NOT reject a leading `_`** — `_EXTRA_ALLOWED = "-_."` admits it
    and `_trim_edges` rejects only a leading `.` — so `is_safe_component("_blob")` is `True` and
    `safe_slug_component("_blob")` returns `"_blob"` (executed against commit `5ba7bd5`). D4.4's swap
    therefore **removes** the only control that stops a plan token named `_blob`; the swap is a net
    tightening on Windows device stems and a net **loosening** on exactly the character this
    reservation depends on. D4.4 states the normative obligation that closes it.
  - **Layer 1 — the path-composition control.** A reserved-prefix rejection (a component may not
    begin with `_`) MUST exist before `_SAFE_TOKEN_RE_PATTERN` is deleted (D4.4).
  - **Layer 2 — the taxonomy control.** `_meta/taxonomy.yaml` is human-written and never passes
    through the plan validator at all, so the reservation is *independently* enforced at taxonomy
    load and by lint **L1-23**: a `domains` entry beginning with `_` is rejected. This is a second
    layer, not a restatement of the first — neither one covers the other's input.

**D1.5 — `_meta/kb.yaml`: a closed key set, and policy is forbidden in it.**

```yaml
# <repo>/_meta/kb.yaml   — git-tracked · closed key set · NO policy
kb_id: "01J8Z…"          # ULID, minted ONCE at `agora repo init`, never rewritten
name: "general"           # display name
declared_kind: personal   # ADVISORY ONLY
```

`kb_id` is a ULID stamped once at repo creation and mirrored into every note's `kb:` frontmatter, so
a note that is copied out still names its origin. **Policy must never live here.** `declared_kind`
is advisory and the *enforcing* value stays `kind` in git-ignored `_kb/repo.yaml`, which is where
`load_harvest_policy` reads both `harvest.*` and `kind` today (`config.py`). A git-tracked,
enforcing `kind` would let an upstream author's `kind: personal` unlock a downstream operator's
personal-scope connectors — converting a **local safety declaration into a remote claim**. For the
same reason `kb_id` is, for any KB not created locally, a **self-claim**: it is display/join
identity, never an authorisation input (see Consequences, residual risk R3). The registry, aliasing
and attach semantics that consume `kb_id` belong to reserved **ADR-0037** and are not decided here.

> **Which reserved federation ADR, and why there are two.** `kb_id` touches two reservations and they
> are deliberately distinct. **ADR-0030** owns outbound **pack composition** — ADR-0027 §7 names it
> the sole COMPOSER, gives it the additive `scopes` parameter, and §9 obliges it to *cite* §8 rather
> than restate it. **ADR-0037** owns the **local, read-only registry**: alias resolution, `kb_id` as
> join/display identity, attach, and result banding. This ADR routes to **0037** everywhere
> (D2.3, OD-5, R3) because everything it defers is registry-shaped and nothing it defers composes a
> pack. The split is recorded in `docs/adr/README.md`'s reservation comments so a future author
> cannot pick the number by proximity.

### D2 — Frontmatter, schema 2

**Common base** (every curator-written note; UTF-8 / LF / no BOM; dates `YYYY-MM-DD`):

```yaml
---
title: <human title>                 # REQUIRED (unchanged)
kind: concept | summary | note | map | entity | index   # REQUIRED; MIRRORS the directory
kb: 01J8Z...                          # REQUIRED; the _meta/kb.yaml kb_id, stamped by APPLY
subjects: []                          # replaces the v1 PATH domain; each value ∈ taxonomy domains
aliases: []                           # unchanged
tags: []                              # unchanged; each must pre-exist in _meta/taxonomy.yaml
created: 2026-09-04                   # REQUIRED; == run_date at first creation (ADR-0010 D1)
updated: 2026-09-04                   # REQUIRED; == run_date on every curator edit (ADR-0010 D1)
status: active | stub | contested | deprecated   # REQUIRED (unchanged)
summary: <one-line precis>            # REQUIRED (unchanged)
derived: false                        # OPTIONAL bool, default false
provenance:
  writers: []                         # AUTHENTICATED principals — TRUSTED
  agents: []                          # agent SELF-DECLARATIONS — RECORDED, NOT TRUSTED
---
```

Per-kind additions carry over from v1 unchanged in shape: `concept` and `summary` add `sources:`
(REQUIRED and non-empty unless `status: stub`), `related:`, `origin:`, `confidence:`,
`body_status:` and the contested triple; `note` adds `date:`, `run_id:`, `sources:`,
`body_status:`; `map` and `index` add `children:`; `entity` adds `sources:` (may be empty while
`status: stub`) and `related:`. The ADR-0014 D2 OKF superset (`description`, deterministic
`timestamp`, `resource`, `okf_version` on the bundle-root index) is unchanged.

**D2.1 — The directory is authoritative; `kind:` is a mirror.** Where the two disagree the
**directory wins** and lint hard-rejects the note. Two reasons the mirror is kept at all rather than
dropped: a note read in isolation (an Obsidian pane, a `kb_read` result, a copied file, an OKF
consumer) must still say what it is; and the frontmatter form is what the OKF mirror in ADR-0014 D2
needs (OD-3). Two reasons the *directory* is authoritative rather than the field: it cannot be
falsified by a brain writing prose, and it is what the reader derives from a path without parsing
the file.

**D2.2 — `subjects:` replaces the path domain, and `[]` is a legal, honest value.** `subjects:` is a
list of zero or more domain tokens, each of which must already exist in `_meta/taxonomy.yaml`
`domains` — [ADR-0010 D6](0010-kb-wiki-schema.md) is preserved exactly: the model still cannot widen
the controlled vocabulary, and a plan naming an undeclared subject is rejected at PLAN validation
before APPLY. **An initial `[]` asserts nothing and loses nothing.**

**What the ADR-0022 no-loss catch-all does now** — three legs, split apart because they were only
ever fused by the path:

1. **The path leg retires.** ADR-0022 §A's `domains[0]` fallback exists because in v1 a note needs a
   *path* and the path needs a *domain*; a fact whose domain could not be resolved had nowhere to
   land, so the floor supplied a possibly-false one. In schema 2 a concept lands at
   `wiki/concepts/<slug>.md` regardless of subject. **Nothing can be dropped for lack of a domain,
   because nothing needs a domain to have a path.** Two sites implement this leg, not one — ADR-0022
   §A's own text says the floor *"unifies with the import lane, which already treats the first domain
   as its catch-all"*, so retiring one without the other leaves the two lanes disagreeing:
   - `adapters/ollama_brain.py` `normalize_plan`'s step-3 `domains[0]` fallback is removed from
     the basename/path derivation;
   - `ingest/vault_import.py`'s catch-all (`moved_dest = f"wiki/{first_domain}/themes/{slug}.md"`,
     `vault_import.py:359`) becomes `wiki/concepts/<slug>.md`, recording the origin folder as a
     `subjects:` entry when it maps to a declared domain and `subjects: []` otherwise — the same rule
     D6 step 2 gives the `--from-kb` lane, so the two importers agree by construction.
2. **The classification leg becomes `subjects: []`.** An unclassifiable capture is filed with an
   empty subject list. This is **strictly more honest** than the v1 floor, which asserted a subject
   that might be false, and it is strictly no-loss: the fact is curated, searchable and linkable.
   ADR-0022's guarantee — *never drop a fact merely for lack of a domain* — becomes true by
   construction rather than by fallback.
3. **The raw shard-key leg keeps `domains[0]`.** `raw/<domain>/<event_id>.md` still needs a
   directory, and `raw/` does not move (D1.4). So `domains[0]` survives **exactly there** and
   nowhere else, which keeps every `sources:` reference derivable and lint L1-8 satisfiable. A
   consequence worth stating: **domains stay ASCII kebab-case tokens** even though they are no
   longer wiki path segments, because they are still `raw/` path segments.

**What carries `subjects:` on the plan wire: nothing new — and that is a decision, not an oversight.**
`Disposition` is `frozen` with `extra='forbid'` and a **singular** `domain: str | None`
(`curator/plan.py`), so a stray or renamed field is a hard parse error; a plan physically cannot
express a two-element `subjects:` today, and giving it one is *by definition* a plan-wire-format
change. This ADR keeps the singular field:

- `Disposition.domain` is **retained unchanged**, with its meaning narrowed to what it still
  determines deterministically — the `raw/<domain>/` shard key (leg 3) — and it **seeds a one-element
  `subjects:`** at APPLY. `subjects: []` arrives via the same field being `None`.
- **PLAN check 4 keeps its current shape:** it grades `disp.domain ∈ domains` exactly as it does
  today. The `subjects ⊆ domains` property is asserted on the *materialised note* by L1-5's
  successor, not on the plan — one rule, one input, one verdict.
- **0..n subjects are an APPLY-and-human capability on day 1, not a model capability.** A curator run
  writes at most one subject; a human editing frontmatter, and the `--from-kb` merge in D6 step 4,
  may write more. That asymmetry is deliberate: widening a *model-facing* wire format is the
  expensive, irreversible half.
- The alternative — `Disposition` gains `subjects: tuple[str, ...]` — is real and cheap to adopt
  later, but it **bumps `curator/plan.py` `SUPPORTED_SCHEMA_VERSIONS`** and would falsify the
  "envelope NOT bumped" line in the Schema-version-impact section. The two version axes being
  *independent* (`config.py:255-268`) means neither implies the other; it does not mean neither
  moves. Recorded as open sub-decision **OD-9**.

**D2.3 — `provenance.writers` vs `provenance.agents`.** Two lists, deliberately not one. `writers`
holds authenticated principals and is trusted; `agents` holds agent self-declarations and is
recorded but never trusted. Without this split the custody claim Agora actually makes —
*"a system of record for what your agents learned"* — is false, because an unauthenticated
self-declared agent name would be indistinguishable from an authenticated one. `origin` is
unchanged and keeps ADR-0010 D4's frozen enum verbatim (an `agora:<kb_id>` origin for attach-import
belongs to reserved ADR-0037 and is **not** added here — this ADR widens no enum it does not have to).

**D2.4 — `derived: bool`.** Marks output of a proposal/derivation plane rather than a curated
claim. Day-1 semantics: a `derived: true` note is **excluded from gold packs** and is **never a
`MERGE_INTO_THEME` target**. No day-1 producer sets it; it is defined now so ADR-0040 and the
proposal plane do not have to invent a competing marker.

**D2.5 — `type:` is retired, and the v1 mapping is frozen.**

| v1 `type:` | schema-2 `kind:` | schema-2 path |
|---|---|---|
| `theme` | `concept` | `wiki/concepts/<slug>.md` |
| `daily` | `note` | `wiki/notes/<yyyy>/<mm>/<slug>.md` |
| `moc` | `map` | `wiki/maps/<slug>.md` |
| `index` | `index` | `index.md` (root, unchanged) |
| — | `summary` | `wiki/summaries/<slug>.md` (new; no v1 antecedent) |
| — | `entity` | `wiki/entities/<slug>.md` (new; no v1 antecedent) |
| — | `person` (derived, never authored) | `wiki/people/<person>/**.md` |

"Retired" means precisely: `type:` is no longer read by any Agora code, is no longer in any required
field set, and is no longer validated against a closed vocabulary. Whether a `type:` key is still
*emitted* as an OKF mirror of `kind` — the way `description` already mirrors `summary` under
ADR-0014 D2 — is open sub-decision **OD-3**, with a recommendation to emit it.

**D2.6 — `notes/` cardinality: one journal per `run_date`, repo-wide.** v1 produced one daily per
domain per run and namespaced the basename `<domain>-YYYY-MM-DD` for one stated reason: *"bare dates
would collide across domains"* (ADR-0010, folder rules, verbatim). With the domain out of the path
that reason is gone. Schema 2: `APPEND_DAILY` writes **one** note per `run_date`, basename
`<yyyy>-<mm>-<dd>`, at `wiki/notes/<yyyy>/<mm>/<yyyy>-<mm>-<dd>.md`, with one `## ` section per
`needs_prose` disposition (the §3.1 rule, unchanged). The consequence: the note↔`run_id` relation
becomes 1:1, which makes the L1-14 successor a clean identity check instead of a per-domain fan-out.
Per-subject journals, if ever wanted, are open sub-decision **OD-4**.

**The basename-uniqueness exemption is KEPT, and it is not the one an earlier draft named.**
DATA-MODEL §10 states global basename uniqueness with no daily exemption in it; the exemption that
actually exists is **[ADR-0011](0011-curator-ingest-contract.md) §4.1 check 5's `(daily exempt)`**
(`curator/plan.py`: `if disp.op in _DAILY_OPS: continue`), and **it must not retire.** Check 5 guards
**pre-existence**, not cross-domain collision: it rejects a new basename already present in the live
worktree. One journal per `run_date` removes collisions *between* domains but changes nothing about
the second `agora curate` of the same day, whose `APPEND_DAILY` names a basename that is by then
already on disk. Retiring the exemption would make every same-day re-run a hard BASENAME failure.
What one-journal-per-`run_date` *does* buy is that the exemption is no longer load-bearing for
uniqueness — a dated basename is unique by construction — only for idempotent re-entry.

**The `<yyyy>/<mm>` shard is derived from `run_date`, never parsed out of a model-supplied
basename.** `_implied_note_path(disp)` takes only the `Disposition` today, and `validate_plan` is
injected with `manifest_event_ids`, `allowed_tags`, `domains`, `live_basenames`, `theme_basenames`
and `gated_candidate_ids` — **no date**. Parsing the shard back out of `disp.basename` would make a
curator-owned, deterministic path segment a function of model output, which is precisely what D1.1's
*"MUST equal the frontmatter `date:`"* rule exists to prevent. So both gain `run_date` as one more
injected deterministic fact (it is already in the run manifest and already reaches lint as
`run_date`), and the PATH/ALLOWLIST check composes the whole path — shard **and** basename — from it.
The check then additionally asserts `disp.basename == run_date`, so a mismatched basename is a PLAN
rejection rather than a note that lints clean in the wrong month.

### D3 — The rules

**D3.1 — Directory is kind, and the three new rules get numbers.** For any note under `wiki/`,
segment index 1 is its kind; the authoritative kind set is
`{concepts, summaries, notes, maps, entities, people}` plus root `index.md`. A directory under
`wiki/` that is not in that set is a hard lint reject — the kind vocabulary is closed *at the
directory level*, which is what makes adding a kind an explicit, reviewable act rather than a side
effect of a model inventing a folder.

Schema 2 introduces three hard rules that v1 had no rule number for. They are **numbered here**, and
appended to ADR-0010's L1 table through that ADR's banner, so no rule enters the ruleset as unnumbered
prose:

| id | rule | replaces |
|---|---|---|
| **L1-22** | a note under `wiki/` whose segment-1 directory is not in the closed kind set | new (D3.1) |
| **L1-23** | a `_meta/taxonomy.yaml` `domains` entry beginning with `_` (the `raw/` reserved-prefix namespace) | new (D1.4 layer 2) |
| **L1-24** | a map `children:` bullet whose child's kind is not in D1.3's admitted set | v1's *unenforced prose* daily prohibition (ADR-0010, folder rules) |

Two numbering facts that must not be lost. **L1-20 exists in code** (`schema/lint.py`, body-sentinel
integrity, ADR-0011 §4.4 check 6) but is **absent from ADR-0010's L1 table**, which stops at L1-19 —
so the table is already one rule behind the implementation and this ADR's banner adds it. **L1-21 is
pre-reserved** by `schema/lint.py`: *"Promote to a hard `L1-21` error ONLY AFTER `agora repo upgrade`
(#63) can perform the one-shot repair"* (the L2-6 promotion). It **must not be reused here**, which is
why the new rules start at L1-22.

**D3.2 — Domain lives in `subjects:` only.** There is exactly one place a subject is recorded. No
code derives a subject from a path in schema 2. Three call sites implement this and all three change
together: `schema/lint.py` `_note_domain`, `core/wiki.py` `_moc_domain`, and
`faces/mcp_server.py` `_wiki_domain` (which feeds `browse`, `graph` and the graph face's domain
filter). Their replacements read `subjects:`.

**D3.3 — `people/` is human-owned, read-first-class, and outside invariant 2's subject.**

> **Invariant 2 ("all writes go through the inbox; only the curator writes `wiki/`") governs the
> CURATED wiki. `wiki/people/**` is a human-owned namespace outside it.**

That sentence is normative and this ADR states it because the invariant's wording otherwise reads as
covering every byte under `wiki/`. Concretely:

- The **curator never writes `wiki/people/**`.** It is carved *out* of the ADR-0011 §4.0 allowlist
  (D4.1): any add, modify, rename or delete under it in a curated diff **fails the run**. This is a
  stronger guarantee than v1's, where the tree simply did not exist.
- Lint treats people notes as **advisory**, and the mechanism is named so the guarantee is testable:
  `wiki/people/**` is **permanently excluded from the graded population inside `lint()` itself** —
  the notes are still parsed and still enter the link-resolution universe, they are simply not graded
  as producer artefacts. It is **not** a severity downgrade: `lint()` grades every rule at its
  declared severity and an L1 finding flips `LintResult.ok`, so a "warn instead of error for this
  subtree" lever would have to be invented, and inventing one puts a second, subtree-keyed severity
  axis into a module whose whole value is that it has one. The exclusion lives in `lint()` rather
  than in a caller-supplied argument for the same reason: **every caller must behave identically**.
  The curator, the dashboard, `kb_status`, `/metrics` and `health()` all call the same function, and
  a lever only the curator passes would leave the read-only surfaces reporting a red KB for a file
  the curator is forbidden to fix. What the dashboard shows for a malformed people note is therefore
  **nothing in the L1 verdict** — it is surfaced, if at all, as an L2 health signal, which is soft by
  construction and never gates a run. The tolerant-consumer posture of ADR-0014 D1/D4 governs them.
- **People basenames do NOT join the global basename identity space.** They are excluded from
  `live_basenames` (`curator/worker.py`: `{n.basename for n in notes}`, fed to PLAN check 5) and from
  L1-1's duplicate check and L1-15's alias/basename union. Without this exclusion a human file at
  `wiki/people/hando/agora.md` makes `CREATE_THEME basename=agora` a **hard PLAN failure** — a
  human-owned tree acquiring veto power over curator naming, which is the exact inverse of
  "human-owned, curator never touches it". The consequence is stated rather than discovered:
  **a people note is addressed by path, never by `[[basename]]`.** A `[[ ]]` link *into* `people/`
  does not resolve and is an L1-2 broken link; the curator may not author one anyway (D1.3's `person`
  row). Links *out of* a people note are ungraded — people notes are not in the graded population —
  so a human's link to a concept that later disappears is a graph gap, never a run failure.
- **Read is first class.** `Wiki.query`, the graph face, the web face and the MCP read tools index
  and return people notes like any other note; their derived kind is `person`. This buys back what
  the "one repo, two namespaces" arrangement was paying for safety with — a human's own writing was
  invisible to search.
- A person's contribution reaches the *curated* tree by exactly one route: a `file:` connector →
  the ADR-0007 candidate gate → `MERGE_INTO_THEME` / `MARK_CONTESTED` / `DROP`. It can never
  originate. **That route needs a fence, because it is the first in-repo connector read.** ADR-0017/
  0018 connectors read *source* files outside the repo; a connector glob pointed at `wiki/people/**`
  is an in-repo read, and nothing stops the same glob from also covering `wiki/concepts/**` — which
  would feed the curator's own output back as candidates, the reworded-loop risk ADR-0027 §8 leaves
  explicitly open. So: **a `file:` connector on a path inside the repo may cover `wiki/people/**` and
  nothing else.** Any other `wiki/` or `raw/` path under a repo-internal glob is skipped with a note,
  mirroring the existing `_is_within_gold` guard (`harvester/connectors.py`, the ADR-0027 §8 path
  exclusion), and `agora doctor` reports the exclusion the way it already reports the gold one.
- **Day 1: `wiki/people/**` is EXCLUDED from gold packs and from `kb_context`.** The outbound
  redaction boundary for a human-owned read corpus is undesigned (stratum note §9): the ADR-0023
  connector-boundary redaction and a *read-corpus* boundary are not the same boundary, and shipping
  people content into every session's standing context before that is designed would be an
  unreviewed egress. This exclusion is a **default, not a permanent rule**; lifting it requires the
  boundary design, not a config flag.
- **The exclusion does NOT cover the MCP read tools, and that gap is recorded rather than papered
  over.** `kb_query` / `kb_read` / `kb_neighbors` return people content to an agent on demand, so by
  ADR-0027 decision 8's own words — *"this section is the single normative spec for every Agora→agent
  emission path"* — they are an emission path that §8 currently does not name. The owner's decision is
  that read stays first class here: a pull-shaped, agent-initiated read of a note the human filed in
  a shared repo is a different risk from a push-shaped standing pack assembled without a prompt, and
  gutting agent read would forfeit the entire reason the tree lives inside the repo. What follows is
  a **scope amendment, not a carve-out**: §8's scope sentence is AMENDED to name the MCP read tools
  as an emission path whose control is **distinct and still undesigned** (the ADR-0027 triage row
  records this; residual risk **R1** owns it). The gold/`kb_context` exclusion is the control for the
  *push* surface only, and this ADR does not claim it as the control for the pull surface.

**D3.4 — `raw/` never moves.** Restated as a rule because it is the constraint most likely to be
"simplified" later: re-pathing `raw/` rewrites the `sources:` chain of every note in every repo and
invalidates every `raw/` reference lint L1-7/L1-8 validates. See D1.4.

**D3.5 — OpenKB byte interop is deliberately not a goal.** Field names and JSON shapes may stay
recognisable, but a long document's `full_text:` points outside `wiki/` (`raw/_pages/`), so an
OpenKB client cannot read an Agora repo directly. The interop path is **export**, not cohabitation:
[`STRATEGY-2026-08.md` §10](../STRATEGY-2026-08.md) forbids cohabitation in one repo on blast-radius
grounds, and that finding is already **ADR-normative** rather than merely strategic — the ADR-0005
T0–T4 addendum (commit `66fa455`, #157) assigns *VectifyAI/OpenKB (Apache-2.0) → **T0**, document
feed only, cohabitation forbidden*, on the same evidence. This ADR carries it forward unchanged. `agora export` does not exist yet; it is named
here as the interop contract and is open sub-decision **OD-5**.

### D4 — The integrity boundary v2 items this ADR authorises

These are the *layout-defined* half of gate A. The layout-invariant half already shipped (PR #160).

**D4.1 — One allowlist constant, with a `wiki/people/` carve-out.** `curator/constants.py` remains
the single source of truth read by both the PLAN check (`curator/plan.py`) and the final-diff
assertion (`curator/worker.py`) — ADR-0011 §4.0's "defined once and referenced by every check" is
preserved. It gains one **exclusion** constant: a path under `wiki/people/` is **not** allowlisted
even though it is under `wiki/`. `is_allowlisted_path` becomes prefix-allow **minus** the exclusion,
and both gates get it for free, which is exactly why it must be one constant and not two checks.

**D4.2 — The `raw/_blob/` capture channel, and the transport that feeds it.** APPLY may write
`raw/_blob/<ab>/<sha256>.<ext>` and its sidecar, recording both in `raw_writes` (now
`dict[str, bytes]`, PR #160). Admission is unchanged in *rule*: membership in `raw_writes` **and**
byte equality. Content-addressing adds a self-check and removes nothing (D1.4, normative paragraph).

**Where the bytes come from is a decision, and this ADR takes it — the destination alone is not a
channel.** Today there is no path by which original bytes reach APPLY: `Inbox.write` takes
`text: str` (*"the knowledge body (or an extraction summary of `raw_ref`)"*) plus an optional
`raw_ref: str`, and `_materialize_raw_source(worktree, ref, body, …)` writes **the event body** and
records `raw_writes[ref]`. A face that extracts a PDF to markdown discards the PDF. So:

- **The inbox item gains an optional ATTACHMENT, written beside the event in the writer's own
  append-only namespace** (`_kb/inbox/<writer>/<id>.blob`), with `raw_ref` naming the
  `raw/_blob/<ab>/<sha256>.<ext>` destination the bytes are destined for. The item stays immutable
  and per-writer-namespaced (invariant 3, untouched); the attachment is written once, with the event,
  by the same face call.
- **APPLY reads the attachment at claim time** and materialises it under `raw/_blob/`, exactly as it
  materialises a free-text capture today, recording the bytes in `raw_writes`. The curator remains
  the sole writer of `raw/` (ADR-0020 decision 3, verbatim).
- **This is a DATA-MODEL §1 amendment** — the inbox-item shape changes — and it is listed as such
  under the triage table. It also narrows the ADR-0020 row's claim: the *routing* is unchanged (faces
  still extract→inbox; the curator still owns `raw/`), but "the write path itself is unchanged" is
  **not** true of the item shape, and an unchanged item shape cannot deliver bytes.
- **Rejected alternative:** a face-side staging area outside the inbox that APPLY reads by `raw_ref`.
  It avoids the §1 amendment but splits one delivery into two independently-failing writes, so a
  crash between them leaves an event citing bytes that do not exist — reintroducing exactly the
  partial-delivery class the append-only single-item write was chosen to eliminate.

**D4.3 — The `raw/_pages/` admission class.** Reserved prefix; no writer on day 1; no gate
exception. Plus the `_blob`/`_pages` reserved-domain-name rule (D1.4).

**D4.4 — The pathsafe swap, and its slugger mirrors.**

- `curator/plan.py:92` `_SAFE_TOKEN_RE_PATTERN` (the *validator*) is replaced by
  `core.pathsafe.is_safe_component`. The closed-set property that made the regex a safe
  path-**escape** control is preserved, because pathsafe is a Unicode-**category allowlist**, not a
  denylist: separators, NUL, C0/C1 controls, bidi overrides, zero-width characters, the fullwidth
  solidus and the Windows-hostile `<>:"|?*` are all unreachable *without being enumerated*, and a
  codepoint added by a future Unicode revision is excluded by default. Pathsafe additionally
  rejects the Windows reserved device stems (`CON`, `COM0`–`COM9`, `LPT0`–`LPT9`, including the
  superscript forms) that today's ASCII regex admits — a net **tightening** on that character class,
  shipped alongside the widening.
- **NORMATIVE — the swap must not ship without a leading-`_` rejection.** The tightening above is
  real but it is *not* uniform: on the reserved-prefix character the swap is a **loosening**.
  `pathsafe` puts `_` in `_EXTRA_ALLOWED` and rejects only a leading `.`, so `is_safe_component`
  returns `True` for `_blob`, `_pages` and `_kb`, every one of which the ASCII regex rejects
  (verified against commit `5ba7bd5`). A reserved-prefix rejection — **a path component may not begin
  with `_`** — MUST therefore be in place **before** `_SAFE_TOKEN_RE_PATTERN` is deleted, either
  inside `core.pathsafe` or at the plan/APPLY path-composition site. Placing it in `pathsafe` is
  preferred (one control, both callers); placing it at the composition site is acceptable; shipping
  neither is not. The D1.4 taxonomy rule (L1-23) is a **second layer over a different input**, not a
  substitute — a plan token never passes through taxonomy load, and a taxonomy entry never passes
  through the plan validator.
- The **producers** mirror it: `adapters/ollama_brain.py:340` `_slugify` and
  `ingest/vault_import.py:303` `_slugify` swap to `core.pathsafe.safe_slug_component`, lowercasing
  *before* the call so ASCII inputs stay byte-identical to today's output.
- **The `note-<sha8>` floor (#57) is kept as the last resort.** `safe_slug_component` returns `""`
  on total rejection precisely so that fallback still fires; it fires far less often, because a
  Korean title now yields a Korean component instead of `""`.
- **Unicode slug rules** are exactly `core/pathsafe.py`'s: NFC-normalise; keep letters, numbers and
  combining marks (`unicodedata.category(ch)[0] in ("L","N","M")`) plus `-`, `_`, `.`; collapse
  every other codepoint to a single `-`; trim leading/trailing `-` and trailing spaces/dots to a
  fixed point; reject a leading `.`; reject Windows reserved stems; cap at 180 UTF-8 **bytes** with
  codepoint-safe truncation, then re-trim and re-check. Case is **not** folded (lossy,
  locale-sensitive, and no collision benefit on case-insensitive filesystems). Homograph
  confusables are a **stated residual**: Cyrillic `а` and Latin `a` are both `Ll` and both survive.
- **NFC applies to the COMPARISON side too, not only to the composer.** `pathsafe` NFC-normalises
  what it *writes*; L1-1 (duplicate basename), L1-15 (alias/basename union) and L1-2 / the markdown
  link resolver compare basenames as **raw strings**. macOS hands back NFD from a directory read while
  the composer emitted NFC, so the same Korean or accented name would compare unequal to itself:
  a duplicate that L1-1 cannot see, and a `[[ ]]` target that L1-2 calls broken. So basename identity,
  alias uniqueness and link resolution all compare **NFC-normalised** strings. This is a widening of
  where an existing idiom applies (`core/hashing.py`, `core/wiki.py` already NFC-normalise), not a new
  rule, and it is byte-inert for ASCII.
- **A behaviour change the swap causes elsewhere, recorded so it is not discovered as a bug:
  Korean aliases stop being skipped.** Aliases run through the same slugger
  (`adapters/ollama_brain.py`: `alias = _slugify(raw_alias)`; empty → `aliases_skipped_unslugifiable
  += 1`), so after the swap a Korean alias slugifies non-empty and is **preserved** rather than
  skipped. ADR-0022's #57 addendum §2 chose skip-and-count explicitly because *"preserving Korean
  aliases verbatim would require widening the closed alias/basename token grammar"* — D4.4 removes
  that blocker, so the intended new behaviour is preservation, and
  `aliases_skipped_unslugifiable` becomes a **residual counter** (still correct, now rarely non-zero)
  rather than the common path. The addendum's *other* rejection (transliteration tables) is untouched.
- **What stays ASCII:** `core/layout.py` `_WRITER_RE`, and everything derived through it — writer
  namespaces (`_kb/inbox/<writer>/`), harvest cursor stems, gold pack names and reader-cache stems.
  These are not content-derived: they come from the closed ASCII vocabularies of the DATA-MODEL §1
  `source` enum and the §8 `adapters.yaml` connector keys, so widening them buys no no-loss and
  changes `_kb/` filenames operators touch. `subjects:`/domain tokens likewise stay ASCII kebab
  (D2.2, leg 3). The known consequence — a repo *directory* named `내지식` cannot address a reader
  cache (#108) — is untouched by this ADR and stays a documented degradation (OD-6).

### D5 — Ranking: how the structural term is seeded when maps replace `<domain>-moc.md`

**The replacement rule for `_is_moc_path`.** Today:

```python
def _is_moc_path(path: str) -> bool:
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "wiki":
        return False
    return parts[2] == f"{parts[1]}-moc.md"      # wiki/<domain>/<domain>-moc.md
```

Schema 2: a note is a map iff `parts[0] == "wiki" and parts[1] == "maps" and len(parts) >= 3`. The
name suffix `-moc` disappears entirely — the kind marker moved from the filename into the directory,
which is the whole point of the flip. `_moc_domain` is replaced by a `subjects:`-reading accessor.
`core/gold.py` imports `_is_moc_path` for the same seeding and changes with it — one shared edit,
not two.

**`CACHE_SCHEMA_VERSION` 2 → 3 is REQUIRED.** `core/index_cache.py`'s own contract is normative and
decides this: *"`CACHE_SCHEMA_VERSION` MUST be bumped whenever the serialized `_Note` shape, the
tokenizer, or the parser changes — the cached … values are DERIVED, so a parser change must
invalidate a cache whose per-file `source_digest` still matches."* Both triggers fire. `is_moc` is
**parser-computed and serialized**: `_parse_note` sets `is_moc = _is_moc_path(path)`, `_Note` carries
it as a field, and the payload writes `"is_moc": note.is_moc` and reads it back — so a cache entry
whose file content never changed would keep a v1 `is_moc` verdict forever. And `_Note` **gains a
`subjects`-reading accessor**, which is a serialized-shape change in its own right. The fresh-repo
argument does **not** cover this: because `SUPPORTED_KB_SCHEMA_VERSIONS` becomes `{1, 2}`, a
schema-1 repo with a populated `_kb/index/` is reachable by the schema-2 build (that is the whole
point of keeping 1 in the set), which is exactly the stale-cache case the bump exists for.

Everything downstream of the seed keeps its ADR-0012 definition **verbatim**: level-0 = maps and
their direct children (`d_moc = 0`); level-1 = root `index.md` and its children (`d_moc = 1`);
BFS to `max_hops = 2` recording MIN distance; `d_moc = 3` for lexically-matched unreachables;
`struct = 0.7 · 1/(1 + d_moc) + 0.3 · indeg_norm`; `W_LEX`/`W_STRUCT`/`FLOOR` unmoved; the multi-seed
label-union rule unmoved; and the #146 addendum's conditionality — the structural term contributes
**only when `lex > 0`** — unmoved.

**The domain in-scope filter is re-expressed, not dropped.** ADR-0012 stage 1 seeds a
`<domain>-moc.md` only if `<domain>` is in `repo.yaml domains:`, and narrows to one MOC when a
question token exactly matches a domain name. Schema 2: a map's subject scope is read from its own
`subjects:` frontmatter; a question token exactly matching a declared subject seeds only maps whose
`subjects:` contain it; otherwise all maps are seeded. This is a frontmatter read replacing a path
read and nothing else.

**What genuinely moves, stated rather than hoped.** In v1 the number of level-0 seeds is bounded by
`|domains|` (one MOC each). In v2 `wiki/maps/` may hold arbitrarily many maps, so the seed
population, the `d_moc` distribution and `indeg_norm`'s denominator can all change **without any
scoring constant changing**. That is a measurement, not an assertion, which is what gate B is for.

**Requirement on the flip PR (normative), scaled to what the fixture actually measures.** The flip
PR **MUST** re-run `python -m tests.rank_golden.regen` and **attach the `diff_snapshots` listing**
produced by `agora_kb.core.rank_snapshot.diff_snapshots(before, after)`. It must **not** be required
to justify the listing line by line: the fixture has already measured this exact mutation —
`tests/rank_golden/README.md`'s coverage table records *"`_is_moc_path` always `False` (**the flip,
simulated**) | **347 / 347** | the whole set"*, i.e. the whole recorded corpus moves in both `fm`
columns. A per-line obligation over ~694 lines is one nobody performs honestly, and an unperformable
gate is a gate that gets waived. The obligation is therefore:

- **explain the listing per CATEGORY** — seed-population change, tie-break artefact annotated
  `(score unchanged)`, and `match_reason` boundary crossings — with a count per category that sums
  to the listing;
- **explain per LINE** only (a) every `match_reason` change and (b) every rank move **not** annotated
  `(score unchanged)`, since those are the moves where a score actually changed;
- **cite the 347/347 baseline**, so a materially larger listing is itself a signal that something
  beyond the seed rule moved.

Three fixture facts make that reviewable and must be honoured:

- the record is **basename-keyed**, deliberately, so it survives a change that renames every path;
- 7 recorded hits (13 in the `fm_off` column) sit in exact score ties broken by `_order_key`'s
  `note.path` tail, and the flip changes every path, so some rank swaps **will** be tie-break
  artefacts — `diff_snapshots` annotates a rank move whose score did not change with
  `(score unchanged)`, and the explanation must use that annotation rather than hand-waving;
- the **pre-flip records must be preserved** (kept alongside, not overwritten in place before the
  listing is taken), because `regen` can only produce the *current* layout and a discarded baseline
  cannot be re-derived.

Preserving them is not free, and the mechanical change is named here so it is not rediscovered mid-PR:
the golden artefacts are **named for the v1 layout and hardcoded** — `regen.py` pins
`GOLDEN_FM_ON = HERE / "golden_v1.json"` / `GOLDEN_FM_OFF = HERE / "golden_v1_fm_off.json"`, the
fixture README defines the record as pinning *"the **v1 wiki layout**"*, and `regen` is *"the **only**
way `golden_v1.json` and `golden_v1_fm_off.json` are produced"*. So the flip PR **adds
`golden_v2.json` / `golden_v2_fm_off.json` and the corresponding `regen.py` / `test_golden.py`
constants**, keeping `golden_v1*.json` committed as the pre-flip baseline the listing is taken
against. `tests/support/kb_builder.py` gains a schema-2 build mode; `tests/rank_golden/corpus.py` is
content-only and needs no change, which is why it was written that way.

An empty listing is a red flag, not a green one: the flip *should* move something, and a listing
with no lines means the seed rule did not actually change.

### D6 — Migration: a clean break, and the one command that crosses it

**`SUPPORTED_KB_SCHEMA_VERSIONS` becomes `{1, 2}` — never `{2}`.** The owner's two live KBs are
schema 1 and `agora repo upgrade` (#63) does not exist, so a build that dropped 1 would strand them.
The set is deliberately a set rather than a ceiling for exactly this case (`config.py:268` says so).

**There is no in-place migrator.** No `agora repo upgrade`, no dual-layout reader, no compatibility
shim in `core/wiki.py`. The migration and back-compat budget (~2–3 person-months) is not spent, on
the strength of a fact that will never be true again: there are no released users.

**New build on a schema-1 repo: reads work, writes refuse.** This *hardens* DESIGN §10 V9's
"new binary on an old repo = read-works / **write-warns**" to a refusal, for this bump only, and the
reason is that the V9 warn posture assumes a write that is merely *suboptimal*. Here it is
*corrupting*: APPLY would write schema-2 paths and schema-2 frontmatter into a schema-1 tree,
producing a repo that is neither, that no lint ruleset can gate, and whose damage is a commit. So:

- `agora query` / `status` / `browse` / `doctor`, the MCP read tools, and the web read routes
  **work** on a schema-1 repo.
- `agora curate` / `watch` / `requeue`, `kb_curate`, and the **inbox write path** (`kb_remember`,
  web upload) **refuse**, with one message naming `agora import --from-kb`. Inbox writes refuse
  rather than succeed because an inbox that can never drain — and that a re-import into a *new* repo
  would orphan — is silent data loss dressed as success.
- `agora doctor` stays the diagnostic exemption it already is.

**The seam is new, and it is named here so the refusal is testable rather than aspirational.**
Today's gate is one all-or-nothing call per *entry point* — `guard_repo_schema_version` /
`assert_supported_kb_schema_version` raise only when `version not in SUPPORTED_KB_SCHEMA_VERSIONS`,
and their call sites are `cli.py:440`, `cli.py:1498`, `faces/web/app.py:429` (inside `build_app`) and
`faces/mcp_server.py:1023` (server construction). With the set widened to `{1, 2}` every one of those
**passes** for a schema-1 repo, and a construction-time guard structurally cannot let `kb_query`
through while refusing `kb_remember` on the same server object. So:

- **New predicate:** `config.assert_writable_kb_schema_version(cfg, *, repo=None)`, which requires
  `version == MAX_SUPPORTED_KB_SCHEMA_VERSION` (not mere membership), raising a new
  `ReadOnlySchemaVersionError(UnsupportedSchemaVersionError)` so a caller can distinguish
  *"this build cannot read your repo"* from *"this build will not write your repo"*, and so existing
  `except UnsupportedSchemaVersionError` handlers keep working.
- **Call sites (exhaustive, per-write-path, not per-entry-point):** `agora curate`, `agora watch`
  and `agora requeue` in `cli.py`; `Inbox.write` itself — one call covers `kb_remember`, the web
  upload route, and every future writer, which is why it goes there rather than at each face; and the
  `kb_curate` MCP handler.
- **Not added to:** `guard_repo_schema_version`, whose read-side membership semantics stay exactly as
  they are, and `agora doctor`, which keeps its diagnostic exemption.

**`agora import --from-kb <v1-repo> <dest>` is the only crossing.** It is a converter, not a
migrator: it reads a schema-1 repo and writes a **new** schema-2 repo, never mutating the source
(the existing `agora import` contract: "source vault … NEVER modified"). Its rules:

1. `type:` → `kind:` per the D2.5 table; the note moves to its kind directory.
2. The **path domain becomes `subjects: [<domain>]`** — *not* `[]`. The v1 path domain is a genuine
   curator assertion and discarding it would lose information the flip is supposed to preserve.
   `[]` is the initial value for *new* unclassifiable notes (D2.2), not the import default.
3. `wiki/<domain>/<domain>-moc.md` → `wiki/maps/<domain>.md`, basename `<domain>` (the `-moc`
   suffix was the kind marker in the name and the kind is now the directory). Every
   `[[<domain>-moc]]` in `related:`/`children:` and every body link to it is rewritten.
4. `wiki/<domain>/daily/<domain>-YYYY-MM-DD.md` → `wiki/notes/<yyyy>/<mm>/<yyyy>-<mm>-<dd>.md`.
   **Same-date dailies from different domains MERGE into one note** (D2.6): their `## ` sections are
   concatenated in domain order, `sources:` is unioned, `run_id` is taken from the first, and each
   merged section keeps its origin domain as a `subjects:` entry on the merged note.
5. **`raw/` is copied byte-identically and `sources:` strings are NOT rewritten.** This is the
   payoff of D3.4 and the single largest reason the conversion is cheap.
6. `_meta/kb.yaml` is minted at the destination with a **new** `kb_id` (the destination is a new KB,
   not a continuation), and every note is stamped with it.
7. Basename collisions introduced by the conversion (a map named `<domain>` colliding with a concept
   of the same name, or two same-date dailies that fail to merge) are a **hard failure with a named
   list** — never a silent rename. A converter that renames silently is a converter that loses
   `[[basename]]` edges.

> **Rule 2 carries one shipped exception**, recorded append-only in the *D6 rule 2 as shipped*
> addendum at the end of this ADR: a path domain the **source taxonomy no longer declares** converts
> to `subjects: []` **with a per-note warning**, because writing it would mint a repo its own lint
> L1-5 rejects. Rule 2's text below is normative for every domain the taxonomy does declare.

**The owner's two KBs are re-imported once.** Re-import, not re-curation: the dogfood `raw/` is
byte-identical to the owner's own prose, so regenerating the wiki through a model would paraphrase
the owner's writing. Measured cost: one to two days.

**#156 becomes unnecessary.** Issue #156 (materialise `domain:` into every note's frontmatter) exists
to close the point of no return *before an in-place flip*, because an in-place flip destroys the path
before anything else records the subject. A converter reads the path **and then writes a new tree**,
so the information is never destroyed at all — the subject survives into `subjects:` in step 2. #156
is closed as unnecessary, not deferred.

---

## ADR triage — every affected decision, verified against its text

`KEEP` = unchanged. `AMEND` = a named clause changes, body preserved append-only.
`SUPERSEDE` = a named section is replaced.

| ADR | Verdict | Precisely what |
|---|---|---|
| **0002** CQRS / single writer | **AMEND** | This is the ADR that actually *states* invariant 2's write clause — *"**Exactly one curator process per repo** reads the inbox and edits the shared `wiki/`, indexes, and `log.md`"* — so D3.3's narrowing has to be recorded here, not only against the invariant's prose. The clause is **re-read as governing the CURATED wiki**, with `wiki/people/**` excepted: exactly the shape of this ADR's own 2026-07-29 spool-custodian appendix (#99), which re-read the same clause as *"one mutator of the event spool"*. The reason the exception is safe is the reason 0002 gives for the rule: its Context is *"file-level 'last write wins' loses data when two writers touch **the same shared file**"* — and no curator write ever touches a file under `wiki/people/**` (D4.1 makes that a run failure), so the two writers never share a file. Single-writer over `wiki/` (curated), `index.md`, indexes and `log.md`: **unchanged**. |
| **0006** repo = tenant | **AMEND** | §Decision's *"optional per-domain ACL (`wiki/<domain>`)"* loses its path basis — there is no `wiki/<domain>` subtree to scope an ACL to. ADR-0036 already demoted that ACL to a convenience filter (#55); this records that its *mechanism* is now gone too. Repo-as-boundary itself: **unchanged**. |
| **0008** sandboxed curation | **AMEND** | §4's allowlist (`wiki/`, `index.md`, `log.md`, schema-approved content paths), quoted verbatim by ADR-0011 §4.0, gains the `wiki/people/` **exclusion** (D4.1). Transaction, sandbox and step ordering: **unchanged**. **One further clause is affected and must not be left implicit: §"Read-after-publish".** Its post-publish sync is `--ff-only` and *"never clobbers a dirty/diverged owner tree, so a failure leaves the publish durable in git but the working copy stale"*, surfaced as *"an observable run-report signal"*. A human editing `wiki/people/**` in Obsidian leaves the owner tree **dirty by design**, so under schema 2 that best-effort fast-forward would fail routinely and a signal meant for an anomaly would fire on the normal workflow. **Rule:** the read-after-publish sync tolerates a working tree dirty **only** under `wiki/people/**` — it proceeds, and the stale-worktree signal is raised only for dirt outside that subtree. The human is under no obligation to commit their notes for a curator run to publish cleanly. The CAS base stays `branch_commit`, unchanged. |
| **0010** wiki schema v1 | **SUPERSEDE (in part)** | **Superseded:** the *"Note types (exactly four)"* table, the *"Folder & naming rules"* block, the `type:` line of the frontmatter spec, and the `moc`/`daily` per-type frontmatter blocks. **Amended (D-level):** D3 (`raw/` write class gains `_blob`; the `raw/<domain>/<inbox-id>.md` shape is unchanged), D5 (the child-bullet *grammar* is unchanged; the admitted *child set* changes — D1.3), D6 (taxonomy stays a fixed read-only input; L1-5's domain check moves from path-derived domain to `subjects:`). **Kept verbatim (D-level):** D1 (deterministic run clock), D2 (derived-only `orphan`/`stale`), D4 (frozen `origin` enum), and the heading-slug anchor algorithm. **The L1 ruleset, rule by rule — the enumeration is exhaustive over L1-1…L1-20, because a partial one is how a rule silently keeps a retired predicate.** *Amended:* **L1-4** (required frontmatter is keyed on `kind`, not `type`, and the common base gains a REQUIRED `kb:`; the per-kind sets are D2's), **L1-5** (domain source moves from path to `subjects:`), **L1-6** (predicate widens from `type in (moc, index)` to `kind in (map, index)`; the set-equality check itself is unchanged), **L1-7 / L1-8 / L1-8b / L1-10 / L1-19** (all five are gated on `if n.type == "theme":` in `schema/lint.py` today; the predicate becomes `kind in {concept, summary}` — `entity` is **excluded** from L1-7's non-stub-needs-sources rule, because D2 lets an entity carry empty `sources:` while `status: stub`, and an entity has no day-1 producer to satisfy the rule anyway), **L1-9** (people carve-out), **L1-11** (`type` → `kind`, cross-checked against the directory), **L1-14** (daily date rule → note date/shard rule). *Kept verbatim:* **L1-1 / L1-2 / L1-3** (with the D3.3 rider that people basenames are outside the identity space), **L1-12**, **L1-13**, **L1-15** (same rider), **L1-16 / L1-17 / L1-18**, and **L1-20** (body-sentinel integrity — which exists in `schema/lint.py` but is **missing from ADR-0010's L1 table**, so the banner adds it). *Added:* **L1-22 / L1-23 / L1-24** (D3.1); **L1-21 stays pre-reserved** for the L2-6 promotion and is not reused. **L2 is NOT kept verbatim:** **L2-1**'s orphan predicate is `n.type == "theme"` in three places (`schema/lint.py`, `faces/mcp_server.py` ×2) and `theme` does not exist in schema 2 — see the ADR-0022 row for what its population becomes. L2-2…L2-6: **kept**. |
| **0011** INGEST contract | **AMEND** | §2's **structural-effect column** changes to the schema-2 paths. **The op names do NOT change** — `CREATE_THEME`/`APPEND_DAILY`/`MERGE_INTO_THEME`/`MARK_CONTESTED`/`DROP`/`NOOP` stay, because renaming them is a change to the *plan envelope* version (`curator/plan.py` `SUPPORTED_SCHEMA_VERSIONS`), and `config.py:255-268` states normatively that the plan-envelope version and the KB schema version *must never reference each other*; renaming would also break every brain prompt for zero semantic gain. §4.0 gains the people exclusion. **§4.1 check by check:** check **4** (TAXONOMY) is **unchanged in shape** — it keeps grading the Disposition's singular `domain` against `domains`; `subjects ⊆ domains` is asserted on the materialised note by L1-5's successor, not on the plan (D2.2). Check **5** (BASENAME) **keeps its `(daily exempt)` clause** — it guards *pre-existence*, which one-journal-per-`run_date` does not remove, so retiring it would fail the second `agora curate` of any day (D2.6) — and its `live_basenames` input **excludes `wiki/people/**`** (D3.3). Check **6** (PATH/ALLOWLIST) uses `pathsafe` plus the leading-`_` rejection (D4.4), the kind-first implied path, and gains **`run_date`** as an injected fact so the `notes/<yyyy>/<mm>/` shard is composed rather than parsed (D2.6). **No new op** — ADR-0022 §C's `CREATE_DOMAIN` remains unimplemented and untouched, and `wiki/entities/` consequently has no producer and ships empty (OD-8). §6.1 (*schema/taxonomy evolution is OUT of INGEST*) is **kept verbatim**. |
| **0012** ranking | **AMEND (append-only addendum)** | §2's `is_moc` row and §4 stage 1's seed + domain-in-scope rules (D5). **Kept:** §0a (no external scorer), §1/§3 tokenizer + the #56 CJK addendum, §5 weights, §6 gate, §7 extraction, §8 `fm` boost, the `FLOOR`, and the #146 addendum's `lex > 0` conditionality. **`CACHE_SCHEMA_VERSION` 2 → 3 IS REQUIRED** (D5): `is_moc` is a parser-computed, **serialized** `_Note` field whose definition changes, and `_Note` gains a `subjects` accessor — both triggers named in `core/index_cache.py`'s own normative bump rule. `indeg`/`d_moc` are indeed recomputed at load and are not the reason; the fresh-repo argument does not cover it either, because `{1, 2}` makes a schema-1 repo with a populated `_kb/index/` reachable by this build. **Gate-B scope, stated so it is not over-read:** the fixture pins the **full-scan** ranking only — `index_cache_used` is always `false` there (`kb_builder` runs no `git init`) and the cache payload is keyed by repo-relative POSIX path, i.e. the one derived structure the flip invalidates **wholesale**. That is harmless only because the destination repo is new; it is not evidence that the cache survives. |
| **0014** OKF/Obsidian | **AMEND** | D3's link **form** is unchanged (standard markdown body links, basename-keyed resolver); the **relative paths** they render change shape (`../maps/x.md`), which is a rendering detail APPLY already computes. D2's *"keep `type:`"* clause is affected — see **OD-3**; the recommendation (emit `type:` as a derived OKF mirror of `kind`, as `description` already mirrors `summary`) keeps D2 true and makes this an amend rather than a supersede. D5's importer places by kind directory (`vault_import.py:196` currently forces a type; `vault_import.py:359`'s `first_domain` catch-all is retired by D2.2 leg 1). **§Non-goals — the boundary that froze the enum — is SUPERSEDED in one bullet:** *"Do **not** adopt free-form `type` for produced notes (**the `index/moc/theme/daily` enum stays**)"*. That enum is replaced by the six-value `kind`. Its *intent* survives intact and that is why this is a supersede of one bullet rather than of the section: free-form `type` is still not adopted — `kind` is a **closed** vocabulary, and D3.1 closes it *at the directory level* (L1-22), which is a stronger guarantee than a frontmatter enum a brain writes prose into. The other three non-goals are **kept verbatim**. **D6 is the clause OD-3 turns on** and is **kept**: *"two orthogonal version axes … `schema_version` … and `okf_version` … they evolve independently"* — so bumping `schema_version` 1 → 2 does not by itself change OKF conformance, and the only OKF-visible question is whether `type:` is still emitted as a derived mirror (OD-3). D1/D4 (strict producer / tolerant consumer) **kept** — and it is D1 that makes `people/` linting advisory. |
| **0015 / 0016** brain routing, CLI brains | **KEEP** | Neither reads a wiki path. Verified: routing is per-act over `worker.Backend`'s two methods. |
| **0017 / 0018** harvester file connector, link-following | **AMEND (one new rule)** | Verified and unchanged: the connector *mechanics* are layout-free — segmentation, cursor, fan-out caps, and ADR-0018's sibling containment (relative to the source file's own directory, not to any Agora layout); candidates still land in `_kb/inbox/harvest-<agent>/` with `domain`/`tags` unset. **What changes is the input set, and the row must not claim otherwise:** these connectors have until now read only *source* files **outside** the repo, and D3.3 makes `wiki/people/**` the first sanctioned **in-repo** read. That is a genuine widening, so it ships with a companion fence — **a `file:` connector on a repo-internal path may cover `wiki/people/**` and nothing else; any other `wiki/` or `raw/` path under such a glob is skipped with a note**, mirroring `_is_within_gold` (`harvester/connectors.py`, the ADR-0027 §8 exclusion), and `agora doctor` reports it as it already reports the gold exclusion. Without the fence a glob covering `wiki/concepts/**` would feed the curator's own output back as candidates — the reworded-loop risk §8 leaves open — through a door this ADR opened. |
| **0019** web stack | **KEEP (posture) / see the read-aggregation row (payload)** | The API-first + server-rendered **posture** is layout-free. The JSON it serves is not — `/api/notes` and `/api/notes/{path}` carry `type`/`domain`/`domains`; that change is recorded two rows below rather than left to be inferred from "posture is layout-free". |
| **0020** upload write-path | **AMEND** | Its stated deferral — *"storing the original binary verbatim in `raw/`"* — now has a defined destination (`raw/_blob/`, D1.4), an admission rule, **and a transport** (D4.2). The **routing** is unchanged: faces still extract→inbox, and the curator/APPLY remains the sole writer of `raw/` (its decision 3, kept verbatim). The **item shape is not**: `Inbox.write` takes `text: str` plus an optional `raw_ref: str` and no bytes, and `_materialize_raw_source` writes the event *body*, so a genuinely unchanged write path could never deliver a binary. The inbox item gains an optional attachment written beside the event in the writer's own namespace (D4.2) — a DATA-MODEL §1 amendment, listed below the table. |
| **0021** graph viz | **AMEND** | `AgoraHandlers.graph()` returns a per-node `domain` derived from `_wiki_domain(rel_path)` (`faces/mcp_server.py:942`) and builds a server-side domain-filter chip row. Schema 2: `domain` (0..1, path-derived) becomes `subjects` (0..n, frontmatter-derived), `type` becomes `kind`, and `wiki/people/**` **joins the node set** (read-first-class, D3.3). `/api/graph`'s JSON is a face contract, so this is a visible shape change and must be released as one. Node cap, BFS, `truncated` and the vendored force-graph choice: **kept**. **`/api/graph` is not the only payload that moves** — see the AgoraHandlers read-aggregation row below. |
| **0003 / 0019 / 0021** the read aggregations (`AgoraHandlers.browse` / `note`) | **AMEND (face contract)** | Recorded as its own row because these payloads had no ADR home and change identically to `/api/graph`. `browse()` returns `{notes: [{rel_path, basename, type, title, status, tags, domain}], domains: [...]}` and `note()` reuses the same summary shape (`faces/mcp_server.py`) — surfaced as the web face's `GET /api/notes` and `/api/notes/{path}`, and as the shape `kb_read` returns. Schema 2: per-node `type` → `kind`, `domain` (0..1, path-derived) → `subjects` (0..n, frontmatter-derived), and the **top-level `domains` aggregate becomes a `subjects` facet** — a union over every note's `subjects:` rather than the set of `wiki/<domain>/` segments, which means it can now be empty and can contain a subject no note is filed under only if the taxonomy says so. `wiki/people/**` joins the note set (D3.3). All of it is a visible face-contract change on the same release as `/api/graph`, not a slipped-in one. ADR-0019's API-first + server-rendered **posture** is layout-free and kept; the posture is not the payload. |
| **0022** taxonomy governance | **AMEND + partial SUPERSEDE** | §A's no-loss floor is **retargeted, not deleted** — three legs, D2.2, applied at **both** sites §A names (`normalize_plan` *and* the `vault_import.py` import lane it says the floor "unifies with"). §B (structured domain-entry shape) and §D (`agora taxonomy add-domain`) are **kept** untouched. **§C is AMENDED, not kept:** its `CREATE_DOMAIN` taxonomy write and `taxonomy_policy` gate are untouched and still unimplemented, but its stated worker duty *"and **creates the lazy MOC**"* — repeated in its atomicity paragraph — **loses its referent**, because that MOC is `wiki/<domain>/<domain>-moc.md` and a domain no longer implies a map. **Decision: a new domain materialises nothing structural.** Maps become editorial artefacts, created because someone wants a map, not because a token was added to a vocabulary; auto-seeding `wiki/maps/<domain>.md` would recreate the empty-MOC-stub population #146 measured beating real answers. §C's atomic-diff guarantee is unaffected — it now covers the taxonomy write plus the run's concepts. **§E's tuning surfaces are kept as SURFACES, but `curator.lint.max_orphans`'s SEMANTICS move with L2-1** (see the 0010 row): the orphan predicate stops being `type == "theme"` and becomes `kind in {concept, summary}`, with **`entity` and `person` exempt** — `entity` because D1.3 bars it from `children:` and `person` because the curator may not link into `people/` at all, so both would otherwise be orphans *by construction* and would push a repo past a threshold that gates a run. `stale_days` / `stub_max_runs` / the per-domain keys: unchanged. The #57 addendum is kept, with one clause **superseded**: its *"widening that regex was explicitly rejected — it guards path traversal, not aesthetics"* is overturned by D4.4, on the ground that `pathsafe` is a closed **category allowlist** and therefore preserves the very property that made the regex safe. Its *other* rejection — transliteration tables, because a versioned table renames future notes for identical input — **stands**, and the `note-<sha8>` fallback it chose **stays** as the last resort. **One further addendum consequence, recorded rather than left to surprise:** §2 ("Aliases: skip + count") rests on the premise that preserving a Korean alias *"would require widening the closed alias/basename token grammar"*. D4.4 removes that premise — aliases run through the same slugger — so Korean aliases are **preserved** and `aliases_skipped_unslugifiable` becomes a residual counter. |
| **0023** context connectors | **KEEP** | Connector-boundary redaction and the fail-closed personal scope are unchanged. Stated, not changed: the ADR-0023 connector boundary is **not** the `people/` read-corpus boundary — see residual risk R1. |
| **0024** bulk / horizontal scale | **AMEND** | Its deferred alternative (b), *"domain-partitioned writers owning disjoint `wiki/<domain>/` subtrees"*, becomes **structurally unavailable**: there are no per-domain subtrees to partition. The ADR's actual decision (shard by repo only, never a second writer within a repo) is **unchanged and unaffected**; only the recorded option set narrows. The OD-3a claim cap is untouched. |
| **0025** web config / multi-upload | **AMEND (one clause)** | The upload/remember `domain` argument (`handlers.remember(markdown, source, domain, tags)`) keeps its meaning as an **inbox field and `raw/` shard key** and becomes a **`subjects:` seed** rather than a wiki path segment. Everything else — `web:` operator config, multi-upload batch receipt, broadened extensions, SSRF guard, zip-bomb cap, `web:<user>` identity — is layout-free and **kept**. |
| **0027** gold packs | **AMEND** | Decision 4's structural-centrality term reuses the `d_moc` machinery and therefore follows D5. Its eligibility set gains **two day-1 exclusions**: `wiki/people/**` (D3.3) and `derived: true` (D2.4). §8 (the outbound sentinel + loop-break contract) is **normative, and its grammar/neutralization/loop-break rules are kept verbatim** — this ADR neither restates nor forks them. **§8's SCOPE SENTENCE is AMENDED**, because leaving it unqualified would make this ADR's own text false: §8 declares itself *"the single normative spec for every Agora→agent emission path"*, and D3.3 admits `wiki/people/**` — content that never passed the curator, the ADR-0007 candidate gate, or ADR-0023 redaction — into `kb_query` / `kb_read` / `kb_neighbors`, which are emission paths §8 does not name. The gold + `kb_context` exclusion is the control for the **push** surface only; the **pull** surface (agent-initiated MCP reads) is named here as an emission path with a **distinct, still-undesigned control**, owned by residual risk R1. Claiming §8 covers it, or claiming the push exclusion covers it, would be the kind of unearned "kept verbatim" that makes a triage table worthless. Decisions 1/2/3/5/6/7 and the byte-identical-rebuild contract: **kept**. |
| **0036** authn/authz | **KEEP** | Proposed; repo-as-boundary unchanged. `_meta/kb.yaml` identity here is deliberately *inert* — no authorisation reads it (D1.5); the registry/attach semantics belong to reserved ADR-0037. |

### Normative documents outside `docs/adr/` that this ADR amends

The triage above covers ADRs. Three normative documents change too, and they are listed so no
amendment enters through an ADR's prose alone:

- **`docs/DATA-MODEL.md` §1 (inbox item)** — **AMEND.** The item gains an **optional attachment**
  written beside the event in the writer's own append-only namespace, with `raw_ref` naming its
  `raw/_blob/` destination (D4.2). This is the transport for original bytes; without it `raw/_blob/`
  has a destination and no channel. The item stays immutable and per-writer-namespaced.
- **`docs/DATA-MODEL.md` §2 (`raw/` shapes)** — **AMEND.** Gains `raw/_blob/<ab>/<sha256>.<ext>` and
  its `<file>.meta.yaml` sidecar; the existing `<domain>/<event_id>.md` and
  `<domain>/<date>-<slug>.<ext>` shapes and the sidecar key set are **unchanged** (D1.4).
- **`docs/DATA-MODEL.md` §10 (ID & naming)** — **AMEND, and one correction.** Basenames stay globally
  unique; `wiki/people/**` is **outside that identity space** (D3.3), and *"Domain MOCs are
  `<domain>-moc.md`"* is superseded by `wiki/maps/<slug>.md`. **§10 contains no daily exemption** —
  the exemption is ADR-0011 §4.1 check 5's, it is **kept**, and D2.6 says why.
- **`docs/INGEST-CONTRACT.md`** — **AMEND**, mirroring the ADR-0011 row (it restates §4.1 verbatim,
  including the `(daily exempt)` clause, and must not drift from it).

### What this ADR overrides in `STRATEGY-2026-08.md` §12, and why the premise changed

§12 (2026-08-15) ruled: *absorb OpenKB's structural ideas **but with no new `type:` values***, landing
the document layer as `type: theme` + additive frontmatter keys — explicitly because that needs
"no new op, no `schema_version` bump, #63 not a prerequisite, fully reversible"; it ruled **SKIP** on
type partitioning on the ground that `wiki/<domain>/{themes,daily}/` is already a nested structure;
and it ruled **엔티티 TAKE-MODIFIED** — entities as a *tag facet*, with **zero schema change**.
**This ADR overrides all three clauses**, and the entity one is listed here because overriding it
silently would be the easiest of the three to miss.

**Why the tag-facet form of entities was rejected.** A facet is a value in `tags:`; it gives an
entity no **node kind**, and "entities have no node kind" is blocker 3 in this ADR's diagnosis. A
tag cannot be a link target, cannot carry `sources:` or `status:`, cannot appear in a graph as a
node, and cannot be the thing a body link points at — so the facet form buys the *retrieval* half of
entities while leaving the *structural* half exactly as broken as it was. §12 was right that the
facet is the zero-cost option; this ADR's premise change is that zero schema cost stopped being the
governing constraint.

**And §12's thin-frontmatter SKIP is answered, not ignored.** §12 ruled thin frontmatter **SKIP**
because *"4 of the 6 scoring fields are frontmatter-authored, so it is actively destructive"* — and
an entity page that is "registered, with gated filling" is exactly that thin-page population. Three
things answer it. (1) **D1.3 keeps thin pages out of the seed set**: entities may not be map
`children:`, so no entity becomes a `d_moc = 0` seed and `indeg_norm`'s denominator does not move
for registrations. (2) **BM25F never sees `kind` either** — `_FIELDS` is
`title/aliases/tags/headings/summary/body` — so a thin entity page competes on exactly the same six
fields as any other note and gets no structural advantage from having a kind; the husk problem is a
*content* problem, and the #146 `lex > 0` guard plus its still-open thin-page xfail is where it is
fought (R5). (3) **On day 1 the population is empty** (OD-8), so the risk is contained by not
existing yet, which is the cheapest containment available and the reason the producer is deferred.

It overrides them because the owner's decision of 2026-09-03/04 removed the premise each rested on.
§12's argument was a *cost* argument, not a *correctness* one: every clause it cites is about
avoiding a schema bump, avoiding #63, and staying reversible. The owner has since decided that (a)
there are no released users, so a bump costs nothing to reverse — the reversal is a re-import; (b)
the migration is a **clean break** via export/import, so #63 is not a prerequisite and never becomes
one; and (c) the flip lands **at once**, so "fully reversible in pieces" is not a property anyone is
buying. What §12 got right survives intact and is carried into this ADR verbatim: the BM25F ranker
never sees `type` (`_FIELDS` is title/aliases/tags/headings/summary/body), so **retrieval is not the
reason to add kinds** — richly-fielded notes existing in the corpus is; `core/wiki.py:802`'s
unrestricted `rglob("*.md")` means new subfolders are indexed automatically, which is what makes the
kind directories cost nothing on the read path; the `sources` tier is a **pointer**, and moving
`raw/` under `wiki/` stays rejected (D3.4); and *explorations* stay **SKIP** — model synthesis must
not sit in the same tier as sourced knowledge, which is exactly why `derived:` exists (D2.4) and why
`summaries/` is governed by ADR-0040 rather than opened now.

One §12 line is **not** overridden and is instead *executed differently*: its cheapest-high-value
buy, "materialise `domain:` into frontmatter", is #156 — and D6 closes #156 as unnecessary, because a
converter never destroys the path it reads from.

---

## Schema-version impact (DESIGN §10 V9)

**Bump: `schema_version` 1 → 2.** Not additive, not none. The canonical value stays in
`_meta/taxonomy.yaml` (ADR-0010 §5.1) and is mirrored into `_kb/repo.yaml` and the schema-doc header,
with lint L1-17 cross-checking the locations present in the worktree — all unchanged.

- `config.SUPPORTED_KB_SCHEMA_VERSIONS` = `{1, 2}` (never `{2}`); `MAX_SUPPORTED_KB_SCHEMA_VERSION`
  follows derivatively, as designed.
- `curator/plan.py` `SUPPORTED_SCHEMA_VERSIONS` (the **plan envelope** version) is **NOT** bumped,
  and that is a consequence of a decision rather than an axiom: it holds *because* `Disposition`
  keeps its singular `domain` field and gains nothing (D2.2 / OD-9). The two axes are proven
  independent by `tests/test_schema_version_guard.py` and must stay that way — a schema-2 wiki does
  not imply a v2 plan envelope — but independence means neither *implies* the other, not that
  neither moves. Adopting OD-9 later bumps the envelope, and that would be a real bump to declare.
- `core/index_cache.py` `CACHE_SCHEMA_VERSION` **IS** bumped, 2 → 3 (D5). It is a **derived-cache**
  version, not a schema axis: it gates nothing an operator sees and simply invalidates stale entries.
  It is listed here so nobody reads "one bump, `schema_version` only" and skips it.
- **Old binary on a schema-2 repo: fail-loud**, unchanged — `guard_repo_schema_version` at CLI
  dispatch and both face constructors, with `agora doctor` the diagnostic exemption.
- **New binary on a schema-1 repo:** read-works / **write-refuses** (D6) — this ADR's one
  hardening of V9, with its reason stated there.

**AGENTS.md / SCHEMA.md re-emission.** `schema/emit.py` is **idempotent-skip unless `force=True`**
(`_write_text` / `_link_or_copy` both return early when the destination exists or is a symlink). So
the flip does **nothing** to an existing repo: a schema-1 repo keeps its schema-1 `AGENTS.md`, its
`_meta/taxonomy.yaml` `schema_version: 1`, and its v1 layout, and stays fully readable. It becomes a
schema-2 repo only by being **re-imported** into a new one — at which point `repo init` emits the
schema-2 doc, the schema-2 templates (one per **kind**), and the new `_meta/kb.yaml`. Existing repos
are never force-re-emitted by this change, and no command silently upgrades one.

---

## Open sub-decisions

_Every row below was **ratified as written** on 2026-09-05 (see the acceptance record at the top of this file). The column is titled *Recommendation* because that is what it was when authored; as of acceptance it is the decision._

| # | Question | Recommendation |
|---|---|---|
| **OD-1** | ULID source for `kb_id` — a permissive third-party dependency vs ~30 lines inline (Crockford base32 over a 48-bit timestamp + 80 random bits). | Inline, in `core/ids.py`. A ULID package would be a **T1 admission** under the ADR-0005 T0–T4 addendum (commit `66fa455`, #157) — *"OSI-permissive only, and only when the entire transitive closure of the pinned version is permissive"* — plus a `docs/BOM.md` ledger entry, which is a real procedure to run for a 30-line algorithm. Inline also keeps `agora repo init` dependency-free. |
| **OD-2** | May `entity` notes appear in a map's `children:`? | **No on day 1** (D1.3, with the #146 thin-page reasoning). Revisit when a content/length prior lands and the xfail in `tests/core/test_wiki_lexical_evidence_146.py` flips. Widening later is additive. |
| **OD-3** | Is a derived `type:` still **emitted** as an OKF mirror of `kind`? | **Yes** — one line in APPLY, exactly the `description`↔`summary` precedent (ADR-0014 D2), and it keeps emitted repos conformant OKF bundles with no build step. The question is *only* about emission, not about conformance in general: **ADR-0014 D6** ("two orthogonal version axes … they evolve independently") means bumping `schema_version` 1 → 2 changes `okf_version` and OKF conformance not at all. `type:` remains retired *as the kind authority* either way. |
| **OD-4** | Per-subject journals under `notes/`, instead of one per `run_date`? | **No for now** (D2.6). Revisit only with a measured need; the merge in D6 step 4 shows the shape it would take. |
| **OD-5** | `agora export` does not exist. What format does the OpenKB interop path emit? | Author it with ADR-0037/0040, not here. §10's finding — `openkb add` accepts plain `.md` — means the export is a *document feed*, not a bundle format. |
| **OD-6** | Should `core/layout.py` reader-cache stems also move to `pathsafe`, fixing #108 (a repo directory named `내지식` addresses no cache)? | Out of scope for the flip; cheap follow-up. Keeping it out preserves the attribution property the whole gate-A split was designed for. |
| **OD-7** | Does `wiki/summaries/` ship empty on day 1, or does `agora import --from-kb` ever produce one? | Ship **empty**. Its internal contract is ADR-0040 and is unratified; an importer that invented summaries would create content no ADR governs. |
| **OD-8** | What **produces** a `wiki/entities/` note? Nothing does on day 1 (no new op, no importer rule, no human route). Candidates: (a) a new closed-vocab op, (b) a `kind` field on `Disposition` widening `CREATE_THEME`, (c) human authoring with a `people/`-style carve-out, (d) derivation from concept `related:` graphs. | **Ship the tier empty and defer the producer**, exactly as OD-7 does for `summaries/`. (b) is the cheapest but bumps the plan envelope (see OD-9); (c) reopens the write boundary this ADR just tightened; (d) needs a content prior that R5 says does not exist yet. Deciding it here would be deciding it with no population to observe. |
| **OD-9** | Does `Disposition` gain `subjects: tuple[str, ...]`, letting a plan express 0..n subjects directly? | **No for the flip** (D2.2). The singular `domain` stays, seeding a one-element `subjects:`; 0..n is an APPLY/human capability. Adopting it later **bumps `curator/plan.py` `SUPPORTED_SCHEMA_VERSIONS`** — cheap, but it is a plan-envelope bump and must be named as one, not smuggled in under "the two axes are independent". |
| **OD-10** | D2.2 leg 2 — the CLASSIFICATION leg becoming `subjects: []` — is **not reachable end to end** as shipped. APPLY handles `domain=None` (`_apply_create_concept` files the concept with `subjects: []`), but PLAN check 5 still hard-rejects a domain-less CREATE_THEME/APPEND_DAILY and `ollama_brain.normalize_plan` still substitutes ADR-0022's catch-all domain, so the wire never carries `None`. | **Deferred, deliberately, and recorded here rather than left implicit in two modules.** Closing it is three coupled changes in one unit: drop check 5's `domain is None` rejection, retire the catch-all substitution (leg 1), and NAME the domain-less `raw/` shard — today `_sources_union` would compose a root-level `raw/<event_id>.md`, a layout no ADR states, and D2.2 leg 3 keeps `raw/<domain>/`. Until then the no-loss floor still asserts a possibly-false subject, exactly as v1 did; the `subjects: []` PATH exists and is exercised, the PRODUCER does not. |

---

## Consequences

- **+** All six blockers in the diagnosis close with one change, and they close *mechanically*: a
  long document has a kind (`summary`), an entity has a kind (`entity`), a new kind is a new
  directory rather than a lint rewrite, and the no-loss floor stops asserting a subject it does not
  know (`subjects: []`). Stated precisely, because "closes the blocker" is doing real work here:
  what closes is the **structural** blocker — the thing that had no home now has one, and giving it
  a home is no longer a lint rewrite. `summaries/` and `entities/` still need a producer each
  (ADR-0040, OD-8) before either tier holds anything; see R2.
- **+** The point of no return is **never crossed**, rather than closed. A converter reads the v1
  path and writes a v2 tree, so the subject is preserved into `subjects:` and #156's frontmatter
  materialisation is unnecessary — the cheapest possible resolution of the thing that was blocking
  the flip.
- **+** `raw/` does not move, so the `sources:` provenance chain of every note survives byte-for-byte
  and lint L1-7/L1-8/L1-8b keep working unmodified. The second, unnamed point of no return is not
  approached.
- **+** The integrity boundary gets **tighter**, not looser: `raw/` admission compares bytes, every
  curator path composition is containment-checked, `wiki/people/` is explicitly excluded from the
  curator-writable allowlist, and `pathsafe` rejects Windows reserved device stems the ASCII regex
  admitted.
- **+** Korean and other non-ASCII titles produce meaningful filenames, and the `note-<sha8>` floor
  becomes a genuine last resort rather than the common path — without weakening the closed-set
  property, because the widening is a Unicode-category **allowlist**.
- **+** A human's own writing becomes searchable, graph-visible and readable through every face for
  the first time, while the curator's inability to touch it is enforced by the same single constant
  that enforces every other allowlist rule.
- **+** The migration/back-compat budget (~2–3 person-months) is not spent, and the schema stays a
  set (`{1, 2}`) so the owner's live KBs keep working while they are re-imported.
- **−** Every path in every repo changes, so **ranking changes**, and the amount is not predictable
  from first principles — the seed population, `d_moc` distribution and `indeg_norm` denominator all
  move. This is why gate B exists and why the flip PR must attach and explain the `diff_snapshots`
  listing (D5). Some rank moves will be pure tie-break artefacts of the path change.
- **−** **Every read payload's node shape changes, not just the graph's.** `/api/graph`,
  `/api/notes`, `/api/notes/{path}` and the `kb_read` / `kb_neighbors` summaries all carry the same
  per-note fields, and all of them move together: `domain` → `subjects` (0..1 path-derived → 0..n
  frontmatter-derived), `type` → `kind`, and `people/` nodes appear. The top-level `domains`
  aggregate in `browse()` becomes a **`subjects` facet** — a union over notes' `subjects:` rather
  than the set of `wiki/<domain>/` path segments, so it can legitimately be empty. These are face
  contracts and must be released as one visible change, not slipped in.
- **−** The owner re-imports two live KBs by hand, once, at a measured cost of one to two days, and
  loses `_kb/` derived state (caches, cursors, packs) that must rebuild.
- **−** A schema-1 repo under a schema-2 build can be read but not written at all — including its
  inbox. That is deliberate (an undrainable inbox is silent loss) but it is a harder stop than
  DESIGN §10 V9's default posture, and an operator who misses the message sees writes fail rather
  than warn.
- **−** Retiring `type:` as an authority costs OKF conformance on that one field unless OD-3's mirror
  is emitted; the recommendation exists precisely to avoid paying it.

### Residual risks — named, not solved

- **R1 — The `people/` outbound boundary is undesigned, and the day-1 control covers only half of
  it.** The ADR-0023 connector-boundary redaction is a *write-side* boundary; a read-corpus egress
  boundary is a different control and does not exist. What ships is a control for the **push**
  surface: `wiki/people/**` is excluded from gold packs and from `kb_context`, so no human-owned
  content enters a standing context nobody asked for. The **pull** surface is deliberately left open
  — `kb_query` / `kb_read` / `kb_neighbors` return people notes to an agent on request (D3.3, an
  owner decision), and those are Agora→agent emission paths that ADR-0027 §8 does not currently name.
  **This ADR does not claim the push exclusion as the control for the pull surface**; it amends §8's
  scope sentence to name the read tools and records that their control is still to be designed. The
  push exclusion must not be lifted by a config flag, and the pull surface must not be pointed at as
  evidence that the boundary is solved.
- **R2 — Two of the six kinds ship with no producer.** `wiki/summaries/` and `wiki/entities/` exist
  as kinds, with directories, frontmatter shapes and lint rules, and **nothing writes into either**:
  summaries wait on **ADR-0040** (which does not exist yet) and entities wait on **OD-8** (which this
  ADR declines to decide). `raw/_pages/` is likewise a reserved prefix with no writer. Shipping the
  *containers* before the contracts is deliberate — it avoids a second migration — but the honest
  statement of the cost is that **a third of the kind vocabulary is empty on day 1**, and a reader of
  the layout diagram will see structure that nothing populates. The mitigation is that both tiers are
  *additive* to fill later, and neither empty tier costs anything at read time.
- **R3 — `kb_id` is a self-claim for any KB not created locally.** It cannot be structurally closed:
  a remote repo asserts its own identity. The mitigation is presentational and must be uniform —
  every provenance badge shows `alias · kb_id · transport` together — and it belongs to reserved
  ADR-0037. Nothing in this ADR may treat `kb_id` as an authorisation input.
- **R4 — Homograph confusables survive the Unicode slug rules.** Cyrillic `а` (U+0430) and Latin `a`
  (U+0061) are both `Ll`, so two visually identical filenames are two distinct files. Confusable
  detection is a different control and cannot live in a per-component pure function; folding it in
  would make the allowlist open-ended. Recorded in `core/pathsafe.py` and restated here.
- **R5 — The thin-page half of #146 is still open.** It is a `strict=True` xfail. The entity-child
  exclusion (D1.3/OD-2) is a *containment* measure for it, not a fix; a content/length prior is the
  fix, and it is not in this ADR.
- **R6 — The ranking oracle's `people/` test is schema-UNCONDITIONAL, and on a schema-1 repo owning
  a literal `people` domain that is a behaviour change.** D3.3's people exclusion has two faces. The
  GRADING face — lint's `skip_people`, the dashboard's orphan count, gold eligibility — is version-
  gated through `schema.notes.is_ungraded_people_note`, because those callers already hold a parsed
  `schema_version` and must agree with `lint()` exactly. The RANKING face is not: `core/wiki.py`'s
  `_is_people_path` is a pure path test, like every other predicate in that module, because D5's
  rule is that the oracle reads no config file (the same rule that makes the subject scope a
  frontmatter read rather than a `repo.yaml` read). The consequence, stated rather than left to be
  discovered: in a **schema-1** repo that happens to have a `wiki/people/` **domain**, those notes
  silently leave the `[[basename]]` identity space and stop contributing in-degree, where the
  pre-flip tree counted them. It is the same pathological directory-name overlap `_path_kind`
  already accepts for `maps`/`concepts`, it is invisible on every repo that does not use that
  domain name, and lint/gold/dashboard are unaffected. **Accepted, not fixed**: version-gating the
  oracle would put a config read on the query hot path and give one repo two rankings depending on
  a `_meta/kb.yaml` field, which is a worse trade than one path-name collision. Reconsider if a
  real schema-1 repo is ever found with that domain; migrating it to schema 2 removes the case.

---

## Addendum — D6 rule 2 as shipped: an UNDECLARED path domain converts to `subjects: []` + a warning (#153, landed 2026-09-04)

D6 rule 2 above says the path domain becomes `subjects: [<domain>]` — *not* `[]` — and gives the
reason: the v1 path domain is a genuine curator assertion, so discarding it loses exactly the
information the flip exists to preserve. That reason is intact and the rule is unchanged **for every
domain the source taxonomy declares**, which is the case rule 2 was written about. Implementation
(`ingest/kb_convert.py`, `_plan_note`) found one case the clause did not anticipate, and this
addendum records the shipped behaviour so the code is not deviating from an unamended ADR.

**The case.** A schema-1 repo can hold `wiki/<domain>/…` for a `<domain>` its own
`_meta/taxonomy.yaml` does **not** list — a domain removed from the vocabulary after its notes were
written, or a hand-created directory that was never added. Rule 5's cheapness argument is what makes
this reachable: the conversion copies the source taxonomy across **verbatim** (it is the destination
taxonomy), so whatever the source did not declare, the destination does not declare either.

**The conflict.** Lint L1-5 grades `subjects:` against the taxonomy's `domains`. Writing
`subjects: [<undeclared>]` would therefore mint a destination repo that **fails its own lint on the
first run** — and D6's whole promise is a conversion whose output is a legal schema-2 repo. Rule 2
would be honoured in the letter and the crossing would be broken in fact.

**The shipped rule.**

- The path domain is written as `subjects: [<domain>]` **whenever the source taxonomy declares that
  domain** — rule 2 unchanged, and the overwhelmingly common case.
- A path domain the source taxonomy does **not** declare converts to `subjects: []` **plus a
  per-note warning** naming the note and the dropped domain, carried in `ConvertReport.warnings` and
  printed by `agora import --from-kb`. The assertion is not discarded *silently*, which is the
  property rule 2 was actually protecting; the operator is told, and the remedy is to declare the
  domain in the **source** `_meta/taxonomy.yaml` and re-convert (ADR-0022 §D's
  `agora taxonomy add-domain` is still unimplemented) — or to accept the `[]`.
- The root `index.md` has **no path domain at all** and writes `subjects: []` **structurally**, with
  no warning. There was never a subject to carry, so there is nothing to report; a warning there
  would fire on every conversion and train the operator to ignore the channel.

**Why not the alternatives.** Extending the destination taxonomy with the undeclared domains would
have the converter make a governance decision ADR-0022 §D reserves for an explicit operator command,
in a repo the operator has not yet seen. Refusing the conversion outright would strand a real KB on
a directory that is, at worst, untidy. Both are worse than writing the honest empty value and saying
so.

**Scope.** This addendum touches rule 2 only. Rules 1 and 3–7 are unchanged, and nothing here
weakens the "source is never modified" property or rule 7's hard-failure-with-a-named-list contract.

---

## Addendum — as-built: the capture transport and the `raw/_blob/` sidecar (#153, landed 2026-09-04)

D1.4 gave `raw/_blob/` a destination and D4.2 gave it a channel. Implementation took three of D4.2's
and D1.4's mechanical details a different way, and this addendum records the shipped shapes so the
code is not deviating from an unamended ADR. **Every normative property is untouched:** APPLY is
still the only writer of `raw/`; admission is still membership in `raw_writes` **with matching
bytes**, and content-addressing is still an additional self-check and never a substitute for that
authorship check (D1.4's normative paragraph, verbatim); the inbox item is still immutable,
append-only and per-writer-namespaced; the sidecar is still `<file>.meta.yaml` so lint L1-8b keeps
working unmodified; `_blob`/`_pages` are still reserved domain names.

**1. The staging path is content-addressed and shared, not one blob per event.** D4.2 says
`_kb/inbox/<writer>/<id>.blob`, *"with `raw_ref` naming the `raw/_blob/<ab>/<sha256>.<ext>`
destination"*. As built it is `_kb/inbox/<writer>/_attach/<sha256>.<ext>`, and the event names its
bytes through a new **optional `attachments:` frontmatter list** (`{sha256, ext, filename,
media_type, bytes}` per entry) rather than through `raw_ref`, which is untouched and keeps its
existing meaning. Two reasons, both discovered by building it:

- *One event may carry several artefacts, and several events may carry one.* `<id>.blob` can express
  neither. Content-addressing gives both for free: two events uploading the same PDF name one staged
  file and, later, one `raw/_blob/` blob with one citation each — which is also what makes D1.4's
  re-cite rule (an existing digest is cited, never rewritten) reachable from the write side.
- *`raw_ref` cannot name the destination without duplicating it.* The destination is a pure function
  of the digest and the extension the record already carries (`raw/_blob/<sha[:2]>/<sha>.<ext>`), so
  storing it as a second, caller-supplied string would let a hand-edited spool file point a
  citation at a path the bytes do not hash to. APPLY composes the ref itself, from the record.

The ordering D4.2 actually cares about — the bytes are written **before** the event that names
them, inside one `Inbox.write` call, so a crash never leaves an event citing bytes that do not exist
— is preserved exactly, and the rejected alternative (a staging area outside the inbox) stays
rejected.

**2. The `raw/_blob/` sidecar has its own closed key set, and the DATA-MODEL §2 key set is
unaffected.** D1.4 says the sidecar carries *"the DATA-MODEL §2 shape (`source_url`, `ingested`,
`ingested_by`, `sha256`, `mime`) unchanged"*, and the triage row repeats it. That was written on the
assumption that a blob is a fetched document like every other `raw/` binary; it is not. The §2 shape
describes a **re-ingest drift record** for a file with a `source_url` that can be fetched again. A
captured artefact has no URL, was handed to us once, and is immutable by construction — for it the
questions worth answering are *which bytes, how many, from whom, when, and under which event*. So
the shipped sidecar is:

```yaml
sha256: <hex>          # == the basename: the integrity self-check
ext: pdf
media_type: application/pdf   # optional, normalised to a bare lowercase type/subtype
bytes: 481920                 # the length actually written, never the record's claim
filename: 2026-q3-report.pdf  # optional, DISPLAY only, sanitised
captured_at: 2026-06-13T10:22:33Z
writer: dochan
source: web:dochan
event_id: <inbox event id>
```

Closed against **additions**; an absent optional is omitted rather than emitted empty. It never
carries the extracted text — that lives in the event body and, after curation, in the note, and a
second copy nothing keeps in step is a liability. **`raw/<domain>/` binaries keep the five-key §2
sidecar unchanged**; the two shapes are distinguished by which tree the file is in, and
`docs/DATA-MODEL.md` §2 now says so explicitly. This supersedes the D1.4 sentence and the triage
row's *"the sidecar key set are unchanged"* **for `raw/_blob/` only**.

**3. `<ext>` is the D1.4 grammar, deliberately NOT narrowed to the extractor's accepted set.** D1.4
says `<ext>` is *"drawn from the extractor's accepted extension set (ADR-0025's broadened list)"*.
The path composer (`core/layout.attachment_ext_for`) enforces the grammar — exactly one component,
`[a-z0-9]{1,16}`, never `meta`, falling back to `bin` for anything else — and consults no extractor
registry. That is the shipped behaviour, and it is the correct one: `agora capture --file` exists
precisely so that **an artefact nobody can extract today can be kept until somebody can**, and a
composer restricted to the extractor's list would either refuse those files or rename every one of
them to `bin`, throwing away the one piece of type information the operator had. The grammar is
what carries the safety property (no `.`, no leading `_`, no dotfile, no `meta`, so
`<sha256>.<ext>` and `<sha256>.<ext>.meta.yaml` stay structurally distinguishable and L1-8b keeps
working); the extractor's list was never load-bearing for path safety. The extension is DISPLAY
metadata attached to opaque bytes, and the operator's own gate for what a face will accept remains
`web.extensions.allowed` (ADR-0025), one layer up.

**4. What a `DROP` means for the bytes (no ADR change; recorded because it is easy to misread).**
APPLY materialises a blob only where a note **cites** it, so a candidate the curator DROPs (or
NOOPs) writes no note, no `sources:` entry, and no `raw/_blob/` file. The artefact is not destroyed
— it drains to `_kb/processed/<date>/_attach/` with its event and is never pruned — but that spool
is git-ignored, so a DROPped capture's bytes never enter the committed tree and `agora sync` never
pushes them. This is the SAME rule a free-text capture has always followed (a DROPped `kb_remember`
writes no `raw/<domain>/<event_id>.md`), and keeping the parity is deliberate: an uncited blob would
be a file the final diff admits that lint L1-8 can never account for. Stated in DATA-MODEL §1/§2,
INGEST-CONTRACT §0.1 and `agora capture`'s own output so the capture surfaces do not overstate what
"kept" means.

**5. `raw/_blob/` is pinned out of git's EOL translation.** `hash(bytes) == basename` is only true
if git stores the bytes it was given. A CRLF artefact with no NUL in it (CSV, TXT, HTML, JSON) is
classified TEXT by git and normalised to LF on commit under `core.autocrlf` — after which the
committed blob no longer hashes to its own filename and every later re-cite of that digest fails
`_materialize_one_blob`'s re-verification, permanently. `Repo.init` therefore seeds a
`.gitattributes` carrying `raw/_blob/** -text -diff -merge`, appended rather than rewritten so a
re-init keeps the operator's own rules and lands ours LAST (gitattributes resolves last match wins),
and `Repo._git` pins `-c core.autocrlf=false` on every invocation (an argv `-c` outranks a
repo-local `.git/config`, which the existing `GIT_CONFIG_GLOBAL/SYSTEM` neutralisation cannot
reach). `core.eol` is deliberately NOT pinned: its default is `native`, so pinning it would change
agora's own working-tree writes on Windows, and it applies only where the `text` attribute is set —
which `raw/_blob/**` unsets. Repos created before this change have no such file; `agora doctor`
answers with `git check-attr` (authoritative about ordering and the operator's own additions) and
prints the one-line remedy.

---

## Addendum — `source_links:` and the read side of a citation (#169, landed 2026-09-05)

D3.4 froze `sources:` as the provenance of record and D1.4 froze the `raw/` tree it points into.
Neither said how a citation is *followed*. Issue #169 answered that in two waves — a read side that
serves `raw/` back, and a write side that emits a followable citation form — and this addendum
records the three decisions with a shape that binds later work. **Nothing normative in D1.4, D3.4 or
D4.2 changes**: `sources:` stays the sole provenance of record, APPLY stays the only writer of
`raw/`, admission stays membership in `raw_writes` with matching bytes, and the sidecar stays
uncitable (L1-8b).

**1. `source_links:` joins D2 as a DERIVED MIRROR of `sources:`, among the `concept` / `summary`
additions and nowhere else.** D2 already carries derived keys beside the ones its common-base block
lists — `type:`, the OKF mirror of `kind` (OD-3), and `description:`, the OKF mirror of `summary`
(ADR-0014 D2) — and this is a third of exactly that species: a rendering of a key that already
exists, kept in the frontmatter because a consumer needs the other spelling. The consumer here is
Obsidian, which linkifies `[[…]]` inside a list property and renders plain strings as inert text;
that inertness is the defect #169 reports.

**How that Obsidian premise was established, since the whole wave rests on it.** The brief made it a
hard gate precisely because nobody had checked it: every `[[ ]]` this repo emits elsewhere is a bare
basename, so a *path* inside a wikilink had no in-repo precedent. It was resolved from Obsidian's own
published documentation — a list property may contain quoted `[[Internal links]]`; a folder path
inside a wikilink is resolved **from the vault root**; and `[[note.md]]` resolves identically to
`[[note]]` — and **not** from an observation in a live vault. Two consequences are recorded here
rather than discovered later: the vault must be opened at the REPO root for a `raw/…` chip to
resolve, and nothing in this repository fails if the premise turns out to be wrong (the committed
gate, `tests/core/test_rank_neutrality.py`, pins ranking neutrality, not rendering).

```yaml
sources:
- raw/ai-tech/2026-09-01-cqrs-notes.md
- harvest:claude-code
source_links:
- '[[raw/ai-tech/2026-09-01-cqrs-notes.md]]'
```

Its properties, all five load-bearing:

- **Never authoritative.** No rule reads it as provenance. L1-7 (non-empty), L1-8 (existence) and
  L1-8b (no sidecar) grade `sources:` and only `sources:`. Deleting the mirror from a note by hand
  loses the click and nothing else; the next curator write restores it.
- **It never names a source `sources:` does not carry, and usually names fewer.** Only entries
  beginning with `raw/` enter it. That filter does two jobs: it keeps the D3.4 relation literally
  true, and it declines to wrap a non-path entry — the `harvest:<agent>` shape `core.gold` still
  branches on, which no path resolution has ever adjudicated against L1-8's `(root / s).exists()` —
  in a wikilink that would resolve nowhere.
  This ADR takes no position on that open disagreement; the filter simply never depends on it.
- **Absent rather than empty, and re-derived on every write.** A mirror that comes out empty is
  removed, not emitted as `source_links: []`. Every site that finishes writing `fm["sources"]` on a
  claim-bearing kind re-stamps it (CREATE, MERGE, CONTEST), so a target that predates the mirror
  gains one on its next merge and a target that has one stays in step with the union it just grew.
  There is deliberately **no backfill pass**: a concept published before this change carries no
  mirror, and no chip, until a run merges or contests into it. That is the cost of the "no re-render
  of notes the run did not touch" posture APPLY already holds, and it is the shape a later
  `agora repo upgrade` (#63) would relieve, not a gap this wave closes.
  A journal (`kind: note`) never receives one: `CLAIM_BEARING_KINDS` gates the emission to the same
  population `_is_sourced_kind` grades, and a derived view of a key no rule checks would be worse
  than none.
- **Never wrapped when the wrapping would lie.** An entry is mirrored only if `[[<entry>]]` reads
  back through the wikilink grammar as the entry itself, so a `raw/` path holding `|`, `[[` or `]]`
  keeps its `sources:` row and gets no chip. APPLY's own refs can never contain those characters;
  a converted or hand-made KB can.
- **Outside the brain's reach.** APPLY stamps the mirror before the PASS-2 snapshot, so the existing
  §4.2 check ("frontmatter is byte-identical across PASS 2") already refuses a brain that edits it.
  No sixth integrity check was added.

**Why the mirror and not a body citation — the measurement that decided it.** A body-carried
citation is not a display choice; it is a change to the ranking oracle. `Wiki.query_lexical` reads
note BODIES, `curator/bundle.py` feeds it every candidate's text to build the planning brain's
`related/` view, and that view picks `MERGE_INTO_THEME` targets — and a wrong merge is permanent,
because the closed ADR-0011 op vocabulary has no DELETE. A body `## Sources` block moved the result
order on **9 of 24** fixed queries; the frontmatter mirror moved **0 of 24** scores and **0 of 24**
orders, which is what a derived key that never enters the token stream must do.

*Where that figure comes from, and what is committed.* It is a ONE-OFF, owner-side measurement taken
on 2026-09-05 against a scratch copy of the author's private KB (24 queries, `query_lexical(limit=5)`
tuple comparison), and it is **not reproducible from this repository**: the corpus is private and the
harness lived in the measuring session's scratchpad, per the design brief, which also forbids citing
`tests/rank_golden/*.json` as neutrality evidence — those goldens never call APPLY and are
structurally blind to a write-path change. Cite it as the reason the decision was taken, not as a
figure a reader can re-derive. What IS committed, and what a future change has to keep green, is
`tests/core/test_rank_neutrality.py`: the mirrored and unmirrored renders of the same note parse to
equal `field_tokens` / `headings` / `outlinks` and to an equal `_note_to_dict`, `query_lexical`
returns byte-identical results over a mirrored corpus, and a control shows the rejected body block
moving all three.

The mirror also leaves the reader cache alone — not because frontmatter cannot reach the cache (it
plainly can: `_note_to_dict` serializes `kind`, `subjects`, `title`, `tags`, `status` and
`field_tokens`, and a frontmatter key is exactly what forced the last 2→3 bump), but because
`_note_to_dict`'s serialized field set is CLOSED and `source_links` is not one of its inputs. The
`_Note` shape is unchanged, so `CACHE_SCHEMA_VERSION` stays **3**. A future frontmatter key that
*does* feed one of those fields still bumps it. Footnotes were rejected on a second, independent
ground (below).

**2. L1-25 keeps the mirror honest, as the one L1 rule that is a WARNING.** A citation of a `raw/`
artifact that `sources:` does not carry is reported on all three surfaces a hand edit can reach:

- **every** `[[X]]` in the mirror key — deliberately with no `raw/` prefix test, because the key is
  derived and holds nothing else when APPLY wrote it, so an entry that is anything else is a hand
  edit to a key no rule reads as provenance. `X` is matched against `sources:` with the `.md`
  extension present or absent on either side, because Obsidian resolves those identically;
- each body markdown-link target that names a `raw/` artifact. Two spellings reach that: a target
  resolved **relative to the citing note** (`../../raw/general/x.md` from `wiki/concepts/x.md`), and
  a target already spelled repo-relative under `raw/`, which is taken **verbatim** — joining that
  one under the note's directory would give `wiki/concepts/raw/…` and the rule would never fire on
  the exact shape `sources:` carries and the schema's own example writes. Both branches are
  ratified here; the asymmetry they imply (`./raw/x.md` from a `wiki/` note is not a citation) is
  accepted as the price of the second one. The surface is the ADR-0014 D3 body-link grammar
  (`_BODY_MDLINK_RE`) as-is, IMAGE-embed exclusion included: `![alt](../../raw/_blob/…)` is an
  asset reference under ADR-0010 §3.5, not a provenance claim, so it reaches neither L1-2 nor
  L1-25 and is graded by nothing. Widening the pattern here would fork the one grammar both rules
  share; the exclusion is ratified and documented instead (schema §3.4 item 3, LIMITATIONS §6);
- each footnote-definition payload naming a `raw/` path, bare or inside a markdown link.

It inherits the `_is_sourced_kind` gate,
so journals stay unscored, and it never re-asks L1-8's existence question. It is a **warning**
deliberately: the notes it fires on are hand edits and vault imports, an error would discard the
whole run's diff, and a run can never repair what failed it — while `agora repo upgrade` (#63) is
open there is no repair path at all. This is the first L1 rule that does not reject; the emitted
schema doc says so at the head of the L1 table.

L1-25 being a warning does **not** make a hand-written body citation safe, and the schema doc says
so where it teaches the form. A markdown link into `raw/**.md` also meets L1-2, which resolves every
`.md` link target to a BASENAME: if no note owns that basename the link is a hard **error** that
discards the run's whole diff; if a note does own it — the common case, since a capture is usually
named after the thing it is about — L1-2 is silent and the link instead resolves to a *different*
file than it names, entering the body link graph `query_lexical` ranks on. Neither outcome is
repaired by this wave, and both are why the emitted form is a frontmatter key.

**3. Footnote `[^n]` citations are demoted, and the reason is a permanent two-renderer split.** The
schema doc recommended them since ADR-0010. Measured: CommonMark — the parser the web face uses —
reads `[^1]: raw/general/x.md` as a **link-reference definition**, so the marker renders as an
anchor pointing at an unrewritten relative path (a 404), and a payload that is itself a markdown
link is mangled; Obsidian reads the same bytes as a footnote. The recommended form was therefore
broken on one face and fine on the other, in a way no amount of care in the brain prompt fixes.
Adding a footnote plugin is not additive either — it would change how already-committed `[^n]:`
bytes parse. The schema now instructs the brain to write no inline `raw/` citation at all; the
citation is attached for it.

**4. Read-side decisions this addendum acknowledges as binding, though they change nothing in the
layout.** They are recorded here because they are cheap to make and expensive to revoke, and because
the ADR is where a later change has to come to argue with them:

- **The web URL shape is `GET /raw/{path}`, where `{path}` is the citation with its stored `raw/`
  prefix removed** (#169 D6 / #169 OD-7 — the design brief's own OD numbering, distinct from this
  ADR's ratified OD-1..OD-10): `sources: raw/ai-tech/x.md` → `/raw/ai-tech/x.md`, and
  `raw/_blob/<ab>/<sha256>.<ext>` → `/raw/_blob/<ab>/<sha256>.<ext>`. Keeping the prefix would give
  `/raw/raw/…`. The stored identity D3.4 froze is never truncated — the handler, `kb_read` and the
  CLI all take the citation string verbatim; only the URL drops the duplicated segment. This shape
  ends up in bookmarks, agent transcripts and cross-face tests, so changing it later is a break.
- **Blob bytes never leave over MCP** (#169 D5). `kb_read` on a `raw/_blob/…` citation returns the
  sidecar's capture facts and no bytes: no base64 field, no opt-in parameter. Four reasons, one of
  which is structural — blob bytes are the only content in the repo that passed neither the curator's
  grading, nor the ADR-0007 candidate gate, nor ADR-0023 redaction — and one of which is arithmetic:
  a 25 MiB PDF is ~33 MiB of base64 that no model can read. The download is the web route, which
  serves every blob as `application/octet-stream` with `nosniff` and `attachment`, never as the
  sidecar's `media_type` (that field and the stored `<ext>` are both uploader-chosen). **A future
  proposal to reverse this must cite this paragraph and retire it explicitly**, the posture D1.4
  takes on content-addressing.
- **The `raw/` bridge is an Agora→agent emission path under ADR-0027 §8**, named there by an
  append-only addendum of its own rather than folded silently under R1. It is the first such path
  serving content the curator never graded, and it inherits R1 (#166) — the pull-surface control that
  is still undesigned — rather than being covered by any control that exists today. Recorded per
  **#169 OD-5** (the design brief's owner decision to ship wave A without #165 — *not* this ADR's own
  ratified OD-5, which is the `agora export` / OpenKB interop format), and consistent with R1's own
  prohibition on citing the push exclusion as a pull control.
