# ADR-0022 — Curator taxonomy governance: governed domain auto-creation + per-domain customization

**Status:** Accepted · 2026-07-05 (Step-0 ratified, #36) · Proposed 2026-06-24

Covers backlog issues **#23** (auto-create & classify domains) and **#24** (per-domain custom
processing). Extends — does **not** relax — [ADR-0010](0010-kb-wiki-schema.md) **D6** (taxonomy is a
fixed, read-only INGEST input) and [ADR-0011](0011-curator-ingest-contract.md) §6.1 / §4.0 / §4.1
(the deterministic INGEST contract: closed op vocabulary, curator-writable allowlist,
model-independent validator). Builds on [ADR-0015](0015-per-task-brain-routing.md) (per-act brain
routing), [ADR-0007](0007-memory-harvester-safety.md) (the candidate gate), and
[ADR-0002](0002-cqrs-single-writer-curator.md) / [ADR-0008](0008-transactional-sandboxed-curation.md)
(the single-writer curator + the transactional CAS publish). The harvester-side connector work that
feeds gated candidates into this lane is [ADR-0023](0023-context-harvester-connectors.md); the bulk /
domain-sharded-batch lane that per-domain PLAN routing is deferred into is
[ADR-0024](0024-bulk-processing-horizontal-curator-scale.md). Lands in the Phase-4/5
curator-governance era (ROADMAP), though the no-loss floor (§Decision A) ships now in the pulled-forward
Phase 3.5 slice.

## Context
Two backlog asks both reach into the curator's relationship with `_meta/taxonomy.yaml` and both
collide with a deliberately-closed design decision, so they are recorded together.

**#23 — facts are silently dropped when no domain matches.** Today the taxonomy is a FIXED, read-only
INGEST input. `_meta/taxonomy.yaml` carries `domains` + `allowed_tags`; it is loaded by
`config._load_taxonomy` (`src/agora_kb/config.py`), modeled by `schema/emit.py` `Taxonomy`
(`extra='forbid'`; `domains: tuple[str, ...]`), and copied read-only into the curator bundle by
`bundle._copy_read_only_inputs` (`bundle/taxonomy.yaml`, `bundle.py:295`). The ONLY path that seeds a
domain is repo creation — `agora repo init --domain …` (default `("general",)`) and
`agora import --domain …` (`cli.py`); there is **no** `agora taxonomy` command. A fact whose best-fit
domain is not already in `domains` is dropped **twice**: the default Ollama brain pre-empts the
validator at `adapters/ollama_brain.py` step 3 ("Domain selection", ~line 438) — for a basename op, if
neither the model's chosen `domain` nor the candidate's hint is in `domains`, it forces `op = "DROP"`
(there is no "route to general" fallback); and independently the PLAN validator rejects any disposition
whose `domain ∉ domains` (`plan.py` check 4 TAXONOMY, plan.py:310) and any CREATE_THEME/APPEND_DAILY
whose basename implies a missing-domain path (check 5 BASENAME, plan.py:336). A rejected plan discards
the whole run. The closed op set
`OPS = {CREATE_THEME, APPEND_DAILY, MERGE_INTO_THEME, MARK_CONTESTED, DROP, NOOP}` (`plan.py:58`)
contains **no domain-creating op**, and ADR-0011 §6.1 / ADR-0010 D6 state plainly that no op may widen
the taxonomy.

A governance primitive already exists but is **inert and unconditionally permissive**:
`taxonomy_policy: open | review-only | capped:<N>` is stored on `Taxonomy` with the code default
`taxonomy_policy: str = "open"` (`emit.py:60`) and parsed by lint with the same unconditional fallback
`loaded.get("taxonomy_policy", "open")` (`lint.py:187`). L1-18 is *specified* to gate a
`(before, after)` taxonomy diff but is documented as NOT evaluated during INGEST — only on "the
SEPARATE admin/human evolution path" (`lint.py:36` and `lint.py:494`-onward; kb_schema.md §5.2) — a path
no command currently implements. Critically, L1-18 today diffs **only**
`taxonomy.allowed_tags(after) − taxonomy.allowed_tags(before)` (kb_schema.md §5.2 prose + the L1-18 row,
"newly-added `allowed_tags` keys violate `taxonomy_policy`"); it **never looks at `domains`**. So #23
is not a prompt tweak: it requires deciding **how a new domain may be born without re-opening the
vocabulary-widening backdoor D6 deliberately closed**, against a small/poisoned local model and against
ADR-0007 gated candidates.

