# ADR-0012 — Deterministic Query Ranking for core.wiki

**Status:** Accepted · 2026-06-13

Refines ADR-0009 (does not supersede); depends on ADR-0001, ADR-0002, ADR-0006, ADR-0008. Amended by ADR-0014 D3 (body graph edges are now standard markdown links `[Title](relative.md)`; frontmatter `related:`/`children:` stay `[[basename]]`).
**Amends DATA-MODEL §9** (see §12).
**AMENDED (append-only) — the §1/§3 frozen tokenizer/stopword/field contract and the §8 `fm_enabled` phase flag are REVISED by the addendum at the end of this file** (*Addendum — CJK bigram tokenizer + aliases/summary fields + fm live (#56, landed 2026-07-24)*); the §1/§3/§8 prose below is retained verbatim for history.

## Context
ADR-0009 froze the *contract* of `core.wiki.query` — a deterministic, model-free `QueryResult` of
ordered `SearchHit` values with an explicit `not_found` — but left the *ranking* unspecified. Without
a pinned scoring function, citation order and the `not_found` floor cannot be tested independently of
a model, and two implementers (a local open-weight curator and a Python author) cannot agree on a
single number. This ADR fixes the algorithm.

An earlier draft asserted that SQLite FTS5 and a pure-Python scorer would both produce byte-identical
`SearchHit`s via "one scoring function". That parity claim is provably false: FTS5's native `bm25()` uses
document-level saturation over column-weighted length with signed Robertson/Sparck-Jones IDF and no
`max(0,·)` clamp, which diverges from the spec's per-field saturation BM25F with clamped IDF on identical
input (illustratively, lex ≈ 0.4994 vs 0.6570), and the two IDFs differ for *every*
term-document-frequency. FTS5's `bm25()` also cannot accept an external IDF. So no accelerator
can be a co-equal scorer; the backend-parity-contradiction class is removed by demoting FTS5 and ripgrep
to **candidate-prefilter-only** accelerators that select which notes to score but never how.

The forces: invariant #1 (markdown is canonical; indexes are rebuildable), invariant #2 (only the
single curator writes `_kb/`, including any derived index — the read path must not), invariant #4
(an OSS, stdlib-only path with no required external service or AGPL dependency), invariant #5 (per-repo
isolation), and ADR-0009's honesty requirement (`not_found` when evidence is absent). We also resolve a
self-contradiction in the draft about the frontmatter `status:` boost: the schema TEMPLATE
(`templates/kb_schema.md`) now EXISTS, but the curator schema EMITTER and the LINT enforcement of the
`status:` enum are not yet wired, so the boost cannot ship enabled in Phase-1a (`fm_enabled=false` until
the emitter + LINT land in Phase-1b).

The design must serve a *personal MVP* of a few hundred notes with zero infra, while being a clean base
for later scale. A strict graph-first gate (orphan notes unscorable) loses to a union frontier (graph
seeds ∪ lexical matches): silently returning `not_found` for a correct-but-unlinked note is worse than
the precision cost, because curator linking discipline is not yet mature. Honesty is preserved instead
by a mandatory lexical-evidence gate.

## Decision
Adopt the **LEXICAL-UNION FRONTIER with a SINGLE PURE-PYTHON SCORER**. The pure-Python scorer is the
ONLY component that computes a `SearchHit` field — it is the oracle and the test reference. FTS5 and
ripgrep are optional candidate prefilters that may only over-approximate the candidate set; the scorer
rescans and scores those candidates, so an accelerator cannot change output, only speed. The test suite
asserts byte-identical `SearchHit` lists across {FTS5 on/off} × {ripgrep on/off} precisely because the
SAME oracle scores in every case.

### 0. Contract (frozen — do not change without a new ADR)

```
core.wiki.query(scope: list[RepoRef], question: str, *, limit: int = 20) -> QueryResult
```

