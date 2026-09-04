# ADR-0014 — OKF + Obsidian interoperability (amends ADR-0010)

**Status:** Accepted · 2026-06-17 · _amends, does not repeal, ADR-0010_
**AMENDED (append-only) — [ADR-0041](0041-stratum-kind-first-layout.md) (Proposed, KB wiki schema 2):** D3's link FORM is unchanged (standard markdown body links, basename-keyed resolver) but the relative paths rendered change shape; D2's *"keep `type:`"* clause is affected — `type:` is retired as the KIND AUTHORITY and its survival as a derived OKF mirror of `kind` is ADR-0041 OD-3 (recommended: emit it, the `description`↔`summary` precedent); D5's importer places by KIND DIRECTORY (and `vault_import.py:359`'s `first_domain` catch-all is retired). **§Non-goals — SUPERSEDED IN ONE BULLET:** *"Do not adopt free-form `type` for produced notes (**the `index/moc/theme/daily` enum stays**)"*. That enum is replaced by the six-value `kind`. The non-goal's INTENT survives and is why this is a one-bullet supersede rather than a section one: free-form `type` is still not adopted — `kind` is a CLOSED vocabulary, closed AT THE DIRECTORY LEVEL (ADR-0041 D3.1 / lint L1-22), which is a stronger guarantee than a frontmatter enum a brain writes prose into. The other three non-goals are KEPT VERBATIM. **D6 is KEPT and is the clause OD-3 turns on:** "two orthogonal version axes … they evolve independently" means bumping `schema_version` 1 → 2 does not by itself change `okf_version` or OKF conformance; the only OKF-visible question is whether `type:` is still emitted as a derived mirror. D1/D4 (strict producer / tolerant consumer) are KEPT — and it is D1 that makes `wiki/people/**` linting advisory. The prose below is retained verbatim for history.

## Context

On **2026-06-12** Google Cloud published the **Open Knowledge Format (OKF) v0.1**
([spec](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md), Apache-2.0;
[announcement](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing/)).
OKF is a vendor-neutral standard that **formalizes the exact pattern Agora is built on** — the
Karpathy "LLM-wiki" (ADR-0010): a directory of markdown files with YAML frontmatter, *one concept per
file*, linked into a graph, shippable in git, readable by humans and agents. The press framing is
"the lingua franca for AI agent knowledge." This is not a tangential standard; it is *our* standard,
published by a major vendor five days before this ADR.

Two interoperability requirements now bear on the schema:

1. **OKF** — an Agora repo should be a *usable OKF bundle* (portable to any OKF-aware agent/tool), and
   Agora should be able to *consume* OKF bundles produced elsewhere (the Phase-2 harvester goal).
2. **Obsidian** — Agora must interoperate with real Obsidian vaults. Dogfooding Agora's schema
   against the owner's real `~/knowledge` vault (the ROADMAP Phase-0 compatibility item) found that
   **9 of 13 notes use Obsidian conventions Agora's strict v1 schema rejects** — and worse, the
   reader *crashed* (uncaught `yaml.ParserError`) on the Obsidian `links: [[a]], [[b]]` frontmatter.
   The crash is fixed (the read/lint/query path is now total — landed in the Phase-1 completion work),
   but Agora still *rejects* those notes for curation. So Agora is **not yet a tolerant consumer**.

**The central tension.** ADR-0010 deliberately freezes a **strict** schema so the curator's
deterministic post-INGEST gate (ADR-0008 step 4) and reproducible navigation (ADR-0009) hold; it
*hard-rejects* broken links, unknown tags, and malformed frontmatter. OKF mandates the **opposite**
for consumers: a conformant consumer **MUST NOT** reject a bundle for missing optional fields, unknown
`type` values, unknown frontmatter keys, broken cross-links, or a missing `index.md`. **Both are
correct for their job.** This ADR reconciles them.

### Agora v1 ↔ OKF v0.1 ↔ Obsidian — the mapping that drives the decision

