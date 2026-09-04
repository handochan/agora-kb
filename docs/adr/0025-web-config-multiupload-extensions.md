# ADR-0025 — Web-face operator config, multi-upload, broadened extensions

**Status:** Accepted · 2026-07-05 (Step-0 ratified, #36; shipped in PR #33) · Proposed 2026-06-24 · covers issue [#29]
**AMENDED (append-only) — [ADR-0041](0041-stratum-kind-first-layout.md) (Proposed, KB wiki schema 2), ONE clause:** the upload/remember `domain` argument (`handlers.remember(markdown, source, domain, tags)`) keeps its meaning as an INBOX field and a `raw/` shard key, and becomes a **`subjects:` seed** rather than a wiki path segment. Everything else in this ADR — `web:` operator config, multi-upload + batch receipt, broadened extractor extensions, the SSRF guard, the zip-bomb cap, `web:<user>` identity — is layout-free and UNCHANGED. The prose below is retained verbatim for history.

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

> [!NOTE]
> **Ratified Accepted in the 2026-07-05 Step-0 session (#36) and shipped in PR #33.** This ADR is
> append-only; the "Proposed"-era headings and wording below are preserved verbatim as
> authoring-time (2026-06-24) language.

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

> [!NOTE]
> **Accepted at Step-0 (#36), 2026-07-05** — the parenthetical below is authoring-time wording
> (append-only ADR). The recommendations carried here were ratified and are the shipped behavior
> (PR #33; see also the #66/#53/#67 appendices below).

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

## Appendix (2026-07-25, append-only) — rec D `.epub` lands + the two upload-hardening guards (issues #66/#53)

Nothing above changes. The "untrusted-input hardening pass" (implementation-sketch item 6) and the
deferred `.epub` half of rec D shipped together as the **network-exposure gate** for team
deployments (the prerequisites the #68 deployment guide points at):

- **SSRF guard (#66) — extractor layer.** `extract_url` now performs the fetch itself (stdlib
  `http.client`, trafilatura keeps the content extraction, fed the fetched bytes): http/https only;
  every resolved address of **every hop** must be public — RFC1918, loopback (127/8, `::1`),
  link-local (169.254/16 incl. the 169.254.169.254 metadata endpoint, `fe80::/10`), unique-local
  (`fc00::/7`), unspecified, multicast, reserved, and any other non-global range refuse, and ONE
  private A record fails the whole set (fail closed). The TCP connection is **pinned to the
  validated IP** while `Host`/SNI/certificate checks keep the original hostname (DNS-rebinding
  defence); redirects are followed manually (≤5) and re-validated per hop; the body is capped at
  25 MiB. Blocked → `ExtractorError` → the face's existing 422. `extract_url(...,
  allow_private=True)` is the local-caller opt-out seam (`extract()` forwards it); the web face
  never sets it. (No CLI command fetches URLs today — `agora import` is vault-only — so the
  `--allow-private-urls` flag lands with whatever CLI URL surface arrives first.)
- **Operator switch — `web.upload.url_enabled`** (default `true`): `false` refuses url capture with
  403 **before any resolve/connect**. This is the switch a team deployment can flip; the extractor
  guard above stays on regardless.
- **Zip decompression-bomb cap (#53) — extractor layer.** `office.py` guards zip-magic bytes
  (docx/xlsx/pptx/epub) via stdlib `zipfile` in two layers **before** markitdown decompresses
  anything: (1) a cheap DECLARED-total pre-check rejects honest bombs without decompressing, and
  (2) because declared sizes are attacker-forgeable smaller than the real payload, each member is
  streamed through a size-capped reader that measures the ACTUAL decompressed length and aborts the
  moment the running total crosses the cap. Exceeding `web.upload.max_uncompressed_bytes` (default
  250 MiB = 10× the 25 MiB compressed per-file cap, under a 256 MiB ceiling) → `ExtractorError` →
  422 (an archive's expansion is a content property, distinct from the face's 413 wire-size
  tolerance). Layer (2) is load-bearing: `ZipExtFile` truncates its *returned* bytes to the declared
  size but only *after* `zlib` has already expanded the chunk in memory (up to 1 GiB per read-all),
  so the declared-size pre-check alone does not bound an under-declared entry — measuring the true
  decompressed length is what closes the forged-smaller case.
- **rec D `.epub`** — routed to the markitdown path (`_MARKITDOWN_EXTS` +
  `application/epub+zip`); **no new dependency** (markitdown's epub converter is core), same bomb
  guard applies. Image OCR / audio transcription remain deferred on rec D's own terms.

## Appendix (2026-07-25, append-only) — `web.identity.trusted_header`: reverse-proxy identity threading (issue #67)

Nothing above changes. The Phase-4-auth seam `build_app(user=...)` reserved now accepts a
**minimal proxy-delegation slice** so a team deployment stops collapsing every contributor into
one process-wide `web:local` stamp (provenance is the basis of promotion judgment and audit —
#55 decisions 7–9):

- **`web.identity.trusted_header`** (default `None` = OFF): the name of the request header an
  **authenticating reverse proxy** (basic auth, SSO, …) injects with the logged-in username
  (e.g. `X-Remote-User`). When set, each WRITE request (`POST /api/upload`,
  `POST /api/upload-batch`, the HTMX `POST /upload`) resolves its user from that header and stamps
  `source = web:<header value>`. Read routes are untouched — identity only matters where
  provenance is stamped.
- **Opt-in by construction.** With the default `None`, *no request header ever influences
  identity* — naming the header **is** the operator's declaration of the proxy trust boundary. A
  client-forgeable header must never steer provenance by default: a directly exposed web port
  would let anyone spoof any teammate with `curl -H`. A supplied non-string header name fails
  **loud** (`ConfigError`, incl. the YAML-1.1 `no`→False trap) — a security opt-in silently
  reading as "off" would have the operator believing per-user identity is on while everything
  stamps `web:local`.
- **Trust boundary (the #68 guide's contract).** The proxy MUST (1) authenticate every request,
  (2) **force-set** the header from the authenticated principal, and (3) **strip/override** any
  client-supplied value of the same header. With `trusted_header` set, the web port must be
  reachable **only** through the proxy (loopback bind or network isolation) — direct exposure
  makes the header forgeable and provenance worthless. Snippets: `deploy/README.md`.
- **Present-but-invalid value → 400, never a silent fallback.** A header that exists but carries
  an empty/garbage value (outside the conservative token `[A-Za-z0-9][A-Za-z0-9._@-]*`, ≤128
  chars — strictly narrower than the inbox model's `web:.+` source rule, so an accepted value can
  never be rejected downstream) is a forgery attempt or a misconfigured proxy. Falling back to
  the process user would *poison the audit trail* (a request that claimed an identity would be
  silently attributed to `web:local`); refusing loudly surfaces the misconfiguration on the first
  upload. Nothing is written on refusal (the batch path refuses before any inbox append).
- **Duplicate identity header → 400.** If the configured header occurs **more than once** on a
  request the write is refused outright (nothing written). An *append*-mode proxy (Apache
  `RequestHeader append`, HAProxy `add-header`) leaves the client's forged copy first, so any
  pick-one rule (first- or last-wins) is a spoofing vector; duplicates are precisely the
  detectable set-vs-append misconfiguration the 400 policy exists to surface.
- **Header *name* is validated at config load, not per request.** `trusted_header` must be an
  RFC 7230 header-name token (`ConfigError` otherwise): a non-latin-1 name (docs-copy-paste
  en-dash `X–Remote–User`) would otherwise 500 every write inside the header lookup, and a
  whitespace-padded name (`"X-Remote-User "`) would match no request ever — a permanent silent
  fallback to `web:local`. Likewise an **unknown `web.identity` sub-key** (the natural
  `trusted-header:` hyphen typo) fails loud — a deliberate exception to the web loader's
  tolerant unknown-key convention, because a typo'd security opt-in silently reading as "off" is
  worse than a crash.
- **Absent header → process `--user` fallback; default config byte-identical.** A personal
  deployment (no `identity:` block) behaves exactly as before — locked by regression test.
- **`web.identity.strip_domain`** (default `false`): truncate an email-form value at the first
  `@` (`alice@example.com` → `alice`) *before* validation, for proxies that forward
  `REMOTE_USER` as a mail address. A bare-domain value (`@example.com`) strips to empty → 400.
- **Audit surface**: the JSON upload receipts carry `identity_source: "header" | "process"` so an
  operator can verify at a glance whether a capture was attributed via the proxy or the fallback.
- **Not auth.** This trusts the proxy wholesale; real authn/authz (tokens, OpenFGA/Forgejo
  delegation) remains ROADMAP Phase 4. This slice only threads an already-authenticated name into
  the existing `web:<user>` source form (DATA-MODEL §1 — unchanged).

## Appendix (2026-07-26, append-only) — `web.security`: Host allowlist + Origin guard (issue #94)

Nothing above changes. A third `WebConfig` sub-block joins `identity` on the **fail-loud** side of
the loader, this time closing the two attacks a browser can mount against an *unauthenticated*
localhost face. The premise being defended is uncomfortable but real: `--host 127.0.0.1` is a
**network** boundary, and a browser walks straight through it — the victim only has to open a
malicious page while `agora web` is running.

- **(A) CSRF → inbox injection.** `POST /api/upload` is a multipart form = a CORS *simple request*,
  so a cross-site auto-submitting `<form>` reaches it with **no preflight**. The attacker cannot
  read the response, but the **write lands**: appended to the append-only inbox (invariant 3),
  curated into `wiki/` on the next run, and **undeletable through product features** (the plan op
  vocabulary has no delete). Worse, the upload path applies no sentinel neutralization, so injected
  text can reach other agents as instructions via `kb_query` / gold packs.
- **(B) DNS rebinding → whole-KB read.** With no `Host` validation, a TTL-0 attacker domain rebound
  to `127.0.0.1` gives the attacker page **same-origin** reads of `/api/notes`, `/api/notes/{path}`,
  `/api/search`, `/api/graph`, `/api/gold/{pack}`, `/metrics`, `/api/dashboard/*` — the entire
  personal KB, and the gold packs are *compressed* summaries, so exfiltration is more efficient than
  raw notes.

- **(C) Clickjacking → framed UI.** A page that iframes `/upload` and lures a click submits from
  the face's *own* origin, so the Origin guard cannot distinguish it from real use.

**Decision — three ASGI-level guards.**

- **`web.security.allowed_hosts`** (default `["localhost", "127.0.0.1"]`) is handed to starlette's
  `TrustedHostMiddleware`: a non-matching `Host` → 400. That is what makes rebinding fail — the
  rebound request still carries the attacker's DNS *name* in `Host`. Entries are bare hostnames
  (exact, or a `*.example.com` subdomain wildcard); the port is never compared because the
  middleware matches on `Host.split(":")[0]`. The middleware is wrapped (`_HostAllowlistMiddleware`)
  for three reasons and no more: the 400 body names the product and the knob instead of upstream's
  bare `Invalid host header` (an operator meeting a proxy 400 cannot otherwise tell proxy from app);
  the `Host` header is lowercased first, since RFC 9110 §4.2.3 makes the host case-insensitive while
  upstream compares `host == pattern` verbatim against loader-lowercased patterns; and
  `www_redirect` is turned **off**, because a 307 preserves method *and* body, so a `www.`-prefixed
  entry would bounce a state-changing request to another URL — scheme taken from the ASGI scope,
  i.e. an http downgrade behind TLS termination — before the Origin guard ever ran. The wrapper's
  pre-check reuses upstream's own matching rule, and upstream stays underneath as the enforcing
  gate, so the two layers cannot disagree.
- **`web.security.require_origin`** (default `false`) hardens the state-changing routes; see the
  absent-Origin decision below.
- **Framing is denied on every response** (`X-Frame-Options: DENY` + CSP `frame-ancestors 'none'`,
  `setdefault` so a route can still opt out). Only `frame-ancestors` is set: a broader CSP would
  have to be reconciled with the vendored htmx/force-graph assets and rendered note bodies, which
  is a separate decision, not a side effect of the framing fix.

**Absent-Origin policy: present-and-mismatched → 403, absent → pass (default), absent → 403 under
`require_origin: true`.** The threat is *browser-mediated* CSRF, and every current browser attaches
`Origin` to a cross-site form POST / `fetch`; refusing **mismatches** therefore already closes the
browser path, and same-origin HTMX POSTs (which do carry `Origin`) are unaffected. Refusing
**absence** by default would instead break this repo's own documented operations — the upload `curl`
procedures in `deploy/README.md` and `docs/DEPLOY-TEAM.md` §2/checklist send no `Origin`, as do
scripted/CI writers generally. (GET health-check `curl`s are *not* affected either way: the Origin
check applies only to state-changing methods.) The **residual risk is stated, not hidden**: a
browser old enough to omit `Origin` on cross-site writes is not covered — a team deployment with no
scripted writers should set `require_origin: true`, and the guide recommends it.

**Comparison baseline: the Origin's `host:port` against the request's OWN `Host` — not the
allowlist, and not the scheme.** The first implementation of this appendix judged the `Origin`
against `allowed_hosts`, on the theory that one operator list is one mental model. Adversarial
review killed that: the allowlist legitimately carries entries that exist for reasons having
nothing to do with write trust, and sharing the list silently promoted every one of them into a
**trusted CSRF origin**. Two proven exploits, both against the configuration this repo's own guide
prints: with `allowed_hosts: [kb.example.com, 127.0.0.1]` (the loopback entry is there for
hub-local health checks and Prometheus) *any* page on *any* team member's laptop —
`http://127.0.0.1:3000` — wrote into the public hub's inbox, and `require_origin: true` did not
close it; with a `*.team.example.com` wildcard, one XSS'd sibling subdomain or one dangling-CNAME
takeover did the same. Judging against the request's own `Host` costs nothing (it is the value
`TrustedHostMiddleware` has *already* validated one layer out, so it is never an arbitrary attacker
string) and both exploits become 403.

The **port is therefore compared** — it is exactly what separates `http://127.0.0.1:3000` from the
face itself, and scheme+host+port *is* the definition of an origin. The **scheme is still not
compared**: a TLS-terminating proxy makes the browser send `https://…` while the app speaks plain
`http`, and comparing it would 403 every proxied deployment. The consequence is a sharpened proxy
contract, now stated in both guides: the proxy must pass the client's `Host` through **verbatim**
(nginx `proxy_set_header Host $http_host`, which unlike `$host` keeps a non-default port; Caddy
does this by default). A proxy that rewrites `Host` no longer "degrades gracefully" — browser
uploads 403 — and the 403 body says exactly that. `Origin: null` (sandboxed iframe / `data:`
document) is a **mismatch**, never "absent"; so is an **empty** `Origin:`, which is present-and-
unusable rather than absent (a truthiness test would have fallen through to `Referer`, making the
policy depend on which other header happened to ride along). **`X-Forwarded-Host` /
`X-Forwarded-Proto` are NOT consulted** — the
#67 trust boundary (a proxy that force-sets and strips a header) is not assumed for headers the
operator never declared; the proxy standard is instead to **preserve the original `Host`**
(`docs/DEPLOY-TEAM.md` §2, where the Caddy and nginx snippets are now unified on it) and to
allow-list the public hostname. Should a future deployment shape force `X-Forwarded-*` trust, that
becomes its own ADR, not a silent default.

**No CORS middleware.** There is no origin we want to *permit* cross-origin access to, and the
browser's default same-origin policy is already the strongest possible answer; adding
`CORSMiddleware` could only widen the surface. Deliberately omitted.

**No CSRF token.** Tokens require a session, and the face has none (no auth, no cookies). A
double-submit token over no session would be theatre. When ADR-0036 lands authenticated sessions,
this decision reopens and this appendix gets updated.

**IPv6 literals are explicitly unsupported.** `TrustedHostMiddleware` matches on
`Host.split(":")[0]`, which mangles `[::1]:8000` into `"["` — no pattern can match it. The
alternatives were to reimplement Host matching (dropping the vetted upstream middleware) or to plant
a synthetic bypass entry in the allowlist; both are worse than declaring the limitation, especially
as `agora web` defaults to `--host 127.0.0.1`, the `deploy/` units hard-code it, and every
documented proxy topology speaks to IPv4 loopback. The posture is made *legible* rather than silent:
the request gets the 400, and an operator who reaches for the obvious fix (`allowed_hosts: ["::1"]`)
gets a `ConfigError` naming the limitation and the workaround. The same load-time validation rejects
ported entries, URLs, malformed wildcards, an empty list, and a bare `*` — the last because it
disables Host validation outright; a security block must not ship a silent kill switch.

**Fail-loud loader, like `web.identity`.** Unknown `web.security` keys raise `ConfigError` (the
`_SECURITY_KEYS` gate), and so does a **non-mapping** `web.security:` — a scalar typo, or the
indentation slip that turns the block into a list. The tolerant `_sub_mapping` helper answers `{}`
for both, which for every other web sub-block only means "use the defaults", but here means the
operator believes a public hostname is allow-listed (or that `Origin` is mandatory) while the
deployment silently runs the permissive default. Same failure as a typo'd `allowed_host:` /
`require-origin:`, reached through a different slip: a security opt-in silently reading as "off" is
worse than a crash.

**Test-suite consequence, recorded because it is a real trap.** All nine `TestClient` construction
sites (eight in `tests/faces/`, one in `tests/curator/test_worker.py`'s Phase-3 exit e2e, which
really does `POST /api/upload`) previously relied on starlette's default `Host: testserver`. They are
pinned to `TestClient(app, base_url="http://127.0.0.1")` instead of adding `testserver` to the
default allowlist: a **production** default must never ship a bypass host to make a test suite pass.

**Not auth.** Everything here is defense in depth *on top of* the unauthenticated premise, and it is
honest about its reach — it closes **browser-mediated** attacks only. Real authn/authz stays
ADR-0036 / Phase 4. Two adjacent gaps are explicitly **not** closed here: the absence of sentinel
neutralization on the upload write path (adversarial text arriving through a *legitimate* upload
still reaches downstream agents verbatim), and the lack of any rollback for an injected item (the
missing DELETE op, #42 / ADR-0031).
