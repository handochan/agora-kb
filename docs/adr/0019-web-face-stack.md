# ADR-0019 — Web face stack: API-first FastAPI + server-rendered HTMX

**Status:** Accepted · 2026-06-21

Realizes the **web app** of ADR-0003 (one core, many faces — the web face is a thin face over the
core API, never a storage shortcut) for **ROADMAP Phase 3** / DESIGN §5.2. Binds the license
constraint of ADR-0005 (fully-OSS BOM; no AGPL/copyleft in the redistributable default path) and
renders the read shape of ADR-0009 / ADR-0012 (deterministic `QueryResult`/`SearchHit`). Designed so
the Phase 4–5 demands of ADR-0006 (repo = tenant boundary; scope/ACL) and the ROADMAP Phase-5
PR-review mode are absorbed **without a frontend rewrite**.

## Context
Phase 3 is the **web face**: browse, search, upload (url/pdf/office → `raw/` + inbox), a read-only
dashboard (KB health + curator + harvester status), and Prometheus metrics. The backend half is
already done and Phase 3 plugs into it verbatim:
- **`AgoraHandlers`** (`faces/mcp_server.py`) is a transport-free layer whose methods return plain
  JSON-serializable dicts — `query()` → `{query,status,hits[]}`, `remember()` →
  `{id,queued,inbox_depth}`, `status()` → `{inbox_depth, last_consolidation, processed_today,
  last_commit, failed, counters{…}}`. The MCP face already consumes it; the web face is a second
  client of the same surface. No new core function is required.
- **Dependencies are already pinned** in `pyproject.toml`: the `web` extra (`fastapi`, `uvicorn`),
  the `ingest` extra (`trafilatura`, `pdfminer.six`, `markitdown`), and the `metrics` extra
  (`prometheus-client`) — all permissive (MIT/Apache/BSD).
- The pure deterministic **`lint()`** (`schema/lint.py`, the ADR-0010 L1 ruleset) is the same code
  path the dashboard reuses for KB-health signals — curator and dashboard never disagree.

The single open choice is therefore the **frontend rendering model**, and it must serve not just
Phase 3 but absorb Phases 4–5 (token→Forgejo→OpenFGA auth, multi-repo scope switching, per-domain
ACL, PR-review diff workflow, coexistence with a Quartz static read view and a Grafana metrics UI)
**without a rewrite**. The binding project constraints: a single small maintainer; deliberate
dependency-lightness (the runtime core is three deps — fastmcp, pyyaml, pydantic); OSS purity with
ongoing license vigilance (ADR-0005); DESIGN §5.2 names this face verbatim as *"a FastAPI +
lightweight-frontend face for humans"*; the Quartz read view (DESIGN §8 BOM; ROADMAP Phase 4) is a
**separate** static surface, not this face's job.

### The crux (repo-grounded, falsifiable)
The hardest forward demands — multi-repo fan-out, token→repos+roles resolution, per-domain scope
threading — are **core work, identical for any frontend**: `Wiki.query(question)` has no scope axis
(`core/wiki.py`) and `auth/` is a docstring-only stub today. So the frontend earns **no differential
credit** on the Phase 4–5 spine. The real axis of choice is therefore **UX ceiling × maintenance ×
reversibility**, and there the project's hard constraints (solo maintainer, dependency-lightness,
OSS purity, FastAPI already chosen) point at server-rendered hypermedia.

### Corroborating evaluation
A judge panel scored four candidates — plain Jinja2+HTMX, SPA (React/Vue+Vite over a JSON API),
**API-first + progressive HTMX**, and a Quartz-static / minimal-FastAPI hybrid — against
forward-looking Phase 3–5 demands, with weights that **deliberately embed this project's stated
priorities** (future-readiness 0.30 + UX 0.25 over dev-cost 0.05; OSS/self-host 0.15, reversibility
0.15, maintenance 0.10) — the reader should audit the weighting as the place the priorities live.
Result: API-first HTMX and SPA **tied at the top**; the panel's only *unconditional* recommendation
went to API-first HTMX, while SPA drew three *conditional* votes whose common failure mode was
**solo-maintainer JS-ecosystem rot** (a Node/Vite/npm supply chain plus an OpenAPI-sync discipline
grafted onto a Python-only, dependency-light project, paid up-front against still-speculative Phase
4–5 demands). The Quartz-hybrid scored lower once fully judged: a static build **cannot per-request
ACL-filter content**, which is the spine of Phases 4–5. The tie is corroborating; the falsifiable
crux above is load-bearing.

