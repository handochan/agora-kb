# Design note: retrieval-substrate positioning vs vector-DB RAG

> **Status: Exploratory · Non-normative · NOT ratified.**
> Snapshot of a design exploration (2026-07-10, produced by an adversarially-verified
> multi-agent analysis). This note **decides nothing and supersedes nothing.** It *informs*
> — but does not commit — reserved **ADR-0032 / 0033 / 0034 / 0035** (and touches the already-reserved
> 0029 / 0030 / 0031). The single source of truth for architecture remains
> [`DESIGN.md`](../DESIGN.md); a section here is superseded the moment an ADR ratifies the corresponding move.
> Treat every "win/parity" grade below as a *hypothesis to be tested*, not a claim.

`docs/notes/` is the home for exploratory, non-normative design notes. Unlike the flat `docs/*.md`
(which describe *decided* state) and unlike `docs/adr/` (which record *decisions*), a note here captures
*rationale/framing that spans several future decisions* and that later ADRs cite.

---

## TL;DR

- **"Beat a central vector-DB RAG stack on *all* axes" is structurally impossible.** Four of the vector-DB
  strengths are the exact **duals of Agora's invariants** — closing them fully would require breaking the
  very invariants that *are* the product.
- **The correct goal is not axis-by-axis domination but SUPERSET positioning:** Agora as a structure-aware
  hybrid retrieval *substrate* — `{sparse-lexical · dense-semantic · curated-structural}` over a
  human-readable, git-rebuildable markdown SSOT with deterministic testable citations and hard tenant
  isolation. Under that framing a vector index is just **one derived, git-ignored, rebuildable view** of the
  substrate, and the vector DB becomes a strict *subset*.
- **Honest ledger:** ~2 genuine composite wins, ~5 parity/partial, ~4–6 permanent invariant-rooted
  trade-offs (see §1). Two moves are provably-safe and shippable now (§6).

---

## 0. Scope & why this note exists

Follows two questions in a 2026-07 design conversation: *"how does git-Agora compare to a vector-DB RAG
stack for team use?"* and *"how would Agora beat vector DB on every axis?"* The answer to the second is
"it can't, and that's correct" — so this note records **(a)** what is permanently unwinnable and why,
**(b)** the reframe that turns that concession into an advantage, and **(c)** a candidate move-plan mapped
onto reserved ADR slots with explicit evidence gates. The comparison target throughout is a **central
server + vector DB** (pgvector / Pinecone / Weaviate) doing embeddings-based semantic RAG, clients hitting
one hosted API.

The **invariants** referenced below are the load-bearing ones from [`../../CLAUDE.md`](../../CLAUDE.md) /
[`DESIGN.md`](../DESIGN.md): (1) markdown+git is the SSOT, any DB/index is derived & rebuildable;
(2) all writes go through the append-only inbox, only the single-writer curator writes `wiki/`/indexes/`log.md`;
(3) the inbox is append-only & per-writer-namespaced (⇒ unscrubbable); (4) every component has an OSS path,
no required proprietary/AGPL core dep; (5) tenant isolation is hard (tenant = repo, no co-mingled cross-tenant
index); (6) tool-agnostic via the adapter registry.

---

## 1. Honest ceiling — what is permanently unwinnable (and which invariant it pays for)

These cannot be won without breaking an invariant. They are the *price* of a rebuildable, plaintext,
git-distributed, single-writer, hard-isolated knowledge SSOT — i.e. the price of being Agora.

