# Architecture Decision Records

Each ADR captures one load-bearing decision: context, the decision, and consequences. Change a
decision only by adding a new ADR that supersedes the old one (don't silently rewrite history).

Format: lightweight [MADR](https://adr.github.io/madr/)-style.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-markdown-git-source-of-truth.md) | Markdown + git is the source of truth (no canonical DB) | Accepted |
| [0002](0002-cqrs-single-writer-curator.md) | CQRS + single-writer curator for concurrency | Accepted |
| [0003](0003-one-core-many-faces.md) | One core API, many faces (MCP / web / dashboard) | Accepted |
| [0004](0004-pluggable-adapters.md) | Three pluggable adapter families (input / read / write) | Accepted |
| [0005](0005-fully-oss-bom.md) | Fully-OSS BOM; proprietary pieces are optional plugins | Accepted |
| [0006](0006-repo-as-tenant-boundary.md) | Repo = tenant boundary (team & personal repos) | Accepted |
| [0007](0007-memory-harvester-safety.md) | Memory harvester with provenance/gate/scope safety | Accepted |
| [0008](0008-transactional-sandboxed-curation.md) | Transactional, sandboxed curator runs | Accepted |
| [0009](0009-deterministic-query-contract.md) | Deterministic query contract; synthesis is optional | Accepted |
| [0010](0010-kb-wiki-schema.md) | KB wiki schema v1 (emitted AGENTS.md/SCHEMA.md) | Accepted · amended by 0014 |
| [0011](0011-curator-ingest-contract.md) | Curator INGEST contract (plan-apply-author) | Accepted · §7.1 routing superseded by 0015 |
| [0012](0012-deterministic-query-ranking.md) | Deterministic query ranking for core.wiki | Accepted |
| [0013](0013-curator-sandbox-mechanism.md) | Curator sandbox mechanism (macOS Seatbelt + cross-platform) | Accepted |
| [0014](0014-okf-obsidian-interoperability.md) | OKF + Obsidian interoperability (strict producer / tolerant consumer) | Accepted |
| [0015](0015-per-task-brain-routing.md) | Per-task curator brain routing (`plan`/`author` via `adapters.yaml`) | Accepted |
| [0016](0016-cli-agent-brains-as-text-generators.md) | Headless CLI agents as curator brains (text-generator shim) | Accepted |
| [0017](0017-harvester-file-connector-mechanics.md) | Harvester file-connector mechanics (segmentation / cursor / loop / scope) | Accepted · realizes 0007 · §2 superseded by 0018 |
| [0018](0018-harvester-link-following.md) | Harvester file-connector link-following (opt-in; follow `[Title](sibling.md)`) | Accepted · supersedes 0017 §2 |
| [0019](0019-web-face-stack.md) | Web face stack: API-first FastAPI + server-rendered HTMX (SPA-island escape hatch) | Accepted · realizes 0003 |
| [0020](0020-web-upload-write-path.md) | Web upload write-path: extract→inbox now, `raw/` binary staging deferred | Accepted · amends 0003 |
| [0021](0021-knowledge-graph-viz.md) | Interactive knowledge-graph viz (vendored MIT force-graph; Neo4j rejected) | Accepted · realizes 0019 §7 |
| [0022](0022-curator-taxonomy-governance.md) | Governed taxonomy: worker-applied `CREATE_DOMAIN` (no-loss catch-all floor) + repo-kind-aware `taxonomy_policy` default + per-domain tuning surfaces; integrity gate stays domain-agnostic | Accepted · governs 0010 D6 · layers on 0011/0015 (#23, #24) |
| [0023](0023-context-harvester-connectors.md) | Context-harvester connectors (`session:`/`dir:`/`git:`/`mail:`/`chat:`/`calendar:` via 0004 Connector Protocol), connector-boundary redaction before append-only write, fail-closed personal scope (team → Phase 4) | Accepted · extends 0007/0017 · OSS-path per 0005 (#25, #28) |
| [0024](0024-bulk-processing-horizontal-curator-scale.md) | Bulk processing: shard-by-repo only (never a 2nd writer within a repo), repo→owner fencing lease for cross-host, bounded-batch claim cap | Accepted · scales 0002 (#27) |
| [0025](0025-web-config-multiupload-extensions.md) | Web operator `web:` settings (per-repo in `build_app`, tenant-safe), multi-upload (N × 0020 extract→inbox + batch receipt), broadened extractor extensions; graph render itself = 0021 | Accepted · additive over 0020 · references 0021 (#29) |
| [0027](0027-gold-context-packs.md) | Gold context packs: derived, deterministic, token-budgeted `_kb/gold/` tier assembled from the wiki + the single normative outbound sentinel/loop-break contract (§8) | Accepted · layers on 0009/0012 · extends 0017 · co-ratifies 0026 sentinel |
| [0036](0036-authn-authz.md) | AuthN/AuthZ: Phase-4 Forgejo-delegated identity + repo-permission mirror (repo = the only security boundary; domain ACL demoted to convenience filter per #55), Phase-5 OAuth 2.1+PKCE / OpenFGA behind evidence triggers | Proposed (#69) · materializes 0006 (demotes its per-domain ACL to a convenience filter, reconciled on Accept) · Phase-4 gate |
<!-- 0026 reserved: outbound skill write-back (opt-in, dry-run/staging only) — deferred, not yet authored (#25). -->
<!-- 0028 reserved: LLM `DISTILL` curator act (→ wiki/digests/, one new closed-vocab op) — evidence-triggered per 0027, not yet authored. -->
<!-- 0029 reserved: connector ecosystem — CWP exec wire, registration UX, enablement re-consent / injection opt-in (extending 0023) — evidence-triggered, not yet authored. -->
<!-- 0030 reserved: federation / team-audience pack composition — Phase-4-coupled, not yet authored. -->
<!-- 0031 reserved: retention — hard prerequisite for mail:/chat: connectors, not yet authored. Candidate scope: bronze crypto-shred (per-subject envelope keys) + tamper-evident erasure ledger — see docs/notes/retrieval-vs-vectordb.md §1/§4. -->
<!-- 0032 reserved: semantic embedding tier as a strictly-additive tail — derived, git-ignored, rebuildable; opt-in, default off; keeps every lexically-answerable query byte-identical; SUPERSEDES 0012 §11 "no embeddings". Exploratory (docs/notes/retrieval-vs-vectordb.md), evidence-triggered (#28), not yet authored. -->
<!-- 0033 reserved: pending read tier / read-your-own-write overlay — model-free additive "pending" band over inbox∪processing on the read path only (no write-time upsert). Exploratory (docs/notes/retrieval-vs-vectordb.md), not yet authored. -->
<!-- 0034 reserved: bulk map-parallel curation — MAP-AUTHOR (PASS-2) fan-out now, MAP-PLAN sharding gated by 0024 §3 saturation; extends 0024. Exploratory (docs/notes/retrieval-vs-vectordb.md), not yet authored (#27). -->
<!-- 0035 reserved: hybrid tri-signal fusion {sparse, dense, curated-structural} + metadata-filter constraint — layers on 0032; #28-triggered (ANN/SQLite dead weight below scale). Exploratory (docs/notes/retrieval-vs-vectordb.md), not yet authored (#28). -->
<!-- 0036 numbered past the 0028–0035 reservations above (assigned by #69); independent of the #55 decision-1 allocations, which take later numbers when authored. -->
