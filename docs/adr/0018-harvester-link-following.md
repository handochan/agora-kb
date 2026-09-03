# ADR-0018 — Harvester file-connector link-following

**Status:** Accepted · 2026-06-20
**AMENDED (append-only) — [ADR-0041](0041-stratum-kind-first-layout.md) (Proposed, KB wiki schema 2):** link-following itself is UNCHANGED — the one-hop rule, the strict link allowlist, the tolerant frontmatter strip, the fan-out cap counting ATTEMPTS, the never-drop rule, and the path-safety containment relative to the SOURCE FILE'S OWN DIRECTORY (which is a source-side property and has nothing to do with the Agora layout). What changes is the same widening recorded on [ADR-0017](0017-harvester-file-connector-mechanics.md): a `file:` connector may now read one IN-REPO path, `wiki/people/**`, and nothing else under `wiki/` or `raw/`. Sibling containment applies inside that subtree exactly as it does outside the repo. The prose below is retained verbatim for history.

Supersedes the "links-not-followed" Phase-2 limitation recorded in
[ADR-0017](0017-harvester-file-connector-mechanics.md) §2 (append-only — ADR-0017 is not rewritten).
Builds on the same connector safety posture (path-safety reuse of `_within` / base-root, candidate
gate as the loop break, the DATA-MODEL §6 cursor) — all UNCHANGED.

## Context
A real `--dry-run` dogfood on the operator's `~/.claude/**/MEMORY.md` confirmed ADR-0017's
noise-risk prediction: those files are a **pointer index** — bullets of the form
`- [Title](slug.md) — summary` whose actual knowledge lives in the sibling `slug.md`. Harvesting only
the bullet (the ADR-0017 v1 behavior) yields thin one-liners (11/13 facts were pointers). Following
the link to the sibling is the fix, but it **widens the read surface from "files matching the glob"
to "files chosen by untrusted file content"**, so it must be done with deliberate safety. An
adversarial design review confirmed a real exploit in the naive draft (base-root containment would
let `projA/MEMORY.md` read `projB/PRIVATE.md`) and corrected several lossy/raising choices.

## Decision
Add **opt-in, per-connector** link-following to `FileConnector` (`follow_links: bool = False`).

1. **Opt-in, default off.** `ConnectorSpec.follow_links` (fail-loud bool in `load_connector_specs`),
   threaded through `build_connectors` → `FileConnector`. Default off preserves the v1 read surface
   and behavior **byte-identically**; an operator with a pointer-index memory turns it on per
   connector (`adapters.yaml`).
2. **Compose, don't replace.** A block with ≥1 *resolvable* local-`.md` link becomes one fact per
   distinct target (source order, deduped within the block): the link markup is replaced by the
   sibling's content, but the bullet's own non-link prose is **preserved** as a blockquote gloss
   above the body (`> {gloss}`) — following never silently drops the operator's inline annotation.
   A block with no resolvable link is kept verbatim (v1).
3. **Strict link allowlist** (`_extract_local_md_links` / `_clean_link_path`): INLINE `[text](dest)`
   only; **excluded** — images (`![..]`), URL schemes, absolute/`~`-rooted paths, a raw `%` (so
   url-encoded traversal stays impossible, not safe-by-accident), backslashes, dotfile final
   components (`.env` masquerade), and non-`.md` targets. A `#fragment`/`?query` is stripped and an
   angle-bracket `<path>` unwrapped before the case-insensitive `.md` test. **Reference-style**
   (`[t][1]`) links are **unsupported** (inline-only; no markdown-parser dependency).
4. **Path safety — source-dir containment.** The sibling resolves relative to the SOURCE FILE's own
   directory and must stay **within that directory subtree** (TIGHTER than the glob base root — this
   closes the cross-project read), reusing the `_within` realpath guard. A **symlinked target is
   rejected** (an in-tree `.md` symlink cannot launder an out-of-intent file). The resolved path must
   be a regular `.md` within `max_file_bytes`. A target that IS a matched index file is **skipped**
   (self-reference). **One hop only** — sibling content is never itself link-scanned (a code
   mechanism, not a convention; no recursion / cycles).
