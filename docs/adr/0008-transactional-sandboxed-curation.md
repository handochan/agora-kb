# ADR-0008 — Transactional, sandboxed curator runs

**Status:** Accepted · 2026-06-13

## Context
Inbox events are immutable, while consolidation changes shared wiki files and invokes an external
agent over untrusted captured content. Letting that agent edit the live working copy creates two
failures: a crash can leave a partially-edited wiki, and prompt injection can modify operational
files or escape the tenant repo.

## Decision
Each curator run is a transaction owned by deterministic orchestration:

1. Under the per-repo lock, atomically move the selected immutable events from `_kb/inbox/` to
   `_kb/processing/<run-id>/` and write a run manifest.
2. Create a temporary git worktree at the current curated revision. The backend runs only there,
   inside an OS sandbox with no network by default and with the repo as its only writable mount.
   The concrete sandbox mechanism is specified and implemented in ADR-0013 (macOS `sandbox-exec`/SBPL
   default, Linux `bwrap`, fail-closed restricted-subprocess fallback; self-tested by `agora doctor`).
3. Pass prompts and item paths as structured process arguments/input, never by shell interpolation.
4. After the backend exits, deterministic validation rejects symlinks/path escapes and changes outside
   the allowlist (`wiki/`, `index.md`, `log.md`, and schema-approved content paths).
5. Commit the validated content change in the temporary worktree, then compare-and-swap the curated
   branch ref from the manifest's base commit to the new commit. Readers resolve a published commit,
   not a backend's mutable worktree. Only after publication succeeds are events moved to
   `_kb/processed/<date>/`; rejected or terminally failed events move to `_kb/failed/` with a separate
   error record.
6. On restart, a processing run with no published commit is recoverable by returning its unchanged
   events to the inbox. A published run is finalized without invoking the backend again.

The backend never receives git credentials and never edits `_kb/`, git configuration, hooks, or the
live operational spool. `_kb/` is git-ignored operational state; retained events and run manifests
provide the audit trail, while git records canonical knowledge changes. Review mode publishes the
validated commit to a branch/PR instead of advancing the curated branch directly.

**Read-after-publish (the CAS base is the curated ref, not the owner HEAD).** The compare-and-swap in
step 5 moves only the curated branch ref; the repo-owner's main working copy stays parked at the run's
base commit. After publication the orchestrator fast-forwards that working copy to the new tip so the
on-disk read path (`core.Wiki` / `kb_query`) resolves the published content. That fast-forward is
**best-effort**: it is `--ff-only` and never clobbers a dirty/diverged owner tree, so a failure leaves
the publish durable in git but the working copy stale. Because the failure leaves HEAD behind the ref,
the **authoritative base for the next run's CAS is the curated branch ref** (`branch_commit`), never the
owner's working-copy HEAD — reading HEAD would plan against missing content and propose a CAS whose
`expected` can never match the moved ref, a durable livelock. The next run also re-attempts the sync
best-effort at start (reconciling a tree that has since been cleaned), and a still-stuck working copy is
surfaced as an observable run-report signal rather than retried silently.

## Consequences
- **+** The visible wiki changes atomically at a commit boundary; partial backend edits are discarded.
- **+** Immutable events and provenance survive crashes without mutating frontmatter status.
- **+** Backend capability is constrained independently of prompt quality.
- **−** Temporary worktrees and sandbox setup add disk and implementation overhead.
- **−** Platform-specific sandbox adapters are required; unsupported platforms must use a restricted
  subprocess mode and emit a clear reduced-isolation warning.