### Why the famous-app landscape does not transfer (surveyed 2026-06)
A survey of well-known AI apps clusters into three categories, none of which is Agora's web face:
**(a) desktop chat / agent clients** → Electron/Tauri + React (Claude Desktop, Codex Desktop, AionUi,
LM Studio, Cursor; Jan is migrating to Tauri); **(b) self-hostable chat servers** → backend + SPA/SSR
(Open WebUI = FastAPI + SvelteKit, LibreChat = Node + React, Lobe Chat = Next.js, Multica = Go +
Next.js); **(c) CLI / IDE tools** → Rust / TypeScript (Codex CLI, VS Code extensions). The durable
takeaway does not depend on the volatile roster: **every one of these is either a desktop client or a
chat product whose UI _is_ the product**, with a streaming-token conversation surface (HTMX's one
genuine weakness) and a company / large-contributor base to carry a JS stack. Agora's web face is
neither — it is a thin face over a memory core whose Phase-3 interaction shape (evidence lists, not
synthesized prose, ADR-0009; fire-and-forget `{id,queued,inbox_depth}` uploads; polling status
panels) is precisely hypermedia's sweet spot. So "famous apps use SPA" does not transfer; it is a
category fact, not a stack verdict. (The closest precedent, Open WebUI, even pairs a **FastAPI**
backend with a non-React frontend — confirming there is no "Python backend ⇒ React" law. Note: Open
WebUI relicensed from BSD-3 to a branding-restricted, non-OSI "Open WebUI License" in 2025 — cited
here for its architecture, **not** as a license model.)

## Decision
The Phase-3 web face is **API-first FastAPI + server-rendered HTMX/Jinja2**, with a named escape hatch
to a per-route SPA.

1. **The JSON API is the first-class, durable contract.** The web face promotes the existing
   `AgoraHandlers` surface to documented, versioned, tested HTTP JSON endpoints — the *same* contract
   the MCP face consumes and the natural surface for a future Streamable HTTP / mobile / third-party
   consumer. The API, not the HTML, is the asset we commit to keeping stable.

2. **Server-rendered HTMX/Jinja2 is the Phase-3 UI.** FastAPI renders Jinja2 pages; HTMX (vendored,
   no Node, no build step) adds partial updates (`hx-get`/`hx-post` fragment swaps) for search,
   upload receipts, and polling dashboard panels. Server-side state; no client state machine. This
   matches Agora's actual interaction shape and keeps the face Python-only.

3. **Scope/ACL is threaded server-side.** The browser only ever receives already-authorized HTML;
   there is no rich client cache that can request or retain out-of-scope content across the hard
   tenant boundary (ADR-0006 / DESIGN §7). When auth lands (Phase 4), the caller's identity is
   resolved in the FastAPI layer and threaded into every core call; the UI renders strictly the
   authorized subset.

4. **OSS-pure default path, no build step.** FastAPI/Starlette/uvicorn (MIT/BSD), Jinja2 (BSD-3),
   HTMX (0BSD/BSD) — zero AGPL/copyleft, one lean Python container (ADR-0005; ROADMAP Phase 5
   "Packaging"). The one exception is charting (§5 below).

5. **Charts are the single bolt-on.** Dashboard timeseries (note growth, queue trends) use one
   permissive JS charting lib (e.g. uPlot or Chart.js, MIT) or server-rendered SVG — HTMX renders no
   charts. **Heavy operational graphing is deferred to Grafana** over the Prometheus export
   (observability split, DESIGN §5.3). Grafana is **AGPL**, so it stays an **operator-supplied
   external sidecar**, never bundled in the redistributable container (DESIGN §8 purity note /
   ADR-0005) — which is exactly why deferring to it does not violate §4's "zero AGPL" default path.
   The in-app dashboard shows content/health, not a Grafana clone.

