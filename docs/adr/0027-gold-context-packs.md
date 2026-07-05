# ADR-0027 — Gold context packs + the outbound knowledge contract

**Status:** Accepted · 2026-07-05 (Step-0 ratified, #36) · Proposed 2026-07-04

Adds a **derived, deterministic, token-budgeted context tier** ("gold packs") assembled from the
existing validator-gated wiki, plus the **single normative outbound sentinel + loop-break contract**
that every future Agora→agent emission path must cite. Layers on
[ADR-0009](0009-deterministic-query-contract.md) (deterministic read) and
[ADR-0012](0012-deterministic-query-ranking.md) (whose structural machinery the gold score reuses —
the frozen §0 query contract is untouched). Extends the derived, git-ignored `_kb/` state posture of
[ADR-0017](0017-harvester-file-connector-mechanics.md) (cursor) to a new `_kb/gold/` surface. Bound
by [ADR-0001](0001-markdown-git-source-of-truth.md) (invariant #1 — packs are rebuildable, never
canonical), [ADR-0002](0002-cqrs-single-writer-curator.md) (invariants #2/#3 — no new writer of
`wiki/`, no inbox change), [ADR-0005](0005-fully-oss-bom.md) (invariant #4), and
[ADR-0006](0006-repo-as-tenant-boundary.md) (invariant #5 — packs are per-repo). Consumption is
agent-neutral (invariant #6). This ADR co-ratifies the sentinel wording with reserved **ADR-0026**
(skill write-back, **#25**) and reserves **ADR-0028** (LLM `DISTILL` curator act, evidence-triggered),
**ADR-0029** (connector ecosystem — the exec-connector wire ("CWP"), registration UX, and the
connector-enablement re-consent / injection opt-in mechanics; evidence-triggered, extending
[ADR-0023](0023-context-harvester-connectors.md)),
**ADR-0030** (federation / team-audience composition, Phase-4-coupled), and **ADR-0031** (retention —
a hard prerequisite for `mail:`/`chat:` connectors). Sibling:
[ADR-0023](0023-context-harvester-connectors.md), whose future session distiller MUST implement §8
below.

## Context
Agents consume the KB today by **pulling**: `kb_query` runs the deterministic ADR-0012 pipeline and
returns ranked evidence per question. That is the right shape for retrieval, but it does not serve
the *standing-context* use case that every CLI agent has converged on (CLAUDE.md-style includes): a
small, stable, high-value slice of the KB injected at session start, cheap enough to sit in every
prompt and byte-stable enough to benefit from prompt caching. Nothing in the repo produces such a
slice; each agent owner hand-maintains one, drifting from the wiki it paraphrases.

In medallion terms — recorded here because the mapping keeps the tiers honest — Agora already has
**bronze** as an ingress *concept* (the append-only inbox spool + `raw/` captures) and **silver** as
the curated `wiki/` SSOT (invariant #1). What is missing is **gold**: a derived, consumption-shaped
tier. The raw material and machinery all exist:

- **Selection signals.** `core/wiki.py` already computes structural centrality — the degree
  surrogate `alpha/(1+d_moc) + beta*indeg_norm` (`wiki.py:18`, `STRUCT_BETA` at `wiki.py:54`,
  `d_moc` on candidates at `wiki.py:433`, applied at `wiki.py:487`) — and parses note `status`
  frontmatter tolerantly (`wiki.py:272–274`). The curator stamps `origin: harvest:<agent>` and the
  gate keeps candidates from originating themes (ADR-0007/0017).
- **Derived-state precedent.** `_kb/` already holds git-ignored, rebuildable, non-canonical state:
  the harvest cursor (`layout.py:120` `harvest_dir`, `layout.py:142` `harvest_cursor_path` with its
  `safe_path_component` traversal guard) written atomically by `CursorStore`
  (`harvester.py:135`, `atomic_write_text` = temp file + `os.replace` + directory fsync,
  `core/atomicio.py:35`). DATA-MODEL §6 calls this class of state "derived, rebuildable, never an
  integrity control".
- **A finalize seam.** `worker.py` already performs best-effort, post-publish `_kb/` IO in the
  happy-path finalize: `compute_harvest_cursor_deltas` is captured at claim (`worker.py:495`) and
  applied beside `_bump_counters` (`worker.py:561`) under an explicit swallow+log discipline
  (`worker.py:574–581`) so derived-state IO can never perturb a durable publish (ADR-0017 §7).
- **Surfacing.** The Prometheus exporter already emits per-connector harvester families
  (`faces/web/metrics.py:152–164`), and `kb_status`/the dashboard reuse `AgoraHandlers`.

Two dangers make this an ADR rather than a feature. First, **injection amplification**: a pack is
injected into *every* session of every subscribed agent, so any harvested (attacker-influencable)
content that reaches a pack turns the harvester's residual reworded loop (ADR-0017 §5) into a
broadcast channel — attacker mail/message → harvest → curated summary → injected everywhere.
Second, **loop closure**: emitted packs land in agent context, agents write memory, the harvester
reads memory — Agora's own output becomes its input. The existing sentinel-strip is not enough:
`FileConnector._neutralize` (`connectors.py:596`, `_AGORA_SENTINEL_RE` at `connectors.py:74`)
removes only the comment *markers* and leaves span *content* in place, so a harvested pack would
re-enter as facts today. The outbound contract in §8 exists to close the verbatim half of that loop
and to say honestly what stays open.

An LLM that *distills* notes into denser summaries is deliberately **not** this ADR: that is a new
generation point with a new write surface, and it is reserved as **ADR-0028** behind an evidence
trigger (§F below). Gold v1 is assembly, not authorship.

## Proposed decision
Introduce gold context packs as a **pure, deterministic function of (curated commit, pack spec)**,
produced by reader-class code into git-ignored `_kb/gold/`, consumed over three agent-neutral
channels, and fenced by a normative outbound sentinel + loop-break contract (§8). The recommended
outcome is **Adopt**.

1. **Gold is a DERIVED tier — not a distiller, not a writer, not a store.** A pack is assembled
   verbatim from existing validator-gated wiki notes (summary lines; full body only for pins). No
   LLM runs in the pack path; the curator's plan vocabulary gains **no new op**; nothing writes
   `wiki/`, the inbox, or indexes — invariants #1/#2/#3 are intact. Medallion mapping recorded:
   **bronze** = ingress concept (inbox spool + `raw/` captures), **silver** = `wiki/` SSOT,
   **gold** = derived packs. The future LLM `DISTILL` curator act (→ `wiki/digests/` via one new
   closed-vocab op) is a separate, evidence-triggered decision reserved as **ADR-0028**.

2. **Producer: a deterministic `PackAssembler` in `core/gold.py` — reader-class code, never the
   sandboxed model.** Three triggers: **(a)** a best-effort rebuild in the `worker.py` happy-path
   finalize, beside the ADR-0017 §7 cursor deltas (`worker.py:561–581`), under the same swallow+log
   posture — a pack IO failure never perturbs a durable publish; **(b)** a lazy `ensure_pack()` on
   read, when the pack meta's `curated_sha` differs from the current curated head; **(c)** an
   explicit `agora gold build` CLI. Writes are atomic temp+rename (the `CursorStore.save` /
   `atomic_write_text` posture, `core/atomicio.py:35`); concurrent writers are safe **because** pack
   bytes are a pure function of (curated commit, spec) — last-writer-wins converges. Stated
   explicitly: with (b), **faces gain a git-ignored-`_kb/` write capability — the first face write
   outside `Inbox.write`**. This widens the face posture deliberately and only for derived,
   rebuildable, non-canonical bytes; read-only face deployments degrade to in-memory assembly
   (serve the assembled pack without persisting it).

3. **Storage + the byte-identical-rebuild contract.** Packs live at git-ignored
   `_kb/gold/<pack>.md` with a sidecar `_kb/gold/<pack>.meta.json` carrying
   `{pack, curated_sha, spec_hash, generated_at, estimator, note_count, est_tokens,
   inputs: [{path, content_sha256, score}]}`. `core/layout.py` gains traversal-guarded
   `gold_dir` / `gold_pack_path` mirroring the `harvest_dir` / `harvest_cursor_path` pattern
   (`layout.py:120/142`). **Byte-identical rebuild at a fixed (curated commit, spec) is a
   regression-tested contract** — prompt-cache economics depend on stable bytes. Therefore
   `generated_at`/age live ONLY in `meta.json`; pack bytes carry only the `curated_sha` in the
   header. v1 ships one implicit zero-config **`default`** pack; a git-tracked `_meta/gold.yaml`
   policy file (pins, per-audience packs, budgets) is named as the future home — it belongs beside
   `_meta/taxonomy.yaml`, outside the curator-writable allowlist (`schema/emit.py:196`) — but is
   **DEFERRED until pins/team packs land** (open sub-decision, §S3).

4. **Selection: a new deterministic gold-score contract — frozen ADR-0012 §0 untouched.**
   *Eligibility:* a note enters a pack only if `status: active`, ungated (not
   `kind=candidate`/`confidence=low`), **and NOT harvest-origin** — notes with `origin: harvest:*`
   (or whose provenance is harvest-only) are **DEFAULT-EXCLUDED**; they enter only via explicit
   human pin, and pinned harvest content carries a per-line **"harvested, unverified"** label.
   Rationale, stated plainly: without this exclusion, gold is a prompt-injection **amplifier** —
   attacker mail/message → harvest → curated summary → injected into every session. Also excluded:
   `stub`/`deprecated`/low-confidence; `contested` is default-excluded (open sub-decision, §S2).
   *Score:* `0.35 ×` structural centrality (reuse the `d_moc`/in-degree machinery,
   `wiki.py:18/54/433/487`) `+ 0.25 ×` recency exp-decay (half-life 30 d) **anchored to the curated
   commit's committer timestamp — never wall clock** (determinism; the reference instant is
   recorded in `meta.json`; a frozen-clock regression test pins it) `+ 0.20 ×` status/confidence
   bucket `+ 0.20 ×` provenance density `min(1, len(sources)/5)`. Recency must **NOT** credit
   updates whose triggering events were harvest-sourced (track the last non-harvest update at
   finalize) — otherwise the reworded harvest loop (ADR-0017 §5) is *rewarded* with pack placement.
   Pins bypass scoring (optional full body). Greedy fill to budget.

5. **Token budget: a script-aware estimator (Phase A, not later).** CJK codepoints count ≈ 1
   token/char; other text at bytes/4. The owner's KB is Korean-heavy and plain bytes/4
   underestimates CJK by 1.5–3×, so this ships in Phase A with a Korean-corpus fixture in the
   budget test. The estimator *name* is recorded in `meta.json` (swappable later without ambiguity
   about which estimator produced `est_tokens`). Default `budget_tokens` = **2000** (open
   sub-decision 2000 vs 4000, §S1).

6. **Freshness: an honest bound, not an SLA.** The invalidation key is `(curated_sha, spec_hash)`.
   The finalize rebuild (2a) means a pack is never staler than silver; the lazy rebuild (2b) means
   faces never *serve* stale. Staleness is surfaced — `meta.json`, a `kb_status` row, a Prometheus
   `agora_gold_pack_age_seconds` gauge (beside the existing families,
   `faces/web/metrics.py:152–164`), and a bronze/silver/gold dashboard panel. Stated plainly:
   **gold freshness = curation cadence** — the KB is eventually consistent by design (DESIGN §2.2);
   tighter freshness is bought by running the curator more often, not by a new mechanism.

7. **Consumption: three agent-neutral channels (invariant #6) + the ROLE RULE.**
   **(1) File:** a documented `@<repo>/_kb/gold/default.md` CLAUDE.md-style include — the one-line
   include **IS** the standing human consent; Agora never writes agent config dirs (pull-only,
   preserving the reserved-ADR-0026 never-auto-write posture). Pull-only does **not** waive review
   needs for harvest/team content — that is exactly why decision 4's eligibility exclusions exist.
   **(2) MCP:** a `kb_context(pack, scopes?)` tool — a SINGLE tool name; `scopes` is a future
   **additive** parameter owned by the federation ADR (reserved **ADR-0030**) — plus an
   `agora://gold/{pack}` resource and a `gold_context` prompt registered in `build_server`
   (`mcp_server.py:794`). **(3) Bridge consumers:** `agora-bridge-aelix` first, never privileged.
   **ROLE RULE (normative):** gold is the sole pack **PRODUCER**; federation face-level composition
   (reserved ADR-0030) is the sole **COMPOSER**; bridges/agents are pure **CONSUMERS** — no other
   component may build packs.

8. **The outbound sentinel + loop-break contract (NORMATIVE).** This section is the single
   normative spec for every Agora→agent emission path. [ADR-0023](0023-context-harvester-connectors.md)'s
   session distiller, reserved **ADR-0026** (skill write-back), and reserved **ADR-0030**
   (federation) MUST cite this ADR §8 rather than restating it.
   - **Grammar.** Every emitted pack is wrapped
     `<!-- agora:pack repo=<r> pack=<p> commit=<sha> -->` …
     `<!-- agora:pack:end repo=<r> pack=<p> commit=<sha> -->` — the closer keeps the `agora:`
     prefix (the `agora:body:start`/`agora:body:end` sentinel family, `apply.py:76–77`), so BOTH
     markers match the existing `_AGORA_SENTINEL_RE` (`connectors.py:74`) and the assembly-time
     neutralization below genuinely covers a forged closer.
   - **Assembly-time neutralization (producer duty).** The `PackAssembler` neutralizes any embedded
     `<!-- agora:` sequence inside assembled content, defeating the forged-early-close attack (a
     hostile summary line containing a literal closer cannot terminate the span early). Regression
     test: a summary containing a literal closer round-trips without breaking span-drop.
   - **Span-drop (consumer duty — NET-NEW code).** The current `FileConnector._neutralize`
     (`connectors.py:596`, `_AGORA_SENTINEL_RE.sub` with the pattern at `connectors.py:74`) strips
     only the comment MARKERS and leaves span content in place — insufficient. `FileConnector` (and
     the future ADR-0023 session distiller) must remove entire sentinel **SPANS** (opening marker
     through closing marker, inclusive) *before* the existing marker-strip runs.
   - **Path exclusion.** The harvester excludes `_kb/gold/` and any documented mirror paths from
     every connector's scan; `agora doctor` checks and reports the exclusion.
   - **Tests.** A pack-bearing `MEMORY.md` yields **zero** pack-derived facts through a real
     harvest run.
   - **Residual, stated honestly.** The *reworded* loop (ADR-0017 §5) is NOT claimed closed: an
     agent restating pack content in its own words defeats any marker. The candidate gate remains
     the general break; this contract closes the verbatim half and instruments the rest —
     **loop telemetry** = a near-duplicate (shingle) counter between emitted pack lines and
     incoming harvest candidates, plus a **cap on the harvest-derived share of any pack**. Both are
     Phase-B acceptance criteria, not later hardening.

9. **Cross-ADR obligations (recorded so the reservations are load-bearing).**
   - Reserved **ADR-0026** (skill write-back, **#25**) shares this sentinel spec; its wording is
     co-ratified with §8 and must not fork.
   - The reserved connector-ecosystem ADR (**ADR-0029**, extending ADR-0023): enabling any
     PII-bearing or networked connector on a repo **re-requires explicit injection opt-in for that
     repo** (a doctor-surfaced ack flag) — consent to inject gold everywhere does not survive a
     material change in what feeds the KB.
   - Reserved retention ADR (**ADR-0031**) is a hard prerequisite for `mail:`/`chat:` connectors.
   - **`schema_version` impact: NONE at v1** — all state lands under git-ignored `_kb/` plus
     additive config, so the in-repo schema contract is untouched. A future `_meta/gold.yaml` stays
     additive config (no `schema_version` bump, per the ADR-0022 back-compat precedent); any new
     frontmatter vocabulary that DOES bump `schema_version` goes through the ADR-0022 L1-17 lint
     discipline (schema-doc header + `_kb/repo.yaml` version edit) before it lands.

### Sub-decisions (resolved in the Step-0 session, #36, 2026-07-05)
- **S1 — Default pack budget → (A) `budget_tokens=2000`.** A standing include stays cheap in every
  prompt; 4000 is one config line away once real packs measure short (revisit after the V4
  curator-economics measurement).
- **S2 — `contested` handling → (A) default-excluded from packs.** A standing context slot is the
  wrong place for unresolved conflict; `kb_query` still surfaces contested notes with full context.
- **S3 — `_meta/gold.yaml` timing → (A) defer until pins/team packs land.** v1 = implicit
  zero-config `default` pack only; shipping the policy file early creates a git-tracked contract
  (and a `schema_version` question, decision 9) with zero users.
- **Freshness bound (V4) → accept eventual consistency.** Gold freshness = curation cadence;
  tighter cadence (watch/cron) is funded only after a one-day curator-economics measurement, not by
  new machinery.

## Alternatives considered
- **LLM-distilled gold as v1 (rejected — reserved ADR-0028).** Nondeterministic bytes defeat the
  prompt-cache/byte-identical-rebuild contract, add a second generation point with a new write
  surface outside the integrity boundary, and violate the rebuildability discipline (invariant #1).
  If packs chronically overflow their budget on summary lines alone, that evidence triggers the
  `DISTILL` curator act (→ `wiki/digests/`, one new closed-vocab op) as its own ADR.
- **A DB/vector store for gold (rejected — [ADR-0005](0005-fully-oss-bom.md) /
  [ADR-0012](0012-deterministic-query-ranking.md)).** The pack is a small markdown file derived
  from a small graph; infrastructure adds an operational dependency for zero ranking benefit, and
  ADR-0012 already rejected accelerators that cannot reproduce the deterministic contract.
- **Committing packs into `wiki/` (rejected).** A second copy of curated knowledge inside the SSOT
  is a divergence engine and puts derived bytes in curator-write territory (invariant #2); packs
  are cache, and cache lives in git-ignored `_kb/` like the cursor (DATA-MODEL §6).
- **Pushing packs into agent config dirs (rejected).** Writing `~/.claude/**` (or any agent
  install) violates the reserved-ADR-0026 never-auto-write posture. The file channel is pull-only:
  the human's one-line include is the consent record.
- **A per-pack YAML manifest directory + freshness-SLA machinery (rejected — premature ceremony).**
  One implicit pack with a sidecar meta and an honest curation-cadence freshness bound covers v1;
  the policy surface arrives with pins/team packs (§S3), the SLA never — freshness is bought by
  curator cadence (decision 6).

## Consequences
- **+** Agents get a standing, token-budgeted, byte-stable context slice assembled from
  validator-gated notes — prompt-cache-friendly by contract (byte-identical rebuild at fixed
  (commit, spec)) and honest by construction (pack bytes carry the `curated_sha` they were built
  from).
- **+** No new canonical store, writer, or curator op: invariants #1/#2/#3 hold; everything under
  `_kb/gold/` is derived and rebuildable from silver, in the exact posture the harvest cursor
  established (ADR-0017, DATA-MODEL §6).
- **+** The injection posture is fixed *before* the risky connectors land: harvest-origin content
  is default-excluded from packs, pinned harvest content is labeled per-line, and recency cannot be
  farmed by harvest-sourced updates — the reworded loop is not *rewarded* even where it is not
  closed.
- **+** The outbound sentinel + loop-break contract (§8) exists once, normatively, before ADR-0026
  / the ADR-0023 session distiller / ADR-0030 need it — with the span-drop gap in the current
  marker-only `_neutralize` (`connectors.py:596`) identified and closed by test.
- **+** Consumption is agent-neutral (invariant #6) across file include, MCP tool/resource/prompt,
  and bridges, with a normative producer/composer/consumer role split that keeps pack assembly in
  exactly one place.
- **−** Faces gain their first non-inbox write (`ensure_pack` into git-ignored `_kb/`) — a
  deliberate, recorded widening of the face posture, bounded to derived bytes, with a read-only
  in-memory degradation path. Any future face write beyond `_kb/gold/` needs its own ADR.
- **−** The reworded loop (ADR-0017 §5) stays open — stated, instrumented (shingle counter +
  harvest-share cap as Phase-B acceptance criteria), but not closed. Gold *increases* the volume of
  Agora-authored text circulating through agent sessions, which is why §8 is normative rather than
  advisory.
- **−** Gold freshness equals curation cadence — eventual consistency (DESIGN §2.2), surfaced but
  not tightened. Operators wanting fresher packs run the curator more often.
- **−** Deferred, on purpose: `_meta/gold.yaml` and pins-as-policy (§S3); team-audience packs and
  multi-repo composition (Phase-4-coupled, reserved ADR-0030); the LLM `DISTILL` act (reserved
  ADR-0028, evidence-triggered); the connector ecosystem incl. re-opt-in mechanics (reserved
  ADR-0029); retention
  (reserved ADR-0031, prerequisite for `mail:`/`chat:`).

## Implementation sketch (when adopted; ordered by risk)
1. **Phase A (1–3 d) — the assembler + the outbound contract, together.** `core/gold.py`
   (`PackAssembler` + gold-score with commit-anchored decay), the CJK-aware estimator,
   `layout.py` `gold_dir`/`gold_pack_path` (traversal-guarded, mirroring `layout.py:142`),
   `agora gold build|status|--check`, the §8 sentinel wrap + assembly-time neutralization,
   `FileConnector` span-drop + `_kb/gold/` scan exclusion + the `agora doctor` check. Tests:
   byte-identical rebuild, frozen-clock decay, Korean-corpus budget fixture, pack-bearing
   `MEMORY.md` → zero facts. Document the `@<repo>/_kb/gold/default.md` include recipe. No
   `gold.yaml`, no mirror paths.
2. **Phase B (~2 d) — freshness + telemetry.** The `worker.py` finalize best-effort rebuild
   (beside `worker.py:574–581`, same swallow+log), the `kb_status` row, Prometheus gauges
   (`agora_gold_pack_age_seconds` et al.), the dashboard bronze/silver/gold panel, and the §8
   acceptance criteria: the near-duplicate (shingle) loop counter + the harvest-derived-share cap.
3. **Phase C (~1–2 d) — the MCP + web channels.** `kb_context(pack, scopes?)` tool,
   `agora://gold/{pack}` resource, `gold_context` prompt in `build_server` (`mcp_server.py:794`);
   `GET /api/gold/{pack}` in the web face.
4. **Phase D — reference bridge.** `agora-bridge-aelix` as the first pure consumer (separate repo;
   zero agora-core changes — the role rule in decision 7 is the test).
5. **Phase E (Phase-4-coupled; reserved ADR-0030) — team-audience packs + multi-repo
   concatenation.** Priority-ordered concatenation only, never relevance-merged: per-repo IDF makes
   cross-repo scores non-comparable ([ADR-0012](0012-deterministic-query-ranking.md) §11).
6. **Phase F (evidence-triggered; reserved ADR-0028) — the `DISTILL` curator act** →
   `wiki/digests/` via one new closed-vocab op; trigger = packs chronically overflow budget on
   summary lines alone.
