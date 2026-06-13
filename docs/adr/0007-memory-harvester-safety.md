# ADR-0007 — Memory harvester with provenance / gate / scope safety

**Status:** Accepted · 2026-06-13

## Context
A high-value optional feature: let Agora **pull** from other agents' memory systems (Claude Code,
Codex, Hermes, Letta, mem0) and autonomously accumulate candidate knowledge — making Agora the shared
long-term memory of all the user's/team's agents. But naive harvesting creates three failure modes:
(1) feedback loops (KB → agent memory → harvest → KB …), (2) noise pollution of the curated wiki, and
(3) privacy leakage (personal memory bleeding into team repos).

## Decision
Implement harvesting as **read adapters** (mirror of the write-adapter curator brains), with three
mandatory safety mechanisms:
1. **Provenance + origin marking** breaks loops: inbox items carry `source=harvest:<agent>`; resulting
   note regions are tagged `origin: harvest:<agent>`; connectors **skip any fact whose origin traces
   back to Agora**.
2. **Candidate gate** prevents pollution: harvested items enter as `status=candidate, confidence=low`
   and **must pass the curator's keep/merge/drop review** before promotion to `wiki/`. They are never
   written to the wiki directly.
3. **Scope lock** protects privacy: a personal agent-memory source may feed **only** a personal repo;
   team harvesting requires explicitly-designated team sources. Consent-based, enforced in config
   (`harvest.scope_lock`) and at the core write boundary.

Harvesting is **opt-in** and disabled by default.

## Consequences
- **+** Agora becomes a "memory of memories" — agents get smarter across tools; knowledge captured in
  any agent flows into the shared base.
- **+** Read/write adapter symmetry keeps the architecture uniform and extensible.
- **−** Requires careful provenance plumbing and a robust review gate; a weak gate would let low-quality
  facts harden into accepted wiki content (lint surfaces `confidence:low`/`contested` for review).
- **−** API connectors (Letta/mem0) add external dependencies; kept behind adapters and optional extras.
