# Agora — Roadmap

Phased delivery. Each phase is independently useful and builds on the last. The guiding rule: **prove
the core (capture → consolidate → query) on a single local repo before adding faces, auth, or scale.**

## Phase 0 — Design (complete)
- [x] Design docs (DESIGN, ARCHITECTURE, DATA-MODEL), ADRs, project scaffold.
- [x] Validate the wiki schema against a real existing KB (`~/knowledge`) for compatibility
      (validated via `agora import` on a `~/knowledge` clone → lint-clean; ADR-0014 D5).

## Phase 1 — Personal MVP (core + MCP face) — DONE (shipped, dogfooded on `~/knowledge`)
Goal: a single local markdown repo with capture, scheduled consolidation, and query — zero infra.
- [x] `core.inbox` (immutable events + event-key idempotency), deterministic `core.wiki`
      (`QueryResult`/`SearchHit`), `core.repo` (layout, git), `core.state`.
- [x] `curator.worker` transactional run loop (claim manifest → sandboxed worktree → validate → publish
      → finalize) with the **local-model**
      write-adapter (Qwen via Ollama) as default brain. OS sandbox per ADR-0013 (`agora doctor`
      self-test); the default Ollama brain does inference OUTSIDE the sandbox (env-scrubbed) by design.
- [x] `curator.triggers`: cron + threshold + idle (deterministic cron matcher + `agora watch`).
- [x] MCP face: `kb_remember`, `kb_query`, `kb_status`, `kb_curate` (FastMCP, stdio).
- [x] `agora` CLI: `serve`, `curate`, `repo init`, `status`, `doctor` (+ `watch`, `import`).
- [x] Register with Claude Code + Hermes; dogfood on `~/knowledge` (both clients ✔ connect to
      `agora serve`; the real `~/knowledge` is imported non-destructively to a curate-able KB and a
      live capture → curate → query loop runs on it).
**Exit:** capture from any MCP client → curator atomically files it → deterministic query returns it
with path/anchor citations; injected or failed backend output cannot modify the live wiki.

## Phase 2 — Pluggable brains + harvester (current/next)
Goal: tool-agnostic curator and autonomous accumulation, still single-user.
- [x] `curator.backends` registry from `adapters.yaml`; verify ≥3 brains (qwen, claude, hermes) +
      per-task routing. Per-act `plan`/`author` routing (ADR-0015) + a generic CLI-agent brain shim
      (ADR-0016, `agora-cli-brain`) ship; **live-verified** 4 distinct real brains end-to-end
      (qwen + hermes via Ollama, claude + codex via the CLI shim), all publishing lint-clean, plus
      per-task routing across two of them. (gemini failed cleanly on the test host — an account-tier
      limitation, not a shim defect.)
- [x] `harvester` with file connectors (Claude Code memory, Hermes `MEMORY.md`); candidate gate;
      provenance + loop prevention; personal-repo scope lock. Shipped (ADR-0017): `agora harvest`
      [+ `--dry-run`, `--connector`] over a `FileConnector` that segments a `MEMORY.md` into facts
      and writes them as gated `kind=candidate`/`confidence=low` inbox items (`source=harvest:<agent>`)
      for the curator's existing keep/merge/drop gate (the PRIMARY loop break); a fail-closed scope
      gate keyed on repo `kind` (personal source → personal repo only); a DATA-MODEL §6 cursor with a
      whole-source fast no-op (the harvester writes `proposed`; the curator-owned `accepted`/`rejected`
      counters are now bumped deterministically at finalize from the plan dispositions over each
      candidate's harvested provenance — happy-path-only mirror of the state-counter bump, per-event
      granularity, best-effort + rebuildable — ADR-0017 §7). Opt-in (`harvest.enabled`, default off); `connectors:` in `adapters.yaml`;
      `agora doctor` shows the connectors line. **Live-verified** on a real `MEMORY.md` shape
      (dry-run + real write + unchanged no-op + a team-repo scope refusal). Opt-in **link-following**
      (ADR-0018) then shipped: a `follow_links` connector follows a pointer bullet's
      `[Title](sibling.md)` and harvests the sibling's content (frontmatter stripped, source-dir-confined,
      symlink-rejected, fan-out-capped, one hop) instead of the thin one-liner. The realistic
      *reworded* KB→memory→KB loop and team/multi-tenant harvesting are documented residual risks
      deferred to later phases (ADR-0017); Letta/mem0 API connectors remain Phase-4/5.