**#24 — some domains deserve bespoke handling.** There is no per-domain customization concept in the
codebase; "domain" is purely a path-namespace token validated against the fixed taxonomy. The brain is
selected per repo and optionally per **act** (`plan`/`author`) — never per domain — and the
routable-act key-space is a deliberately CLOSED set `_ROUTABLE_ACTS = ("plan", "author")`
(`curator/backends.py:102`), co-extensive with the two `worker.Backend` methods; ADR-0015 states
per-op/per-tier routing is unsupported in v1 because PASS-1 plans the whole batch in one `plan()` call.
The prompts are static module constants — `_PASS1_PROMPT`, `_PASS2_GROUNDED_PROMPT_TEMPLATE`, per-OP
(not per-domain) `_OP_INSTRUCTIONS` (`curator/subprocess_backend.py`). Deterministic tunables that
*could* vary by domain (`body_byte_bound`, default `_DEFAULT_BODY_BYTE_BOUND = 8192`,
`subprocess_backend.py:195`; `max_orphans`; `related_k`, default `_RELATED_K = 8`, `bundle.py:53`) are
documented in ADR-0011 §1.3 but are hardcoded constants and silently-ignored `repo.yaml` keys today
(`load_repo_config` parses only `curator.{backend,max_attempts,allow_reduced_isolation,triggers}`,
`config.py:135`-on; DATA-MODEL §3: "Any other keys … are silently ignored"). The data dependency for
per-domain work already exists: `bundle.py` writes a per-candidate `domain` into `candidates.json`
(`bundle.py:158`), and AUTHOR is invoked **once per region** (`subprocess_backend._pass2_prompt` per
`AuthorRegion`), so a per-region/per-domain AUTHOR dispatch is a clean seam while per-domain PLAN is
not.

