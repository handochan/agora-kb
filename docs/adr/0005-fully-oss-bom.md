# ADR-0005 — Fully-OSS bill of materials; proprietary pieces are optional plugins

**Status:** Accepted · 2026-06-13

**AMENDED (append-only) — the Decision's license-hygiene clause is EXTENDED by the addendum at
the end of this file** (*Addendum — license tiers T0–T4: transitive closure, vendoring, artifacts
(landed 2026-09-02)*); the prose below is retained verbatim for history.

## Context
A self-hostable knowledge/memory product must be trustworthy and free of lock-in. The most capable
coding agents (Claude Code, Codex, Gemini) are proprietary clients, and some popular infra has
non-OSI licenses (Redis) or copyleft/AGPL terms (Grafana, pymupdf) that complicate redistribution.

## Decision
The **core must run with zero proprietary dependencies.** Every layer has a permissive-OSS default;
anything proprietary is an **optional plugin behind an adapter**, never required to run.
- Curator brain default: OSS agents (OpenCode/Goose/Hermes) + local open-weight model (Qwen via
  Ollama/vLLM). Proprietary agents are opt-in write-adapters.
- Storage/host: git + Forgejo/Gitea. Auth: Keycloak/Authentik/Ory + OpenFGA/Casbin. MCP: FastMCP.
- **License hygiene:** prefer MIT/Apache/BSD in the **core library**. Avoid AGPL/copyleft in
  redistributable code (use `pdfminer.six`, not AGPL `pymupdf`). Use **Valkey**, not (non-OSI) Redis.
  AGPL tools acceptable only as *separate self-hosted services* (e.g. Grafana for dashboards), never
  linked into the core. For a 100%-OSS editor, use **Logseq/Foam** instead of (non-OSS-core) Obsidian.
- Project license: **Apache-2.0** (permissive, patent grant, adoption-friendly).

## Consequences
- **+** Anyone can self-host the whole stack with no proprietary account or license.
- **+** Clear contribution boundary; proprietary integrations don't contaminate the core's license.
- **−** The default (local-model) curator may be less capable than a top proprietary agent; mitigated
  by per-task routing to an optional stronger backend when the user opts in.
- **−** Ongoing license vigilance on new dependencies (CI check recommended).

---

## Addendum — license tiers T0–T4: transitive closure, vendoring, artifacts (landed 2026-09-02)

**This addendum EXTENDS the Decision above (append-only convention, cf. ADR-0012/0022/0023 addenda).
Nothing above is retracted; the prose is retained verbatim.**

### Context

The Decision states a *posture* ("permissive in the core, AGPL only as a separate service") but not a
*procedure*. Three surveys in 2026-08 — OpenKB, ByteDance's OpenViking, and Graphify — plus the
PageIndex license correction recorded in `docs/STRATEGY-2026-08.md` §5 showed the posture has three
implicit gaps, and **each one had already produced a wrong call**:

1. **Transitive closure was never named.** A package's own license is not the test. `pageindex` is
   permissive and hard-depends on AGPL `pymupdf` — the exact package this ADR names as excluded — so
   `pip install pageindex` violates this ADR while the package metadata reads clean [코드].
2. **A vendoring tier already exists in practice but is written nowhere.** ADR-0021 vendored MIT
   `force-graph` rather than taking a Node/CDN dependency; that decision had no tier to belong to.
3. **An artifact tier was never stated.** Reading a third party's *output files* is not linking, and
   without saying so the project cannot compose on other tools' outputs even when doing so imports
   no code at all.

### Decision — five tiers; every third-party component is assigned exactly one

- **T0 — artifact.** A third party's *output files* (`.md` / JSON / JSONL / SQLite) may be read by an
  ADR-0004 adapter **regardless of the producer's license**. Reading a data file is not a derivative
  work. Treat the bytes as untrusted input (ADR-0023) and stamp provenance (ADR-0011). No code, no
  process, no dependency is admitted by this tier.
- **T1 — hard dependency.** OSI-permissive only, and only when **the entire transitive closure of the
  pinned version** is permissive. The closure — not the package's own metadata — is the test.
- **T2 — vendoring.** Copy a leaf asset into the tree when it is permissive but its dependency path
  would drag in a forbidden transitive edge. Precedent: `force-graph` (ADR-0021), and PageIndex
  Flash, whose own imports are `pypdfium2`/`PyPDF2`/`regex`/`sortedcontainers` while the release
  pulls AGPL `pymupdf` (STRATEGY §5).
- **T3 — optional extra.** Weak copyleft (LGPL/MPL) lives behind an opt-in extra; the core must
  degrade cleanly when it is absent, exactly as the `ingest` extra already does.
- **T4 — separate service, or idea only.** AGPL/GPL/SSPL/BUSL may run only as a separate process
  across a network or CLI boundary (this is the Grafana clause above, generalized), or be read for
  design. Importing, vendoring, or redistributing is forbidden.

### Current assignments

| Component | Tier | Why |
|---|---|---|
| `force-graph` (MIT) | **T2** | ADR-0021, recorded retroactively |
| PageIndex Flash | **T2** | permissive imports; the *release* hard-depends on AGPL `pymupdf` |
| VectifyAI/OpenKB (Apache-2.0) | **T0** | document feed only. **Cohabitation forbidden** — its rollback snapshots `wiki/concepts`/`wiki/entities` by directory and `unlink()`s live files absent from the backup [조사] |
| volcengine/OpenViking (AGPL-3.0) | **T4** | AGPL; and PyPI `openviking-sdk` declares *no* license at all, which is worse than a declared AGPL, not better [조사] |
| Graphify (Apache-2.0 + MIT) | **T0** | licensing would permit T1, but `graph.json` carries no `schema_version` and its top-level key set varies by writer version — no stable contract to depend on [조사] |
| `pymupdf` (AGPL) | **T4-forbidden** | named in the Decision above; reaffirmed |

### Consequences

- **+** The "compose on artifacts, never on runtimes" strategy is legal at zero cost, and the reason
  is written down rather than re-derived per survey.
- **+** A new dependency now has a stated admission test (closure evidence), not a vibe.
- **−** T1 admission is more expensive: the closure must be checked and recorded, and the CI check
  this ADR already recommends is still not implemented. The ledger is tracked as issue
  [#157](https://github.com/handochan/agora-kb/issues/157) (`docs/BOM.md`).
- **−** Tier assignments for OpenViking and Graphify are **[조사]** — a 2026-08-21 snapshot of
  fast-moving repos. Re-verify before any assignment is used to justify code.
