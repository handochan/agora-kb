# ADR-0004 — Three pluggable adapter families

**Status:** Accepted · 2026-06-13

## Context
The system must accept many input formats, pull from many agent-memory systems, and let the curator's
reasoning be done by many different agents/models (the user explicitly wants the curator brain to not
be tied to one tool). Hard-coding any of these would make the core brittle and tool-specific.

## Decision
Keep the core small and push all format-specific and cognitive work into **adapters**, in three
families, all driven by `adapters.yaml`:
- **Input adapters** (`ingest/extractors`): bytes|url → markdown for `raw/` (url, pdf, office, …).
- **Read adapters** (`harvester/connectors`): scan an agent-memory source since a cursor → candidates
  (file-based for Claude/Codex/Hermes memory; API-based for Letta/mem0).
- **Write adapters** (`curator/backends`): the curator's *brain* — a **headless CLI agent** invoked
  per batch (`claude -p`, `codex exec`, `qwen -p`, `gemini -p`, `opencode run`, `hermes chat -q`).
  Because all these agents read the repo's `AGENTS.md` schema, they ingest by the same conventions.

Critical split: **orchestration is deterministic code** (lock, queue, dedup, git, state); **only the
cognitive INGEST step is delegated** to the swappable write-adapter. Swapping the brain never threatens
data integrity. Per-task routing is allowed (bulk→local model, hard-merge→stronger model).

## Consequences
- **+** New format / new memory source / new curator brain = one adapter, no core change.
- **+** Fully-OSS curator path (OpenCode/Goose/Hermes + local model) and optional proprietary brains
  coexist behind the same contract.
- **+** Symmetry clarifies the product: input (anything→raw), read (memory→candidate), write
  (candidate/inbox→wiki).
- **−** Adapters vary in capability/quality; the curator must tolerate a weak brain (validation, retry,
  failed/ quarantine). Contracts must be stable and well-tested.