**The shared crux.** Both asks press on the same surface: #23 wants the curator to *create* a domain;
#24 wants per-domain *handling*. The non-negotiable property to preserve is ADR-0011's "success is a
pure function of (plan.json, git_diff, manifest, bundle, lint)" with a **domain-agnostic,
model-independent** integrity gate — so a poisoned model cannot widen the vocabulary (#23) and a
"special" domain cannot relax what is admissible (#24). The two asks also share a per-domain keyspace,
so the `_meta/taxonomy.yaml` domain-entry shape must be settled **once**, now, so #24 is purely
additive over #23.

This collides with load-bearing decisions (D6, §6.1, §4.0), so per the repo invariants it requires an
ADR.

## Proposed Decision
Establish a **governed taxonomy-evolution lane** and a **tuning-surface-only per-domain customization
lane**. The sandboxed brain may **NEVER** directly widen `_meta/taxonomy.yaml`; that property of D6 is
preserved exactly. The relaxation is narrow and deterministic.

**(A) No-loss floor — ship first, invariant-neutral (#23).** The deterministic no-loss floor is the
**first declared domain `domains[0]`** (list order) of the fixed taxonomy — **for a default repo this
is exactly `general`** (the `repo init` seed), and every repo created via `agora repo init` /
`agora import` has ≥1 domain, so `domains[0]` always exists. *(Amended 2026-07-05 during Phase-3.5
implementation: an earlier draft named a "reserved `general` domain guaranteed to exist"; that premise
is false — `agora repo init --domain foo --domain bar` produces a taxonomy with no `general`, and
nothing injects it at load. Using `domains[0]` needs zero taxonomy mutation, passes the check-4
TAXONOMY validator by construction, and unifies with the import lane, which already treats the first
domain as its catch-all — see `vault_import.py`. Materializing a literal `general` was rejected: it
would require an INGEST-time `_meta/taxonomy.yaml` write (breaks D6) or a config-vs-bundle taxonomy
divergence.)* `adapters/ollama_brain.py` step 3 is changed so a basename op whose domain cannot be
resolved against the taxonomy falls back to `domains[0]` **instead of `op = "DROP"`** (removing the
silent downgrade-to-DROP at ~line 438) for a NON-gated durable capture; because `parse_taxonomy`
collapses `domains` to an unordered set, a small helper recovers the ordered `domains[0]` before that
collapse. The prompt (`kb_schema.md`) is updated to instruct "route to the first declared domain (the
catch-all) rather than DROP when unsure of domain". A gated candidate (`is_gated`, computed in
`bundle.py`) keeps its gate verbatim — it may still only MERGE/CONTEST/DROP and can never originate
(the step-2 gate runs before step 3, and `GATE_ALLOWED_OPS ∩ _BASENAME_OPS = ∅`, so the floor is
structurally unreachable for gated candidates). This alone makes "never drop a fact merely for lack of
a domain" literally true with near-zero risk and no contract change.

**(B) Structured domain entry shape (#23+#24 coupling).** `domains` in `_meta/taxonomy.yaml` MAY be
either the existing `list[str]` (back-compat) **or** a mapping `{<domain>: {status, created,
created_by}}`. This mirrors the list-or-mapping PATTERN `allowed_tags` already follows — but `domains`
is **list-only in all three readers today**: `config._load_taxonomy` (the inline comment "domains is a
list" at `config.py:416`), `ollama_brain.parse_taxonomy` (accepts `(list, tuple, set)` only,
`ollama_brain.py:231`), and `schema/lint.py` (`isinstance(domains_value, list)`, `lint.py:185`). So the
list-or-mapping normalizer is **net-new work in each of the three readers** — not an existing-tolerance
freebie — and must be added + tested in all three. A single normalizer collapses both forms to the
domain-name set + metadata for the validator/bundle; `Taxonomy(extra='forbid')` stays satisfied (the
mapping is normalized before the model is constructed, so the `extra='forbid'` guard is untouched).
`status ∈ {proposed, active}` lets an auto-created domain be marked provisional and surfaced for
triage; #24's per-domain config attaches additively to this same record.

**(C) Governed domain creation — a new CLOSED op, applied by the deterministic WORKER (#23).** Add
`CREATE_DOMAIN` to the closed op vocabulary (`plan.py` `OPS`, ADR-0011 §2, INGEST-CONTRACT, the emitted
`kb_schema.md`). The brain only **PROPOSES** a name + reason; the deterministic **WORKER** (the single
writer) materializes it: it slugifies/dedupes the name (reusing the basename-uniqueness pattern already
in `ollama_brain.py`), writes `_meta/taxonomy.yaml`, and creates the lazy MOC. APPLY is **gated by
`taxonomy_policy`**:
- `open` → worker auto-applies (writes the structured domain entry + lazy MOC);
- `review-only` → worker emits a domain **proposal** artifact, no taxonomy commit;
- `capped:<N>` → at most N new domains per run, excess degrades to proposal/catch-all.

`CREATE_DOMAIN` is forbidden for `is_gated` candidates by **extending `plan.py` check 10** (CANDIDATE-GATE,
the same gate that today restricts gated candidates to `GATE_ALLOWED_OPS = {MERGE_INTO_THEME,
MARK_CONTESTED, DROP}` at `plan.py:77`), so poisoned/harvested memory (ADR-0007/0023) can never mint a
domain. This carves a **worker-only, policy-gated** `_meta/taxonomy.yaml` write that is explicitly
**NOT** brain-reachable and **NOT** part of the §4.0 INGEST allowlist (the brain still fails L1-9/§4.0
on any `_meta/` touch).

**Atomicity / replay — same all-or-nothing CAS-publish-or-discard as every op.** A `CREATE_DOMAIN`
run's taxonomy write + lazy MOC + the run's themes are **ONE atomic worktree diff published by one CAS**
(ADR-0008). A failed publish (lost CAS race, lint reject) discards the **entire** worktree — including
the taxonomy entry — so there is **no half-created domain**: `_meta/taxonomy.yaml` on the curated ref is
left byte-unchanged and the fact is **not lost** (it returns to the inbox and falls to the catch-all
floor §A on the retry, or to the inbox retry budget). A model-chosen domain name is nondeterministic in
the same class as theme basenames/titles (already model-decided, then worker-materialized); ADR-0008
recovery finalizes the *published* commit without re-deciding, and a discarded-then-retried run re-plans
against the OLD taxonomy and may pick a different name or be absorbed by the catch-all — never producing
a duplicate or orphaned `_meta/` entry. `taxonomy_policy` + L1-18 finally get their designed job:
enforced on the **creation lane only**, never during plain INGEST.

**(D) `agora taxonomy add-domain` admin CLI (#23).** Provide the human side of the governed lane — a
command that adds a domain in one admin commit (mirroring `repo init`'s emit + lint-clean + commit
flow), runs L1-18, and is how `review-only`/`capped` repos apply the proposals the curator emitted.

**(E) Per-domain customization layers ONLY on tuning surfaces (#24).** Per-domain config may touch
**only**: (1) a read-only `_meta/domains/<domain>.md` prompt/glossary copied into the bundle next to
`schema.md` (extending `bundle._copy_read_only_inputs`, `bundle.py:295`) for the domains touched this
run; (2) per-domain **AUTHOR** brain routing (AUTHOR runs once per region — the clean seam — so
`RoutedBackend.author` resolves the region's domain before the ADR-0015 precedence
`--backend > routing[author] > repo default > registry default`); (3) the existing soft thresholds
`body_byte_bound`/`max_orphans`/`related_k`, **wired repo-globally first** (closing the inert ADR-0011
§1.3 gap) and then per-domain-overridable via `_kb/repo.yaml curator.domains.<domain>`. The closed op
vocabulary, the §4.0 allowlist, the fixed taxonomy, and the §4.1/§4.4 validators stay **domain-agnostic
and model-independent**. The **default brain resolves to OSS Qwen for every domain** (invariant #4); a
stronger/proprietary brain is opt-in behind the adapter registry. A per-domain config block for a
domain **absent from the fixed taxonomy fails loud at load** (mirroring `BackendRegistry` fail-loud
validation), not silently ignored. **Per-domain PLAN routing is deferred** to the
[ADR-0024](0024-bulk-processing-horizontal-curator-scale.md) domain-sharded-batch lane (PASS-1 plans
the whole batch in one call, so there is no per-candidate PLAN seam without splitting the claim batch by
domain).

### Migration / back-compat
The structured `{<domain>: {status, created, created_by}}` mapping is **purely ADDITIVE**. It does
**NOT** bump `schema_version`, so **L1-17** (schema_version drift, kb_schema.md / `lint.py`) stays
untouched and no schema-doc header / `_kb/repo.yaml` version edit is required. The net-new `_load_taxonomy`
normalizer (§B) accepts both the bare list and the mapping, and `emit_schema` keeps emitting the bare
list for repos that never auto-create a domain. **No migration command is needed**: the bare-string list
remains valid indefinitely, and already-dogfooded repos are silently fine.

### Recommended outcome
**Adopt.** Ship the catch-all no-loss floor (A) + repo-global threshold wiring (E.3 repo-global leg)
**first** — both cheap and invariant-neutral (Phase 3.5). Build the structured shape (B) before the
heavier CREATE_DOMAIN lane (C) so #24 is additive.

**HARD prerequisite (not aspiration):** the same change set that lands worker-applied `CREATE_DOMAIN`
(C) **MUST** flip the effective `taxonomy_policy` default to be **repo-kind-aware** — `kind=personal` →
`open`, `kind=team` (and any non-personal / unknown kind) → `review-only` (or `capped:1`) **unless
`taxonomy_policy` is explicitly set**. Today the code default is unconditionally `"open"` in *both*
readers (`emit.py:60`, `lint.py:187`); if CREATE_DOMAIN ships against that default, **every** repo —
team repos included — would auto-apply brain-proposed domains with no review, undermining the very
anti-sprawl guarantee this ADR sells. This is single-writer-safe (the worker, not the brain, writes
`_meta/`, so invariant #2 is intact) but is a governance hole, so the repo-kind-aware default and
CREATE_DOMAIN land **together**. Keep the integrity gate domain-agnostic throughout.

### Open sub-decisions (Proposed; recommendations carried but not yet ratified)
1. **Where a domain is born — INGEST op vs admin-only path.**
   - *A) Brain writes `_meta/taxonomy.yaml` directly.* Rejected — re-opens the exact D6 backdoor;
     breaks invariant #2 and replay determinism.
   - *B) Admin-only proposal + `agora taxonomy add-domain`; fact waits in inbox meanwhile.* Safe but
     reintroduces the silent data loss #23 targets (fact dies at `max_attempts → failed/` absent a
     catch-all).
   - *C) Hybrid — catch-all floor + worker-applied CREATE_DOMAIN gated by `taxonomy_policy`.*
   - **Recommendation: C**, implemented as A's mechanism (worker, not brain, writes) gated by policy.
2. **Safety net when creation is disallowed (review-only/capped exceeded, or gated candidate).**
   - *A) Route to the guaranteed catch-all domain — never drop a non-gated durable capture.*
   - *B) Hold in inbox via the retry budget; after `max_attempts` → `failed/`.*
   - *C) Per-run quarantine domain flagged for dashboard triage.*
   - **Recommendation: A as the floor (guaranteed no-loss), with C's triage signal layered on.**
