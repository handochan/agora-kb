# ADR-0002 — CQRS + single-writer curator for concurrency

**Status:** Accepted · 2026-06-13

## Context
Many writers (multiple agents, multiple people, the harvester) must contribute concurrently, while the
shared artifacts (wiki pages, indexes, `log.md`) must stay consistent. File-level "last write wins"
loses data when two writers touch the same shared file. OS locks (`flock`) are fragile across
container↔host and network boundaries. We want correctness without heavyweight coordination.

## Decision
Separate writes from the read-model (CQRS), and make the read-model **single-writer**:
- **Writes** append **one immutable file per writer** to `inbox/<repo>/<writer>/<id>.md`. Disjoint
  keys ⇒ concurrent writes never conflict (event-sourcing append log).
- **Exactly one curator process per repo** reads the inbox and edits the shared `wiki/`, indexes, and
  `log.md`. One writer ⇒ no races on shared files. A `curator.lock` (flock) enforces the singleton
  locally; horizontal scale shards repos so each repo has one owner.
- **git** is the audit log, rollback unit (one commit per run), and optimistic-concurrency backstop
  for distribution (pull --rebase before push).

## Consequences
- **+** Conflict-free concurrent capture; race-free consolidation; no network-wide locks needed.
- **+** Crash-safe & idempotent: unmoved inbox items reprocess; `content_sha256` dedups.
- **+** Scales to teams unchanged — more writers just mean more disjoint inbox files.
- **−** **Eventually consistent**: a captured item isn't queryable until the curator runs. Mitigated by
  triggers (threshold/idle) and by surfacing backlog via `kb_status`.
- **−** The per-repo curator is a singleton bottleneck for *write-model* throughput; acceptable because
  consolidation is batch/background, not on the request path.
