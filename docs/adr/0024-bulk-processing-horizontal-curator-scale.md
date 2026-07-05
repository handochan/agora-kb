# ADR-0024 — Bulk knowledge processing & horizontal curator scale (shard by repo)

**Status:** Accepted · 2026-07-05 (Step-0 ratified, #36) · Proposed 2026-06-24

Extends [ADR-0002](0002-cqrs-single-writer-curator.md) (CQRS + exactly one curator per repo) and
[ADR-0006](0006-repo-as-tenant-boundary.md) (repo = tenant boundary; the natural sharding axis).
Builds on the optimistic-concurrency backstop of [ADR-0008](0008-transactional-sandboxed-curation.md)
(the curated-ref CAS). Bounded by [ADR-0005](0005-fully-oss-bom.md) (any coordination tech stays
OSS-pure in core; copyleft/AGPL coordinators only behind optional adapters — invariant #4). Sibling
to the Phase-5 face work in [ADR-0019](0019-web-face-stack.md) §6/§7 (externalized session store +
repo-affine owner routing). Frames ROADMAP backlog issue **#27** (대량 지식 처리 — bulk knowledge
processing via multiple non-conflicting curators).

## Context
Issue #27 asks to "process knowledge in parallel with MULTIPLE curators WITHOUT overlap/conflict" as
the team and write-throughput grow. This is a **write-model throughput** request, and the phrase
"multiple curators" has a safe reading and a dangerous one that must be kept apart, because the
dangerous one breaks invariant #2.

**Current state (grounded):** today there is exactly **one** curator writer per repo, enforced at two
layers.
- A **host-local, non-blocking** per-repo flock: `curator_lock()` (`curator/claim.py:50-72`) does
  `fcntl.flock(LOCK_EX | LOCK_NB)` on `_kb/curator.lock`; if held, a second curator **on the same
  host** raises `LockHeld` and the run returns `status="noop"`. This is `fcntl` (`claim.py:27`), so it
  is **host-local** — it does NOT protect against two curators on two hosts/containers sharing a
  working copy.
- An **optimistic CAS** on the single curated branch ref: `Repo.compare_and_swap_branch()`
  (`core/repo.py:347-363`) wraps `git update-ref <ref> <new> <expected>`; if another writer advanced
  the branch since `base_commit`, the swap returns `False`, and the worker **discards the whole
  worktree diff and retries next run** (`worker.py:534-545`, `cas_conflict=True` — a CAS conflict
  never burns the §5.1 retry budget). The CAS base is the curated branch ref, not owner HEAD
  (ADR-0008 read-after-publish). Critically, `compare_and_swap_branch` is an **UPDATE on ONE ref**
  (`repo.py:357-363`: `refs/heads/<branch>`) — every writer for a repo serializes through that single
  ref regardless of which files it touched. **This already makes accidental double-writers
  safe-but-wasteful: the loser discards a full run's work; it never corrupts.**

The claim is **whole-inbox with no event cap.** `claim()` / `_fifo_snapshot()` (`claim.py:75-`)
snapshots **every** pending `_kb/inbox/*/*.md` event in FIFO (event-id == chronological) order,
applies tier-1 `writer:event_key` dedup authoritatively under the lock (`_dedup_tier1`), and
`os.replace`s the selected files into `processing/<run-id>/events/` — and the snapshot is "the run's
sole event universe" (`claim.py:18`; DESIGN §4 step 2). `build_bundle()` (`curator/bundle.py`) then
emits **one candidate per tier-2 `content_sha256` group for the ENTIRE claimed set into one prompt**.
So a backlog surge produces **one large prompt** that can exceed a brain's context window and silently
degrade plan quality long before any "need more curators" signal fires.

**There is already a DOCUMENTED-BUT-UNWIRED bundle cap.** INGEST-CONTRACT §1.3 (`docs/INGEST-CONTRACT.md:160`)
specifies `repo.yaml curator.limits.max_candidates_per_run` (default **32**) as "the FIFO claim caps
the snapshot so a run never overflows context", alongside `related_k` (default 8) and
`related_excerpt_bytes`. But `load_repo_config` (`config.py:116-158`) reads only
`curator.{backend,max_attempts,allow_reduced_isolation,triggers}` (plus `harvest.*`) via explicit
`.get()` on the raw mapping and **silently ignores** `curator.limits` (DATA-MODEL §3,
`docs/DATA-MODEL.md:83-85`: "a forward-looking `curator.limits` … is **silently ignored** — not yet
wired"). So **the contract already names a per-run cap of 32, but no code enforces it** — exactly the
status this ADR's proposed `max_events_per_run` would otherwise share. (`RepoConfig` is `extra='forbid'`,
but because `load_repo_config` maps explicit fields rather than `RepoConfig(**raw)`, an unknown
`curator.limits.*` key is **ignored, not rejected** — so wiring either cap is purely additive.)

Every run touches **genuinely shared files.** The curated allowlist
(`apply.py:17-18`: `wiki/**`, `index.md`, `<domain>-moc.md`, `log.md`, `assets/**`) includes the
root `index.md`, which `_update_index` rewrites with the current domain set on essentially every run,
and the append-only `log.md`. Two concurrent curators editing one repo would both write `index.md`
and `log.md` — so "disjoint file sets" is **false today** even under a domain partition.

The ROADMAP already names the safe scaling axis: Phase 5 lists "horizontal curator sharding by repo"
(`ROADMAP.md:101`) and "Horizontal face scaling with repo-affine owner routing and failover fencing"
(`ROADMAP.md:102`), and Phase 4 the "Single repo-owner service per working copy; gateways route
captures to the owner (no competing clones)" (`ROADMAP.md:90`). DESIGN §7 and ARCHITECTURE §2 already
state "if scaled horizontally, repos are sharded so each repo is owned by one repo-owner/worker pair."
So multi-curator **across** repos is designed-but-unbuilt; multi-curator **within** a repo is excluded
by invariant #2 / ADR-0002.

The intent therefore decomposes into three sub-goals with very different risk profiles, which this ADR
keeps separate: **(1)** horizontal scale **across** repos (N single-writer curators on N repos);
**(2)** higher throughput **within** one repo without a second writer (bounded batching + trigger
tuning); **(3)** TRUE intra-repo parallelism (≥2 curators editing one repo's wiki at once), which
directly conflicts with invariant #2 / ADR-0002.

## Proposed Decision
**Parallelism is ACROSS repos only. One owner/curator per repo remains an invariant** (this ADR
*extends*, never amends, ADR-0002/0006). "Multiple curators without conflict" is realized as **N
single-writer curators on N repos**, never as a second writer to one repo. Concretely:

1. **Intra-repo throughput is pipelining, not a second writer (pull forward NOW; invariant-neutral).**
   Wire a `repo.yaml` `curator.limits` event cap that bounds how many events `claim()` pulls per run.
   The cap **slices the FIFO snapshot at the head** so the run keeps:
   - FIFO / chronological order (`_fifo_snapshot` sort, `claim.py`),
   - authoritative tier-1 `writer:event_key` dedup (`_dedup_tier1`),
   - the "the snapshot is the run's sole event universe" contract (`claim.py:18`; DESIGN §4 step 2).

   The remainder stays in the inbox for the **next** trigger (smaller, more frequent runs). The head
   is chronological across **all** writers, so no per-writer starvation. This is the single
   highest-leverage, lowest-risk change because the **real first bottleneck is per-run prompt size,
   not writer count** (`bundle.py` emits one candidate per `content_sha256` group). See OD-3 for the
   `max_events_per_run`-vs-`max_candidates_per_run` reconciliation, which is the open sub-decision
   here.

2. **Cross-host single-writer requires a repo→owner fencing lease (Phase 5).** Because the flock is
   host-local (`claim.py:27`), the curated-ref CAS (`repo.py:347-363`) is the **only** cross-host
   backstop today, and it merely *serializes* (the loser discards work, `worker.py:534-545`) — it
   never corrupts but it wastes a whole run. Cross-host horizontal scale therefore needs a per-repo
   **owner lease**: a CAS on a dedicated ref (e.g. `refs/agora/owner/<repo>`) with a heartbeat/TTL,
   plus a **fencing token** that gates the publication CAS so a host that lost the lease cannot
   advance the curated branch. This **reuses the CAS primitive already trusted for publication**,
   needs **zero new infra** (honoring the zero-infra ethos), and stays **OSS-pure** (ADR-0005). An
   external coordinator (etcd/Consul, Apache/MPL) is permitted **only as an optional adapter behind an
   interface**, never a core dep (invariant #4); the git-ref lease is the OSS default. The filesystem
   lease-file option is rejected (it repeats the cross-boundary NFS/container fragility ADR-0002
   explicitly cites for flock).

3. **Throughput observability is the evidence gate, AND the cross-host footgun guard (pull forward NOW).**
   Extend the Prometheus exporter (`faces/web/metrics.py`) + dashboard with **per-repo** queue depth
   (inbox backlog), run latency, batch size (events/run), and a **CAS-conflict-rate** counter (fed from
   the `cas_conflict=True` discard path, `worker.py:534-545`). These make "is one curator saturating?"
   measurable — the precondition for ever revisiting sub-goal (3). The CAS-conflict-rate is a
   **first-class dashboard signal/alert**, not just a number: a non-trivial conflict rate on a repo with
   no intentional multi-owner setup is the symptom of an accidental cross-host double-writer (whose
   losing runs *look like a hang* under load). In addition, add a cheap **host-local stale-lock /
   double-heartbeat warning**: when a curator observes a lock or owner heartbeat it does not own that is
   live for the same repo (>1 curator heartbeat seen), log a warning so a wasteful cross-host writer is
   **visible** rather than silently discarding full runs. Reads current state per scrape (no `lint()`
   on scrape, consistent with ADR-0019 §5 / the existing no-lint rule). This makes the cross-host
   residual (see Consequences) operationally **detectable** before the §2 lease lands, rather than
   relying on prose "unsupported".

4. **Intra-repo MULTI-writer is EXPLICITLY OUT OF SCOPE pending an ADR + a proven benchmark.** TRUE
   intra-repo parallelism (sub-goal 3) is not adopted. The only invariant-respecting form would be
   **domain-partitioned** writers owning disjoint `wiki/<domain>/` subtrees — but it founders on TWO
   structural blockers, not one:
   - **(i) Shared files.** The root `index.md` (rewritten by `_update_index` nearly every run) and the
     append-only `log.md` are in the curated allowlist (`apply.py:17-18`) and touched by nearly every
     run regardless of domain, so "disjoint file sets" is false without a large schema restructure.
   - **(ii) Single curated-ref CAS.** Even if file sets WERE made disjoint (per-domain index fragments +
     per-curator log shards), every writer still publishes through the **one** curated branch ref:
     `compare_and_swap_branch` is an UPDATE on a single `refs/heads/<branch>` (`repo.py:357-363`), so
     two writers to one repo serialize and one always loses the CAS regardless of file disjointness.
     Domain-partitioned writers would therefore *also* need either **per-domain branches** (a bigger
     model change to the publish/read model) or a **serialized publish step** — i.e. they do not escape
     the single-writer publish even after the shared-file problem is solved.

   Both blockers exist for **unproven** benefit. Revisiting it requires both a superseding/amending ADR
   **and** a measured single-curator-saturation benchmark from §3's metrics. **Default expectation:
   no-go.**

### Open sub-decisions (this ADR is Proposed; these are the deliberately-deferred choices)

- **OD-1 — Do we ever allow TRUE intra-repo parallelism (≥2 writers to one repo)?**
  - (a) **No** — single-writer per repo is permanent; scale only by sharding across repos + bounded
    batching.
  - (b) **Yes, domain-partitioned** — disjoint `wiki/<domain>/` subtrees, *plus* a fix for BOTH the
    shared root `index.md` + `log.md` (per-domain fragments / per-curator log shards merged
    deterministically) AND the single curated-ref CAS (per-domain branches or a serialized publish step).
  - (c) **Yes, full** — multiple writers with merge/rebase + CAS-retry on a shared branch.
  - **Recommendation:** (a) now; keep (b) as a researched, ADR-gated future **only if** §3's metrics
    prove a single repo saturates a single curator. (c) is rejected outright — it reintroduces exactly
    the shared-file race ADR-0002 was created to avoid and breaks invariant #2.

- **OD-2 — What mechanism assigns and fences repo→owner for cross-host scale?**
  - (a) **Git-ref lease** (CAS on `refs/agora/owner/<repo>` + heartbeat/TTL + fencing token).
  - (b) **Filesystem lease file** on shared storage with mtime heartbeat.
  - (c) **Optional external coordinator adapter** (etcd/Consul) behind an interface.
  - **Recommendation:** (a) as the OSS default, with (c) available behind an interface for large
    deployments. (b) is rejected (repeats the cross-boundary fragility ADR-0002 rejects for flock).

- **OD-3 — Reconcile the per-run cap: wire the EXISTING `max_candidates_per_run` (32), add a new
  `max_events_per_run`, or both?**
  - **Context:** INGEST-CONTRACT §1.3 already documents `curator.limits.max_candidates_per_run` (default
    32) but it is **unwired** (`config.py:116-158` ignores `curator.limits`). The two caps bound *nearly*
    the same thing at different stages: events dedup to candidates via tier-2 `content_sha256` grouping,
    so they differ only by the dedup ratio.
  - (a) **Wire the already-specified `max_candidates_per_run` only** (close the spec-vs-code gap). This
    bounds the **POST-dedup** candidate bundle directly (what actually drives prompt size). **Back-compat
    note:** the contract already documents a default of **32**, so wiring it means the *spec'd* default
    becomes effective — this is honoring the contract, not "silently changing to unbounded".
  - (b) **Add a new `max_events_per_run` in addition.** This slices the **FIFO head PRE-dedup** (in
    `claim()`), bounding the work claimed off the inbox before tier-1/tier-2 ever run. Justified only if
    we want to bound *claim* cost (large duplicate floods) independently of bundle size.
  - (c) **Both**, with explicit interaction: `max_events_per_run` slices the FIFO head **pre-dedup** in
    `claim()`; `max_candidates_per_run` bounds the **post-dedup** bundle in `build_bundle()`. The event
    cap is the coarse claim-cost bound; the candidate cap is the precise prompt-size bound.
  - **Recommendation:** **(a)** first — wire the already-documented `max_candidates_per_run` (default 32),
    because prompt size is the real first bottleneck and the contract already names this knob; treat (b)
    `max_events_per_run` as the optional **pre-dedup** add-on under (c)'s stated interaction only if claim
    cost (not bundle cost) is later shown to dominate. Whichever lands, **update INGEST-CONTRACT §1.3 in
    lockstep** (flip the relevant cap from documented-but-unwired to wired; keep the other's
    documented-but-unwired caveat accurate). Design every cap to slice the FIFO **head** so tier-1 dedup
    and the "sole event universe" contract are byte-for-byte preserved. A token-budget cap (cap by
    prompt-token budget rather than raw count) is a follow-up — more correct but needs a
    tokenizer-dependency decision.

## Alternatives considered
- **Two writers to one repo's branch with merge/rebase (OD-1c).** Rejected. Reintroduces the exact
  shared-file race (`index.md`, `log.md`) ADR-0002 was created to avoid AND still contends on the single
  curated-ref CAS; a direct invariant #2 breach.
- **Domain-partitioned intra-repo writers now (OD-1b).** Rejected for this ADR. Founders on BOTH the
  shared root `index.md` + append-only `log.md` (`apply.py:17-18`) AND the single curated-ref CAS
  (`repo.py:357-363`), so "disjoint file sets" alone is insufficient — for benefit that no metric yet
  shows is needed (consolidation is batch/background by design, ADR-0002 consequences). Researched,
  ADR-gated future only.
- **Inventing a brand-new event cap while the contract's `max_candidates_per_run` (32) stays unwired.**
  Rejected as the *first* move — it would ship a second, differently-named, differently-defaulted cap
  that contradicts a contract already documenting 32. Wire the existing candidate cap first (OD-3a);
  add an event-level pre-dedup cap only with the stated interaction (OD-3c).
- **Default-on bounded batch with a silent new default.** Rejected — wiring `max_candidates_per_run`
  honors the contract's documented 32; introducing a *new* unbounded→bounded default silently would
  change every existing repo's run shape and could surprise dogfood KBs.
- **Filesystem lease for cross-host fencing (OD-2b).** Rejected — repeats the cross-boundary
  NFS/container fragility ADR-0002 cites for flock.
- **Lean on Redis/etcd as a required core lock.** Rejected as core — would pull a coordinator
  dependency into the core (invariant #4 / ADR-0005). Permitted only as an **optional** adapter; the
  git-ref CAS lease keeps core dependency-free.
- **Build intra-repo parallelism speculatively before measuring.** Rejected — no measured evidence of
  single-curator saturation exists; the bounded-batch claim addresses the real first bottleneck
  (per-run prompt size) at near-zero risk, and §3's metrics are the honest gate for anything more.

## Consequences
- **Invariant impact (the central point): NONE for the adopted slice.** §1 (bounded batch) and §3
  (observability) are pure intra-repo pipelining + read-side metrics — the single-writer flock + CAS
  are untouched, FIFO + tier-1 dedup + "sole event universe" are byte-for-byte preserved, and the cap
  defaults follow the documented contract (32 for `max_candidates_per_run`; `max_events_per_run` would
  default unbounded). §2 (fencing lease) is *additive* and writes the lease ref outside the curated
  tree, so the ADR-0008 integrity boundary and the publication CAS are unchanged (the fencing token
  only *gates* the existing CAS). Invariant #2 / ADR-0002 is **strengthened**, not weakened: this ADR
  pins "parallelism is across repos only" in one citable place.
- **+** "Multiple curators without conflict" is delivered honestly: N single-writer curators on N
  repos under per-repo owner leases — the literal request, with no shared-file race.
- **+** The CAS already guarantees correctness under accidental contention (loser discards, never
  corrupts), so the safety story holds even before the lease lands; the lease converts "wasteful but
  safe" into "exclusive and efficient" across hosts.
- **+** Fixes the real first bottleneck (large per-run prompt size) cheaply and immediately for
  dogfooding by finally wiring the contract's already-documented cap, independent of Phase 4.
- **+** Co-designed with ADR-0019 §6/§7: the Phase-5 repo-affine FACE routing and the owner-routing
  curator sharder share one owner-assignment layer (faces route a capture to the owner of its target
  repo).
- **− (residual risk) Misreading "multiple curators" as a second writer.** Highest risk. Mitigated by
  pinning the by-repo-only axis here and by the CAS making accidental double-writers safe-but-wasteful.
- **− (residual risk) Cross-host false safety.** `fcntl` flock is host-local (`claim.py:27`); assuming
  it gives multi-host mutual exclusion is a latent integrity assumption if two hosts share a working
  copy. The CAS serializes but the loser silently discards a full run (wasted compute / latency that
  can *look* like a hang under load). The §2 lease is the documented fix; until it lands, the §3
  CAS-conflict-rate alert + the host-local double-heartbeat/stale-lock warning make the footgun
  **operationally visible** rather than relying on prose "unsupported".
- **− (residual risk) Harvester counter race.** More parallel repos/curators widen the window on the
  best-effort harvest-cursor `accepted`/`rejected` writes (ADR-0017 §7; load-then-save). Confirm
  last-writer-loses there stays a mere counter inaccuracy on a derived/rebuildable value (DATA-MODEL
  §6), never an integrity issue.
- **Deferred (not solved): intra-repo MULTI-writer** (OD-1b/c) — out of scope behind an ADR + a
  proven single-curator-saturation benchmark, with the shared root `index.md` + append-only `log.md`
  **and** the single curated-ref CAS recorded as the two concrete blockers. **The fencing lease +
  owner-routing sharder service** land in **Phase 5**, after the Phase-4 multi-repo owner service exists
  (you cannot shard across repos before multi-repo exists). Token-budget batching is a follow-up.

## Implementation sketch
- **Now (Phase 3.5, invariant-neutral):**
  - Teach `load_repo_config` (`config.py:116-158`) to read `curator.limits` (today silently ignored,
    DATA-MODEL §3) onto `RepoConfig`. Per OD-3a, wire the **already-documented**
    `max_candidates_per_run` (default 32) so `build_bundle` bounds the post-dedup candidate set;
    optionally (OD-3b/c) also thread a `max_events_per_run` into `claim()` so `_fifo_snapshot` /
    `_dedup_tier1` slice the **head** of the FIFO list pre-dedup. Whichever lands, **update
    INGEST-CONTRACT §1.3 in lockstep** (mark the wired cap wired; keep the other's
    documented-but-unwired caveat accurate) and update DATA-MODEL §3. Pure pipelining; single-writer
    untouched. Add a test asserting an over-cap backlog claims/bundles only up to the cap, the
    remainder stays in the inbox, and FIFO + tier-1 dedup are unchanged.
  - Extend `faces/web/metrics.py` + the dashboard with per-repo queue depth, events/run, run duration,
    and a **CAS-conflict-rate** counter (fed from the `cas_conflict=True` discard path,
    `worker.py:534-545`) surfaced as a first-class dashboard signal/alert. Add the host-local
    stale-lock / >1-heartbeat warning. Read current state per scrape, no `lint()`.
  - Promote the existing one-liners in DESIGN §7 / ARCHITECTURE §2 into a named "Bulk / horizontal
    scale: shard BY REPO, never within a repo" subsection citing this ADR; record that flock is
    host-local and CAS is today's only cross-host backstop. Split the ROADMAP Phase-5 sharding bullet
    into (5a) repo→owner assignment + fencing lease and (5b) intra-repo throughput knobs, marking
    intra-repo multi-writer out of scope pending an ADR + evidence.
- **Phase 5 (after the multi-repo owner service):**
  - Implement the repo→owner lease: CAS-claim `refs/agora/owner/<repo>` with heartbeat/TTL; mint a
    fencing token that gates `compare_and_swap_branch` (`repo.py:347-363`) so a lost-lease host cannot
    publish. Provide an optional external-coordinator adapter interface (etcd/Consul) with the git-ref
    lease as the OSS default.
  - Build the owner-routing/sharder supervisor: given a set of repos, run each repo's curator
    concurrently under its owner lease (N single-writer curators on N repos), co-designed with the
    ADR-0019 §7 repo-affine face routing and per-repo trigger backpressure.
- **Only if metrics prove saturation (ADR-gated research spike):** time-boxed go/no-go memo + draft
  ADR on domain-partitioned intra-repo writers (per-domain index fragments + per-curator log shards
  merged deterministically, **plus** per-domain branches or a serialized publish step to escape the
  single curated-ref CAS). Output is a memo, not code. Default expectation: no-go.
