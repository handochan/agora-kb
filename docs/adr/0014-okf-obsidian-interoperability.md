# ADR-0014 — OKF + Obsidian interoperability (amends ADR-0010)

**Status:** Proposed · 2026-06-17 · _amends, does not repeal, ADR-0010_

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

**D3 — Linking: wikilink-canonical + deterministic OKF export (recommended).** The curator keeps
authoring `[[basename]]`; ADR-0010 D5's frozen resolver and the L1-2/L1-6 grammars are UNCHANGED
(lowest risk). Because `[[basename]] → path` is already a total function, a pure, deterministic
**OKF link export** renders a conformant view with standard markdown links
(`[Title](/wiki/<domain>/themes/<slug>.md)`). Since OKF consumers MUST tolerate "broken" links, the
raw repo is already a non-rejected OKF bundle; the export upgrades it to graph-traversable. An opt-in
`repo.yaml` switch enables **dual-emit** (both `[[basename]]` and the resolved markdown link inline)
for repos that want a natively-graph-traversable OKF bundle with no export step. _(Standard markdown
links also resolve in Obsidian, so switching the canonical form to OKF links remains a future option;
it is rejected NOW only because it would rewrite the integrity-critical curator/lint grammar.)_

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
- **−** Linking carries a dual representation (`[[]]` canonical + derived markdown links); a resolver
  bug could desynchronize the two. Mitigated by making the markdown form a pure one-way derivation.
- **−** OKF v0.1 is five days old and explicitly "a starting point"; it may evolve (possibly
  breaking). Mitigated by adopting only the stable core (frontmatter fields + bundle shape), which is
  good markdown hygiene regardless, and by OKF's backward-compatible-growth pledge.
- **−** `type` is semantically overloaded (structural enum here vs concept-kind in OKF); harmless
  (OKF tolerates any string) but worth documenting.
- **−** Our `index.md` carries frontmatter, which OKF reserves as a no-frontmatter listing; tolerated
  by conformant consumers but technically non-ideal (see open questions).

## Open questions (to ratify before implementation)

1. **Linking (D3) ratification:** wikilink-canonical + OKF export (recommended), dual-emit by default,
   or eventually switch the canonical graph to standard markdown links?
2. **`summary` vs `description`:** emit both (redundant value), make `description` canonical with
   `summary` a read-accepted alias, or keep `summary` and only add `description` on the OKF export?
3. **`index.md`:** accept the frontmatter superset (tolerant consumers won't reject), or emit a
   separate OKF-plain listing on export?
4. **Defaults:** emit the OKF fields at `repo init` by default, or behind a `--okf` flag /
   `repo.yaml` switch?
5. **Timestamp fidelity:** is the deterministic `<updated>T00:00:00Z` acceptable, or does any consumer
   need true sub-day datetimes (which would break D1 replay determinism)?
6. **`log.md`:** reshape to OKF's date-grouped-newest-first format, or keep the curator-run log and
   rely on OKF's tolerance?

## Implementation phasing (Phase-2; sequenced, each independently shippable)

1. **Tolerant consumer read boundary (D4)** — STARTED: the `frontmatter.parse` robustness fix shipped
   in the Phase-1 completion work makes read/lint/query total on malformed frontmatter. Extend to
   foreign links, unknown keys, and non-`.md` files; cover with tests over real-vault fixtures.
2. **OKF producer fields (D2)** — emit `description`/`resource`/`timestamp`/`okf_version`; update the
   schema doc + emit + lint (additive rules only).
3. **OKF link export (D3)** — the pure `[[basename]] → /path.md` renderer; optional dual-emit switch.
4. **Obsidian import/normalizer (D5)** — the opt-in admin op that makes a real vault (`~/knowledge`)
   curate-able by choice.
