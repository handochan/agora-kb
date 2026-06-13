# ADR-0002 — CQRS + single-writer curator for concurrency

**Status:** Accepted · 2026-06-13

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