| Aspect | Agora (ADR-0010) | OKF v0.1 | Obsidian | Note |
|---|---|---|---|---|
| Container | dir of `.md` + YAML fm, git | **same** | vault (same) | identical philosophy |
| One concept / file | `theme` | concept = file | note | aligned |
| Concept identity | globally-unique **basename** | **path** minus `.md` | path/basename | differ (basename vs path) |
| Required frontmatter | `title`,`type`,`created`,`updated`,`status`,`summary` | **`type` only** | none | Agora stricter |
| `type` | enum `index/moc/theme/daily` | **free-form** string | none | compatible (OKF tolerates any string) |
| One-line precis | `summary` | `description` | — | name mismatch |
| Resource URI | — | `resource` | — | Agora lacks |
| Timestamp | `created`/`updated` (date, run-clock D1) | `timestamp` (ISO-8601 datetime) | varies | shape differs |
| **Graph edges** | **`[[basename]]`** wikilinks | `[text](/path.md)` std md links | `[[wikilinks]]` | the key fork |
| `index.md` | a note **with** frontmatter + `children` | reserved, **no** frontmatter, listing | optional | minor conflict |
| `log.md` | append-only curator log | date-grouped history, newest first | — | close |
| Broken links | **hard reject** (L1-2) | **MUST tolerate** | soft | opposite philosophy |
| Unknown keys | strict (`extra=forbid`) | **preserve** | preserve | opposite philosophy |
| Version | `schema_version: 1` | `okf_version: "0.1"` (bundle-root index) | — | orthogonal axes |

The encouraging finding: Agora's `type: theme` *is* a valid (tolerated) OKF type; our `[[basename]]`
is Obsidian-native; and our `related:`/`children:` already store links as quoted `"[[basename]]"`
strings — the valid Obsidian "Properties" YAML form. We already share ~80% of the design.

## Decision

The reconciling principle, from which everything else follows:

> **Agora is a STRICT OKF *producer* and a TOLERANT OKF/Obsidian *consumer*.**

The strict internal schema (ADR-0010) continues to govern everything the curator **writes**; a new,
explicitly-tolerant **boundary** governs everything Agora **reads, harvests, or imports**. The
curator's deterministic integrity gate is never weakened.

The concrete target is a **single committed repo that is natively all three at once** — a valid git
repo, a working Obsidian vault, AND a conformant, graph-traversable OKF bundle — with **no build or
export step** (achieved by the link representation in D3). There is no hard incompatibility between
the three; the only design choice with real cost is the graph-link form.

**D1 — Producer/consumer split (the load-bearing decision).** ADR-0010's strict lint applies to
curator-produced notes ONLY. A separate "external read" posture (D4) applies to foreign input and is
deliberately tolerant. The two never mix: tolerated foreign content is never published into the
curated tree without passing the strict producer lint (via the import/normalizer of D5).