| Permanently unwinnable | Which invariant it is the dual of |
|---|---|
| Cross-machine **deterministic `not_found`** on a *semantically-only-answerable* query | **1** (rebuildable, machine-independent SSOT) ↔ a neural embedder + int8 quantization make the `sem_floor` boundary hardware/BLAS-dependent. You cannot have model-driven semantic recall **and** a cross-machine-deterministic honest floor on the same query. |
| **Calibrated global cross-tenant ranking** | **5** (no co-mingled cross-tenant index) ↔ a globally-comparable score needs a shared DF/IDF/vocabulary or embedding space. Rank-based RRF fuses a list with *zero* shared statistics (isolation stays structural), but a small-repo rank-1 with inflated IDF can out-rank a large-repo globally-best hit — heuristic order is the ceiling. |
| **Physical delete-by-id** of canonical (silver) knowledge | **1 + 3** (plaintext markdown SSOT that cannot be encrypted + append-only tamper-evident git). PII already distilled into silver can only be forward-redacted or removed via break-glass `git filter-repo`, which breaks the hash chain and forces coordinated re-clone/force-push. |
| **Ingest-surge throughput** | **2** (single-writer curator): `apply_plan` + global `validate_plan` + global `lint` + final-diff-allowlist + one commit + one CAS run single-process over the whole assembled tree, O(N) in surge size → Amdahl ceiling. Parallelizing the reduce = a second writer. |
| **Semantic quality of a just-written item** (pre-consolidation) | **2** (CQRS + sleep-time consolidation): a fresh capture is a durable *lexical* file instantly, but has no embedding/structure until a curator run. |
| **Ecosystem maturity + casual-cloud onboarding** | *Not* invariant-rooted — structural to the self-hosting posture. Years of vendor RAG tooling/community/observability and instant hosted signup cannot be manufactured by an ADR. |

---

## 2. Superset thesis (the reframe)

> Position Agora **not as a worse vector DB but as a structure-aware hybrid retrieval SUBSTRATE**:
> three fused signals — **sparse-lexical** BM25F (shipped), **dense-semantic** (a derived, droppable embed
> view), **curated-structural** (`struct = struct_alpha·1/(1+d_moc) + struct_beta·indeg_norm` = MOC-distance
> + in-degree = curator editorial judgment, *already built*) — computed over a human-readable, git-rebuildable
> markdown SSOT that emits deterministic, testable citations and enforces hard per-repo isolation.

In this framing a vector index is merely **one derived, git-ignored, rebuildable view** — exactly the
[ADR-0012](../adr/0012-deterministic-query-ranking.md) §9 prefilter carve-out that already admits FTS5/ripgrep.
The vector DB is then a strict subset: it holds `{sparse, dense}` flat vectors with **(a)** no editorial/
link-graph structural term, **(b)** no plaintext rebuildable SSOT, **(c)** no deterministic honest `not_found`
floor, **(d)** no hard tenant isolation, **(e)** no crypto-shred-by-subject, **(f)** no portable clone-and-leave.

Campaign discipline: keep the **frozen deterministic lexical+structural core (Contract A) byte-identical and
default-on**, and add semantic recall strictly as an **opt-in, quarantined, non-canonical tail (Contract B)** —
so you gain the vector DB's one real advantage (paraphrase recall) without forfeiting the five guarantees it
can never offer.

---

## 3. Scorecard (axes A–G)

Verdicts are post-adversarial-review. "Win" = a composite a central vector DB structurally cannot assemble;
"parity" = matches; "partial" = closes most, with a residual; grades are hypotheses, not commitments.

