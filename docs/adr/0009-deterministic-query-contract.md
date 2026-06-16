# ADR-0009 — Deterministic query contract; synthesis is optional

**Status:** Accepted · 2026-06-13

## Context
The core query path is described both as navigation/grep and as natural-language question answering.
Without a result contract, citation correctness and Phase 1 behavior cannot be tested independently
of a model.

## Decision
`core.read` is deterministic retrieval. It returns a structured `QueryResult` containing ordered
`SearchHit` values with repo, path, heading/line anchor, excerpt, match reason, and score. It returns
an explicit `not_found` result when no supported evidence exists.

For Phase 1, `kb_query` renders those hits and citations directly; it does not invent or synthesize
claims. A later optional answer-synthesis adapter may produce prose only from the returned hits and
must attach citations to every material claim. The deterministic result remains available to callers
and is the stable core API.

## Consequences
- **+** Retrieval and citation behavior are testable without a model.
- **+** The MVP remains local, predictable, and honest when evidence is absent.
- **+** Better synthesis can be added without changing storage or retrieval contracts.
- **−** Phase 1 responses are evidence-oriented rather than polished conversational answers.