3. **Promotion threshold — create-on-first-fact vs cluster-then-promote.**
   - *A) Create-on-first (simplest; matches lazy-MOC).*
   - *B) Hold in catch-all; promote only once N facts cluster (needs cross-run clustering Agora lacks).*
   - *C) Create-on-first as `status: proposed`; a deferred compaction/merge op GCs thin domains.*
   - **Recommendation: A for personal repos, C for team repos (sprawl reversible).**
4. **Is `max_orphans` per-domain or repo-global-only?** Orphans are a whole-tree L2-1 derivation; making
   it per-domain changes the derivation to a domain subtree. **Recommendation:** repo-global for now;
   revisit if a strong domain genuinely needs a tighter subtree orphan budget.

## Alternatives considered
- **Brain writes the taxonomy when it invents a domain (#23).** Simplest (one prompt change) but
  re-opens the precise vocabulary-widening backdoor D6 closed: a poisoned/gated candidate could spray
  arbitrary domains, and a nondeterministic mid-INGEST name threatens replay idempotency. Rejected — it
  breaks invariant #2 (only the deterministic worker writes) and D6.
- **Admin-only domain creation, no catch-all (#23).** Keeps D6 intact but the fact still silently dies
  at `max_attempts → failed/`, exactly the loss #23 exists to kill. Rejected as the *whole* answer; the
  admin command (D) is kept as the human side of the hybrid lane.
- **Per-domain validator knobs — e.g. "require ≥3 sources before CREATE in the strong domain" (#24).**
  This parameterizes §4.1/§4.4 by domain, destroying ADR-0011's "success is a pure function of (plan,
  diff, manifest, bundle, lint)" and the "two independent implementations grade identically" property.
  Rejected — "refinement" is expressed as a stronger brain + richer prompt + the already-soft
  thresholds, never a domain-parameterized integrity gate.
- **Per-domain PLAN routing now (#24).** Has no clean seam: PASS-1 plans the whole batch in one `plan()`
  call (ADR-0015), so a per-domain PLAN sub-invocation would have to partition its own manifest slice
  and risks COVERAGE-check violations. Deferred to ADR-0024, where the domain-sharded batch split is
  needed anyway; AUTHOR-only routing is the safe feasible subset.
- **Routing a "strong" domain to a proprietary brain by default (#24).** Would make the core require a
  proprietary dependency. Rejected against invariant #4 — proprietary brains stay opt-in; the shipped
  default resolves every domain to OSS Qwen.
- **Everything in `_kb/repo.yaml` incl. inline prompts, or everything in one `_meta/domain-policy.yaml`.**
  Rejected in favor of the existing split: editorial rules the model must obey (prompt/glossary) live
  git-tracked + auditable under `_meta/domains/<domain>.md` and travel into the bundle read-only like
  `schema.md`; deployment policy (thresholds + brain) lives operator-local in `_kb/repo.yaml`.

## Consequences
- **+ (#2 / D6 preserved)** The sandboxed brain still never writes `_meta/`; a domain is born only via
  worker-applied `CREATE_DOMAIN` gated by `taxonomy_policy`, mirroring the existing plan-apply-author
  split. The "model can never widen the controlled vocabulary" property is intact.
- **+ (#23 literal ask)** No non-gated durable capture is ever dropped merely for lack of a domain — the
  catch-all floor (A) makes this true even before the full creation lane lands.
- **+ (#7 / ADR-0007 / ADR-0023 gate held)** Gated/harvested candidates can never mint a domain
  (check-10 extension), so memory-poisoning cannot spray taxonomy entries.
- **+ (#1 preserved)** Auto-created domains live in git-tracked `_meta/taxonomy.yaml`, durable and
  rebuildable; the worker materializes the name and the single atomic CAS commit is the replay truth, so
  ADR-0008 recovery stays idempotent and a failed publish leaves no half-created domain.
- **+ (#24 envelope held)** Per-domain customization touches only prompts + AUTHOR brain + soft
  thresholds; the validator stays domain-agnostic, so two implementations still grade identically.
- **+** `taxonomy_policy` + L1-18, inert since they were specified, finally get their designed job on the
  creation lane (L1-18 extended to also diff `domains`).
- **−** Taxonomy sprawl risk at team scale: create-on-first yields a long tail of single-note domains —
  the anti-pattern D6 guards. Mitigated by the repo-kind-aware default (team → review-only/capped),
  `status: proposed` + dashboard triage, and a deferred compaction/merge op.
- **−** Schema migration risk if #23 ships a bare-string entry and #24 later needs config — eliminated by
  pinning the structured shape (B) now (additive, no `schema_version` bump) so #24 is purely additive.
- **−** Sequencing risk: CREATE_DOMAIN is XL; shipping it before the cheap catch-all (A) would land the
  no-loss promise late. Mitigated by shipping A + repo-global thresholds first (Phase 3.5).
- **−** Per-domain AUTHOR routing multiplies the `(act × domain)` network-posture matrix `agora doctor`
  must surface, and routing a high-volume domain's AUTHOR to a metered API multiplies per-region cost
  (the ADR-0015 cost foot-gun, now per-domain).

### Deferred / out of scope (stated, not solved)
- **Per-domain PLAN routing** → the ADR-0024 domain-sharded-batch lane (no PASS-1 seam today).
- **Cross-run clustering / threshold-based promotion** of catch-all facts into real domains → relates to
  #26 (search at scale, implements Accepted [ADR-0012](0012-deterministic-query-ranking.md)) and
  ADR-0024 (bulk).
- **Compaction/merge op** to GC thin `status: proposed` domains → noted, deferred.
- **No shared/global domain registry** — domain creation stays strictly per-repo (each repo's own
  `_meta/taxonomy.yaml`) to preserve tenant isolation (invariant #5).
- **Per-domain `max_orphans`** subtree semantics (open sub-decision 4) — repo-global for now.

## Implementation sketch
Sequenced so the cheap no-loss work lands first and the integrity-sensitive lane lands behind it.
1. **No-loss floor (M).** `ollama_brain.py` step 3 → catch-all fallback (not `op = "DROP"`, removing the
   silent downgrade at ~line 438) for non-gated basename ops with an unresolvable domain; update
   `kb_schema.md` prompt; gated candidates unchanged. Deterministic (zero-model) test: an unclassifiable
   non-gated capture is NEVER DROPped.
2. **Repo-global threshold wiring (M).** Parse `curator.limits.body_byte_bound` /
   `curator.limits.related_k` / `curator.lint.max_orphans` in `load_repo_config` (today silently
   ignored; nesting matches the DATA-MODEL §3 example) and
   thread them (`SubprocessBackend(body_byte_bound=…)` replacing the hardcoded `_DEFAULT_BODY_BYTE_BOUND
   = 8192`; `lint`; `bundle` related fetch replacing `_RELATED_K = 8`). No per-domain yet.
3. **Structured domain entry (M).** Extend `Taxonomy` + `emit_schema` rendering + the **three** list-only
   `domains` readers (`config._load_taxonomy` `config.py:416`, `ollama_brain.parse_taxonomy`
   `ollama_brain.py:231`, `schema/lint.py` `lint.py:185`) to accept `domains` as list **or**
   `{<domain>: {status, created, created_by}}`, normalized to a name-set + metadata; keep
   `extra='forbid'`; back-compat for string lists; no `schema_version` bump. Test the normalizer in each
   of the three readers.
4. **CREATE_DOMAIN op + worker APPLY (XL).** Add to `OPS`/§2/INGEST-CONTRACT/`kb_schema.md`; PLAN
   requires a proposed name + reason and **extends check 10 (CANDIDATE-GATE)** to forbid it for
   `is_gated`; APPLY in the worker slugifies/dedupes + writes `_meta/taxonomy.yaml` + lazy MOC under the
   `taxonomy_policy` gate (open=apply / review-only=proposal / capped:N=bounded) — a worker-only write
   carved out of the §4.0/L1-9 allowlist, never brain-reachable, published as ONE atomic CAS diff
   (failed publish ⇒ whole worktree discarded, fact falls to the catch-all floor). **Land in the SAME
   change set: flip the effective `taxonomy_policy` default to repo-kind-aware** (personal→open,
   team/unknown→review-only or capped:1 unless explicitly set) in both readers (`emit.py`, `lint.py` /
   the resolver `load_repo_config` consults) — HARD prerequisite, see Recommended outcome.
5. **Extend L1-18 to diff `domains` (M).** Today L1-18 (kb_schema.md L1-18 row + §5.2 prose;
   `schema/lint.py` `check_taxonomy_policy`) diffs only `allowed_tags(after) − allowed_tags(before)` and
   never reads `domains`. Add a `domains` set-difference branch:
   `domains(after) − domains(before)` is gated by `taxonomy_policy` exactly as new `allowed_tags` keys
   are (`review-only` rejects in direct mode; `capped:<N>` rejects over-cap). Concrete edits: the L1-18
   row + §5.2 prose in `kb_schema.md`, `check_taxonomy_policy` in `schema/lint.py`. Enforced on the
   creation lane only, never during plain INGEST. Test: an over-cap domain addition is rejected.
6. **`agora taxonomy add-domain` admin CLI (M).** One admin commit, lint-clean, runs L1-18 — the human
   apply path for proposals.
7. **Per-domain config (#24) (L).** Extend `RepoConfig` with `domains: dict[str, DomainPolicy]`
   (fail-loud if the domain is absent from the fixed taxonomy) + a `for_domain(d)` resolver; copy
   `_meta/domains/<domain>.md` into the bundle for touched domains and append it to the PASS-1/PASS-2
   prompts; resolve per-domain AUTHOR brain in `RoutedBackend.author` (default OSS Qwen); surface the
   per-domain × act table + a commented `curator.domains` example in `agora doctor`.
8. **Dashboard triage (M).** Surface `status: proposed` domains, single-note (sprawl) domains, and the
   catch-all backlog via the existing `lint()`/health path.
9. **Tests (L), zero-model, hand-authored plans (ADR-0011 §4 style):** (1) no-drop guarantee — an
   unclassifiable non-gated capture is never DROPped; (2) gated candidate can NEVER CREATE_DOMAIN nor
   originate; (3) `open` allows / `review-only` rejects in-INGEST creation / `capped:N` bounds, **plus a
   team repo with no explicit `taxonomy_policy` does NOT auto-create a domain in-INGEST** (HARD-prereq
   default test); (4) **replay / atomicity** — a CREATE_DOMAIN run whose CAS publish fails leaves
   `_meta/taxonomy.yaml` byte-unchanged on the curated ref and the fact is NOT lost (catch-all floor);
   the published-then-recovered path finalizes the same domain without re-deciding; (5) L1-18 extension —
   an over-cap `domains` addition is rejected; (6) a per-domain block for a non-taxonomy domain fails
   loud at load; (7) per-repo isolation (no cross-repo domain leakage).

Remember to add the `0022` row to `docs/adr/README.md` (href byte-identical to
`0022-curator-taxonomy-governance.md`; note ADR-0026 is reserved for the deferred skill write-back) and
reflect the structured domain shape in `docs/DATA-MODEL.md` §3 / ADR-0010 §5, the no-drop policy +
repo-kind-aware default in `docs/DESIGN.md` §4 / INGEST-CONTRACT, and the per-domain item in
`docs/ROADMAP.md` (Phase 3.5 catch-all floor; Phase 4/5 governed creation).

## Addendum — Non-ASCII (Korean) no-loss: `note-<sha8>` filename fallback (#57, landed 2026-07-24)

§A's no-loss floor rescued only the *domain* leg of "never drop a fact merely for failing to
classify it"; the *slug* leg still leaked: `_slugify` is ASCII-only by design (it must satisfy
`plan.py`'s PATH/ALLOWLIST safe-token regex, which is a **path-safety** boundary, not a style
choice), so a purely-Korean CREATE_THEME seed slugified to `""` and step 4 silently downgraded the
capture to DROP. Issue **#57** closed that sibling hole; this addendum records the settled policy —
including the strategy-umbrella **decision 5** (Korean filename policy: `note-<sha8>` vs a
transliteration table), resolved here in favor of the hash fallback.

### 1. Basenames: deterministic `note-<sha8>` fallback (decision 5 resolved)

The CREATE_THEME seeds are tried IN ORDER — model `basename` → model `title` → capture text — and
the first that slugifies non-empty wins (a Korean basename alongside an ASCII title still gets the
meaningful title slug). Only when EVERY seed slugifies empty does `normalize_plan` reach the
fallback — and it no longer DROPs: the note is named
`note-` + the first 8 hex chars of the candidate's canonical `content_sha256` (DATA-MODEL §11.2 —
the hash `bundle.py` already stamps into `candidates.json`, reused as the single source; recomputed
from the candidate text via `core.hashing.content_sha256` only when the field is absent, which
yields the same bytes). The existing `-2` uniqueness suffix loop applies to fallback slugs
unchanged.

**Transliteration table rejected.** A romanization table is a *versioned artifact*: any table
revision (or engine/library drift) renames future notes for identical input — a nondeterminism
class the deterministic-curator contract cannot carry — and Korean romanization is genuinely
ambiguous (McCune-Reischauer vs RR vs ad-hoc). The sha fallback is a pure function of the canonical
content bytes: deterministic across runs/implementations, ASCII slug-safe by construction, and it
leaves `plan.py`'s path-safety regex byte-identical (widening that regex was explicitly rejected —
it guards path traversal, not aesthetics). The Korean *meaning* is preserved where it belongs:
`title:`/`summary:` are arbitrary strings, search indexes the body, and links resolve by basename —
an opaque ASCII filename is harmless (the Obsidian/Zettelkasten "id filename + human title"
pattern).

### 2. Aliases: skip + count, NOT hash-substituted

An un-slugifiable alias is **skipped and counted** (`normalize_plan` `stats` out-param →
`run_plan`'s `$AGORA_BRAIN_DEBUG` record `aliases_skipped_unslugifiable` + one stderr warning),
never silently discarded as before — and never hash-substituted: a `note-<sha8>`-style alias has
zero search/link value, and *preserving* Korean aliases verbatim would require widening the closed
alias/basename token grammar (a schema change with LINT L1-15 uniqueness and path implications),
which is out of scope here. The plan schema itself stays a closed set — the count rides diagnostic
channels only.

### 3. Companions in the same change (#57)

The repo-language prompt directive (`repo.yaml` `curator.language`, `None` → both pass prompts
byte-identical; set → one LANGUAGE line in PASS-1/PASS-2: prose in the repo language, slug/domain/
tag tokens keep the schema's ASCII rules; per-domain arrives with #24) and the fallback-summary
sentence/어절-boundary truncation (`_truncate_summary`, replacing the mid-sentence `text[:200]`
hard cut; brain-supplied summaries untouched, and a `limit // 4` floor keeps a degenerate early
boundary from collapsing the summary below the old hard cut) landed alongside. The shim paths that
REBUILD the PASS-2 prompt (the CLI-agent `text_only` template and the minimal fallback) re-attach
the worker's LANGUAGE line from the stdin prompt, so the directive reaches every brain family —
unset stays byte-identical. `curator.language` is read fail-loud (a non-string raises
`ConfigError`): `language: no` (Norwegian) is a YAML 1.1 boolean unless quoted, and the operator's
stated policy must never be silently dropped. Neither alters any integrity gate: the shim stays
outside the boundary and the worker re-grades everything.
