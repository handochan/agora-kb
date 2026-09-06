# ADR-0006 — Repo = tenant boundary (team & personal repos)

**Status:** Accepted · 2026-06-13
**AMENDED (append-only) — [ADR-0041](0041-stratum-kind-first-layout.md) (Proposed, KB wiki schema 2) removes the PATH BASIS of the §Decision clause *"optional per-domain ACL (`wiki/<domain>`)"*:** the subject leaves the path for `subjects:` frontmatter, so there is no `wiki/<domain>` subtree left to scope an ACL to. ADR-0036 had already demoted that ACL to a convenience filter (#55); repo-as-tenant-boundary itself is UNCHANGED. The prose below is retained verbatim for history.
**AMENDED (append-only) — [ADR-0037](0037-local-multi-kb-federation.md) (Proposed, local multi-KB federation) REVISES the reading of §Decision's *"Isolation is structural"* clause, and with it invariant 5:** a repo is a **security / audience / custody boundary, not an account**. One process MAY **READ** N repos — that is what the `$AGORA_HOME` registry and the declaration-order bands are — provided each KB is a separate `Repo`/`RepoLayout`/handler instance, no module-global mutable state is introduced, and every write path names its KB explicitly. **WRITE CUSTODY IS UNCHANGED and stays one repo per process:** a curator/face still writes only its own repo's files, the per-repo `_kb/curator.lock` remains the single-writer gate (ADR-0002/0008), and an attached KB is `role: reader` — structurally read-only, never a write target. **INVARIANT 2 IS UNCHANGED AND UNTOUCHED:** an attached mirror's `wiki/` tree is a git-materialised REPLICA of an upstream curator's output, so "only the curator writes `wiki/`" binds that upstream curator; the only local writers of a mirror directory are `agora kb attach` / `refresh` / `detach` (whole-tree `git clone` / `fetch` + re-pin-by-checkout / removal, never a note edit) plus the root `.agora-mirror.yaml` marker, and nothing else in this process may write inside a mirror (ADR-0037 D9). The §Decision line *"queries fan out only over those"* is therefore **realized, not widened**: the fan-out is local, read-only, and composes *results* without merging repos. Cross-KB movement of knowledge remains exactly two shapes — read composition, or a provenance-carrying inbox event (new invariant 7). Nothing here grants authorization: `kb_id` stays display/join identity (ADR-0041 D1.5/R3) and the registry's `role:` is a LOCAL structural capability declaration about a local path, never the ADR-0036 permission lattice. The per-domain ACL clause is untouched here — that reconciliation stays ADR-0036's. **The AGENTS.md invariant text (5 revised, 7 and 8 added) lands only when ADR-0037 is Accepted** — the enumerated list is the "do not violate without an ADR" set and takes no draft. The prose below is retained verbatim for history.

## Context
Agora must serve individuals and teams, with per-team access separation and a **separate personal
store** per user, without data from one tenant leaking into another. We also want to reuse, not
reinvent, access control where possible.

## Decision
The **tenant boundary is the repository.** Each knowledge repo is an independent git repository with
its own inbox, curator state, and schema. **Team repos** (shared) and **personal repos** (private to
one user) are equal-class.
- A user belongs to ≥0 teams and owns ≥1 personal repo.
- **Write routing** by `target` (`team:<x>` | `personal`); default personal.
- **Read scope** by the repos the caller may read; queries fan out only over those.
- **Roles** `owner > editor > reader` per repo; optional per-domain ACL (`wiki/<domain>`).
- **Access control, two tiers:** (1) delegate to the git host (Forgejo/Gitea) — repos/teams/roles/PRs
  already exist there; (2) **OpenFGA** (Zanzibar-style ReBAC) when per-domain rules are needed.
- **Isolation is structural:** a repo's curator and faces only ever touch that repo's files; separate
  git repos make cross-tenant leakage impossible by construction.

## Consequences
- **+** Hard isolation with minimal custom code (offload ACL to Forgejo to start).
- **+** Personal and team knowledge coexist cleanly; personal harvest can be scope-locked to personal.
- **+** Per-repo curator singleton (ADR-0002) maps naturally onto the tenant boundary.
- **−** Cross-repo synthesis (a query spanning many repos) needs explicit fan-out + per-repo ACL checks;
  no global join. Acceptable — isolation is the priority.
- **−** Many repos = many curators/working copies to schedule; mitigated by sharding and idle triggers.