5. **Fan-out cap.** A new per-scan `max_followed` budget (default 256) counts every follow
   **attempt** (not just successes) and is checked **before** `_read_sibling`, so a hostile index of
   broken / escaping / symlinked links cannot amplify filesystem-probe work; excess links fall back
   to the thin bullet with a note.
6. **Tolerant frontmatter strip.** The sibling's leading `---`…`---` fence is dropped **textually**
   (NOT via `core.frontmatter.parse`, which raises on absent/unclosed/malformed/non-mapping YAML and
   would drop a harvestable sibling). The fact title reuses the sibling's own leading H1 when present,
   else synthesizes `# {link_text}`. `fact_key` hashes the **sibling body only** (excluding the title
   + gloss), so the same body reached via different link text collapses on dedup. Every composed fact
   still runs through `_neutralize` + `max_fact_bytes`.
7. **Never-drop fallback.** A link that cannot be safely followed (unresolvable / escapes-source-dir
   / symlink / oversized / non-md / decode-failure / self-reference / empty-body / fan-out-cap) is
   not followed, with a DISTINCT, target-naming note so `--dry-run` shows WHY. The original bullet is
   kept whenever NO link in the block followed **OR any link was unfollowable** — so in a mixed block
   (some links resolve, some don't) the unfollowed pointer survives verbatim alongside the followed
   facts, never silently dropped.
8. **No-op hash folds siblings (D7).** When `follow_links` is on, the whole-source no-op hash
   includes the followed siblings' bytes, so a **sibling-only edit** (index byte-identical) is
   re-harvested on the next scan rather than silently stale. EVERY followed sibling is folded —
   including after the `max_facts` emission cap is reached (reading continues for the hash even when
   fact emission stops) — so a capped scan's hash is still complete. The cost is reading siblings on
   the no-op fast path (read anyway); accepted over the silent-staleness failure mode.

## Consequences
- **+** A pointer-index memory (the OMC `~/.claude` shape) now harvests real knowledge instead of
  thin one-liners, while a flat-prose memory keeps `follow_links` off.
- **+** The read-surface widening is contained: source-dir-subtree only, symlink-rejected,
  `.md`-only, fan-out-capped, one hop — and the candidate gate (`confidence=low`, cannot originate a
  theme) remains the load-bearing loop/pollution break (ADR-0017 §5 unchanged).
- **−** Confidentiality: a sensitive `.md` living in the operator's own memory directory beside a
  `MEMORY.md` is followable if a bullet links it — accepted (it is the operator's own declared,
  opt-in source dir; cross-project reads are blocked).
- **−** Larger prompt-injection surface than a one-line bullet (a whole sibling body enters the
  candidate bundle) — mitigated by `_neutralize` + the DATA-treatment of candidates + `max_fact_bytes`.
- **−** Best-effort link extraction (code-span / nested-bracket edges, `%`-encoded paths) may
  occasionally keep a link thin (with a note) rather than follow it — bounded by the never-drop
  fallback; not a markdown-parser dependency.
- **−** Following erodes the "cheap no-op" slightly (siblings read each scan); the stat-only variant
  is the documented fallback if it ever matters on large corpora.

## Live verification (2026-06-20)
A pointer index `- [Curator](curator.md) — how it works` with a frontmatter'd sibling harvested to a
single fact reusing the sibling's H1, preserving the bullet gloss as a blockquote, frontmatter
stripped; `follow_links: false` stayed the byte-identical thin pointer. Adversarial inputs
(`../../../../etc/passwd.md`, a cross-project `../projB/secret.md`, an in-tree `.md` symlink, a
self-reference to the index, a `%2e%2e` path, a >cap fan-out, a one-level A→B→C chain) were all
refused or bounded as specified; a sibling-only edit re-harvested (D7).
