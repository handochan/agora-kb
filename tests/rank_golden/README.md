# The ranking golden — gate B for the Stratum layout flip

This directory holds a **model-free, deterministic golden fixture** that pins what
`agora_kb.core.wiki.Wiki.query` returns *today*, over a synthetic 46-note knowledge repo built in
the **v1 wiki layout** (`wiki/<domain>/themes|daily` + `wiki/<domain>/<domain>-moc.md`).

## Why it exists

The Stratum plan (issues [#153](https://github.com/handochan/agora-kb/issues/153) /
[#155](https://github.com/handochan/agora-kb/issues/155), plan note
`.omc/plans/agora-kh-design-judgement-2026-09-03.md`) flips the wiki layout axis from
domain-first to kind-first. That is not a cosmetic move:

```python
def _is_moc_path(path: str) -> bool:
    """True iff `path` matches wiki/<domain>/<domain>-moc.md (ADR-0012 §2)."""
```

`_is_moc_path` reads the MOC **out of the path**. It is what seeds `d_moc` — the structural term —
for the entire corpus, so every note's `struct` (and therefore its combined score, its rank, and
which notes a gold pack picks up under ADR-0027) depends on where files sit. Change the layout and
ranking changes with it, silently.

The original gate B was an `n=24` five-arm search harness. It never existed as code. So gate B is
re-scoped to this: **record the ranking before the flip, so the change can be attributed to the
flip** rather than merely noticed after the fact.

## What is pinned

| file | what |
| --- | --- |
| `corpus.py` | 46 notes over `cooking` / `engineering` / `finance` — content only, no paths |
| `queries.yaml` | 44 probes: the 4 verbatim #146 queries, 30 positives (24 paraphrases — 6 Korean, 2 mixed-script — plus the 6 `p25`–`p30` mechanism probes), 10 negatives (2 Korean) |
| `golden_v1.json` | the record with the ADR-0012 §8 frontmatter boost **on** (the live build mode) |
| `golden_v1_fm_off.json` | the same record with the boost **off** |
| `regen.py` | the only sanctioned producer of both records |
| `test_golden.py` | the gate |
| `queries_dogfood.yaml` | the owner-side eval set for the private KB (see below) |

Each record is `{"header": {...}, "queries": [...]}`; every query carries its `status` and its
**full ordered hit list** with `score` (6 dp), `match_reason`, 1-based `rank`, and the ADR-0012 §7
extraction triple `anchor` / `line` / `excerpt`. A hit is keyed on the note's **basename**, never
its path — a path-keyed record would report every note as "gone, and a new one appeared" the
instant the layout flips, which is precisely the change the fixture has to measure *through*.
`anchor` / `line` / `excerpt` are recorded because they derive from note CONTENT and not from the
path, so they survive the flip unchanged and cost nothing in flip robustness.

Both §8 columns are recorded because the boost can mask a structural change, and they now differ in
ways a structural change would disturb differently: `p26`'s **top hit** is `finance-moc` with the
boost on and the `deprecated` near-duplicate with it off (the −0.15 demotion is the only thing
separating them), `p29` records 13 hits on and 14 off, and the `contested`/stub notes move several
places throughout.

**Nothing here scores anything.** ADR-0012 §0a reserves `lex` / `struct` / `fm` / `score` /
`match_reason` / `anchor` / `line` / `excerpt` to the core oracle; `core/rank_snapshot.py`
transcribes what `Wiki.query` returned and this directory compares transcriptions. `lex` / `struct`
/ `fm` are recorded as `null` because they are not on the frozen `SearchHit` contract — the keys
exist so a future core explain seam can populate them without a schema bump. That absence is worth
naming: **`match_reason` is this fixture's only direct read-out of the structural term.** A `d_moc`
change that crosses a reason boundary shows up as `linked-theme` → `lexical`; one that does not is
visible only as an unattributed `score` delta. If stronger attribution is ever wanted, the §0a-clean
route is an opt-in `Wiki.query(..., explain=True)` seam in core plus an ADR addendum — `_hit_record`
already reads the three fields with `getattr`, so it would populate with no change here.

**Determinism.** No model, no network, no clock (`kb_builder.BUILDER_DATE` freezes every date), no
locale, no randomness. Independence from filesystem iteration order rests on one line in core —
`Wiki._iter_note_files` sorts — which
`test_the_read_path_scans_in_sorted_order_whatever_the_filesystem_says` asserts directly;
`test_build_kb_ignores_spec_order` separately asserts that reversing the spec list produces a
byte-identical tree. (Those are two different claims, and the earlier single test that built the
corpus in reversed order proved neither: on APFS the two trees come out identical, so it was
comparing a record against a record of the same bytes.)

Cross-platform, measured over the full 44-query run in both §8 modes (3196 `_lexical` and
`_structural` samples, 498 `_combined`): **12 distinct combined values land on an exact 6-decimal
rounding boundary**, where a one-ULP difference would flip the printed digit. The margins say that
cannot happen. `_combined` is `max(0, min(1, 0.65*lex + 0.35*struct + fm))` over inputs the oracle
has ALREADY rounded to 6 dp, i.e. IEEE-754-exact basic ops on exactly-representable-enough operands,
and the upstream terms are nowhere near a boundary: the closest `_lexical` sample sits 1.7e-9 away
and the closest `_structural` 1.7e-7 — both many orders of magnitude above a ULP at that scale, so a
libm `math.log` difference cannot reach them. This has **not** been demonstrated on a second
machine; the honest way to close it is to run `python -m tests.rank_golden.regen` on the CI Linux
image and confirm both files are byte-identical, and better still to run this job in CI on Linux so
a future divergence surfaces as a red gate rather than an unexplained local diff.

`header["agora_version"]` is recorded but **excluded from the golden test's comparison**, so a
release bump does not turn this gate red. It *is* rewritten by `regen`, so after a version bump a
fresh regeneration produces a two-file diff whose only content is that key — expected, and needing
no justification. (`diff_snapshots` does report it, because a caller comparing two arbitrary records
wants to know they came from different builds.)

`header` also distinguishes the cache POLICY from the cache FACT: `index_cache_enabled` is the
`repo.yaml` flag (on by default), `index_cache_used` is whether the ADR-0012 §2 cached read path was
actually engaged. Here it is always `false` — `kb_builder` runs no `git init`, so there is no
curated commit and every number came from the full scan, which is the oracle. **Gate B pins the
full-scan ranking only.** The §2 promise that a cached read is byte-identical to a scan is a
separate, git-backed fixture, and the cache payload is keyed by repo-relative POSIX path — i.e. the
one derived structure the layout flip is guaranteed to invalidate is not covered here.

## Known gaps in the RANKING

**One.** `p27` (`gilt staircase over two decades`) declares `observed_rank: 2`: the alias-bearing
orphan `bond-ladder-basics` is second, because "over two decades" is long-horizon vocabulary that
`expense-ratio-drag` owns outright and wins on. Every other positive ranks its declared `note`
first, and all 44 queries produce their declared `status`.

If a future change means a positive's expected note is no longer at its declared rank, the honest
fix is *not* to tweak the corpus until it is. Record the truth: add or update `observed_rank: <N>`
on that entry in `queries.yaml`, regenerate, and list the query here with a sentence on why the
ranker now prefers something else. `test_every_query_expectation_holds` asserts that exact rank —
and so does `regen`, which now exits non-zero on a contradiction — so a declared gap stays pinned
instead of becoming a hole.

## Known gaps in the COVERAGE

A query set can look thorough and still leave a scoring term unobserved, so every term was checked
by MUTATION: change the ranker, re-record, and see whether the golden moves. The table below is that
measurement (`diff_snapshots` line counts against the two committed records, taken 2026-09-04), so
the next reader knows which parts of the diff at the flip are actually gated and which are not.

| mutation | fm-on / fm-off diff lines | the query that is the evidence |
| --- | --- | --- |
| `_is_moc_path` always `False` (the flip, simulated) | 347 / 347 | the whole set |
| `STRUCT_ALPHA` → 0 · `STRUCT_BETA` → 0 | 328/348 · 237/250 | the whole set |
| `PIVOT` 1.5 → 1.0 | 268 / 259 | the whole set |
| CJK bigrams → unigrams (#56) | 323 / 325 | `p17`–`p24`, `n09`, `n10` |
| `fm` `active` +0.10 → +0.05 | 223 / 0 | the whole set (fm-on only, correctly) |
| `fm` `deprecated` −0.15 → 0 | 45 / 0, incl. a **top-hit flip** | `p26` |
| `FLOOR` 0.18 → 0.23 (→ 0.30) | 4 (8) dropped hits | `p28` |
| `FLOOR` 0.18 → 0.13 (→ 0.10) | 1 appeared hit | `p29` |
| `_combined`'s #146 guard reverted | 1 / 1 — the husk reappears at 0.28 | `p25` |
| `aliases`: weight 3.0 → 1.0 · b-exemption removed · field dropped | 5 · 5 · 9, each with a **rank move** | `p27` (`p05` alone gave only a 4th-decimal wobble) |
| `anchor`/`line`/`excerpt` blanked | 487 / 489 | every hit |
| summary · tags · headings weights | 109 · 42 · 18 | the whole set |
| `MAX_HOPS` 2 → 1 · `MAX_HITS` 20 → 10 · path tie-break reversed | 64 · 33 · 6 | the whole set |

**The one term this fixture cannot observe** is `_passes_gate`'s second (`d_moc == 0`) branch:
deleting it entirely leaves both goldens byte-identical. That is arithmetic, not an oversight —
since the #146 fix a `lex == 0` candidate takes no structural term, so its whole score is its
frontmatter boost (at most +0.10), always under `FLOOR = 0.18`, and it can never become a hit. No
output-level fixture can see that branch. `test_a_lexless_candidate_can_never_clear_the_floor`
pins the arithmetic; the branch itself is covered by the unit regression in
`tests/core/test_wiki_lexical_evidence_146.py`. What gate B *does* pin is the #146 **guard**, via
`p25`.

Two further limits, stated rather than left to be rediscovered: the ADR-0012 §2 **cached** read path
is never engaged (see `index_cache_used` above), and 7 recorded hits (13 in the fm-off column) sit
in exact score ties broken by `_order_key`'s `note.path` tail — the flip changes every path, so that
many rank swaps in the flip diff may be tie-break artifacts. `diff_snapshots` annotates a rank move
whose score did not change with `(score unchanged)`, so a reviewer can tell those apart without the
paths, which the record deliberately omits.

## Regenerating

```sh
python -m tests.rank_golden.regen     # from the repo root; uv run python -m ... under uv
```

This is the **only** way `golden_v1.json` and `golden_v1_fm_off.json` are produced. There is
deliberately no `--regen` flag on the test: a test that can rewrite its own expectation is not a
gate — the first red run gets "fixed" by regenerating, and the baseline the flip is measured
against quietly becomes whatever the flip produced.

`regen` **writes both files even when a query contradicts `queries.yaml`** (that is how you read the
new rank in order to record an `observed_rank`) but then **exits non-zero**, so a scripted
`regen && git add` cannot commit a baseline that contradicts the query file.

What defends a committed golden is reproduction, not formatting.
`test_snapshot_matches_the_committed_golden` re-runs the ranker and compares field for field — that
is what catches a nudged score, a swapped rank or a renamed note.
`test_golden_files_are_canonically_serialized` is a narrower check: it catches a file that was
*re-serialized* (reformatted by an editor, written by a different dumper, merged by hand), and an
edit that leaves the JSON canonically formatted passes it.

### The rule for changing a golden

A PR that changes a golden file **must justify it in the PR description with a
`diff_snapshots` listing** — the failing test already prints exactly that text:

```python
from agora_kb.core.rank_snapshot import diff_snapshots
diff_snapshots(before, after)   # -> ["p01: audit-materiality-threshold rank 15 -> 13", ...]
```

Paste the listing, then say which change produced it and why the new ranking is the intended one.
A golden diff with no explanation is the review's cue that a ranking change shipped by accident —
which for the layout flip is the entire failure mode this gate exists to catch.

The listing reports every recorded field — `status`, `rank`, `score`, `match_reason`, `title`,
`type`, `anchor`, `line`, `excerpt`, and each header key — so an empty listing means the two records
really are equivalent. The one exception is a version-only diff: after a release bump `regen`
rewrites `header["agora_version"]`, and a two-file diff whose only line is that key needs no
justification.

## The owner-side dogfood snapshot

The committed golden covers a synthetic corpus, because that is the only corpus that can live in
git. The private KB is covered by the same machinery, run locally by the owner:

```sh
agora eval --repo ~/knowledge-agora-dogfood \
           --queries tests/rank_golden/queries_dogfood.yaml \
           --out .omc/eval/dogfood-v1.json
```

`agora eval` prints a per-query table, exits `1` if any query's `status` differs from its `expect`
(and `0` otherwise), and with `--out` writes the record with the same canonical serializer. Take
the snapshot **before** the flip, keep it under `.omc/eval/` (never commit KB content), take a
second one after, and read the pair with `diff_snapshots`. `queries_dogfood.yaml` declares only
`expect: ok` — no expected note, since naming one would leak a fragment of a private KB — and the
owner is expected to append their own questions to it.

## Running the gate

```sh
uv run pytest -q tests/rank_golden tests/support tests/core/test_rank_snapshot.py
```