Deterministic, model-free retrieval. Auth filters `scope` to readable repos/domains BEFORE this runs
(ARCHITECTURE §3.5); ranking is intra-`scope` only. Tenant isolation (invariant #5): per-repo index,
graph, and file scans only. Output is **exactly** the DATA-MODEL §9 shape, nothing added:

```
QueryResult = { query: str, status: "ok" | "not_found", hits: list[SearchHit] }
SearchHit   = { repo: str, path: str, anchor: str, line: int,
                excerpt: str, match_reason: "linked-theme"|"heading"|"lexical", score: float }
```

| Field | Type | Source |
|---|---|---|
| `repo` | str | the scoped repo name that owns the note (e.g. `personal`) |
| `path` | str | repo-relative POSIX path |
| `anchor` | str | heading/wikilink slug per §7; **may be `""`** for a pre-heading lexical match (widening of DATA-MODEL §9, see §12) |
| `line` | int | 1-based line number per §7 |
| `excerpt` | str | per §7, whitespace-collapsed, ≤ 240 chars, deterministic |
| `match_reason` | enum | `linked-theme`\|`heading`\|`lexical`, exactly one, §6 precedence |
| `score` | float | combined SCORE ∈ [0,1], 6 decimals (per-component pre-rounding, §6.1) |

**At most ONE `SearchHit` per `(repo, path)`** — the frontier and lexical candidate sets are unioned
and de-duplicated by `(repo, path)`; a note matching via multiple reasons keeps the single
highest-precedence reason (§6).

### 0a. Backend roles (authoritative — read before §2/§4/§9)

| Backend | Role | Required? | Computes any SearchHit field? |
|---|---|---|---|
| **pure-Python** (`re` + file scan) | THE SCORER & FIELD EXTRACTOR — the oracle and test reference | Yes (always present) | **YES — all of them** |
| SQLite FTS5 (CPython-bundled) | candidate PREFILTER only (`MATCH` → set of paths) | No | **NO** |
| ripgrep (external binary) | candidate-file PREFILTER only (`*.md` files containing a token) | No | **NO** |

A prefilter may only OVER-approximate the candidate set (return a superset of, or all of, the notes that
could score > 0); the pure-Python scorer then rescans and scores those candidates, so a prefilter cannot
change output, only speed. **No accelerator ever computes `lex`, `struct`, `fm`, `score`, `match_reason`,
`anchor`, `line`, or `excerpt`.** Consequence: the prior claim that FTS5's native `bm25()` would yield
identical scores to the per-field-saturation BM25F is FALSE (FTS5 uses document-level saturation over
column-weighted length with signed RSJ IDF and no clamp; illustratively, on identical input lex ≈ 0.4994
vs 0.6570, and the two IDFs diverge for every term-doc-frequency). This ADR therefore
never uses FTS5's `bm25()`/`snippet()` to populate the contract; it only uses FTS5 `MATCH` to prefilter.

### 1. Configuration — `_kb/repo.yaml` → `query:` (FROZEN DEFAULTS, normative for tests)

```yaml
query:
  k1: 1.2                 # BM25 term-freq saturation (pure-Python scorer)
  b: 0.75                 # BM25 length normalization (pure-Python scorer)
  field_weights:          # BM25F per-field weights (pure-Python scorer)
    title: 3.0
    tags: 2.5
    headings: 2.0
    body: 1.0
  pivot: 1.5              # lexical normalization pivot: lex = s/(s+pivot)
  w_lex: 0.65            # combined-score weight on lexical
  w_struct: 0.35         # combined-score weight on structural
  struct_alpha: 0.7      # structural: weight on MOC-distance term
  struct_beta: 0.3       # structural: weight on in-degree term
  fm_enabled: false       # PHASE-1a: false (fm=0 for all). PHASE-1b flips true once schema emitter+LINT ship.
  fm_boost_promote: 0.10  # status == active (only when fm_enabled)
  fm_boost_demote: -0.15  # status == deprecated (only when fm_enabled)
  fm_demote_stale: -0.15  # OPTIONAL: applies to read-time-DERIVED 'stale' (NOT a status value); off by default
  status_default: neutral # status-less / {stub, contested} note → fm=0 (NEVER an implicit promote)
  max_hops: 2            # BFS depth from MOC/index seeds
  floor: 0.18            # not_found threshold on the combined [0,1] score
  max_hits: 20           # max SearchHits returned (also the default `limit`)
  excerpt_max_chars: 240
  excerpt_window_tokens: 32
  stopwords: [a, an, and, are, as, at, be, by, for, from, how, in, is, it, of,
              on, or, that, the, to, was, what, when, where, which, who, why, with]
```

> `k1`/`b` are PINNED for the pure-Python scorer (they are no longer constrained to match FTS5, since
> FTS5 is prefilter-only). Tokenizer, stopwords, and stemming change scores, so they are part of the
> contract (pinned, not magic constants).

### 2. Note model & the optional derived index (rebuildable from markdown — invariant #1)

For each `*.md` under `wiki/` plus root `index.md`, the pure-Python parser builds a `Note`:

| Attr | Definition |
|---|---|
| `path` | repo-relative POSIX path |
| `basename` | filename without `.md`; globally unique per repo (DATA-MODEL §10); only root file is `index` |
| `is_moc` | path matches `wiki/<domain>/<domain>-moc.md` |
| `is_index` | path == `index.md` |
| **title** | first H1 (`# ...`) text; else frontmatter `title:`; else basename with `-`→space |
| **headings** | text of all H2–H6 ATX headings, each retaining (text, slug, 1-based line) |
| **tags** | frontmatter `tags:` (kebab-case, schema-validated upstream) |
| **body** | all prose with frontmatter, code fences, and `[[...]]`/`[](...)` link *punctuation* stripped to the visible label |
| `status` | frontmatter `status:` normalized to the §8 enum (default `neutral`-for-ranking if absent/unknown) |
| `outlinks` | ordered, de-duplicated basename targets resolved via the unique-basename map; unresolved targets recorded but contribute no edge. Per ADR-0014 D3, body graph edges are standard markdown links `[Title](relative.md)` (`.md` target path → basename via the unique-basename map; image `![]()` and non-`.md` links excluded); frontmatter `related:`/`children:` remain `[[basename]]`. The shipped resolver is `src/agora_kb/schema/notes.py` `body_link_basenames`/`child_bullets`/`wikilinks`. |
| `indeg` | in-degree over resolved `outlinks`, after all notes parsed |
| `heading_lines` | ordered `(level, text, slug, line)` for anchor resolution |
| `field_tokens` | the §3 `tokenize()` output per field — THIS feeds BM25F and (if used) the FTS5 prefilter index |

Headings appear in BOTH `headings` and `body` (intentional double-count). Frontmatter parsing uses a
stdlib YAML-subset reader for `key: scalar` / `key: [list]`; unknown keys ignored.

**Derived READER cache (git-ignored, NEVER canonical, fully rebuildable from markdown at the curated commit):**
- Location: `_kb/index/<repo>.notes.json` (parsed-note + graph cache) and OPTIONALLY
  `_kb/index/<repo>.fts.sqlite` (FTS5 prefilter) + `_kb/index/<repo>.meta.json`
  `{path: {content_sha256, indeg, status, d_moc?}, curated_commit: <sha>}`.
- **Invalidation key = curated-commit-SHA + per-file `content_sha256` ONLY.** `(mtime,size)` is a
  fast-path HINT to decide whether to recompute a file's sha; it is NEVER a correctness gate. Two clones
  of the same commit (which git does not give identical mtimes) therefore rebuild byte-identical caches
  (invariant #1). An incremental refresh that cannot prove `content_sha256` equality MUST re-read the file.
- **WHO WRITES IT (honors invariant #2 / ADR-0008 / contract C10):** `_kb/index/` is the READER's
  rebuildable cache; it is **NEVER written by the sandboxed curator backend** (the model never touches it,
  and it is not in the ADR-0008 INGEST allowlist). It is materialized by deterministic worker/reader code
  ONLY, as a cache of the committed markdown at the curated commit — it holds NO canonical knowledge and
  is reproducible byte-for-byte from that commit. The READ path (`core.wiki.query`, invoked by `kb_query`
  from any reader) opens the cache **read-only**. If the cache is absent, its `meta.json curated_commit`
  ≠ the current curated commit (stale), or the file is locked/unreadable, the reader silently falls back
  to a full pure-Python scan of the committed markdown — it never blocks, never errors.

> **As-built (issue #26, 2026-07-05 — an implementation of this Accepted ADR; NO new ADR).** The cache
> shipped with three invariant-preserving refinements of the sketch above:
> - **Single folded artifact.** `_kb/index/<repo>.notes.json` carries its meta INLINE
>   (`{cache_schema_version, curated_commit, notes: {<path>: {sha, note}}}`) rather than a separate
>   `.meta.json` — one atomic read/write, no cross-file race.
> - **`source_digest`, not `content_sha256`, is the per-file gate.** The §2 "content_sha256" is
>   realized as a sha256 over the EXACT tolerant-decoded parser input: the shared
>   `core.hashing.content_sha256` normalizes NFC/CRLF/trailing-whitespace for DEDUP and would wrongly
>   equate two byte-divergent notes that parse differently (e.g. CRLF changes `raw_lines`), so a cache
>   keyed on it could serve a stale parse. `source_digest` is a strict refinement (equal digest ⇒
>   identical parser input ⇒ identical parse), preserving the byte-identical-vs-scan contract. A
>   `CACHE_SCHEMA_VERSION` integer in the payload invalidates the whole cache on any
>   parser/tokenizer/serialization change (the cached `field_tokens`/`headings`/`outlinks` are derived).
> - **`indeg`/`d_moc` are NOT persisted** — recomputed globally at load, so a partial cache is
>   byte-identical to a full scan (a stored global degree would be a stale-global bug).
>
> - **Candidate prefilter = the EXACT in-memory inverted index** over the already-loaded
>   `field_tokens` (free — the notes are parsed for scoring anyway). It is exact even for tokens the
>   tokenizer SYNTHESIZES (abutting link labels → one token, kebab-tag splits) — precisely the case a
>   raw-bytes accelerator under-approximates. The §9 candidate accelerators (**FTS5**, **ripgrep**)
>   are **DEFERRED to a future load-avoiding reader** (issue #28 scale): while every note is loaded
>   for repo-wide IDF, an over-approximating external accelerator offers no candidate-loading saving
>   and only adds parity risk (ripgrep can't see synthesized tokens; a committed-snapshot FTS DB
>   under-approximates a diverged working tree). So `_kb/repo.yaml` `index:` has only the `enabled`
>   kill-switch — no accelerator flag — at v1.
>
> Writers: deterministic worker-finalize (synced-only, best-effort, swallow+log — mirrors ADR-0017 §7)
> + `agora index build`; the read path opens the cache strictly read-only and full-scans on any
> miss/stale/corrupt/schema-bump. Byte-identical output vs the uncached scan is regression-tested.
> Surfaced by `agora index status` + an `agora doctor` line.

**Optional FTS5 prefilter** (probe once at startup with `CREATE VIRTUAL TABLE … fts5(…)` in `:memory:`;
on `OperationalError` set fts5=unavailable). **The FTS5 table is populated with the Python `tokenize()`
output, NOT raw markdown**, so tokenizer divergence is impossible:

```sql
CREATE VIRTUAL TABLE notes USING fts5(
  norm_tokens,            -- space-joined output of the §3 Python tokenizer (already lowercased, stopworded)
  path UNINDEXED, repo UNINDEXED,
  tokenize = 'ascii'      -- whitespace split over already-normalized ASCII tokens; no porter, no diacritics folding
);
```

FTS5 here is used SOLELY as `SELECT path FROM notes WHERE notes MATCH :expr` to obtain a candidate path
set; its `bm25()`/`snippet()` are NOT used. Rebuild deterministically: enumerate `wiki/**/*.md` +
`index.md` in **sorted path order**, store each note's `norm_tokens`, then
`INSERT INTO notes(notes) VALUES('rebuild');`. A missing/corrupt cache → readers fall back to a
pure-Python scan and the cache is rebuilt by deterministic code (never by the sandboxed backend).

### 3. Tokenizer (single shared function — the ONLY tokenizer; FTS5 indexes its output)

```python
def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS]
```

No C stemmer, no diacritics folding — `[a-z0-9]+` simply drops non-ASCII/accented characters and
digits-in-other-scripts. Because the FTS5 prefilter is fed `tokenize()` output (not raw markdown) over
`tokenize='ascii'`, the FTS5 and pure-Python token vocabularies are IDENTICAL by construction (no
`porter`, no `unicode61 remove_diacritics`). Kebab tags expand deterministically: a tag `single-writer`
contributes the full token `single-writer` AND its split parts `single`, `writer` to the `tags` field.
If stemming is ever wanted it is a vendored pure-Python Porter step applied INSIDE `tokenize()` (so both
the scorer and the FTS5-indexed token stream change together — parity preserved).

### 4. Pipeline (7 deterministic stages — all scoring by the pure-Python oracle)

#### Stage 1 — SEED (navigation roots; Karpathy LLM-wiki model)
Parse root `index.md` and EVERY in-scope `<domain>-moc.md`. Each link they contain to a note basename is a
frontier seed:
- targets of a `<domain>-moc.md` → `d_moc = 0`;
- targets of root `index.md` → `d_moc = 1`;
- the MOC notes themselves → `d_moc = 0`; `index.md` itself → `d_moc = 1`.

Record the MOC link label (visible link text, else surrounding list-item text) for linked-theme detection
(§6). Basename resolves unambiguously (DATA-MODEL §10); unresolvable targets are logged and skipped.
Domain in-scope filter: a `<domain>-moc.md` is seeded iff its `<domain>` is in `repo.yaml domains:` and
the repo is in `scope`. If the question contains a token exactly matching a `<domain>` kebab name, seed
only that domain's MOC; otherwise seed ALL in-scope MOCs.

> Note (ADR-0014 D3): MOC and index body graph edges are standard markdown links `[Title](relative.md)`
> (basename recovered from the `.md` target path via the unique-basename map; image `![]()` and non-`.md`
> links excluded). Frontmatter `related:`/`children:` remain `[[basename]]`. The shipped resolver is
> `src/agora_kb/schema/notes.py` `body_link_basenames`/`child_bullets`/`wikilinks`.

#### Stage 2 — FRONTIER (graph walk ∪ lexical candidates)
BFS-expand the body link graph from all seeds, following resolved `outlinks`, up to `max_hops = 2`,
recording each note's **MIN** hop distance as `d_moc`. `d_moc` is a property of minimum distance and is
therefore INDEPENDENT of BFS expansion order.

```
candidate_set = (frontier notes) ∪ (notes whose field_tokens contain ≥1 q_token)
```
de-duplicated by `(repo, path)`. A lexically-matched note unreachable within `max_hops` gets
`d_moc = max_hops + 1 (= 3)`. `indeg_norm(note) = indeg(note) / max(1, max_indeg_in_scope) ∈ [0,1]`.

**Multi-seed attribution:** when a note is a `d_moc=0` child of MULTIPLE MOC seeds, the `moc_link_label`
used by the §6 linked-theme test is the UNION of ALL such seeds' labels (token-set union). This is
order-independent, so eligibility cannot flip on BFS order. (Where any per-note seed attribution other
than the label-union were ever needed, the tie-break is the ASCII-smallest seed basename — but the union
rule means §6 never needs it.)

#### Stage 3 — TOKENIZE the question
`q_tokens = tokenize(question)`. If empty after stopword filtering → return `status=not_found`, `hits=[]`
immediately.

#### Stage 4 — LEXICAL score (BM25F; the pure-Python oracle)
Per candidate, over fields f ∈ {title, tags, headings, body} with weights w_f and **repo-wide** per-field
`avgdl_f` (mean field length over ALL in-scope notes):

```
ftd(t)  = Σ_f  w_f · tf(t,f) / (1 − b + b · len_f / avgdl_f)        # if avgdl_f == 0, denom = 1
idf(t)  = max(0, ln( 1 + (N − n(t) + 0.5) / (n(t) + 0.5) ))         # N = #in-scope notes; n(t) = repo-wide doc freq (term in ANY field)
raw(D)  = Σ_{t ∈ set(q_tokens), ftd>0}  idf(t) · ftd(t)·(k1+1) / (ftd(t) + k1)
lex(D)  = raw(D) / (raw(D) + pivot)   if raw(D) > 0 else 0.0         # ∈ [0,1), monotone in raw, pivot=1.5
```

IDF is REPO-WIDE (not frontier-scoped) so a note's lexical score is stable regardless of frontier
composition. This formula is the ONLY definition of `lex`; FTS5 does not produce it.

**FTS5 prefilter expr (optional, candidate selection only):** `expr = OR of phrase-quoted distinct
q_tokens` (`"a" OR "b"` — phrase-quoting defangs FTS5 operator injection);
`SELECT path FROM notes WHERE notes MATCH :expr` yields candidate paths that are then scored by the
formula above. ripgrep prefilter: `rg -F -S -g '*.md' --files-with-matches` per token, unioned — files
only, NO offsets.

#### Stage 5 — STRUCTURAL score (degree surrogate, no iterative PageRank)
```
struct(D) = struct_alpha · 1/(1 + d_moc(D))  +  struct_beta · indeg_norm(D)     # α=0.7, β=0.3
            with 1/(1 + 3) = 0.25 for the worst (unreached) bucket
```
O(V+E), exactly reproducible. A fixed-iteration PageRank (d=0.85) is a clean future swap behind this same
`struct()` interface — OUT of MVP.

#### Stage 6 — COMBINE + LEXICAL-EVIDENCE GATE (mandatory)
```
fm    = 0.0                                              if not fm_enabled (PHASE-1a)
        +fm_boost_promote if status == active                          (PHASE-1b)
        +fm_boost_demote  if status == deprecated                      (PHASE-1b)
        0.0  otherwise (status ∈ {stub, contested}, or absent/unknown → status_default=neutral)
        # OPTIONAL: a read-time-DERIVED 'stale' flag (NOT a status value; from link graph + run_date)
        #           MAY additionally apply fm_demote_stale; off by default in Phase-1b.
SCORE = clamp01( w_lex·lex(D) + w_struct·struct(D) + fm )      # clamp01(x)=max(0,min(1,x)); see §6.1 for rounding
```

**LEXICAL-EVIDENCE GATE** — a candidate is ELIGIBLE only if:
```
lex(D) > 0
  OR
( d_moc(D) == 0  AND  set(q_tokens) ∩ tokenize(moc_link_label_union + " " + D.tags_text + " " + D.title) ≠ ∅ )
```
Candidates passing NEITHER are DROPPED before the §5/§8 floor test, regardless of structural score.
(Illustrated by the §10 fixture: `inbox-design` is a `d_moc=0` child with `struct=1.0`, yet `lex=0` and
`q∩theme=∅`, so it is DROPPED — without the gate it would score ~0.35 under fm=0 / ~0.45 under fm=on and
falsely clear the 0.18 floor.)

##### 6.1 Float determinism
Honest stance: byte-identical output is GUARANTEED **across runs on a fixed CPython build**; across
heterogeneous libm/CPU it is best-effort and made robust by quantization. To minimize ULP-boundary flips:
compute `lex`, `struct`, `fm` independently, **round EACH component to 6 decimals (`round(x, 6)`), THEN
combine in the fixed order `w_lex*lex_r + w_struct*struct_r + fm_r`, clamp, and round the result to 6
decimals.** Summations iterate in the spec's fixed orders (sorted terms, sorted fields, sorted candidates)
so IEEE-754 addition order is deterministic. `math.log`/division are the only libm-dependent ops; the
(repo, path) tie-break (§7) absorbs any residual sub-ULP equality. The illustrative §10 numbers are a
worked example only; the AUTHORITATIVE reference vector is produced by a checked-in fixture test when
`core.wiki` is implemented (run on the test suite's fixed CPython build) — it is not a byte-pinned
cross-machine guarantee.

#### Stage 7 — ORDER (total order, no ties survive)
```
sort key = ( -SCORE,
             reason_rank[match_reason],     # linked-theme=0 < heading=1 < lexical=2
             -lex,
             -indeg,
             (repo, path) )                  # repo-relative POSIX path, ASCII ascending — ABSOLUTE tie-break
```
Truncate to `limit` (default `max_hits = 20`).

### 5. STATUS (not_found) — honest when evidence is absent (ADR-0009)
`status = ok` iff ≥1 ELIGIBLE candidate (passed §6 gate) survives AND `best_score ≥ floor (0.18)`.
Otherwise `status = not_found`, `hits = []`. **not_found gates:** (a) empty `q_tokens` (incl. an
all-stopword question); (b) zero eligible candidates after the lexical-evidence gate; (c) best eligible
SCORE < floor; (d) **EMPTY REPO** (no `index.md`, no `wiki/` notes) → `not_found`, `hits=[]`, no error —
`core.wiki.query` is callable on a freshly-initialized Phase-1 repo before any notes exist. NEVER
synthesize, never emit a hit below floor or one that failed the gate.

### 6. match_reason (frozen enum) + anchor — evidence precedence, exactly one per hit
**match_reason, anchor, line, and excerpt are ALWAYS computed by the deterministic pure-Python extractor
from the parsed markdown, for EVERY backend.** (FTS5 cannot expose per-column/per-line match info and has
no line numbers; ripgrep offsets are byte offsets that do not map to token windows — neither participates
in field extraction.) Evaluate in order; the FIRST that holds is the reason and fixes the anchor:

| # | reason | condition | anchor | line | excerpt |
|---|---|---|---|---|---|
| 1 | **linked-theme** | `d_moc==0` AND q_tokens intersect `title`/`tags`/MOC link-label union (the §6 linked-theme branch) | slug of note H1/title; or `[[basename#heading]]` target slug if the seed link carried `#anchor` | 1-based line of that H1 (or 1) | the title line, or the MOC's one-line catalog summary for this note |
| 2 | **heading** | not linked-theme; highest-idf matched term lands in `title` or a `headings` line | slug of that matched heading | 1-based line of that heading | heading text + first non-empty body line beneath it |
| 3 | **lexical** | match only in `body`/`tags` | slug of nearest ENCLOSING heading above the first matched body line (`""` if none precedes) | 1-based line of first matched body token | ±`excerpt_window_tokens` window around first matched term |

"Highest-idf matched term" tie-break: field-weight order (title>tags>headings>body), then first-occurrence
line. Frontier-only candidates with no lexical line default to anchor = H1 slug, line = 1.

**Slug rule (deterministic, GitHub/Obsidian-compatible):**
```python
slug = re.sub(r"[^a-z0-9]+", "-", heading.strip().lower()).strip("-")
```
Identical-slug collisions within one note get `-1`, `-2`, … by 1-based heading order.

### 7. anchor / line / excerpt extraction (deterministic, pure-Python stdlib only)
- **line** ALWAYS 1-based (`enumerate(lines, start=1)`), computed by the pure-Python extractor by
  locating the matched term in the parsed note body.
- **excerpt:** single-line, whitespace-collapsed, ≤ `excerpt_max_chars` (240). Body matches: a stdlib
  ±`excerpt_window_tokens` (32) token window around the first matched body token. Heading/linked-theme:
  the heading/title line plus the following non-blank line. **FTS5 `snippet()` is NOT used for the
  contract excerpt** (its token windowing differs from the stdlib window and would diverge); a face
  (MCP/web) MAY re-highlight for display, but the `SearchHit.excerpt` is always the stdlib extractor's
  output. ripgrep byte offsets are NOT fed into excerpt/anchor/line.

### 8. Wiki-note `status:` enum — CONSUMED HERE (schema authority is ADR-0010; this ADR maps it to fm)
The schema TEMPLATE (`templates/kb_schema.md`) now EXISTS, but the curator schema EMITTER and the LINT
enforcement of the `status:` enum are not yet wired. When they land (Phase-1b), the emitter MUST surface
the enum in each repo's `AGENTS.md`/`SCHEMA.md` and `_templates/`, and curator LINT MUST enforce it. The
frozen status vocabulary (ADR-0010 is the authority) is exactly four values:

```
status: active | stub | contested | deprecated      # ranking default when absent/unknown: NEUTRAL (fm=0)
```
Ranking maps these to three fm buckets:
- **promote (+0.10, Phase-1b only):** `active`.
- **neutral (fm=0):** `stub`, `contested` (also the default for an absent/unknown status).
- **demote (−0.15, Phase-1b only):** `deprecated`.

`orphan` and `stale` are NOT status values — they are DERIVED at read/dashboard time from the link graph
plus `run_date`, never persisted in frontmatter. A derived `stale` flag MAY OPTIONALLY apply a separate
`fm_demote_stale` (−0.15); it is off by default and is independent of the status enum.

**Phase sequencing (resolves the fm self-contradiction):**
- **Phase-1a (ranking ships first):** `fm_enabled=false`; `fm=0` for ALL notes. NORMATIVE vector = §10
  fm=0 column.
- **Phase-1b (schema emitter + curator LINT land):** flip `fm_enabled=true`. A status-less note still
  gets `fm=0` (`status_default=neutral`) — un-triaged notes are never auto-promoted above
  curator-reviewed ones.

### 9. Optional accelerators (PREFILTER ONLY — must NOT change output; see §0a)
| Accelerator | Role | Required? | Notes |
|---|---|---|---|
| SQLite FTS5 | candidate prefilter (`MATCH` → path set) | No (CPython-bundled) | indexed over Python-`tokenize()` output with `tokenize='ascii'` (no porter/diacritics) so vocab matches exactly; `bm25()`/`snippet()` UNUSED; cache under `_kb/index/` is the READER's rebuildable cache (rebuildable from markdown at the curated commit, invariant #1), never written by the sandboxed curator backend; read path read-only, falls back to scan if absent/stale/locked |
| ripgrep | candidate-file prefilter | No (external binary, MIT/Unlicense) | `rg --version ≥ 13.0` (for stable `--files-with-matches`); `rg -F -S -g '*.md' --files-with-matches <token>` per token, unioned; NO byte offsets, NO json, NO line numbers fed into extraction; absence → pure-Python file walk, identical candidates |

The pure-Python path is the source of truth; the test suite asserts byte-identical `SearchHit` lists
across {FTS5 on/off} × {ripgrep on/off}.

### 10. Worked example (ILLUSTRATIVE — the SCORING FORMULA in §4–§7 is normative; these NUMBERS are not)
Repo `personal`. The FIVE notes below illustrate the fixture corpus to be committed under
`tests/fixtures/personal/`. The specific avgdl/df/IDF/lex/SCORE numbers below are an ILLUSTRATIVE worked
example: they show how the §4–§7 formula composes, but they are NOT a byte-pinned cross-machine
guarantee and have NOT been machine-verified here. The AUTHORITATIVE reference vector is produced by a
checked-in fixture test (a regeneration script + assertion run on the test suite's fixed CPython build)
when `core.wiki` is implemented.

**`index.md`:**
```
# personal

- [[ai-tech-moc]]
```
**`wiki/ai-tech/ai-tech-moc.md`:**
```
---
status: active
---
# AI Tech

- [[curator-concurrency]] — how the single-writer curator serializes writes
- [[inbox-design]] — append-only per-writer inbox
```
**`wiki/ai-tech/themes/curator-concurrency.md`:**
```
---
status: active
tags: [single-writer, concurrency]
---
# Curator Concurrency

The curator acquires a per-repo flock on curator.lock so exactly one writer advances the curated branch. Concurrency control is enforced by compare-and-swap on the branch ref.
```
**`wiki/ai-tech/themes/inbox-design.md`:**
```
---
status: active
tags: [inbox, append-only]
---
# Inbox Design

The inbox is append-only and per-writer namespaced. Items are never edited or reordered.
```
**`wiki/personal/roadmap.md`:**
```
---
status: deprecated
tags: [roadmap]
---
# Roadmap

Phase 1 is the personal MVP milestone.
```

Derived facts (recomputed, not asserted): `N=5`; `avgdl={title:1.6, tags:1.8, headings:0.0, body:12.2}`;
`df(curator)=2 idf=0.8755`, `df(concurrency)=2 idf=0.8755`, `df(control)=1 idf=1.3863`; `max_indeg=1`,
`indeg={ai-tech-moc:1, curator-concurrency:1, inbox-design:1, index:0, roadmap:0}`.

Query: **"curator concurrency control"** → `q_tokens=[curator, concurrency, control]`.

(Illustrative numbers — recomputed by the fixture test, not pinned here.)

| note | raw BM25F | lex | d_moc | indeg_norm | struct | status | SCORE (fm=0, Phase-1a) | SCORE (fm=on, Phase-1b) | reason | gate |
|---|---|---|---|---|---|---|---|---|---|---|
| `curator-concurrency` | ~4.0810 | ~0.7312 | 0 | 1.0 | 1.0 | active | ~0.8253 | ~0.9253 (+0.10) | linked-theme | PASS (lex>0) |
| `inbox-design` | 0.0 | 0.0 | 0 | 1.0 | 1.0 | active | — | — | — | **DROP** (lex=0; q∩theme {append,append-only,design,inbox,only,per,writer} = ∅) |
| `roadmap` | 0.0 | 0.0 | 3 | 0.0 | 0.175 | deprecated | — | — | — | **DROP** (lex=0, d_moc≠0) |

`status = ok`; single hit `curator-concurrency`, anchor `curator-concurrency`, line 1, with an
illustrative score of ~0.8253 in Phase-1a / ~0.9253 in Phase-1b (the exact value is recomputed by the
fixture test). (Note: this AMENDS DATA-MODEL §9's prior example of anchor `single-writer`/line 42/score
0.91 to these fixture-derived values — see §12.)

Unrelated query **"quantum biology photosynthesis"**: every candidate has `lex=0` and no `d_moc=0` theme
overlap → ALL DROPPED by the lexical-evidence gate → `status = not_found` (`inbox-design`'s
structural-only ~0.35 is correctly suppressed, never reaching the 0.18 floor).

### 11. What is explicitly OUT
No embeddings/vector search, no RRF, no LLM rerank. No time-decay/recency term. **Cross-repo merge is
NON-NORMATIVE / DEFERRED for Phase-1** (target is the single local `personal` repo): each repo would
score independently (repo-isolated IDF, invariant #5) and merge under the §7 global order, but because
per-repo IDF makes scores only loosely comparable, the cross-repo ordering is left out of the frozen
contract until multi-tenancy (when a documented cross-repo normalization can be decided). The §0 contract
only pins what Phase-1 exercises (single-repo). Iterative PageRank, Porter stemming, and time-decay are
clean future swaps behind their respective interfaces. Optional synthesis (ADR-0009) may ONLY consume
returned hits.

### 12. Cross-doc amendments & files this design drives (Phase 1)
- **AMENDS DATA-MODEL §9:** (a) update the canonical `SearchHit` example to `anchor: curator-concurrency`,
  `line: 1`, with an illustrative `score` (~0.8253 Phase-1a / ~0.9253 Phase-1b; the exact value is the
  fixture test's output, not byte-pinned in prose); (b) add a one-line note that `anchor` MAY be `""` for
  a pre-heading lexical match. Cross-reference ADR-0012 §10.
- **DATA-MODEL §4 (state.json):** acknowledge `_kb/index/` as a recognized rebuildable, git-ignored
  READER cache (rebuildable from markdown at the curated commit), never written by the sandboxed curator
  backend (contract C10).
- `src/agora_kb/core/wiki.py` — `query()`, `Note`/cache, graph build, the single `score_note()` oracle,
  ordering, anchor/excerpt extraction, FTS5/rg PREFILTER detection + fallback (no scoring in accelerators).
- `src/agora_kb/core/repo.py` — scope→repo resolution, layout walking, `_kb/index/` cache location,
  read-only cache open + stale/locked fallback.
- `src/agora_kb/core/wiki.py` (reader-side) — deterministic rebuild of the `_kb/index/` reader cache from
  the committed markdown at the curated commit (NEVER the sandboxed curator backend; not in the ADR-0008
  INGEST allowlist).
- `src/agora_kb/schema/` — Phase-1b: emit the `status:` + `tags:` frontmatter contract (§8, status enum
  `active|stub|contested|deprecated` per ADR-0010) into the KB schema template; curator LINT enforces it.
- `_kb/repo.yaml` `query:` block — the §1 constants.
- `tests/fixtures/personal/` — the §10 corpus + a checked-in regeneration script that PRODUCES the
  authoritative reference vectors (fm=0 and fm=on) on the test suite's fixed CPython build; `tests/` —
  backend-agnostic ranking tests (FTS5 on/off × rg on/off identical) pinned to §1 defaults and to those
  generated vectors, plus an empty-repo not_found test and an input-file-order-permutation property test
  (asserts identical `SearchHit` output, catching order-dependent IDF/avgdl accumulation).

## Consequences
- **+** Retrieval is fully deterministic and model-free: the §4–§7 scoring formula is normative, and a
  checked-in fixture test (when `core.wiki` is implemented) produces the authoritative reference vector
  from the §10 corpus, so two implementers can independently reproduce it — satisfying ADR-0009's
  testable-without-a-model requirement. (The §10 numbers themselves are illustrative, not byte-pinned.)
- **+** The single pure-Python scorer is the sole oracle (stdlib-only, no required external service, no
  AGPL — invariant #4); FTS5/ripgrep are prefilter-only, so byte-identical output across {FTS5 on/off} ×
  {ripgrep on/off} is true by construction, eliminating the backend-parity contradiction.
- **+** `_kb/index/` is a READER's rebuildable cache (rebuildable from markdown at the curated commit),
  never written by the sandboxed curator backend and not in the ADR-0008 INGEST allowlist; the read path
  is read-only and falls back to a pure-Python scan — honoring invariant #2, ADR-0008, and contract C10,
  with no multi-writer race across concurrent `kb_query` callers.
- **(status, 2026-07-05 — issue #26 SHIPPED):** the §2/§9 derived READER cache
  (`_kb/index/<repo>.notes.json`, meta folded IN per the §2 as-built note) was specified here but not
  built in Phase-1 (which ships the pure-Python scan only). Issue #26 now IMPLEMENTS that cache so
  query stays fast as the KB grows. **This is an implementation of this already-Accepted ADR — it
  needs NO new ADR**, and it does not relax any
  invariant here: the cache stays git-ignored, NEVER canonical, and fully rebuildable from the curated
  commit (invariant #1, keyed on curated-commit-SHA + a per-file `source_digest` — the §2 as-built
  refinement of `content_sha256`); it is materialized by deterministic worker/reader code ONLY —
  NEVER by the sandboxed curator backend and NOT in the ADR-0008 INGEST allowlist (invariant #2 /
  contract C10); the candidate prefilter is the EXACT in-memory inverted index over `tokenize()`
  output, and the pure-Python BM25F oracle (§0a/§4) stays the sole source of every `SearchHit` field.
  The §9 **FTS5/ripgrep candidate accelerators are DEFERRED** (see the §2 as-built note): while every
  note is loaded for repo-wide IDF the exact inverted index is free, so an external over-approximating
  accelerator adds parity risk for no candidate-loading saving — they belong to a load-avoiding reader
  (issue #28). Semantic/vector search (ROADMAP "explicitly deferred") likewise stays DEFERRED until
  corporate volume (issue #28) proves lexical + navigation insufficient — NOT in scope for #26.
- **+** Cache invalidation keyed on curated-commit-SHA + per-file `content_sha256` only means any clone of
  the same commit rebuilds an identical cache (invariant #1); mtime/size are a non-correctness fast-path
  hint that can never cause a silent cache/markdown mismatch.
- **+** The union frontier preserves recall for correct-but-unlinked notes, while the mandatory
  lexical-evidence gate preserves honesty (by construction it drops the `d_moc=0`/`struct=1.0`/`lex=0`
  false positive `inbox-design` and returns `not_found` for unrelated queries).
- **+** Repo-wide IDF and a degree-based structural surrogate (no iterative PageRank) make scores stable
  and O(V+E)-reproducible; the order-permutation property test guards against order-dependent
  IDF/avgdl accumulation.
- **+** Phase sequencing (fm=0 in Phase-1a, flipped on in Phase-1b once the schema emitter + LINT ship)
  resolves the prior self-contradiction and never silently promotes un-triaged notes.
- **−** FTS5/ripgrep accelerate only candidate selection, not scoring, so the pure-Python rescan cost is
  always paid; negligible at Phase-1 scale (hundreds of notes), still O(candidates) at larger scale.
- **−** A conceptually-central note sharing zero surface tokens AND no theme-label overlap is excluded by
  the gate — acceptable for a model-free retriever; semantic recall is deferred to a later optional
  vector layer.
- **−** Two reference vectors (the fm=0 Phase-1a column and the fm=on Phase-1b column) must be kept in
  sync with the §1 constants — mitigated by deriving both from one committed fixture via the checked-in
  regeneration script + fixture test (the authoritative source of the numbers).
- **−** Byte-identical scores are guaranteed only across runs on a fixed CPython build; cross-machine
  reproducibility is best-effort, contained by per-component 6-dp rounding and the absolute (repo, path)
  tie-break. The test suite must pin the exact interpreter.
- **−** Cross-repo (multi-tenant) ranking is deferred: per-repo IDF makes scores only loosely comparable,
  so a cross-repo normalization decision is required before multi-repo querying is a contract.

## Addendum — CJK bigram tokenizer + aliases/summary fields + fm live (#56, landed 2026-07-24)

**This addendum REVISES the §1/§3 frozen contract and flips the §8 phase flag.** The §1/§3/§8
prose above is retained verbatim (append-only convention, cf. ADR-0022/0023 addenda); where they
conflict, THIS section is normative. Issue **#56** found the defect class: `tokenize()`'s
`[a-z0-9]+` alphabet silently drops every CJK codepoint, so a Korean question tokenizes to `[]`
(→ instant `not_found` via gate (a)) and a Korean note's four `field_tokens` are all empty
(→ `lex = 0` forever, unreachable by ANY query) — while ADR-0027 decision 5 documents this very
KB as Korean-heavy and ships a CJK range table for gold budgeting. The system counted Korean but
could not search it. Two already-authored frontmatter fields the ranker never consumed (`aliases:`
— schema-annotated "(powers QUERY)", L1-15-unique, yet score contribution 0; `summary:` — the
note's densest sentence, stripped with the frontmatter before body tokenization) are folded into
the same revision, as is the long-satisfied §8 precondition.

### A1. Tokenizer (revises §3): NFC + CJK character bigrams

```python
def tokenize(text):
    norm = NFC(text).lower()
    for run in scan(norm):              # [a-z0-9]+ runs OR maximal CJK-range runs, in order
        if ascii_run(run):  yield run                       # verbatim §3 behavior
        else:               yield from bigrams(run)         # per letter/number subrun; len 1 → unigram
    # stopword filter unchanged (English list; cannot collide with a CJK bigram)
```

- **NFC first.** macOS/NFD-decomposed Hangul jamo compose to the syllables a query carries; for
  pure-ASCII text NFC is the identity, so **English/digit tokenization is byte-invariant** (the
  §10 fixture scores are unchanged by the tokenizer itself).
- **CJK runs → character bigrams; a length-1 (sub)run → unigram.** Why bigrams: Korean is
  agglutinative — a particle suffix (`큐레이터가`) would make whole-word tokens unmatchable by the
  stem query (`큐레이터`), while bigrams overlap 3/4 ({큐레,레이,이터,터가} ∩ {큐레,레이,이터}),
  giving graded partial matching under unmodified BM25F. Bigrams need no dictionary, no language
  detection, and are the standard CJK fallback in lexical engines.
- **The CJK range table is SHARED, not duplicated**: extracted verbatim from `core/gold.py`
  (ADR-0027 decision 5) into `core/cjk.py`; both gold's token estimator and this tokenizer import
  it, so the two CJK definitions can never drift. Gold's estimator behavior (and pack bytes) is
  unchanged.
- **Within a CJK run, codepoints of Unicode category `P*`/`S*`/`Z*`** (CJK punctuation `。`,
  fullwidth symbols, the ideographic space — all inside the shared ranges) **split the run** like
  any non-token character, so a bigram never bridges a sentence boundary. Determinism stance
  unchanged from §6.1: pure function of codepoints under the test suite's pinned
  CPython/unicodedata build.
- **Morphological analyzers (mecab-ko, kiwipiepy) REJECTED.** (a) Their output is a function of a
  *versioned dictionary/model*: any dictionary revision re-tokenizes the same bytes differently,
  which breaks the byte-identical rebuild contract (invariant #1, §6.1) exactly the way the
  ADR-0022 addendum rejected transliteration tables for slugs — a nondeterminism class the
  deterministic reader cannot carry. (b) Licensing is non-permissive for the core path (mecab-ko
  lineage is GPL/LGPL-encumbered; invariant #4 forbids copyleft in the core), and a required
  native/external analyzer would also violate the stdlib-only oracle posture (§0a). Recall lost vs
  a true morphological index is accepted; bigrams recover the practical bulk of it.
- **Korean stopwords: none.** Josa/eomi surface inside bigrams and are diluted by IDF; a curated
  Korean stopword list would itself be a versioned artifact (same objection as above).
- **Residual — single-syllable queries vs multi-syllable runs.** A length-1 run emits a unigram
  and a length-≥2 run emits only bigrams, so the query `밤` can never match a note carrying only
  `밤나무` (tokens `밤나`/`나무`) and vice versa. Emitting boundary unigrams as well would need a
  cache re-bump and a recall/precision decision — deferred; stated here so the gap is a contract,
  not a surprise.
- **Residual — fullwidth alphanumerics (U+FF01–FF5E).** NFC (not NFKC) is applied, so fullwidth
  Latin/digits are NOT folded to ASCII; they sit inside the shared CJK ranges and are category
  `L*`/`N*`, so they stay in the run and bigram (`ＡＩ` → token `ａｉ`), never matching the
  halfwidth query `ai` — and a fullwidth-Latin↔Hangul boundary is bridged by a bigram (`ｉ에`)
  where the halfwidth boundary splits. No worse than pre-#56 (such text was entirely invisible);
  a deterministic FF01–FF5E fold in the scan is a possible follow-up (needs a cache re-bump).

### A2. Scoring fields (revises §1/§2): `aliases` 3.0, `summary` 2.0

```yaml
  field_weights:          # BM25F per-field weights (pure-Python scorer) — REVISED by #56
    title: 3.0
    aliases: 3.0          # NEW: alternate titles (L1-15 globally unique) — title-equivalent
    tags: 2.5
    headings: 2.0
    summary: 2.0          # NEW: the note's densest sentence, frontmatter-only
    body: 1.0
```

- Both are parsed from the SAME frontmatter pass the note loader already runs (no new parser);
  tolerant shapes mirror `tags:` (list of scalars or a bare scalar; non-string → absent).
- Both join `field_tokens`, the per-field `avgdl`, repo-wide `df`/IDF, and the exact
  inverted-index candidate prefilter. Field iteration order is fixed as
  `(title, aliases, tags, headings, summary, body)` (§6.1 float-order determinism); notes without
  these keys contribute empty fields, so **an aliases/summary-free corpus scores byte-identically
  to the pre-#56 scorer modulo A3**.
- The §6 tie-break "field-weight order" becomes `title>aliases>tags>headings>summary>body`
  (equal-weight pairs: title before aliases, headings before summary). Reason 2 (`heading`) still
  fires only for title/headings; a top-idf match landing in aliases/summary is frontmatter
  evidence → reason 3 (`lexical`) with the H1-fallback anchor, unless the note is a `d_moc==0`
  linked theme.
- **The §6 gate's theme-token set now includes `aliases` tokens** (and so does the reason-1
  linked-theme set): an alias IS an alternate title, so a `d_moc==0` theme reachable by its alias
  behaves exactly like one reachable by its title.
- **`aliases` is exempt from length normalization (per-field `b`: aliases 0.0, all others 0.75 —
  `FIELD_B`).** aliases is a sparse OPTIONAL field: with corpus-wide avgdl the BM25F denominator
  `1 − b + b·len_f/avgdl_f` explodes for exactly the notes that HAVE aliases (a 42-note corpus
  with one 2-token alias list gives avgdl≈0.05 → denom≈33 → effective weight ≈0.09, losing to a
  single passing body mention), which would falsify the "title-equivalent 3.0" claim above. An
  alias list is a tiny controlled set of alternate titles (L1-15-unique) — document-length
  normalization carries no signal there, so `b=0` (denom=1). Scoring-only: the cache stores
  tokens, so no schema re-bump; alias-free corpora are byte-identical either way.
- **Curator write-path caveat (#57).** `normalize_plan` slugifies every model-proposed alias and
  SKIPS un-slugifiable (e.g. pure-Korean) ones, so a curator-managed wiki cannot currently carry
  a Korean alias — Korean-alias retrieval is a defensive capability for hand-edited or
  externally-generated repos. #56 gives Korean aliases real search value, which weakens half of
  #57's skip rationale ("zero search/link value"); revisiting that skip (e.g. separating the
  closed link-token grammar from search-only aliases) is an explicit follow-up.

### A3. `fm_enabled` flipped TRUE (executes §8 Phase-1b — no contract change)

The §8 preconditions ("schema emitter + curator LINT land in Phase-1b") shipped long ago (the
schema emitter and the L1 status-enum LINT are live since Phase 1). `FM_ENABLED = True` now:
`active` +0.10, `deprecated` −0.15, all else 0. Without this, a `deprecated` note ties with the
`active` note that superseded it — an ordering bug once #43 lifecycle transitions arrive. The
normative reference column is now the §10 "fm=on" column.

### A4. Cache + tests

- `CACHE_SCHEMA_VERSION` 1 → 2 (`core/index_cache.py`): the cached `field_tokens` are derived by
  the tokenizer/parser this addendum changes, so every v1 cache is invalidated whole (the read
  path silently full-scans and the next deterministic build rewrites it — §2 as-built mechanics
  unchanged).
- No byte-pinned reference vectors existed to regenerate (the §10 numbers were always
  illustrative; `tests/core/test_wiki.py` pins ordering/fields, not floats) — the #56 test
  additions cover: Korean probes (plain Hangul, mixed-script `AI에이전트`, particle variation
  `큐레이터가`→`큐레이터`, alias-mediated Korean→English-slug retrieval, a mandatory Korean
  `not_found` negative), English rank-identity regression (the pre-existing ordering suite,
  unchanged), active-beats-deprecated fm demotion, v1-cache invalidation, and double-index
  byte-identity over a Korean corpus.
- MCP (`kb_query`) and the web face both route through the one `Wiki.query` (verified; no face
  change).
- **Anchor scope for pure-CJK titles/headings.** The §6 slug rule stays ASCII-only, so a
  title/heading with no `[a-z0-9]` material has anchor `""` — the documented no-deep-link value
  (`SearchHit.anchor` MAY be `""`). The parser no longer fabricates dedup suffixes for empty
  slugs (previously a note's second empty-slug heading shipped anchor `"-1"`, valid under no
  slugger). A Unicode-preserving (GitHub-style) slug is deferred: `_Heading.slug` serializes into
  the reader cache (another `CACHE_SCHEMA_VERSION` bump) and must first be reconciled with the
  web face's markdown-it-py heading-id rule.
- Korean-only matched lines return the whole line whitespace-collapsed (≤240 chars) as the
  excerpt; the ±32-token excerpt window stays ASCII-token-based (§7 scope, unchanged contract).
