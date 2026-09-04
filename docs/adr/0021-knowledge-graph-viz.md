# ADR-0021 — Interactive knowledge-graph viz: the first ADR-0019 §7 vendored per-route viz

**Status:** Accepted · 2026-06-24 · realizes [ADR-0019](0019-web-face-stack.md) §7(b)
**AMENDED (append-only) — [ADR-0041](0041-stratum-kind-first-layout.md) (Proposed, KB wiki schema 2) changes the graph NODE SHAPE:** `domain` (0..1, derived from the path by `_wiki_domain`) becomes `subjects` (0..n, read from frontmatter), `type` becomes `kind`, and `wiki/people/**` JOINS the node set (read-first-class). `/api/graph` is a face contract, so this is a visible release change. The node cap, the BFS, the `truncated` flag and the vendored force-graph choice are UNCHANGED. The prose below is retained verbatim for history.

Realizes the **interactive knowledge-graph / backlink explorer** that ADR-0019 §7(b) enumerated as a
qualifying trigger for the **per-route SPA-island / client-canvas viz** escape hatch. Binds the OSS
license constraint of [ADR-0005](0005-fully-oss-bom.md) (no AGPL/copyleft in the redistributable
default path), the thin-face rule of [ADR-0003](0003-one-core-many-faces.md), and invariant 1
(markdown + git is the only source of truth; DBs are rebuildable indexes, never canonical).

## Context
The web face (ADR-0019) ships browse/search/upload, a dashboard, and metrics, but the link graph —
which `lint()` already resolves in memory and the dashboard already derives orphans from — was never
*shown*. Users want an Obsidian-like graph: a global whole-KB view and a per-note local/backlink
mini-graph, with drag/zoom/hover/click. ADR-0019 §7 pre-committed that such a surface is adopted
**per-route, consuming the unchanged JSON API, never rewriting the backend, and only if a Node
toolchain is genuinely warranted.**

The graph is **small and derived**: every node is a note from `Wiki.list_notes()`; every edge is an
already-parsed link (`schema.notes.body_link_basenames` for body `[T](x.md)` links +
`wikilinks(related/children)` for the frontmatter `[[basename]]` arrays — the *same* surface the
dashboard's orphan derivation uses, ADR-0019 / `health()`). Nothing about drawing it needs a new
store.

### Options
- **(A) Vendor a single permissive force-graph UMD lib, no Node** — like the htmx vendor: one MIT
  `.js` file served from `static/`, zero build step, zero CDN. **Chosen.**
- **(B) Static server-rendered SVG** — non-interactive (no drag/zoom/hover), not the requested
  Obsidian-like surface. Rejected.
- **(C) A full React/Vite SPA island** — a Node/npm/Vite toolchain grafted onto a Python-only,
  dependency-light, solo-maintained project for *one page*. Overkill; rejected (the §7 hedge is "no
  Node toolchain until genuinely warranted").
- **(D) A graph database (Neo4j, as asked)** — rejected on three independent grounds, below.

## Decision
1. **Vendor `force-graph` (vasturiano, MIT) as a single UMD static asset** —
   `faces/web/static/force-graph.min.js`, with a license header, pinned at the vendored version
   (1.51.4). It bundles d3-force/d3-zoom/d3-drag and renders to canvas (native drag/zoom/hover/
   click). No Node, no build, no CDN — the htmx posture (ADR-0019 §4). This is the **first firing**
   of the ADR-0019 §7 escape hatch.
2. **Graph data is a read-only thin-face handler.** `AgoraHandlers.graph(center, depth, domain)`
   returns `{nodes:[{id, title, domain, status, type, orphan}], edges:[{source, target}], node_total,
   edge_total, truncated, center, depth}`. `id == rel_path`; edges are resolved basename→rel_path and
   restricted to real nodes (dangling links are a lint signal, not an edge); the `orphan` flag reuses
   `health()`'s `referenced`-set derivation verbatim-in-spirit (parity is test-locked). No canonical
   change, no graph store, never another repo's files — like `browse`/`health` (ADR-0003).
3. **Two surfaces over one contract** (ADR-0019 §1/§6 — the JSON is the asset, the HTML a layer over
   it): a global `GET /graph` page + `GET /api/graph`; and a per-note **local ego-graph**
   (`/api/graph?center=<rel_path>&depth=1`, an undirected BFS) embedded on `/note/<path>`. The page
   builds a domain-filter chip row server-side; a vanilla `static/graph.js` hydrates each
   `[data-graph-src]` container from the JSON (node click → `/note/<id>`). force-graph (177 KB) loads
   **only** on `/graph` and `/note` (a templated scripts block), never globally.
4. **XSS-safe + bounded.** force-graph injects the node-label tooltip as `innerHTML`, so titles are
   escaped **client-side** in `graph.js` (the JSON layer stays faithful — it returns the raw title);
   intra-wiki link rewriting and attribute values go through Jinja autoescape + `urlencode`. For a
   very large KB the global graph applies an **honest** node cap (`MAX_GRAPH_NODES`) — `truncated` is
   flagged and `node_total`/`edge_total` still report the true pre-cap counts, surfaced as a UI
   banner — and the domain filter narrows scope.

### Why Neo4j was rejected (the user asked)
- **License (ADR-0005).** Neo4j is GPLv3 (Community) / commercial (Enterprise) — copyleft in the
  default path, the same reason the BOM picked pdfminer over AGPL pymupdf and Valkey over Redis, and
  keeps Grafana an external sidecar.
- **Invariant 1.** A graph DB as a *store* makes a non-markdown surface canonical; Agora's link graph
  must remain **derived and rebuildable** from the markdown (lint already resolves it in memory).
  It's also heavy JVM infra against a zero-infra / self-host / dependency-light ethos.
- **Overkill.** The graph is small (hundreds–thousands of nodes) and already derived — **no graph DB
  is needed to draw it.** If real graph-DB power (Cypher, graph algorithms at scale) is ever wanted,
  it is an **optional adapter**, and KùzuDB (embedded, MIT) or NetworkX (BSD) fit far better than
  Neo4j.

## Consequences
- **+** Obsidian-like interactive graph with **zero Node/build**, one MIT static asset, a Python-only
  face — honouring ADR-0019 §4/§7 literally; reversible by construction (the JSON API is the durable
  contract, §6).
- **+** The graph is a **derived view**: rebuildable from markdown, no new persistence, no migration,
  no second source of truth (invariant 1). The data handler reuses `Wiki.list_notes` +
  `schema.notes` + `health()`'s orphan logic, so it can't drift from the dashboard.
- **−** One more **vendored JS asset** (177 KB) to license-track and occasionally refresh by hand (no
  automated dep bump — the same posture and trade-off as the htmx vendor).
- **−** force-graph's tooltip is `innerHTML`, so **client-side label escaping is load-bearing**
  (documented in `graph.js`; the JSON layer's raw-title faithfulness is test-asserted).
- **−** A very large KB depends on the **node cap / domain filter**; the cap is honest (a `truncated`
  flag + the true `node_total`, surfaced in a banner), never a silent truncation.

## Future work (reserved, not implemented)
- Edge weighting / clustering and a search-to-focus control on the global graph.
- An **optional** embedded graph-engine adapter (KùzuDB / NetworkX) **iff** Cypher-class queries or
  large-scale graph algorithms are ever needed — strictly a derived index behind an adapter, never
  canonical (invariant 1 / ADR-0004).