| Axis | Verdict | Lever (invariant-safe) | Residual |
|---|---|---|---|
| **A · Semantic recall** | 🟢 **win (composite)** | Embeddings as a **strictly-additive tail**: candidate over-approximation + a `match_reason="semantic"` tier granted *only* to otherwise-ineligible notes, ranked after all lexical/structural evidence. Every lexically-answerable query stays **byte-identical** to today. | `not_found` status non-determinism for sem-only near-floor queries; semantic-only hits cite at note (H1) granularity, not span. |
| **B · Freshness / read-your-own-write** | 🟡 parity | On the **read path**, `Wiki.query` scans `inbox ∪ processing` → a labeled, per-writer "pending" band; capture is retrievable the instant the atomic rename returns. **Sever** any write-time upsert (an unsafe N-writer RMW of shared JSON). | Fresh item is *lexical-only* until consolidation; two-band (coherent vs pending) UX. |
| **C · Ingest throughput / scale** | 🟡 partial | Ship **MAP-AUTHOR (PASS-2) fan-out** now (already per-region-independent, zero consolidation-recall cost, unchanged validate+lint+single-CAS reduce). Quarantine MAP-PLAN sharding behind ADR-0024 §3's saturation metric. | Single-writer reduce is O(N) (Amdahl); speedup contingent on **K independent inference lanes** — single-Ollama default yields ~none. |
| **D · Hybrid / metadata filter / ANN** | 🟡 partial (**structural term = win**) | Tri-signal fusion `{sparse, dense, curated-structural}`; the **structural term is the one component a flat embedding space cannot hold even in principle**. Metadata filter = free pure-Python constraint over `notes.json` frontmatter; ANN is dead weight below #28 scale (numpy flat-cosine is sub-ms). | Dense participation forfeits byte-identical determinism + honest floor → opt-in quarantine only. Structural win collapses to parity on a flat/unlinked corpus. |
| **E · Cross-tenant global answer** | 🟡 partial | **Rank-based RRF** as the fusion spine (zero shared corpus statistics → isolation is *structural, not policed*); fan out one single-repo query per repo over the post-ACL readable set. | True global calibration unwinnable (see §1); fan-out latency O(Σ repo sizes); contingent on Phase-4 auth (fail-closed to omission). |
| **F · Delete / retention / blob-GC** | 🟡 partial (**erasure proof = win**) | **Bronze crypto-shred** (opt-in encrypted `raw/`+`assets/` + mail:/chat: bodies via per-subject envelope keys; destroy-key ≠ edit-event) + a tamper-evident **erasure ledger/certificate**. | Silver-plaintext knowledge is not physically shreddable (§1). Never commit a keyed `subject_id` to the cloned ledger (brute-force oracle) — use a random request-id + git-ignored subject map. |
| **G · Turnkey ops / ecosystem** | 🟡 partial (**portability = win**) | Make **anti-lock-in executable & CI-verified**: a tested one-command `agora export` / clone-and-leave that rebuilds the entire product (wiki + gold packs + rebuilt indexes/dashboards) from the plain git repo on a clean host. MCP-as-SDK. | Vendor ecosystem maturity + casual-cloud onboarding cannot be manufactured (§1); managed hosting reintroduces an (optional, invariant-4-clean) vendor. |

### Genuine composite wins (what a vector DB structurally cannot assemble)

1. **A composite** — zero-lexical-overlap paraphrase recall *delivered with* deterministic citations +
   model-free lexical fallback + git-rebuildable provenance + hard isolation.
2. **D structural-fusion term** — an editorial link-graph signal a flat vector space has no place to hold
   (conditional: a win to the degree the KB is well-linked).
3. **G portability** — knowledge as a portable open git+markdown corpus that reconstructs the whole product
   on a clean host (permanent, CI-verifiable).
4. **F erasure proof** — a signed, distributed, verifiable proof-of-erasure without scrubbing an immutable byte.

---

## 4. Candidate move-plan → reserved ADRs (ordered; evidence gates, NOT decisions)

Ordering rationale is sequencing-of-work, not a commitment to build. Each is gated.

| Order | Reserved ADR (candidate) | Move | Gate / why this slot |
|---|---|---|---|
| 1 | **0033** | Pending read tier / read-your-own-write overlay (Axis B) | Model-free, zero new deps, touches no frozen contract; closes freshness to parity and proves the read-path overlay discipline the later derived views reuse. **No write-time upsert.** |
| 2 | **0032** | Semantic embedding tier as a strictly-additive tail (Axis A) | Keystone. Must **supersede** [ADR-0012](../adr/0012-deterministic-query-ranking.md) §11's "no embeddings" line and amend the §6 gate / `match_reason` / `reason_rank`. Default `sem_enabled=false` keeps §10 reference vectors byte-identical. Embedder pinned to fastembed/ONNX (Apache/MIT), not torch, to protect the lean OSS core. |
| 3 | **0035** | Hybrid tri-signal fusion + metadata-filter constraint (Axis D) | Depends on the 0032 embed view; carries the real differentiator (structural term). #28-scale-triggered (ANN/SQLite are dead weight below it). Default `mode=lexical`, `w_sem=0`. |
| 4 | **0034** | Bulk map-parallel curation (Axis C) | Extends [ADR-0024](../adr/0024-bulk-processing-horizontal-curator-scale.md), resolves its OD-1. Orthogonal to retrieval. Ship PASS-2 fan-out now; quarantine MAP-PLAN. Collision path falls back to K=1 (not same-K re-run) to avoid livelock. |
| 5 | **0030** (reserved) | Cross-repo composition — Stage-8 RRF fusion + promotion airlock (Axis E) | Needs the Phase-4 auth `readable_repo_set` (fail-closed to omission). **Permanently forbid** any shared cross-tenant DF/IDF/vocab/embedding table. Annotate ADR-0012 §11's "no RRF" as scoped to the *intra*-repo scorer. |
| 6 | **0031** (reserved) | Retention / right-to-delete — bronze crypto-shred + erasure ledger (Axis F) | Hard prerequisite for the mail:/chat: connectors; constrains 0030 federation. Confine crypto-shred to bronze; per-repo keystore rides the curator's existing sole-writer `raw/`-materialization step. |
| 7 | **0029** (reserved) | Connector ecosystem / CWP + read-only integrations surface; + non-invariant Phase-5 deploy profile (Axis G) | Capstone: benefits from every interop contract being frozen first, and from 0031 retention existing before advertising PII-bearing connectors. Deploy profile is a non-ADR addendum. |

