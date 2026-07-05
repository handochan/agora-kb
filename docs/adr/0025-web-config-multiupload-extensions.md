# ADR-0025 — Web-face operator config, multi-upload, broadened extensions

**Status:** Accepted · 2026-07-05 (Step-0 ratified, #36; shipped in PR #33) · Proposed 2026-06-24 · covers issue [#29]

Extends [ADR-0019](0019-web-face-stack.md) (web-face stack: API-first FastAPI + server-rendered
HTMX; §5 "charts are the single bolt-on"; §7 SPA-island triggers) and [ADR-0020](0020-web-upload-write-path.md)
(web upload write-path = extract → `Inbox.write`; the curator stays the sole `raw/` writer). References
[ADR-0021](0021-knowledge-graph-viz.md) (Accepted — the interactive knowledge-graph viz, already
shipped on `feat/web-knowledge-graph-viz`) as the **first consumer** of the config this ADR reserves.
Binds the license constraint of [ADR-0005](0005-fully-oss-bom.md) (fully-OSS BOM; no AGPL/copyleft in
the default path; optional deps behind lazy extras), the input-adapter family of
[ADR-0004](0004-pluggable-adapters.md) (extractors are pure transforms), and the tenant boundary of
[ADR-0006](0006-repo-as-tenant-boundary.md) / DESIGN §7 (a face for one repo must never serve
another's content — invariant 5). It does **not** move any of the six non-negotiable invariants; every
decision here is additive over the already-shipped Phase-3 face.

## Context
Phase 3 shipped the web face (`faces/web/app.py`, `agora web`). Issue #29 (web 기능 강화 — "strengthen
the web features") asks for four things; one of them (the graph view) has **already shipped** under a
separate Accepted ADR, and the other three have no home today.

- **A knowledge-graph view — already shipped (ADR-0021).** The interactive Obsidian-like graph is
  **done and Accepted**: `feat/web-knowledge-graph-viz` (tip `1bad37e`) adds
  `AgoraHandlers.graph(center, depth, domain)` to `faces/mcp_server.py` (a thin, deterministic read
  over `Wiki.list_notes` reusing the dashboard's orphan/backlink derivation), the JSON `GET /api/graph`
  + the HTMX `GET /graph` page (`faces/web/app.py`), the vendored MIT `static/force-graph.min.js`
  (pinned 1.51.4), `static/graph.js`, `templates/graph.html`, and the per-note local ego-graph link in
  `templates/note.html`. ADR-0021 is the **first firing of the ADR-0019 §7(b) escape hatch** and
  frames that decision (one vendored permissive UMD lib, no Node toolchain). **This ADR does not
  re-litigate the graph render or §7** — it references ADR-0021 and adds the operator config the
  graph's caps will consume. The graph's only #29-remaining seam is its **two hardcoded caps**
  `MAX_GRAPH_NODES = 2000` / `MAX_GRAPH_DEPTH = 3` (`mcp_server.py:59-60`), today module constants with
  an honest `truncated` flag and a depth clamp to `[1, MAX_GRAPH_DEPTH]` — see Decision 1.

- **A general settings surface.** There is none. `agora web` (`_cmd_web`, `cli.py`) threads only
  `--host`/`--port`/`--writer`/`--user` into `build_app(*, repo_path, writer, user)` (`cli.py:686`,
  keyword-only `build_app` at `app.py:84`). `MAX_UPLOAD_BYTES = 25 MiB` is a module constant
  (`app.py:56`). `load_repo_config` parses `name`/`kind`/`domains`/`schema_version` and a `curator:` /
  `harvest:` sub-mapping by mapping **named fields** — it does **not** do `RepoConfig(**raw)`, so
  unknown `repo.yaml` keys are *silently ignored, not rejected* (`config.py:116-158`; DATA-MODEL §3
  confirms a forward-looking `curator.limits` / `health` key "neither takes effect nor breaks
  loading"). There is no `web:` block, no `web.yaml`, no `--config` flag.

- **Drag-and-drop multi-file upload.** `upload.html` is a single `<input type="file" id="file"
  name="file">` (`upload.html:13`) with no `multiple` attribute and no drop-zone; the form `hx-post`s
  to `/upload`. The shared `_do_upload` (`app.py:332-418`) handles **exactly one** input with `file` >
  `url` > `text` precedence, enforces `MAX_UPLOAD_BYTES` per input, prepends a deterministic ADR-0020
  provenance header, then calls `handlers.remember(markdown, source=f"web:{user}", domain, tags)` — the
  sole write path → inbox. Both `POST /api/upload` and the HTMX `POST /upload` reuse this one body.

- **Broader file extensions.** `extract()` (`ingest/extractors/base.py:110-168`) routes `url` →
  trafilatura, `.pdf`/`application/pdf` → pdfminer.six, and the six office exts/MIMEs
  (`.docx/.doc/.xlsx/.xls/.pptx/.ppt`, `_OFFICE_EXTS`/`_OFFICE_MIMES`) → markitdown; **everything else
  raises `ValueError("unsupported or ambiguous upload")`** (`base.py:164-168`). So `.txt`, `.md`,
  `.csv`, `.html`, `.json`, `.epub`, images, audio all fall through today. All ingest libs are the lazy
  optional `ingest` extra (`pyproject.toml:27-32`; `markitdown[docx,xlsx,pptx]>=0.1` is **already
  pinned** and MIT, and markitdown already reads html/csv/json/epub).

### The crux
Each remaining ask must stay a **thin face** (ADR-0003) over the **unchanged** inbox write path
(ADR-0020) and the **derived** read seam (invariant 1). The only one with real design weight is
**where settings live and whether they stay non-canonical and tenant-safe**: a settings surface is the
first place a Phase-4 multi-tenant world leaks if it becomes a global mutable the browser can flip. The
graph render is settled (ADR-0021); this ADR only gives its two caps — and the upload limits and the
extension allowlist — a single, per-repo, tenant-safe config home.

## Proposed Decision
1. **The shipped graph's hardcoded caps become `WebConfig`'s first consumers — post-merge.** The graph
   render is Accepted and shipped (ADR-0021); the only #29-remaining graph work is to lift
   `MAX_GRAPH_NODES`/`MAX_GRAPH_DEPTH` (`mcp_server.py:59-60`) into `web.graph.{max_nodes, max_depth}`
   config, defaulting to the current values (2000 / 3). This is a **post-merge follow-up**: do **not**
   churn the in-flight branch with config plumbing mid-flight — that invites conflict and scope creep,
   and the constants already ship as sane soft-cap defaults. After `feat/web-knowledge-graph-viz` merges
   to `main`, #29's settings unit adopts them, giving `WebConfig` a concrete, tested first consumer that
   proves the config path end-to-end. The graph stays a dumb renderer over already-authorized JSON (no
   raw HTML), inheriting the ADR-0019 / ADR-0021 XSS posture; this ADR adds no graph surface.

2. **Web settings are operator-local, non-canonical, per-repo policy.** Reserve an **optional
   top-level `web:` block** in `_kb/repo.yaml` (the established git-ignored operator-policy file,
   DATA-MODEL §3, that already tolerates unknown keys — outside the curated git tree, so invariant 1
   holds: derived operator policy, never canonical knowledge the curator round-trips):
   - `web.graph.{max_nodes, max_depth}` (the ADR-0021 caps' new home)
   - `web.upload.{max_bytes, max_files, allowed_extensions}`
   - `web.features.{graph_enabled}`

   It is parsed by a new `WebConfig` + `load_web_config(layout)` mirroring `load_repo_config`'s tolerant
   parsing (unknown keys silently ignored; a typed mismatch fails CLEARLY as `ConfigError`), and
   resolved **once, per-repo, server-side** in `build_app(*, repo_path, …)` — **never** a global mutable
   the browser can flip across the tenant boundary (invariant 5; mirrors the ADR-0019 §3 server-side
   scope threading, so when Phase-4 auth lands, identity → per-repo `WebConfig` scope threads here
   without a rewrite). Until wired, the keys are silently-ignored-if-unset, so the namespace can be
   reserved now and consumed incrementally with zero breakage.

3. **Multi-upload = N independent ADR-0020 writes with a best-effort batch receipt.** Drag-and-drop of
   *k* files becomes *k* independent `extract → remember` calls (reuse `_do_upload`'s core), each an
   immutable inbox event with its own provenance header and `content_sha256`. **Independent
   best-effort, not atomic**: one corrupt PDF does not block the other captures. A new
   `POST /api/upload-batch` (+ HTMX handler) returns a **batch receipt** — a list of per-file
   `{filename, id, queued, inbox_depth | error}` — additive over the existing `UploadReceipt`
   (`app.py:71-82`). The single-writer transaction (ADR-0002) and the inbox-is-append-only invariant
   (invariant 3) are untouched. Caps: per-file `MAX_UPLOAD_BYTES` **and** new per-batch
   `web.upload.max_files` / total-bytes guards, so a drag-drop of 500 files cannot DOS the localhost
   host.

4. **Broaden extensions = a cheap, OSS-clean tier now; OCR/audio deferred.** Add a **zero-dependency
   `.txt`/`.md` passthrough** extractor (bytes → markdown + `content_sha256`, no lib) and **widen the
   `extract()` dispatcher** to markitdown-backed `.html`/`.csv`/`.json`/`.epub` — **no new dependency**
   (markitdown is already pinned, MIT, and already reads those formats). The accepted set is gated by
   `web.upload.allowed_extensions`. Image-OCR and audio/video transcription are **explicitly deferred**
   behind their own opt-in extra with separate ADR-0005 license vetting (some OCR/transcription wrappers
   pull copyleft) and resource-exhaustion guards — not rushed in here. `extract()` stays the single
   documented extensibility seam; each new format is a localized, testable change. **Error-mapping
   note:** the dispatcher itself raises `ValueError` for an unsupported/ambiguous shape, which `_extract`
   maps to **400** (`app.py:421-429`); a wrapped extractor failure on malformed input raises
   `ExtractorError` → **422**, and a missing `ingest` dependency raises `ExtractorUnavailable` → **503**.
   Broadening a format therefore means: (a) extend the dispatch table so the new ext no longer hits the
   `ValueError`/400 floor, and (b) ensure the new extractor maps malformed input to `ExtractorError`/422.

5. **The graph and search (#26) share one read seam.** The graph reads `Wiki.list_notes` O(N) per
   request, exactly like the dashboard health derivation; #26 (search performance, which **implements
   the Accepted [ADR-0012](0012-deterministic-query-ranking.md)** derived-cache, not a new ADR) reads
   the same seam. A future #26 cache that speeds `list_notes` must transparently speed the graph too —
   both consume the **same** seam (a future `_kb/index/` derived cache), never a divergent path, so
   determinism (ADR-0009 / ADR-0012) and ranking stay shared.

### Open sub-decisions (this ADR is Proposed; these are the choices reviewers should ratify)
- **D-a — Where settings live.** Options: (A) an optional `web:` block in `_kb/repo.yaml` parsed by
  `load_web_config`, CLI/env overrides for ops (host/port already exist); (B) a new dedicated
  `_kb/web.yaml`; (C) env + expanded CLI flags only. **Recommendation: A (hybrid).** repo.yaml is the
  established git-ignored per-repo policy file and already silently tolerates unknown keys, so the
  namespace is reservable now and wirable later with zero breakage; per-repo resolution keeps it
  tenant-safe for Phase 4. A separate `web.yaml` fragments config for no benefit; flags-only cannot
  express an `allowed_extensions` list ergonomically.
- **D-b — How broad to go on extensions.** Options: (A) cheap tier only (`.txt`/`.md` passthrough +
  widen markitdown to html/csv/json/epub, no new dep); (B) cheap tier + image OCR (new heavy `ocr`
  extra); (C) everything markitdown supports, auto-sniffed; (D) cheap tier now, OCR/audio explicitly
  deferred behind their own extra. **Recommendation: D.** Most breadth is free and OSS-clean today;
  OCR/transcription deserve a dedicated extra + their own license vetting + resource guards, and
  deferring them bounds the untrusted-input attack-surface increase.
- **D-c — Multi-upload partial-failure contract.** Options: (A) independent best-effort + per-file
  batch receipt; (B) atomic all-or-nothing; (C) sequential-stop-at-first-failure.
  **Recommendation: A.** Each capture is already independent and idempotent (content-hash event keys,
  invariant 3), so all-or-nothing has no integrity benefit and would throw away good captures; the
  receipt is the additive ADR-0020-faithful shape, plus the new per-batch caps.
- **D-d — Who moves the graph constants into config.** Options: (A) #29 owns it as a post-merge
  follow-up; (B) re-open the merged graph code to read config from the start; (C) leave hardcoded
  indefinitely. **Recommendation: A.** Plumbing config into the actively-developed parallel branch
  invites conflict; letting #29's settings unit adopt the two constants after merge gives `WebConfig` a
  concrete, tested first consumer that proves the config path end-to-end.

## Alternatives considered
- **Re-litigate the graph render / §7 here.** Rejected — moot. ADR-0021 already adopted the graph as
  the first firing of the ADR-0019 §7(b) escape hatch (one vendored MIT force-graph UMD lib, no Node);
  the route, page, vendored JS, and per-note link are shipped. This ADR references that decision and
  only homes its two caps in config; re-deciding render is out of scope.
- **A dedicated `_kb/web.yaml`.** Rejected (D-a). It fragments operator config for no benefit when
  `repo.yaml` already carries `curator.*` / `harvest.*` per-repo policy and tolerates new keys.
- **A module-level / global mutable settings object.** Rejected. It is the precise mechanism by which
  one repo's web policy would bleed across the Phase-4 tenant boundary (invariant 5); settings must be
  resolved per `repo_path` in `build_app`, read-at-startup.
- **Atomic multi-upload (reject the whole batch on any failure).** Rejected (D-c) — no integrity
  benefit given idempotent per-event writes, and it discards good captures over one bad file.
- **Eagerly add image-OCR / audio transcription now.** Rejected (D-b) — heavy, sometimes
  copyleft-adjacent deps and a resource-exhaustion vector that deserve their own extra + ADR-0005
  vetting, not a rushed inclusion.

## Consequences
- **+** All remaining asks stay thin-face: settings are operator-local policy, multi-upload is N audited
  inbox writes, broader extensions are pure extractors behind the lazy `ingest` extra. Zero new write
  surface; single-writer (ADR-0002) and append-only inbox (invariant 3) intact.
- **+** `WebConfig` is the keystone: the shipped graph's caps (ADR-0021), upload limits, and the
  extension allowlist all consume one consistent, per-repo, tenant-safe config — and it pre-positions
  the seam where Phase-4 auth (ADR-0006) later threads per-tenant scope, so the settings surface is not
  a later rewrite.
- **+** Most of the extension breadth is free and OSS-clean (markitdown already pinned); the `extract()`
  dispatcher remains the single, documented, testable extensibility point.
- **−** Multi-upload + more formats **grow the untrusted-input attack surface**: decompression bombs on
  zip-based office/epub (markitdown decompresses untrusted bytes with no size cap, already flagged
  deferred at `office.py:56-57`), SVG/HTML-in-markdown XSS, SSRF via the url extractor
  (`extractors/url.py:29-30` and the module note at `base.py:20-24`, a Phase-4 concern), and batch DOS.
  Phase 3 is localhost single-user so these are footgun bounds today, but per-file **and** per-batch
  caps are mandatory and a hardening pass closes the surface; the design must not bake in assumptions
  that block Phase-4 hardening.
- **−** Eventual-consistency confusion is amplified: N drag-dropped files all return `queued` but none
  are searchable until the next curator run (DESIGN §2.2). The batch-receipt copy must keep that framing
  and show per-file success/error, not just an aggregate count, so failures aren't silently swallowed.
- **Deferred (not solved, documented):** image-OCR and audio/video transcription extractors (own opt-in
  extra + ADR-0005 vetting + resource guards); curator-side `raw/` binary staging on multi-upload (still
  ADR-0020 deferred); the #26 `_kb/index/` derived cache the graph would also consume (implements
  ADR-0012); Phase-4 auth threading identity → per-repo `WebConfig` scope. The reworded harvest
  round-trip and team/multi-tenant boundaries remain ADR-0007/0017 / connector-ADR
  ([ADR-0023](0023-context-harvester-connectors.md)) concerns, untouched here.

## Implementation sketch
1. **Design-only (this ADR + docs).** Reserve the `web:` block in DATA-MODEL §3; update DESIGN §5.2
   (multi-file upload → independent inbox captures; extensible pure-extractor set; the `/graph`
   read-only derived view links to ADR-0021); annotate ROADMAP as Phase-3.5 invariant-neutral
   incremental work with OCR/binary-staging deferred; document `extract()` as the extension seam.
2. **`WebConfig` + `load_web_config(layout)`** in `config.py`, tolerant-parse like `load_repo_config`
   (unknown keys ignored; typed mismatch → `ConfigError`). `build_app(*, repo_path, …)` resolves it once
   at startup and passes caps/limits/allowed-extensions/features into the routes; CLI/env keep
   host/port. Per-`repo_path`, never global. **(keystone — blocks the rest.)**
3. **Graph caps → config** (after `feat/web-knowledge-graph-viz` merges): refactor
   `MAX_GRAPH_NODES`/`MAX_GRAPH_DEPTH` (`mcp_server.py:59-60`) to read `web.graph.{max_nodes, max_depth}`
   (first `WebConfig` consumer; defaults preserve 2000 / 3). No new graph surface — the route, page,
   vendored lib, and per-note link already shipped under ADR-0021.
4. **Multi-upload** (parallel to 5): `upload.html` gains `multiple` + a drop-zone (HTMX/vanilla JS, no
   Node); `POST /api/upload-batch` + HTMX handler loop files through the existing extract→remember core,
   returning the batch receipt; enforce per-file + per-batch caps from `WebConfig`.
5. **Broaden extensions** (parallel to 4): zero-dep `.txt`/`.md` passthrough extractor; widen `extract()`
   routing to markitdown-backed `.html`/`.csv`/`.json`/`.epub` (no new dep); gate via
   `web.upload.allowed_extensions`; tests per format incl. malformed input (assert `ExtractorError`/422),
   and assert a now-supported ext no longer hits the dispatcher's `ValueError`/400 floor.
6. **Untrusted-input hardening pass** (after 4+5): decompression-bomb size caps for zip-based
   office/epub (close the `office.py:56-57` deferral), SVG/HTML sanitization in passthrough markdown
   (markdown-it `html=False` already covers render-time), per-batch caps; a guard test each. Localhost
   footgun bounds now, Phase-4-ready structure.