**D2 — Emitted repos are conformant OKF bundles (additive superset).** `agora repo init`/emit and the
curator produce frontmatter that satisfies OKF while remaining ADR-0010-valid:
- keep `type` (the `index/moc/theme/daily` enum is a valid OKF `type` string; document the dual
  meaning — structural here, "concept kind" in OKF's examples);
- emit `description` (the OKF one-line field) carrying the same value as `summary` (see open question
  on alias-vs-rename); `title` is already required;
- add an OPTIONAL `resource` (URI) on themes — a canonical external locator for the concept;
- emit a DETERMINISTIC `timestamp` derived from `updated` (`<updated>T00:00:00Z`), so OKF's
  "last meaningful change" is satisfied WITHOUT reintroducing a wall clock (preserves ADR-0010 D1
  replay determinism); `created`/`updated` stay canonical;
- emit `okf_version: "0.1"` in the bundle-root `index.md` frontmatter;
- `tags` stay the CLOSED taxonomy for produced notes (ADR-0010 D6 intact); OKF's free tags apply only
  to foreign bundles on read.

**D3 — Linking: one representation native to all three (recommended).** The one link form that is
simultaneously native to git, Obsidian, AND OKF is the **standard markdown link in the note BODY**
(`[Title](<relative-path>.md)`): git/GitHub renders it, Obsidian resolves it as a first-class graph
edge (backlinks + graph view), and OKF treats it as a conformant relationship edge. So the curator
emits **markdown links in bodies**, and frontmatter `related:`/`children:` stay `"[[basename]]"`
strings — the Obsidian "Properties" native form, which OKF preserves as extra keys. Agora's internal
globally-unique **basename identity (ADR-0010 D5) is retained**; the resolver maps basename↔path and
the curator emits the resolved relative path. The cost is a CONTAINED, one-time change to the
integrity-critical link grammar — L1-2 link resolution and the L1-6 MOC child-bullet grammar move
from `[[ ]]` to markdown-link targets, and the curator emits markdown links — accepted because it
buys PERMANENT native tri-compatibility with NO export/dual-representation to keep in sync. The exact
relative path form that resolves in BOTH Obsidian and OKF (relative `./` vs bundle-absolute `/`) is
pinned during implementation. _(Lower-effort fallback: keep `[[basename]]` canonical and add a pure
`[[basename]] → /path.md` OKF export — viable because OKF consumers MUST tolerate the un-exported
wikilinks as "broken" — chosen only if we decide to defer the grammar change.)_

**D4 — Tolerant consumer/read boundary.** The READ / harvest / import path MUST NOT crash on foreign
input. Concretely it MUST tolerate, surfacing a finding or a skip rather than an error: malformed YAML
frontmatter (SHIPPED — `frontmatter.parse` now wraps `yaml.YAMLError` in the typed `FrontmatterError`,
so `kb_query`/lint/harvest degrade); unknown frontmatter keys (ignore, preserve on round-trip); broken
or foreign links (`[[]]` or markdown) — no crash, simply no resolved edge; missing optional fields and
a missing `index.md`; and non-`.md` files (`.canvas`, `.obsidian/`) — ignore. This boundary is
ORTHOGONAL to the curator's strict producer lint.

**D5 — Obsidian tolerance + opt-in normalizer.** Agora reads Obsidian "Properties" (YAML lists of
`"[[x]]"` strings — already supported) and tolerates, but does NOT emit, Obsidian-only constructs
(block-refs `^id`, transclusions `![[..]]`, `cssclass`) — consistent with ADR-0010's existing
portability guidance. A new OPT-IN admin op (`agora import` / a vault normalizer) migrates an external
Obsidian vault's frontmatter and layout into ADR-0010-conformant form (on a backup branch), so a real
vault like `~/knowledge` becomes curate-able by EXPLICIT CHOICE — never by silent auto-mutation.

**D6 — Two orthogonal version axes.** A repo carries BOTH `schema_version` (Agora's internal editorial
schema, currently `1`, ADR-0010 D6/L1-17) and `okf_version` (external OKF-conformance, `"0.1"`). They
evolve independently: an internal schema bump need not change OKF conformance and vice-versa.

### Non-goals (explicit boundaries)

- Do **not** relax the curator's internal lint to "tolerant" — produced notes stay strictly gated
  (ADR-0008/0009 integrity preserved).
- Do **not** adopt free-form `type` for produced notes (the `index/moc/theme/daily` enum stays).
- Do **not** auto-migrate or mutate external vaults; import/normalization is an explicit opt-in op.
- Do **not** introduce a database or persisted graph index (Invariant 1 — markdown stays the source
  of truth; any OKF export/graph view is rebuildable).

## Consequences

- **+** Portability/interop: an Agora repo is a usable OKF bundle and Agora can consume OKF bundles —
  no lock-in, directly serving Invariants #1/#4/#6 and the "shared-memory hub for agents" thesis.
- **+** One effort, two payoffs: the tolerant-consumer boundary (D4) is exactly what's needed to
  consume real Obsidian vaults, so OKF-compat and Obsidian-compat are largely the same work.
- **+** External validation: OKF independently confirms Agora's foundational bet (markdown+git+
  frontmatter LLM-wiki) is the right pattern.
- **+** Additive and low-risk: the integrity-critical curator/lint/resolver (ADR-0010 D5, L1-2/L1-6)
  is unchanged; this adds fields, a pure export, and a separate read boundary.
- **−** A strict-producer/tolerant-consumer boundary must be policed: tolerated foreign content must
  never reach the curated tree except through the strict producer lint.
- **−** Adopting standard-markdown-body-links (D3) is a one-time change to the integrity-critical
  link grammar (L1-2/L1-6) and curator emission; accepted because it yields permanent native
  tri-compatibility with no ongoing export or dual-representation to keep in sync.
- **−** OKF v0.1 is five days old and explicitly "a starting point"; it may evolve (possibly
  breaking). Mitigated by adopting only the stable core (frontmatter fields + bundle shape), which is
  good markdown hygiene regardless, and by OKF's backward-compatible-growth pledge.
- **−** `type` is semantically overloaded (structural enum here vs concept-kind in OKF); harmless
  (OKF tolerates any string) but worth documenting.
