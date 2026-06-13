# ADR-0003 — One core API, many faces

**Status:** Accepted · 2026-06-13

## Context
Agents reach the KB over MCP; people reach it over a web app (browse, search, **upload**); operators
need a dashboard. A naive design would let each surface read/write storage directly, duplicating
tenancy and access-control logic and risking pipeline bypass (e.g. a web upload that skips the inbox
and writes the wiki directly, creating races).

## Decision
Define a single internal **core API** — `write(target, item) → inbox`, `read(scope, query) → wiki`,
`meta(scope) → status` — and make every surface a **face** over it:
- **MCP server** (agents), **Web app** (people + uploads), **Dashboard** (read-only status).
- **All writes go through `write`→inbox; all reads through `read`→wiki.** No face touches storage
  directly. Tenancy and access control are enforced **once**, in the core.
- Uploads are not special: an upload stores the original in `raw/` and then calls `write` like any
  other capture (reusing the inbox path).

## Consequences
- **+** Concurrency safety, provenance, and access control are properties of the core; every face
  inherits them. New faces (CLI, chat bot, IDE plugin) are cheap and safe.
- **+** Clear test surface: the core API and adapter contracts are what we verify.
- **−** A thin indirection cost; faces can't take storage shortcuts (intended).
- Enforced by the dependency rule: faces depend on `core`; `core` depends on no face.
