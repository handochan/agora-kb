# ADR-0015 — Per-task curator brain routing

**Status:** Accepted · 2026-06-20

Depends on ADR-0004 (pluggable adapters — the swappable write-adapter brain) and ADR-0008/ADR-0011
(the deterministic transaction + INGEST contract that grade a brain's output); extends DATA-MODEL §8
(`adapters.yaml`). **Supersedes ADR-0011 §7.1** (the PRE-PLAN escalation routing heuristic, not
adopted) with this static per-act `routing: {plan, author}` design.

## Context
ADR-0004 makes the curator's brain a swappable write adapter and explicitly allows **per-task
routing** ("bulk → local model, hard-merge → stronger model"). Phase 2 (ROADMAP item 1) calls for a
registry of ≥3 brains plus that routing. The registry already loads named `backends` + a
`default_backend` from `adapters.yaml`, but every run uses one brain for the whole run; there is no
way to run a cheap local model for one part of the work and a stronger model for another.

The constraint that shapes the design: the curator delegates to a brain at **exactly two points** —
`worker.Backend` is a Protocol with two methods, `plan(bundle_dir)` (PASS-1, called once to produce
the whole batch's `plan.json`) and `author(worktree, needs_prose, context)` (PASS-2, prose). PASS-1
plans the *entire* claimed batch in one call, so there is **no per-candidate seam** at which to route
a single MERGE differently from a single CREATE without restructuring PASS-1 into per-candidate
invocations. Any routing we ship now must fit those two delegation points.

## Decision
Add an **optional, per-act routing table** to `adapters.yaml`. The routable-act key-space is the
**closed set `{plan, author}`** — co-extensive with the two `worker.Backend` methods, so the config
cannot promise sub-act routing the worker can't deliver.

```yaml
backends:
  qwen:   { argv: [agora-ollama-brain], cwd: "{worktree}", prompt: stdin, network: loopback }
  claude: { argv: [claude, -p],          cwd: "{worktree}", prompt: stdin, network: none }
  hermes: { argv: [hermes, chat],        cwd: "{worktree}", prompt: stdin, network: none }
default_backend: qwen
routing:                 # OPTIONAL. Omit the whole block → behaves EXACTLY as before this ADR.
  plan: claude           # PASS-1 backend.plan() runs on 'claude'
  author: qwen           # PASS-2 backend.author() runs on 'qwen'
```

1. **`routing` is a sibling of `backends`/`default_backend`**, a flat `act → backend-name` map — NOT
   a `BackendSpec` field, so `BackendSpec` stays frozen/`extra='forbid'` and existing specs
   round-trip unchanged. Each key is independently optional; an omitted key (or an absent/empty
   block) falls back to the repo's **default brain** for that act (precedence below). Each act
   resolves to a full `BackendSpec`, so `plan` and `author` may carry **different `network`
   postures**.

2. **`BackendRegistry.resolve(act, default=…) → BackendSpec`** is the single dispatch point
   (`routing[act]` → the caller-supplied `default` → the registry's own `default_backend`);
   `routed_backends(default=…)` returns the resolved `{act: name}` table for observability.
   Validation lives in the **constructor**, so every construction path — including a direct test
   build — is guarded: an unknown act key, or a value naming an undefined backend, is a **hard config
   error** (mirroring `BackendSpec`'s `extra='forbid'`), never a silent fallback. The unknown-act
   message names the routable set and states that per-op/per-tier routing is unsupported in v1, so a
   misguided `routing: {merge: ...}` gets a forward-pointer.

3. **`RoutedBackend` implements the existing `worker.Backend` Protocol** by delegation: `plan()` →
   the `plan`-routed backend, `author()` → the `author`-routed backend. Each delegate is an ordinary
   `SubprocessBackend` with its own spec and its own injected ADR-0013 isolation. **`worker.run` is
   unchanged** — it calls `plan`/`author` exactly as for a single backend and never learns it is
   routing. Routing chooses *which* brain runs an act; it never changes *how* that act's output is
   validated, so the ADR-0011 §4 gate and ADR-0013 confinement are reused per delegate. Routing is
   therefore **integrity-neutral**.

4. **One shared builder, `build_routed_backend(registry, …)`**, used by both the CLI and the MCP
   face so they cannot drift. It returns a plain `SubprocessBackend` when both acts resolve to the
   same spec (the no-routing / single-brain path — byte-for-byte the prior object), else a
   `RoutedBackend`. It fails closed (returns `None` with a clear message) when a `network: none` act
   has no usable OS sandbox and `allow_reduced_isolation` is `False`.

5. **`agora curate --backend NAME`** pins BOTH acts to one named brain, bypassing `routing` — a
   deterministic operator escape hatch that never edits `adapters.yaml`. An unknown NAME is a clean
   non-zero exit, not a traceback. **`agora doctor`** prints the routing table with each act's network
   posture (reporting only; never affects the health verdict).

6. **Default-brain precedence.** For each act the brain is: the `--backend NAME` override (both acts)
   if given; else the `routing[act]` entry if present; else the **repo's default brain**
   (`repo.yaml` `curator.backend`), which the faces thread through `build_routed_backend`; else the
   registry's `adapters.yaml` `default_backend`. This keeps `repo.yaml curator.backend` authoritative
   for an unrouted run **exactly as before this ADR** — `routing` and `--backend` only add overrides
   on top, so a repo with no `routing:` block is unchanged.

## Consequences
- **+** Config-only per-act brain selection (ADR-0004's intent): run the cheap local model for the
  bulk PASS-1 plan and a stronger model for PASS-2 prose, or vice versa — no code change.
- **+** Zero behavioral change for any repo that does not opt in: no `routing:` block ⇒ the builder
  returns today's exact single-backend object and `worker.run` is untouched.
- **+** Integrity is unaffected: a routed brain is still fully sandboxed and fully validated; routing
  adds no path around the deterministic gate.
- **−** Operability foot-guns the operator must understand (surfaced by `agora doctor`): PASS-2
  `author` runs **once per region**, so routing `author` to a metered API multiplies per-region cost
  (the intended default keeps `author` on the cheap/local brain); and routing an act to a
  `network: none` brain trips the OS sandbox on a host where the default loopback brain never did
  (fail-closed at build time, pre-checkable via `doctor`).
- **−** v1 cannot route per-op or per-tier (see below).

## Future work (reserved, not implemented)
Designed so these are additive, never a breaking change:
- **Per-op routing** (route a MERGE vs a CREATE differently) requires splitting PASS-1 into
  per-candidate `plan()` calls. When that lands, **append members to `_ROUTABLE_ACTS`** — existing
  `{plan, author}` configs keep working because `resolve(act)` still returns a `BackendSpec`.
- **Capability tiers / richer values**: a routing *value* may later widen from a bare backend name
  (string) to a small mapping (e.g. an escalation predicate) by type-switching in the parser; v1
  string values remain valid. The `RoutedBackend`/worker seam never moves.