- **−** Our `index.md` carries frontmatter, which OKF reserves as a no-frontmatter listing; tolerated
  by conformant consumers but technically non-ideal (see open questions).

## Resolved decisions (ratified 2026-06-17)

1. **Linking (D3): standard-markdown-body-links** — the single form native to git + Obsidian + OKF;
   no export/dual-emit. Internal basename identity (ADR-0010 D5) retained. (The wikilink-canonical +
   export fallback is recorded but NOT chosen.)
2. **`summary` + `description`:** keep `summary` as the internal canonical one-line field (no rename
   now — avoids churning every fixture/lint rule) AND emit `description` carrying the same value for
   OKF conformance. They MAY converge in a later `schema_version` bump.
3. **`index.md`:** accept the frontmatter superset — conformant OKF consumers MUST NOT reject for it;
   no separate OKF-plain listing.
4. **Defaults:** OKF fields are emitted BY DEFAULT at `repo init` and during curation — OKF
   conformance is the default posture, not an opt-in flag.
5. **Timestamp:** the deterministic `<updated>T00:00:00Z` is accepted (preserves ADR-0010 D1 replay
   determinism); no wall-clock datetime is introduced.
6. **`log.md`:** keep the curator-run log format for now (OKF tolerates it); reshaping to OKF's
   date-grouped-newest-first is deferred to a later increment.

## Implementation phasing (Phase-2; sequenced, each independently shippable)

