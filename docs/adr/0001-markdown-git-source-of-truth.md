# ADR-0001 — Markdown + git is the source of truth

**Status:** Accepted · 2026-06-13

## Context
Knowledge can live in a vector DB, a graph DB, a relational DB, or plain files. We want it to be
human- and agent-readable, diffable, durable, portable, and free of infra/operational weight. We also
want retrieval that an LLM agent is already good at (reading a structured corpus) rather than opaque
similarity search.

## Decision
Canonical knowledge is **interlinked markdown files in a git repository**. There is **no database that
holds canonical knowledge.** Any SQLite/Postgres used for indexes, metadata, or caches must be fully
**rebuildable from the markdown**. Retrieval is *navigation* (index → `[[links]]` → grep), the
Karpathy "LLM wiki" pattern, not vector RAG.

## Consequences
- **+** Inspectable, diffable, version-controlled, portable; works with Obsidian/Logseq/VS Code; agents
  read it like a codebase. No vector infra to run. Zero data lock-in.
- **+** git provides history, audit, rollback, and a distribution mechanism for teams.
- **−** No semantic similarity out of the box; relies on good indexes/tags/links (the curator's job).
  If navigation proves insufficient at scale, add a *thin* semantic layer over the markdown — never
  make it canonical (deferred; see ROADMAP).
- Forces discipline: schema (frontmatter, unique basenames, taxonomy) and an active curator to keep
  the corpus navigable.