> ADR-0028 (LLM `DISTILL`) stays reserved for the outbound-pack/distill unification — adjacent to, but off,
> this retrieval-vs-vector-DB axis set.

---

## 5. Two ship-now, provably-safe wins

Both close a real gap **without touching a frozen contract**:

1. **Strictly-additive semantic tail (ADR-0032 candidate).** `sem` may *only* (a) over-approximate the
   candidate set and (b) grant eligibility+score to notes otherwise ineligible (`lex==0 ∧ no theme`), tagged
   `match_reason="semantic"` ranked after all lexical/heading/linked-theme evidence. This converts the vague
   "sem never demotes a lexical hit" into a **provable, testable invariant**: *every lexically-answerable
   query is byte-identical to today; semantic only ever appends a lower tier and never perturbs, reorders, or
   demotes existing evidence* — confining all new non-determinism to that appended tail.
2. **MAP-AUTHOR (PASS-2) parallelism (ADR-0034 candidate).** PASS-2 is already structured as independent
   per-region work (each region edits only its own sentinel markers, structure/links/frontmatter already
   materialized by `apply_plan`). Fan `needs_prose.items()` out under a `max_map_workers` cap → real speedup
   of the second brain pass with zero consolidation-recall cost; the unchanged `validate_author_diff` + lint
   + single-CAS reduce still gate the whole assembled diff once.

**Highest-leverage strategic move:** move the fight onto the **curated-structural term**. Lead every
comparison with `{sparse, dense, curated-structural}` vs the vector DB's `{sparse, dense}`. Do **not** lead
with ANN or metadata filtering (pure catch-up parity). The structural signal is the one retrieval component a
flat embedding space cannot hold even in principle.

---

## 6. Open decision gates

Nothing here is decided. Each move ripens on concrete evidence:

- **0032 / 0035 (semantic + hybrid):** does navigation genuinely prove insufficient at scale? The stated
  trigger is **#28** (corporate volume). Below team-wiki scale (hundreds–low-thousands of notes) a full
  pure-Python rescan is fast and the marginal recall from embeddings may not justify the determinism cost.
- **0034 (bulk parallel):** is there a real ingest-surge bottleneck *and* provisioned K independent inference
  lanes? On the single-Ollama default the speedup is ~zero — measure ADR-0024 §3 saturation first.
- **0030 (cross-repo):** blocked on Phase-4 auth existing (post-ACL `readable_repo_set`).
- **0031 (retention):** blocked *ahead of* any mail:/chat: connector; must land before PII-bearing sources.
- **Global determinism:** is a cross-machine byte-identical honest floor a hard product requirement? If yes,
  semantic recall stays permanently opt-in/default-off (Contract B).

---

## 7. Provenance & non-commitment

Produced 2026-07-10 by a two-stage adversarially-verified multi-agent analysis (grounding readers over the
ADRs/DESIGN/`src`, per-axis designers, and adversarial verifiers that checked each proposed move for hidden
invariant breaches and win-level honesty). **This is a snapshot of an exploration, not a plan of record.**
Code citations (`file:line`, ADR §) were current at authoring time and must be re-verified before any move is
authored as an ADR. When a move is ratified, promote its section into the corresponding ADR and leave a
`superseded by ADR-00xx` pointer here.

Related: [`DESIGN.md`](../DESIGN.md) · [`ROADMAP.md`](../ROADMAP.md) · [`adr/README.md`](../adr/README.md).
