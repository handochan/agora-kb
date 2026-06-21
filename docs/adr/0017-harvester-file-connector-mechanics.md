# ADR-0017 — Harvester file-connector mechanics (segmentation, cursor, loop, scope)

**Status:** Accepted · 2026-06-20

Realizes [ADR-0007](0007-memory-harvester-safety.md) (memory harvester with provenance / gate /
scope safety) for Phase 2. Depends on ADR-0002 (the inbox is the only write path; per-writer
namespacing) and ADR-0011 (the curator's deterministic candidate gate + tier-2 content dedup, which
this leans on as the real anti-pollution control). Companion to the write-adapter ADRs 0015/0016.

## Context
ADR-0007 fixes the *what* — read adapters that pull other agents' memory into gated candidates, with
three mandatory safety mechanisms (provenance/loop, candidate gate, scope lock). It deliberately
leaves the *how* of a **file connector** unspecified: how a `MEMORY.md` is split into facts, how the
per-connector cursor advances, how loop-prevention actually works for a file source, and exactly
where scope is enforced. Pinning those down requires choices that touch load-bearing surfaces
(the DATA-MODEL §6 cursor schema, the inbox write contract, privacy), so they are recorded here
rather than slipped into code. An adversarial design review surfaced two over-claims in the first
draft (a loop-prevention marker that nothing emits onto the return path; a scope check keyed on an
inert field); this ADR records the corrected, honest decisions.

## Decision
1. **Module shape.** `harvester/connectors.py` (the `Connector` Protocol + `FileConnector` +
   `HarvestedFact`) and `harvester/harvester.py` (the orchestrator + the `HarvestCursor` / scope
   gate). The `adapters.yaml` `connectors:` parser lives in `config.py` next to
   `load_backend_registry` (the config seam; it returns lightweight `ConnectorSpec`s so config never
   imports the harvester). CLI: `agora harvest [--repo] [--connector] [--dry-run]` + a `doctor`
   connectors line. No new MCP tool this phase (the CLI proves the flow; `kb_harvest` is deferred).

2. **Segmentation (file connector, non-configurable v1).** One fact per **top-level markdown list
   item** (`-`/`*`/`+`/`N.` at column 0, including its indented children) **and** each non-list
   **prose paragraph** between blank lines. **Headings are context, never facts**, so the leading
   `# Memory Index` / title boilerplate real `MEMORY.md` files carry is skipped automatically.
   *Limitation (accepted for Phase 2):* v1 captures the bullet's own summary line and **does not
   follow** the markdown links many memory bullets point at; the linked sibling prose is not
   harvested. Per-fact size and per-scan fact/file/match counts are capped.

3. **Cursor = exactly the DATA-MODEL §6 schema.** `_kb/harvest/<connector>.json` holds
   `{connector, source_path, last_scan, last_content_sha256, proposed, accepted, rejected}` and
   **nothing more** — the draft's unbounded `seen_fact_keys` set is **dropped** (it broke the §6
   shape and the bounded/rebuildable-state discipline). Incremental scanning uses two legs: a
   whole-source `last_content_sha256` **fast no-op** (an unchanged file emits nothing), and the inbox
   `event_key` for pending-delivery idempotency (next item). The cursor is a derived, git-ignored
   **performance optimization, never an integrity control**: a missing/corrupt cursor loads fresh and
   the scan re-reads from scratch (the candidate gate absorbs any re-flood).

4. **`event_key = fact_key` (ratified overload).** A fact's identity is the canonical
   `content_sha256` of its (normalized) text (DATA-MODEL §11.2), reused as the inbox `event_key`.
   This is sound because each connector writes to its **own writer namespace** `harvest-<agent>` and
   `Inbox._find_by_event_key` keys on `(writer, event_key)`: two *different* agents' identical text
   live in different namespaces and **both** provenance tuples survive; within one connector the same
   fact re-proposed is a genuine re-delivery the caller wants suppressed — exactly `event_key`'s
   contract. `content_sha256` remains independently auto-computed and is the curator's authoritative
   tier-2 dedup key; the write-path `event_key` check is explicitly best-effort.

