# ADR-0020 — Web upload write-path: extract-to-inbox now, raw/ binary staging deferred

**Status:** Accepted · 2026-06-21
**AMENDED (append-only) — [ADR-0041](0041-stratum-kind-first-layout.md) (Proposed, KB wiki schema 2) gives this ADR's stated deferral a DESTINATION:** the original bytes land at `raw/_blob/<ab>/<sha256>.<ext>` (+ a `<file>.meta.yaml` sidecar), content-addressed and immutable, written ONLY by the deterministic APPLY pass and admitted ONLY by membership in `raw_writes` with matching bytes. **The ROUTING in this ADR is UNCHANGED** — faces still extract→inbox, and decision 3 (the curator remains the sole `raw/` writer) is kept verbatim. **The INBOX ITEM SHAPE is not, and it cannot be:** `Inbox.write` takes `text: str` plus an optional `raw_ref: str` and NO bytes, and `_materialize_raw_source` writes the event BODY — so an unchanged write path could never deliver a binary, and a destination without a transport is not a channel. ADR-0041 D4.2 adds an OPTIONAL ATTACHMENT to the inbox item, written beside the event in the writer's own append-only namespace with `raw_ref` naming its `raw/_blob/` destination; APPLY reads it at claim time. That is a `docs/DATA-MODEL.md` §1 amendment, recorded in ADR-0041. The prose below is retained verbatim for history.

Depends on ADR-0003 (one core, many faces — a face writes only through `write`→inbox, never storage
directly) and ADR-0002 (CQRS single-writer curator — only the curator writes `raw/`, the wiki, and
indexes). Realizes the **upload** path of the Phase-3 web face (ADR-0019; DESIGN §5.2). **Amends
ADR-0003's** "an upload stores the original in `raw/` and then calls `write`" clause (§Decision,
bullet 4) with the Phase-3 shape below; the single-writer invariant ADR-0003 also states is
preserved unchanged.

## Context
ADR-0003 sketched the upload path as: a face stores the original upload in `raw/`, then calls
`write` like any other capture. Two facts make the literal "the *face* stores the original in
`raw/`" reading wrong for Phase 3:

- **Only the curator may write `raw/`.** ADR-0002 makes the curator the *single writer* of the
  whole curated tree — `wiki/`, indexes, `log.md`, **and `raw/`**. A web face that wrote `raw/`
  itself would be a second writer into the curated tree, exactly the pipeline-bypass race ADR-0003
  exists to prevent. So the *face* cannot be the thing that stores the original.
- **The extractors are pure transforms with no destination side effects.** The Phase-3a ingest
  extractors (`ingest/extractors`, ADR-0004) already turn an upload (url / pdf / office bytes) into
  an `ExtractedDoc{markdown, title, source_url, content_sha256, …}` and deliberately touch nothing
  on the destination side. They produce *markdown + provenance*, not a stored binary.

Storing the **original binary** verbatim in `raw/` (plus the DATA-MODEL §2 / DESIGN §5.2 sha256
re-ingest-drift sidecar) is a real feature, but it requires a **curator-side binary-staging
materialization**: the face would have to hand the bytes to the core, the curator would have to
write them under `raw/<…>` atomically inside its transaction, and the drift sidecar would have to be
maintained against re-uploads. That is a separate integrity change to the single-writer transaction,
not part of shipping browse/search/upload.

## Decision
1. **Phase-3 web upload = extract → `Inbox.write`.** The web face runs `extract(url=… | data=… ,
   filename=…)` to get the `ExtractedDoc`, prepends a **deterministic provenance header** to the
   markdown, and calls `AgoraHandlers.remember(text=<provenanced markdown>, source="web:<user>")` —
   the ordinary inbox write path. No face writes `raw/` (ADR-0002/0003 preserved). For pasted
   `text`, the body is used verbatim under the same header.

2. **Provenance travels in the capture body, not a sidecar.** The header is a small YAML frontmatter
   block (`captured-by`, `extractor`, optional `source-title` / `source-url`, `content-sha256`) with
   a fixed field order and omitted-when-absent fields, so the same upload yields the same bytes. It
   rides the inbox event into the curator, which materializes `raw/` from the capture body — so the
   origin (url/title/extractor/hash) is recoverable from the markdown itself.

3. **The curator remains the sole `raw/` writer.** Materializing `raw/` from the capture body stays
   the curator's job inside its transaction (ADR-0002). The face's only output is an immutable inbox
   event; the integrity boundary (who may write the curated tree) does not move.

4. **Identity is threaded, not enforced (Phase 3).** `source = web:<user>` is stamped from the
   app-level `user` param (the inbox `web:<user>` source form, DATA-MODEL §1). Phase-3 is localhost
   single-user, so there is no authn/authz here; threading the identity now means Phase-4 auth fills
   in `user` from the authenticated session without a write-path change.

5. **A localhost size guard, not an access control.** Uploads over `MAX_UPLOAD_BYTES` (25 MiB) are
   rejected before extraction; extractor failures map to documented HTTP statuses (`ExtractorError`
   → 422, `ExtractorUnavailable` → 503 with the install remedy, unsupported shape → 400). These are
   footgun bounds for a single-user host, not a security control (cf. the SSRF note in
   `ingest/extractors`, a Phase-4 concern).

## Consequences
- **+** Ships Phase-3 upload with **zero new write surface**: the face reuses the audited inbox path
  and the pure extractors; the single-writer transaction is untouched (ADR-0002/0003 intact).
- **+** Provenance is durable and self-contained: it lives in the capture body, so it survives into
  `raw/` when the curator materializes it — no separate sidecar to keep in sync in Phase 3.
- **+** The amendment is honest about ADR-0003: the *intent* (uploads are not special; they go
  through the inbox) is kept; only the "the face stores the original in `raw/`" mechanic is corrected
  to respect single-writer.
- **−** The **original binary is not stored verbatim** in Phase 3 — only its extracted markdown plus
  a content hash. A re-extraction with a newer extractor cannot be diffed against the original bytes,
  and a non-extractable artifact is lost beyond its extracted text. This is a deliberate deferral.
- **−** The drift-detection sidecar (DATA-MODEL §2 / DESIGN §5.2) is absent until binary staging
  lands, so re-ingest drift is not yet detectable.

## Future work (reserved, not implemented)
Designed so each is additive over the inbox event, never a breaking change:
- **Curator-side binary staging**: pass the original bytes through the core to a curator step that
  writes `raw/<event>/<original>` atomically inside the transaction (single-writer preserved), with
  the inbox event referencing it via `raw_ref` (already a field on `InboxItem`).
- **Re-ingest-drift sidecar** (DATA-MODEL §2): persist the upload's `content_sha256` alongside the
  staged binary so a re-upload of the same source is detectable.
- **Phase-4 auth** fills `user`/`writer` from the authenticated session and adds per-upload scope
  (ADR-0006); the write path above does not change.
