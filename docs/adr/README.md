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
| [0011](0011-curator-ingest-contract.md) | Curator INGEST contract (plan-apply-author) | Accepted |
| [0012](0012-deterministic-query-ranking.md) | Deterministic query ranking for core.wiki | Accepted |
| [0013](0013-curator-sandbox-mechanism.md) | Curator sandbox mechanism (macOS Seatbelt + cross-platform) | Accepted |
| [0014](0014-okf-obsidian-interoperability.md) | OKF + Obsidian interoperability (strict producer / tolerant consumer) | Accepted |
| [0015](0015-per-task-brain-routing.md) | Per-task curator brain routing (`plan`/`author` via `adapters.yaml`) | Accepted |
| [0016](0016-cli-agent-brains-as-text-generators.md) | Headless CLI agents as curator brains (text-generator shim) | Accepted |