5. **Loop prevention — the candidate gate is PRIMARY; the marker is secondary/verbatim-only.** The
   curator stamps `origin: harvest:<agent>` only into **wiki-note YAML frontmatter** (`apply.py`), and
   **nothing in the codebase pushes KB content back into an agent's `MEMORY.md`**. So a regex scanning
   agent prose for that token catches at most a verbatim frontmatter copy, never the realistic
   *reworded* round-trip. Therefore the ADR-0007 §1 loop guarantee in this phase is the **candidate
   gate**: every harvested fact enters `kind=candidate` + `confidence=low` and must re-pass the
   curator's keep/merge/drop review every cycle (`plan.py` `GATE_ALLOWED_OPS = {MERGE_INTO_THEME,
   MARK_CONTESTED, DROP}` — a gated candidate may never *originate* a theme). The connector also
   strips agora structural sentinels (`<!-- agora:body:… -->`) from untrusted text (defense-in-depth
   for the planning prompt). **The semantic (reworded) KB→memory→KB loop is NOT eliminated** — it is a
   stated residual risk bounded by the gate + content dedup; the loop is **not claimed closed**. A
   real origin-trace carrier (e.g. `kb_query` emitting a preserved provenance footer) is deferred.

6. **Scope lock — a hard, fail-closed pre-write gate keyed on repo kind.** Enforcement requires
   `connector.scope == repo.harvest.scope_lock == repo.kind` (two layers: config policy + identity
   backstop). The check keys off the **concrete `RepoConfig.kind`** of the repo being written, not the
   inbox `target` field (which is *inert* in Phase 1 — `Inbox.write` records it but routes no repo).
   It **fails closed**: an absent/unreadable `repo.yaml` or a missing/unknown `kind` is treated as
   `team`, so a personal source can never silently feed a repo whose personal identity is not
   explicitly declared. `harvest.enabled` defaults **false** (opt-in). This is a **single-process
   harvester pre-write gate, NOT the ADR-0007 "core write boundary"**: a caller constructing a
   harvester/`Inbox` against the wrong layout bypasses it. The true core-boundary + multi-tenant
   routing (and the deferred Letta/mem0 API connectors that would call core directly) are **deferred
   to Phase 4**.

7. **Cursor counter ownership (IMPLEMENTED).** ADR-0011's flow contracts the **curator** to update
   the §6 `accepted`/`rejected` counters from plan dispositions at finalize; this is now wired in
   `worker.py` (`compute_harvest_cursor_deltas` + `_apply_harvest_cursor_deltas`). The **harvester**
   still owns `connector` / `source_path` / `last_scan` / `last_content_sha256` / `proposed` and the
   **curator** owns `accepted` / `rejected`; each writer load-then-saves via the atomic `CursorStore`
   so neither clobbers the other. The chosen semantics:
   - **Happy-path-only, mirroring `_bump_counters`.** The increment is applied in the SAME happy-path
     finalize block as `state.counters` and is **never replayed in `_finalize_recovered`** (the
     recovery path). That placement is exactly what makes it exactly-once (or under-count +
     rebuildable on a rare crash) with NO `is_published` guard — the cursor is a derived, git-ignored,
     **rebuildable** value (DATA-MODEL §6), so it is **best-effort, not transactional with the CAS**;
     the additive write lands in `_kb/` (OUTSIDE the curated git tree), so the ADR-0008 integrity
     boundary is byte-for-byte unchanged.
   - **Per-harvested-event granularity.** A disposition over a candidate whose provenance carries K
     harvested tuples from connector C contributes K to C (matching how `proposed` counts one per
     written fact). A **mixed-provenance** candidate counts ONLY its `harvest:<agent>` tuples, each
     attributed to its own connector; local-capture tuples are ignored. So `proposed` and
     `accepted + rejected` reconcile at the same granularity.
   - **accepted / rejected / NOOP.** `accepted` += op ∈ {`MERGE_INTO_THEME`, `MARK_CONTESTED`}
     (kept/corroborated/contested); `rejected` += `DROP` (discarded as noise); **`NOOP` = SKIP** (an
     exact duplicate already represented — neither newly accepted nor rejected). `NOOP`, `CREATE_THEME`
     and `APPEND_DAILY` are all OUTSIDE the §4.1-check-10 `GATE_ALLOWED_OPS` set ({`MERGE_INTO_THEME`,
     `MARK_CONTESTED`, `DROP`}) and so can never occur for a genuinely gated candidate — they are
     rejected at validation. The NOOP-skip and the CREATE_THEME/APPEND_DAILY accepted fall-through are
     therefore purely DEFENSIVE, guarding only a non-gated candidate that happens to carry harvested
     provenance; they should never fire in practice.
   - **Configured-connectors-only.** A harvested tuple's `source = harvest:<agent>` is mapped to its
     connector NAME via the configured `adapters.yaml` connector list (`{agent → name}`); a tuple
     whose connector has since been removed from config is **skipped** (no stray cursor is created).
     No `adapters.yaml` / no harvested provenance ⇒ no cursor writes.

## Consequences
- **+** ADR-0007 is realized end-to-end for the file connector: opt-in `agora harvest` pulls an
  agent's `MEMORY.md` into gated candidates that flow safely through the existing curator gate, with
  per-connector cursors and a `--dry-run` noise preview.
- **+** Honest safety story: the candidate gate (not a marker heuristic) is the load-bearing loop
  break, scope is fail-closed on real repo identity, and untrusted memory is path-safe + size-capped
  + sentinel-neutralized.
- **+** The `Connector` Protocol is the clean Phase-4 seam for Letta/mem0 API connectors.
- **−** The reworded round-trip loop and the team-scope/multi-tenant boundary are explicitly
  *deferred*, not solved — documented residual risks rather than hidden ones.
- **+** `accepted`/`rejected` cursor counters are now populated by the curator at finalize (§7),
  closing the contracted-but-deferred gap; they are best-effort + rebuildable, never an integrity
  control.
- **−** v1 harvests one-line bullet summaries only (links not followed), so pointer-heavy memory
  files yield low-value candidates the curator gate must reliably DROP — validate noise behavior with
  `--dry-run` on a real `MEMORY.md` before relying on it.

## Live verification (2026-06-20, throwaway repo, real `MEMORY.md` shape)
A personal repo with `harvest.enabled: true` + a `file:demo-agent` connector: `agora harvest
--dry-run` segmented a heading + 3 bullets (one with a nested child) + 1 prose paragraph into 4
facts; the real run wrote 4 inbox items all `kind=candidate` / `confidence=low` /
`source=harvest:demo-agent` / `writer=harvest-demo-agent`; a second run was an unchanged no-op; the
§6 cursor advanced (`proposed=4`, `accepted`/`rejected`=0). A **team** repo + a **personal** connector
was **refused** (`ScopeViolation`, nothing written); a disabled repo was a clean no-op.