1. **Tolerant consumer read boundary (D4)** — DONE. The `frontmatter.parse` robustness fix (Phase-1)
   wraps malformed YAML in the typed `FrontmatterError`; `core.Wiki` and `schema.notes.parse_all_notes`
   now decode non-UTF8 notes LOSSILY (`errors="replace"`) so `kb_query`/lint never crash on a foreign
   vault (the byte-level L1-16 scan stays the producer's encoding gate); broken links, unknown
   frontmatter keys, and non-`.md` sidecars (`.canvas`/`.obsidian`) were already tolerated. Locked in
   by `tests/core/test_wiki_tolerant.py` over an adversarial vault.
2. **OKF producer fields (D2)** — DONE (emit `description`/`timestamp`/`okf_version`; `resource`
   documented as accepted-on-read).
3. **Standard-markdown body links (D3)** — DONE (MOC/index body bullets are markdown links; frontmatter
   `related:`/`children:` stay `[[basename]]`; read path + lint resolve both).
4. **Obsidian import/normalizer (D5)** — DONE (the opt-in `agora import` admin op; verified to make a
   `~/knowledge` clone lint-clean + curate-able, non-destructively).

## Addendum — D1 applies INSIDE `wiki/` too (2026-09-04, issue #152)

**Status:** Accepted · amends nothing above; it FIXES an unimplemented half of D1.

D1 says the strict lint applies to **curator-produced notes ONLY**. The curator did not implement
that qualifier: its §4.4 gate graded every note in the worktree, so a note a human wrote in `wiki/`
was scored as curator output. Measured on a real repo, one hand-written note produced:

| what the human wrote | result before | after |
|---|---|---|
| a schema-valid note | `published`, bytes preserved | unchanged — `published`, bytes preserved |
| an Obsidian Properties block (`tags:`/`aliases:`/`cssclass:`) | `failed` — `LINT L1-11 unknown or missing 'type'` | `published`; the note is read, not graded |
| no frontmatter at all (Obsidian's default) | `failed` — `LIVE-TREE: unparseable note` | `published`; the note is read, not graded |

No data was ever lost — the human's file is byte-identical in every row. What was lost is
**curation itself**, permanently: the offending file lives in the base commit, so every subsequent
run re-read it and re-failed. D4's tolerance machinery already existed; it simply was not wired to
this path. And the stated goal of "one repo that is at once a git repo, an Obsidian vault and an
OKF bundle, with no build step" cannot survive a rule where saving a note in Obsidian stops the
curator forever.

### Decision

The producer gate keeps its subject: **what the curator produced.** Two discriminators, unioned:

* **(a) this run's output** — every path the run's plan created or modified, read from the run's own
  `git diff --name-status` against `base_commit` (the same view the final-diff gate grades). This is
  the authoritative list of what the run is answerable for.
* **(b) the curator's earlier output** — every note whose frontmatter carries a key only the ENGINE
  writes: `sources:` (ADR-0011 §2: "the WORKER writes `sources:`, never the model") or `timestamp:`
  (D2's deterministic `<updated>T00:00:00Z`, stamped on every note type APPLY creates or re-renders,
  including the seed `index.md`). Presence of **either** is the curator's stamp.

A note carrying no stamp is a **human note**. It is:

* **left byte-identical** — the curator never writes it (unchanged; that was already true);
* **excluded from the MERGE/CONTEST target registry** (`theme_basenames`), so it can never be
  merged into — and **withheld from the `related/` view** the planning brain picks targets from
  (`build_bundle(mergeable_paths=…)`), which is the half that makes the exclusion safe rather than
  merely correct. Registry exclusion on its own is not a no-op for the operator: PASS 1 is
  instructed to choose `MERGE_INTO_THEME` targets out of `related/<id>.json`, an unstamped note
  that lexically overlaps a candidate is a legitimate top hit there, and naming it is a `BASENAME`
  plan error — which fails the WHOLE run, not just that disposition. That is the same
  fails-every-run-forever shape this addendum exists to remove, re-entering through the plan gate,
  so the engine must not hand the model a menu item it will then be punished for choosing. The
  filter is a pure path-set drop over an already-computed `QueryResult` (no `lex`/`struct`/`fm`/
  `score`/`match_reason` is recomputed outside core, ADR-0012 §0a). The set handed to
  `mergeable_paths` is the curator's own **THEME** notes, not merely its own notes: the plan gate
  accepts a MERGE/CONTEST target only out of `theme_basenames`, so a curator daily, MOC or
  `index.md` in that view is exactly as run-killing a pick as a human note. Because the filter runs
  AFTER retrieval, the fetch over-fetches and trims back to `related_k`, so the view still carries
  K *eligible* hits — an emptied view is what tells PASS 1 "genuinely new → `CREATE_THEME`", i.e. a
  duplicate theme beside the note it should have merged into;
* **kept in the basename-collision registry** (`live_basenames`) — its basename stays RESERVED.
  This is a deliberate refinement of "excluded from the plan registry": APPLY writes
  `wiki/<domain>/themes/<basename>.md` unconditionally, so a CREATE_THEME reusing a human note's
  basename would **overwrite the file**. Uniqueness-reservation is what prevents that, and it is the
  one part of the registry a human note must stay in. Link resolution follows the same set, so a
  curator note may legally link to a human note and the L1-2 check resolves it.
* **still read** — `kb_query`/BM25F, `kb_read`, the graph and the index cache all see it
  (`wiki_dir.rglob("*.md")` has no schema filter), so tolerant admission buys search coverage;
* **reported** — one `LIVE-TREE:` warning on the (published) run report naming the notes, a
  `unmanaged_notes` count in the report and in `health()`, and an `agora doctor` `notes:` line.

**Strictness is scoped, not dropped.** `parse_all_notes(strict=True)` no longer aborts the run on
the first unparseable note; instead the scan classifies. A malformed note that still visibly carries
the stamp is a damaged **curator** artifact — a real integrity signal — and fails the run exactly as
before, naming the file. An unparseable note with no stamp is somebody's draft.

**Three paths are the curator's by construction, stamp or no stamp.** APPLY re-opens the root
`index.md` (`_update_index`), the domain MOC `wiki/<domain>/<domain>-moc.md` (`_update_moc`) and the
per-domain daily (`_apply_append_daily`) with an *unguarded* `frontmatter.parse`, and — unlike a
MERGE/CONTEST target, which must first clear `theme_basenames` — none of them is gated by the plan.
Classifying a malformed one of those as "somebody's draft" would not tolerate it; it would let it
reach APPLY and raise out of `run()`, stranding the claimed batch in `_kb/processing/` while
`agora status` reports `failed_events: 0` and every `agora watch` tick re-claims and re-crashes —
strictly worse than the hard rejection this addendum removes. So a malformed note at one of those
three paths is classified as a damaged curator artifact and fails the run, named. The theme path a
human actually saves into is untouched by this carve-out. A second, independent guard backs it up:
a `FrontmatterError` escaping `apply_plan` is caught at the call site and routed through the same
clean `_fail`, so the §4 "never an uncaught traceback out of `run()`" contract does not depend on
the classification being exhaustive.

**No rule and no severity changes.** L1 severities stay frozen by `kb_schema.md`; the closed ADR-0011
op vocabulary is untouched; the final-diff allowlist and L1-9 are untouched. `schema.lint.lint()`
gains one optional `scope` argument; omitted (every read-only surface — dashboard, `kb_status`,
`agora doctor`) it grades the whole worktree byte-identically, and the dashboard deliberately keeps
doing so: a health panel reports the tree honestly, and `unmanaged_notes` is what tells an operator
why a finding is not being fixed by the next run.

### Rejected alternatives

* **Downgrade L1-11 (or L1-4) to `warning`.** `schema/lint.py` states that every L1 rule is `error`
  because `kb_schema.md` freezes L1 as "STRUCTURAL (hard; reject the commit)". That is a
  **schema-visible** change requiring a `schema_version` conversation (#98), and it would weaken the
  gate for the curator's OWN output — the opposite of D1. The problem was never the severity; it was
  the subject.
* **Discriminator (c): git authorship** (a note is the curator's iff a curator commit created it).
  Rejected twice over: the history is absent from a fresh clone (and from any squashed/imported
  repo), so the classification would not survive a re-clone; and a human hand-editing a curator note
  is legitimate and would silently reclassify it. The frontmatter stamp is a pure function of the
  bytes in the tree, which is the same property every other deterministic gate here relies on.
* **Forbid human writes in `wiki/`** (the OpenKB posture inverted). Nothing enforces it without a
  write boundary Agora does not have until Phase 4, and it contradicts the no-build-step vault goal.
  The OpenKB posture itself — accept anything, overwrite it later ("manual edits to those pages are
  overwritten") — is rejected outright: it trades the user's bytes for convenience.

### Known residuals (recorded, not fixed)

* A curator note published by a build predating D2 that is a `moc`/`index` (no `sources:`, no
  `timestamp:`) classifies as human until the curator next re-renders it — at which point APPLY
  stamps it. The consequence is bounded: it is not lint-graded for those runs, and a `moc`/`index`
  is not a MERGE/CONTEST target anyway.
* A human note that happens to carry `sources:` or `timestamp:` is graded as producer output — i.e.
  exactly today's behaviour. The discriminator is deliberately conservative in that direction.
* **Promotion is unspecified.** Turning a human note into a curated one (a normalizer op, or
  re-harvesting it as a candidate the way `notes/` content is) is out of scope here; the `notes/`
  lane and tolerant `wiki/` admission coexist for now. Open in #152.
* **A human note in `wiki/<domain>/themes/` IS adopted as a MOC child.** An earlier draft of this
  addendum claimed the opposite ("only APPLY writes MOC children and it works from the plan").
  APPLY's `_update_moc` derives the child set from a DIRECTORY GLOB (`themes/*.md`) unioned with
  the run's new basenames, so any note dropped in that folder lands in the MOC's `children:` and in
  a body bullet the next time the domain is touched. Kept deliberately — it is the friendly
  Obsidian story (your note appears on the map of its domain) and the link resolves, since human
  basenames stay in the link-resolution set. The bounded cost: the MOC is a graded producer
  artifact, so if the human later deletes or renames that note, the MOC's `L1-2` broken-link check
  fails until a run re-globs that domain. Intersecting the glob with the curator's own paths is the
  alternative if that cost ever bites.
* A human note whose frontmatter block is FENCED but YAML-invalid **and** contains a line starting
  `sources:` / `timestamp:` is classified as a damaged curator artifact and fails the run — the
  conservative direction of the textual discriminator, stated under "Strictness is scoped, not
  dropped" above. `sources:` is an ordinary property name in a vault, so this is the one Obsidian
  shape that can still stop the curator; the failure names the file, and removing the key or fixing
  the YAML clears it. Narrowing the textual stamp (requiring the full engine shape) was rejected:
  it would reclassify a genuinely damaged curator note as somebody's draft, which is the failure
  direction that loses integrity rather than availability.
* A leading UTF-8 BOM does NOT demote a curator note to "human": the fence test strips it before
  looking for the stamp, so a BOM'd producer note stays a damaged producer artifact and fails
  loudly. Without that strip it would have been reclassified as human and dropped out of the lint
  scope where `L1-16` — the rule that exists to catch exactly that damage — reads it. This matters
  natively on Windows (epic #85), where PowerShell `Set-Content`/`Out-File` write UTF-8-with-BOM by
  default.
