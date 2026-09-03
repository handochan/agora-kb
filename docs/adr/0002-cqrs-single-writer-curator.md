# ADR-0002 — CQRS + single-writer curator for concurrency

**Status:** Accepted · 2026-06-13 · **scope clarified by the 2026-07-29 appendix (issue #99): the
single-writer clause bounds who may mutate the event spool, and the five-clause spool-custodian rule
says on what terms a non-curator process may.**
**AMENDED (append-only) — [ADR-0041](0041-stratum-kind-first-layout.md) (Proposed, KB wiki schema 2) narrows the WIKI half of the single-writer clause, in the same shape as the #99 appendix narrowed the spool half.** This ADR's Decision reads *"exactly one curator process per repo reads the inbox and edits the shared `wiki/`, indexes, and `log.md`"*; under schema 2 that clause is **re-read as governing the CURATED wiki**, with `wiki/people/**` — a HUMAN-OWNED namespace — outside it. The exception is safe on this ADR's own stated ground: the Context justifies the rule because *"file-level 'last write wins' loses data when two writers touch **the same shared file**"*, and no curator write ever touches a file under `wiki/people/**` — ADR-0041 D4.1 carves that subtree out of the ADR-0008 §4 / ADR-0011 §4.0 allowlist, so any add/modify/rename/delete under it in a curated diff FAILS the run. The two writers therefore never share a file, and the invariant the clause exists to protect is untouched. Single-writer over the curated `wiki/`, `index.md`, the indexes and `log.md`: **unchanged**. The prose below is retained verbatim for history.

## Context
Many writers (multiple agents, multiple people, the harvester) must contribute concurrently, while the
shared artifacts (wiki pages, indexes, `log.md`) must stay consistent. File-level "last write wins"
loses data when two writers touch the same shared file. OS locks (`flock`) are fragile across
container↔host and network boundaries. We want correctness without heavyweight coordination.

## Decision
Separate writes from the read-model (CQRS), and make the read-model **single-writer**:
- **Writes** append **one immutable file per writer** to `_kb/inbox/<writer>/<id>.md`. Disjoint
  keys ⇒ concurrent writes never conflict (event-sourcing append log).
- **Exactly one curator process per repo** reads the inbox and edits the shared `wiki/`, indexes, and
  `log.md`. One writer ⇒ no races on shared files. A `curator.lock` (flock) enforces the singleton
  locally; horizontal scale shards repos so each repo has one owner.
- **git** is the curated-content audit log and rollback unit (one commit per run). One repo owner
  advances the curated branch; gateways do not push competing working-copy commits.

## Consequences
- **+** Conflict-free concurrent capture; race-free consolidation; no network-wide locks needed.
- **+** Crash-safe & idempotent: run manifests recover claimed events; event keys prevent duplicate
  delivery and content hashes identify equivalent content without discarding provenance.
- **+** Scales to teams unchanged — more writers just mean more disjoint inbox files.
- **−** **Eventually consistent**: a captured item isn't queryable until the curator runs. Mitigated by
  triggers (threshold/idle) and by surfacing backlog via `kb_status`.
- **−** The per-repo curator is a singleton bottleneck for *write-model* throughput; acceptable because
  consolidation is batch/background, not on the request path.

## Appendix (2026-07-29, append-only) — the spool-custodian rule (issue #99)

Nothing above changes. This appendix answers a question the Decision above does not: **who, other
than the curator, may mutate the event spool, and on what terms?**

**Context.** `agora requeue` returns terminal-failure events from `_kb/failed/` to `_kb/inbox/`
after an operator has fixed the cause of the failure. Three things about it are new:

1. a **non-curator process writes under `_kb/failed/`**;
2. an event enters the inbox by **rename of an existing immutable event** rather than through
   `Inbox.write`;
3. a **non-curator process acquires `curator_lock`**.

`_kb/inbox/` itself has always been many-writer (that is the point of the Decision's first bullet —
DESIGN §2.2; the harvester, the MCP face and the web face all append there today), so *that* part
is not new and is not what needs a ruling. What needs one is the lock: holding it buys mutual
exclusion, not capability. A command that took `curator_lock` and rewrote `wiki/` would be perfectly
serialized and completely illegitimate.

**Decision.** This ADR's single-writer clause is re-read as **"one mutator of the event spool at a
time, of which the curator remains the only one that may CREATE or PUBLISH."** A non-curator process
may mutate the spool only when all five of the following hold:

- **C1 — serialized.** It holds `curator_lock` for the whole operation (scan → plan → execute),
  serialized against the curator by the same primitive. Non-blocking: contention is a clean refusal,
  never a wait and never a partial batch.
- **C2 — location-only.** Every mutation is a lifecycle *location* transition of an existing spool
  file — one `os.replace`, content byte-identical, `id` unchanged. Nothing is created, edited or
  truncated. (Creating a directory as a rename destination is not creating content.)
- **C3 — addresses are derived, not accepted.** The destination derives from the moved file's own
  immutable content **or its existing spool address**, run through the DESIGN §7 traversal guards
  (`validate_writer`, `is_valid_event_id`, `safe_path_component` as applicable) — never from raw
  operator input. `_kb/failed/` is operator-editable, so a hand-edited frontmatter `id` is untrusted
  input like any other.
- **C4 — read-model untouched.** It never touches `wiki/`, `raw/`, `index.md`, `log.md`, git, or
  `_kb/state.json`, and publishes nothing.
- **C5 — non-destructive.** An occupied destination is skipped and reported, never overwritten
  (overwriting would destroy an immutable event — invariant 3), and nothing is ever deleted. A
  record leaving `failed/` is *relocated*, never removed; even an emptied `failed/<date>/<run>/`
  directory is left in place.

`agora requeue` satisfies C1–C5 and is test-locked against each. A hypothetical `agora prune` /
`agora forget` (which deletes) does **not** satisfy C5 and needs its own ADR — which is the point of
stating the rule as five clauses rather than "holds the lock ⇒ may act".

**Consequences.**

- **(a)** `_kb/failed/ → _kb/inbox/` becomes a documented lifecycle **back-edge**, alongside the
  `_kb/processing/ → _kb/inbox/` retry edge the curator has always had. `docs/DATA-MODEL.md` §1
  names both.
- **(b)** The already-delivered pre-check `agora requeue` performs (tier-1 `state.event_keys`, so an
  event whose key is already published is skipped rather than turned into an inbox zombie) is
  **best-effort, not authoritative**: `worker.recover()` runs **outside** `curator_lock`
  (`worker.py:941`; callers `cli.py:615`, `cli.py:1377`, `faces/mcp_server.py:867`) and writes
  `state.event_keys` via `_finalize_recovered` (`worker.py:1017-1018`). No lock span requeue could
  take would make the check authoritative. **Unlocked `recover()` is a known, open gap in this
  ADR**, tracked separately; this appendix does not close it and must not be read as doing so.
- **(b′)** C5's occupied-destination guard is `dest.exists()` followed by `os.replace`
  (`core/inbox.py`, `resolve_inbox_return` → `return_event_to_inbox`) — a **check-then-act pair, not
  an atomic operation**. Against the curator's own runs the pair is safe, because C1's lock excludes
  them; against `recover()` it is not, for exactly the reason in (b) — that path writes both
  `_kb/inbox/` and `_kb/failed/<date>/<run>/` while holding no lock. No reachable interleaving
  exists on today's code (it would need one event id present in both `_kb/failed/` and
  `_kb/processing/<run>/events/` at once, which no non-hand-edited path produces), so this is a
  latent hazard of the same open gap rather than a live defect. It is recorded here so that closing
  (b) is understood to close this too; C5 is not to be read as promising atomicity.
- **(c)** `curator_lock` is `fcntl`-based and therefore POSIX-only, so making the recovery command
  lock-mandatory (C1) makes it unavailable on native Windows — an obligation for epic #85, not a
  reason to relax C1.
- **(d)** The rule is enforced by **tests, not convention**: lock-held-is-a-no-op, rename-only
  byte-and-inode identity, occupied-destination non-destruction, deletes-nothing, and
  never-writes-`state.json`.