**Exit:** swap the curator brain via config with no data risk; harvested candidates flow safely.

## Phase 3 — Web face: upload + dashboard — DONE (shipped; web/dashboard/metrics behind optional extras)
Goal: humans contribute and observe via the browser.
- [x] `ingest` extractors (url/pdf/office) → `raw/` + inbox. Shipped (PR #14): pure
      `ingest/extractors/{base,url,pdf,office}.py` — `url` (trafilatura) / `pdf` (pdfminer.six,
      MIT over AGPL pymupdf) / `office` (markitdown) → an `ExtractedDoc` carrying extracted markdown +
      `content_sha256` (reuses `core.hashing`); the third-party libs are a lazy, optional `ingest`
      extra (a missing dep surfaces as a clean `ExtractorUnavailable`, not an import error). The
      curator stays the sole writer of `raw/` (per ADR-0020 the upload path goes extract → inbox).
- [x] Web app: browse, search, upload. Shipped (PR #15, ADR-0019 + ADR-0020): `faces/web/app.py` —
      an API-first FastAPI app with a first-class JSON API (`GET /api/{status,search,notes,notes/{path}}`,
      `POST /api/upload`) **and** a server-rendered HTMX/Jinja2 UI (`/`, `/search`, `/note/{path}`,
      `/upload`). Note bodies render via `markdown-it-py` with raw HTML disabled (`html=False`,
      XSS-safe) and intra-wiki links rewritten to `/note/...`. New core read helpers
      `Wiki.list_notes`/`get_note` + `AgoraHandlers.browse`/`note`; upload write-path (ADR-0020)
      extracts then `Inbox.write`s a gated capture (curator remains sole writer of `raw/`; storing the
      original binary verbatim + its drift sidecar is a deliberate deferral). CLI `agora web` (lazy,
      optional `web` extra += jinja2/python-multipart/markdown-it-py); localhost, no auth.
- [x] Dashboard: KB health + curator status + harvester status (reads existing metadata). Shipped
      (PR #16): a read-only dashboard over `AgoraHandlers.health()`/`curator_status()`/`harvester_status()`,
      surfaced at `GET /dashboard` + `/api/dashboard/*` with HTMX-polling fragments. Health reuses the
      deterministic `lint()` verbatim (DESIGN §5.3) and reports distinct `orphans` (L2-1, read-time
      link-graph derivation) vs `broken_links` (L1-2, dangling outbound) signals.
- [x] Prometheus metrics export; optional Grafana dashboards. Shipped (PR #17): `faces/web/metrics.py`
      — a `GET /metrics` exporter of cheap operational metrics that reads CURRENT state per scrape and
      **never** runs `lint()` or a whole-tree scan; `prometheus_client` is the lazy, optional `metrics`
      extra (a missing dep returns a clean HTTP 503 with an install remedy). Grafana stays an external
      AGPL sidecar (out of the core; see the deferred list). And (PR #18, ADR-0017 §7) the curator now
      wires the harvest cursor `accepted`/`rejected` counters at finalize (happy-path-only mirror of the
      `_bump_counters` state bump, per-event, best-effort + rebuildable), so the dashboard and metrics
      surface real harvest dispositions rather than zeros.
- [x] Interactive knowledge graph (Phase-3-adjacent enhancement). Shipped (ADR-0021): `GET /graph` +
      `GET /api/graph` + a per-note local ego-graph embedded on `/note`, backed by the read-only
      `AgoraHandlers.graph()` (reuses `Wiki.list_notes` + `schema.notes` + `health()`'s orphan
      derivation — no graph store; invariant 1). Drawn by a vendored MIT force-graph lib (no
      Node/build/CDN) — the first firing of the ADR-0019 §7 per-route-viz escape hatch; a graph DB
      (Neo4j) was rejected on license/SSOT/overkill grounds.
**Exit:** upload a PDF/URL in the browser → it becomes a linked wiki note; dashboard shows the queue.

## Phase 4 — Small team (multi-tenant, network)
Goal: a few trusted people share repos over a private network.
- [ ] `core.repo` multi-repo + team/personal kinds; write routing + read scope.
- [ ] Single repo-owner service per working copy; gateways route captures to the owner (no competing clones).
- [ ] MCP **Streamable HTTP** transport.
- [ ] AuthN/AuthZ via **Forgejo delegation** (repos, teams, roles); token auth + Tailscale.
- [ ] Shared git remote (Forgejo); Quartz read-only web view.
**Exit:** two users, two teams + personal repos, hard-isolated, over the network.

## Phase 5 — Full team (governance & scale)
Goal: production-grade access control and review.
- [ ] **OpenFGA** for fine-grained, per-domain ACL.
- [ ] **OAuth 2.1 + PKCE** via Keycloak/Authentik.
- [ ] PR review mode (curator opens PRs instead of direct commits).
- [ ] API harvester connectors (Letta, mem0); horizontal curator sharding by repo.
- [ ] Horizontal face scaling with repo-affine owner routing and failover fencing.
- [ ] Packaging: Docker Compose, Helm (optional), published `agora-kb` (PyPI) + images.
**Exit:** self-hostable multi-team deployment with auth, review, and governance.

## Backlog (post-Phase-3) / corporate-AX — re-sequenced

The seven backlog issues (#23–#29) re-sequenced into phases. **No invariant changes**: every item is
additive over the existing seams (read-connectors/extractors per ADR-0004, the candidate gate +
scope + provenance triad per ADR-0007, the single-writer curator per ADR-0002, markdown-SSOT per
ADR-0001). Load-bearing pieces are tracked to **Proposed** ADRs and presented as *planned*, not
settled. The graph piece of #29 is already **shipped** on `feat/web-knowledge-graph-viz` (tip
`1bad37e`): the vendored MIT force-graph render over a JSON graph endpoint, the per-note ego-graph,
and `AgoraHandlers.graph()` (caps `MAX_GRAPH_NODES`/`MAX_GRAPH_DEPTH` at `mcp_server.py:59-60`) all
land under the **Accepted ADR-0021** — the first firing of the ADR-0019 §7 escape hatch. So #29's
*remaining* scope here is config/upload/extensions, **not** the graph render.

ADR numbering for the new work (ADR-0021 is the graph, already Accepted): taxonomy governance =
**ADR-0022**, context-harvester connectors = **ADR-0023**, bulk/horizontal scale = **ADR-0024**,
web config/multi-upload/extensions = **ADR-0025**, skill-suggestion write-back = **ADR-0026**
(reserved, deferred).

### Phase 3.5 — Web hardening + throughput floor (pull-forward; additive over ADR-0019/0020/0021)
Low-risk, repo-local, OSS-safe; lands post-Phase-3 with no invariant change.
- [ ] **#29 web enhancement (config/upload/extensions).** The knowledge-graph render is already shipped
      (Accepted ADR-0021); this slice is the post-merge follow-up tracked to a new **ADR-0025 —
      web config, multi-upload, extensions** (Proposed). Lift the two hardcoded graph caps
      (`MAX_GRAPH_NODES`/`MAX_GRAPH_DEPTH`, `mcp_server.py:59-60`) into an operator-configurable
      `web.graph.{max_nodes,max_depth}` (the first consumer of the new `web:` block). Add drag-and-drop
      **multi-upload** (each file → one independent ADR-0020 extract→inbox capture + a per-file batch
      receipt, best-effort not atomic; per-batch max-files/total-bytes caps; curator stays sole writer
      of `raw/`) and **broadened extractors** behind the `ingest` extra via the `extractors/base.py`
      dispatch (`.txt`/`.md` dependency-free passthroughs; widen markitdown routing to
      html/csv/json/epub — already pinned, no new dep; image-OCR/audio DEFERRED behind their own opt-in
      extra + ADR-0005 license vetting). Add a `web:` block in git-ignored `_kb/repo.yaml`
      (`web.graph.{max_nodes,max_depth}`, `web.upload.{max_bytes,max_files,allowed_extensions}`,
      `web.features.{graph_enabled}`) resolved **per-repo** in `build_app(repo_path)` (never a global
      mutable — tenant-safe for Phase 4, invariant #5); silently-ignored-if-unwired, same pattern as
      today's forward-looking keys (DATA-MODEL §3). Plus an untrusted-input hardening pass
      (decompression-bomb caps, SVG/HTML XSS, per-batch caps). Update DESIGN §5.2 (multi-upload +
      extensible formats; the read-only `/graph` already documented under ADR-0021).
- [ ] **#26 search index.** Implement the **Accepted** ADR-0012 derived ranking cache as a rebuildable,
      git-ignored `_kb/index/` (metadata/index, never canonical — ADR-0001) with an optional
      CPython-bundled FTS5 prefilter behind the deterministic-query contract (ADR-0009); the pure-Python
      BM25F stays the oracle. This is an **implementation of the already-Accepted ADR-0012, not a new
      ADR**. Rebuildable-from-markdown is a hard requirement; **semantic/vector** stays deferred (see
      below), with #28 corporate volume as the explicit future trigger.
- [x] **#27 (part) bounded-batch claim + batch observability — DONE via #60 (ADR-0024 OD-3a addendum,
      Phase 3.5).** The already-specified `max_candidates_per_run` (INGEST-CONTRACT §1.3, default 32) is
      WIRED: `load_repo_config` parses it and `claim()` slices the FIFO head at that many distinct
      tier-2 content groups (candidates), leaving the remainder in the inbox for the next trigger; per
      OD-3a **no** sibling `max_events_per_run` was introduced. **Intra-repo pipelining, NOT a second
      writer** — single-writer CAS+flock stays the ceiling. Batch size/cap/queue-remaining surface on
      the run log, `RunReport.counts`, `state.json last_batch`, and `/metrics` gauges; small-model cap
      guidance (≤8B: 8–12, 30B-A3B: 16–24) documented in §1.3, calibrated later by #44. The
      CAS-conflict-rate metric remains with the rest of #27.
- [x] **#23 (floor) no-loss catch-all + repo-global thresholds — DONE (ADR-0022 step 1/2, Phase 3.5).**
      "No matching domain" no longer defaults to DROP: an unclassifiable non-gated basename op is routed
      to the deterministic catch-all — the **first declared domain `domains[0]`** (default repo →
      `general`) — in `ollama_brain.normalize_plan` step 3, a data-loss safety net that lands *before*
      the governed auto-create lane. Shipped with the repo-global threshold wiring (E.3 leg): the
      previously-inert `curator.limits.{body_byte_bound,related_k}` / `curator.lint.max_orphans`
      (the last a warning-only L2-1 lint signal) are now parsed by `load_repo_config` and threaded
      through `build_routed_backend` / `worker.run` → `build_bundle` / `lint` at all three run sites
      (curate, watch, mcp). Documented in DESIGN §4 + DATA-MODEL §3.1; `domains[0]` chosen over a
      literal `general` (which `repo init --domain foo,bar` erases). The governed CREATE_DOMAIN lane +
      per-domain customization remain Phase-4-coupled below.

### Phase 4-coupled — Governed taxonomy + per-domain + personal connectors (multi-tenant-adjacent)
Gated on / co-developed with Phase-4 multi-tenancy where a source is team/shared.
- [ ] **#23/#24 governed CREATE_DOMAIN lane + per-domain customization.** New **ADR-0022 — curator
      taxonomy governance** (Proposed): record that domain creation is a *governed* lane, **not** a
      relaxation of ADR-0010 D6 inside INGEST — the sandboxed brain may NEVER directly widen
      `_meta/taxonomy.yaml`; it only PROPOSES, the deterministic worker applies. The (currently inert)
      `taxonomy_policy` + L1-18 get their job on the creation lane only: `open` = deterministic
      auto-create (solo/MVP); `review-only` = emit a domain *proposal*/PR; `capped:<N>` = ≤N new domains
      per run. **HARD prerequisite**: the same change that lands worker-applied CREATE_DOMAIN MUST flip
      the effective default to be repo-kind-aware (personal→`open`; team→`review-only` or `capped:1`) —
      today the code default is unconditionally `"open"` (`emit.py Taxonomy.taxonomy_policy="open"`;
      `lint.py loaded.get("taxonomy_policy","open")`); a team repo with no explicit policy must NOT
      auto-create a domain in-INGEST (test-locked). **L1-18 must be EXTENDED**, not "activated": today it
      diffs only `allowed_tags(after)−allowed_tags(before)` and never looks at `domains` (kb_schema.md
      L1-18 row + §5.2 prose, `schema/lint.py check_taxonomy_policy`) — add a `domains` set-difference
      branch + a test that an over-cap domain add is rejected. CREATE_DOMAIN follows the SAME
      all-or-nothing CAS-publish-or-discard semantics as every op (taxonomy write + lazy MOC + theme are
      ONE atomic worktree diff published by one CAS); a failed publish discards the whole worktree (no
      half-created domain) and the fact falls to the catch-all floor. Pin a structured
      `_meta/taxonomy.yaml` `domains` shape (list-of-strings **or**
      `{<domain>: {created, created_by, status: proposed|active, source_run_id?}}`) — mirroring the
      `allowed_tags` list-or-mapping PATTERN, but note `domains` is currently **list-only in all three
      readers** (`config._load_taxonomy`, `ollama_brain.parse_taxonomy`, `schema/lint`), so the
      normalizer is net-new in EACH; additive, does NOT bump `schema_version` (L1-17 untouched), no
      migration command needed (the bare list stays valid). Per-domain customization (#24) layers only
      onto *tuning* surfaces (PASS-1/PASS-2 prompts in read-only `_meta/domains/<domain>.md`, per-act
      brain selection, soft thresholds `body_byte_bound`/`max_orphans`/`related_k`) — it may NEVER alter
      the closed op vocabulary, the §4.0 allowlist, the fixed taxonomy, or the §4.1/§4.4 validators; the
      integrity gate stays domain-agnostic + model-independent (default brain = OSS Qwen for every
      domain, invariant #4). `is_gated` candidates can NEVER mint a domain (extend `plan.py` check-10).
      Sequence the deterministic+prompt parts early (repo-global first); per-domain brain routing later
      (gated behind ADR-0015 per-domain split).
- [~] **#25 (read side) session connector + #28 (personal) local/work-context connectors.**
      **#25 session connector SHIPPED (ADR-0023 Accepted):** `session:<agent>` distills agent
      transcripts (assistant marker reflections, deterministic + model-free) into gated candidates,
      redacting secrets at the connector boundary before the immutable inbox write (the live
      `core/redact.py` wiring + `harvest.redact.{enabled,pii,allow,deny}` config +
      `HarvestCursor.redacted` counter deferred from #39 all landed here); the `SessionReader` seam
      (`ClaudeCodeJsonlReader`) keeps it tool-agnostic. **#28 (`dir:`/`git:`/`mail:`/`chat:`/`calendar:`)
      still Proposed** under the same ADR-0023 envelope. Reframe the
      harvester (ADR-0007, DESIGN §6) from "agent MEMORY files" to "agent memory AND working-context
      sources" via **ADR-0023 — context-harvester connectors** — same gate/scope/provenance,
      no new core path (orchestrator/cursor/gate/scope reused; only `build_connectors` gains type
      branches behind the existing ADR-0004 Connector Protocol). Reserve the connector-type grammar
      `<type>:<agent>` beyond `file:`: `session:<agent>` (e.g. `session:claude-code`,
      `path: ~/.claude/projects/**/*.jsonl`), `dir:`, `git:` — `build_connectors` dispatches on type with
      the existing fail-loud unknown-type behavior; each maps to `source=harvest:<agent>` (the FIXED inbox
      source enum needs no new members — corporate sources reuse the parametric `harvest:<agent>` form).
      v1 distillation is deterministic + model-free. **Personal-first, team/shared scope deferred to
      Phase-4** core write-boundary. ANY PII-bearing source (local OR networked) MUST run a shared
      deterministic `core/redact.py` before the append-only `Inbox.write` (the immutable inbox cannot be
      retro-scrubbed — invariant #3); `session:`/`dir:`/`git:` are HIGH-PII (verbatim prompts, pasted
      secrets, file contents), so `redact.py` is a HARD dependency of the `session:` connector merge and
      the first concrete trigger for the redaction-policy decision (with a metadata-only redaction-event
      counter via Prometheus + `agora harvest --dry-run` printing what WOULD be redacted, never the
      secret). `git:` reuses the fixed DATA-MODEL §6 whole-source `last_content_sha256` no-op (hash of the
      concatenated since-cursor commit payloads — not a separate SHA cursor — so `extra='forbid'` is
      respected). Transcript-derived candidates get the same `is_gated` treatment; their lower
      signal-to-noise makes reliable DROP a `--dry-run` validation requirement (INGEST-CONTRACT §6).

### Phase 5 — Cross-host single-writer + networked corporate sources + skill write-back
- [ ] **#27 repo→owner fencing lease + sharder.** Promote the scattered one-liners into one rule:
      **shard BY REPO, never within a repo** (ARCHITECTURE §2 / DESIGN §7), tracked to **ADR-0024 —
      bulk processing / horizontal curator scale** (Proposed). The per-repo `flock` is host-local
      (`fcntl`), so the CAS on the single curated ref (`repo.py compare_and_swap_branch` updates one ref)
      is the only cross-host net — two hosts merely SERIALIZE (loser discards, never corrupts), wasteful
      not unsafe. Sub-items: **(5a)** repo→owner assignment + fencing lease (git-ref CAS + fencing token,
      OSS-pure default; optional etcd/Consul adapter never core) for cross-host single-writer; **(5b)**
      within-repo throughput knobs (bounded batch from Phase 3.5 + trigger tuning). Don't rely on prose
      "multi-host unsupported": make CAS-conflict rate a first-class dashboard alert + a host-local
      stale-lock warning when >1 curator heartbeat is seen, so a wasteful cross-host double-writer is
      VISIBLE (not "looks like a hang"). **Intra-repo MULTI-writer is explicitly out of scope** pending
      an ADR + benchmark evidence of single-curator saturation — even disjoint file sets (per-domain
      index fragments + per-curator log shards) still contend on the SINGLE curated-branch-ref CAS, so
      domain-partitioned writers would need per-domain branches or a serialized publish step (default
      no-go).
- [ ] **#28 networked corporate work-context connectors.** Extends **ADR-0023**: (1) each is an ADR-0004
      read-adapter, zero core change; (2) a mandatory OSS path per class — mail (IMAP/JMAP; Gmail/Graph
      optional), chat (Matrix; Slack/Teams optional), calendar (CalDAV; Google/MS optional), git
      (commit/diff), local folders (`dir:`), meetings (file/extractor); (3) safety = ADR-0007 triad
      **plus** connector-boundary PII/secret redaction (`core/redact.py`) **plus** a pre-gate
      summarization/clustering stage so high-volume/low-signal sources don't overwhelm the per-run bundle
      cap (INGEST-CONTRACT §1.3 `max_candidates_per_run=32`, wired at the FIFO claim since #60) —
      summarization is a read-side transform, the gate still adjudicates; (4) team/corporate-shared scope
      deferred to Phase-4 multi-tenancy. **Letta/mem0 API connectors are one member of this broader
      connector family** (was the only Phase-5 connector listed). **How the collected context is USED**
      (the issue's open second half) is bounded, not vague: (a) once curated it becomes queryable wiki
      knowledge via the existing read/query/MOC/graph path; (b) the digest/clustering consumption stage
      is the genuinely-open piece, gated on a CONCRETE evidence trigger (inbox backlog depth or per-run
      DROP-rate from #27's metrics), not "until volume exists". DESIGN §6 broadens "Memory harvester" →
      "Context harvester" with a source-class table.
- [ ] **#25 skill-suggestion write-back.** Opt-in, **never auto-write** to a locally-installed agent;
      needs its **own ADR-0026** (Proposed, reserved) — sequenced after the #28 context-collection
      design, mirroring how Letta/mem0 write-back was deferred. Acceptance criteria: opt-in flag, emits a
      proposed `SKILL.md` to stdout or `_kb/staging/` only, NEVER writes `~/.claude/skills`, a
      human-confirm gate, and a test asserting no filesystem write outside the staging dir. Note (recorded
      in ADR-0023 §9) that the `session:` connector + a future skill write-back together form a potential
      KB→skill→session→KB cycle the verbatim origin-marker skip cannot catch (reworded by construction),
      so the write-back must stamp provenance the session distiller recognizes and drops — loop-break
      responsibility sits in BOTH the write-back ADR and the session distiller, not the candidate gate
      alone.

### Explicitly deferred from the backlog (with evidence-triggers)
- **Image-OCR / audio extractors** (#29) and **binary-staging on multi-upload** — defer until a concrete
  format need + an OSS-pure engine (ADR-0005); multi-upload ships text/markdown + existing extractors first.
- **Intra-repo multi-writer** (#27) — defer pending an ADR **and** measured single-curator saturation on a
  real repo (sharding is by-repo only until then).
- **Semantic/vector search** (#26) — already on the global deferred list; revisit only if FTS5 + the
  ADR-0012 ranking cache prove insufficient at scale, with #28 corporate volume as the explicit trigger.


## Cross-cutting (every phase)
- Tests for the **core API** and **adapter contracts** (the stable surfaces).
- Keep core dependency-light and OSS-pure (ADR-0005); proprietary pieces stay behind adapters.
- Dogfood: Agora's own design/dev knowledge lives in an Agora repo as soon as Phase 1 lands.

## Explicitly deferred / out of scope (revisit later)
- Semantic/vector search layer over the markdown (only if navigation proves insufficient at scale).
- Real-time collaborative editing (CRDT/OT).
- Hosted/SaaS offering.