6. **The discipline this ADR commits to (load-bearing).** Core `read`/`write`/`meta` MUST stay
   exposed as documented, versioned, **independently-testable JSON** — HTML fragments are a *separate*
   presentation layer over it, never a replacement for it. The single failure that would void this
   ADR's central benefit is letting the API rot into HTMX-only HTML-fragment endpoints; the stack does
   not enforce the discipline, the maintainer's process must. Web **session state is externalized**
   from the start (not in-process), so the Phase-5 horizontal face scaling (repo-affine owner routing)
   is not a retrofit.

7. **SPA-island trigger (the reversibility hedge, named).** Adopt a SPA **per-route** — consuming the
   unchanged JSON API, never rewriting the backend — if and only if a qualifying surface arrives. The
   qualifying set (enumerated so a future maintainer can adjudicate it, not "feels app-shaped"):
   - **(a)** an inline-comment / side-by-side diff-review workbench — the expected first trigger, the
     **Phase-5 PR-review mode** (ROADMAP Phase 5);
   - **(b)** an interactive knowledge-graph / backlink explorer (client-side canvas/SVG HTMX cannot
     render);
   - **(c)** real-time multi-user editing;
   - **(d)** a product pivot toward a **chat / agent-workspace** experience — at which point Agora has
     entered famous-app category (b) and the Open WebUI shape (FastAPI + an SPA) becomes correct.

   Until one of these fires, no SPA, no Node toolchain.

**Quartz is not this face.** The Quartz read view (DESIGN §8 BOM; scheduled in ROADMAP Phase 4;
publish conventions in ADR-0010) is a *separate, public/static* surface built from the curated git
refs; this ADR governs only the dynamic auth/upload/dashboard face. The two coexist (ADR-0014 interop
keeps links/anchors consistent); the dynamic face is not a document renderer.

## Consequences
- **+** Phase 3 ships fast and Python-only: no Node/Vite toolchain, no second test ecosystem, one
  lean OSS container; thin-face by construction (ADR-0003) and honouring DESIGN §5.2's
  "lightweight-frontend" literally.
- **+** Reversibility is real and cheap: the clean JSON API keeps the SPA option open **per-route at
  zero backend cost** — the panel's top reversibility score, and the precise hedge against the one
  future (chat-productization) in which SPA would win.
- **+** Tenancy is leak-resistant: server-threaded scope/ACL means no client over-fetch can cross the
  repo boundary (ADR-0006 / DESIGN §7).
- **−** Charting forces one permissive JS dependency (or hand-rolled SVG) — a small crack in the
  "no client JS" story, contained to the dashboard.
- **−** The §7 qualifying surfaces (PR-diff reviewer, graph explorer, multi-user editing) may force a
  SPA island; if *many* such surfaces arrive, two UI stacks coexist and the single-stack maintenance
  win partially erodes. The §6 API-first discipline keeps the blast radius to one route, not a
  rewrite.
- **−** Auth is roll-your-own: Starlette is not batteries-included, so Phase-4 token/session, Forgejo
  token exchange, and Phase-5 OAuth 2.1 + PKCE against Keycloak/Authentik (client glue via e.g.
  Authlib; ROADMAP Phase 5) are a bespoke, security-sensitive surface the maintainer owns, with no
  framework guardrails.
- **−** The reversibility thesis depends on **maintainer discipline** (§6), which the stack cannot
  enforce; an undocumented, untested, fragment-coupled API silently closes the SPA escape hatch.
- Enforced, like ADR-0003, by the dependency rule: the web face depends on `core`; `core` depends on
  no face. The JSON API is the contract both the web HTML layer and any future SPA island bind to.

## Future work (reserved, not implemented)
Designed so each is additive, never a breaking change:
- **SPA island per-route** when a §7 trigger fires — same JSON API, no backend re-plumbing.
- **Externalized session store** before horizontal face scaling (ROADMAP Phase 5); chosen now so it
  is config, not a refactor.
- **Optional synthesis adapter** layering prose-with-citations over the same `SearchHit` JSON
  (ADR-0009 already reserves this) — a later API capability the HTML view merely reflects.
- **Pluggable identity** (token → Forgejo → OpenFGA → OIDC) resolved in the FastAPI layer; the UI
  threads whatever the core returns, so the auth regime can swap underneath without a UI rewrite.
