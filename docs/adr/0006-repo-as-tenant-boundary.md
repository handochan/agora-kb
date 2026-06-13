# ADR-0006 — Repo = tenant boundary (team & personal repos)

**Status:** Accepted · 2026-06-13

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
