# The Stratum flip diff — `golden_v1` → `golden_v2`

This file discharges the normative obligation in
[ADR-0041 §D5](../../docs/adr/0041-stratum-kind-first-layout.md): *"The flip PR **MUST** re-run
`python -m tests.rank_golden.regen` and **attach the `diff_snapshots` listing**"*, explained **per
category** with counts that sum to the listing, **per line** for every `match_reason` change and
every rank move *not* annotated `(score unchanged)`, and **citing the 347/347 baseline**.

It is not prose about the records — it is asserted against them.
`test_flip_diff_publishes_the_exact_listing` re-derives
`diff_snapshots(golden_v1, golden_v2)` and requires the two embedded listings below to match it
line for line, and `test_flip_diff_category_counts_sum_to_the_listing` requires the category table
to add up to them. If either record moves, this document goes red with it.

```python
from agora_kb.core.rank_snapshot import diff_snapshots
diff_snapshots(golden_v1, golden_v2)          # the fm-on listing, verbatim, below
```

---

## The headline

**423 lines in each `fm` column, and the D5 seed rule contributed none of them.**

That is the finding, and it is the opposite of what the fixture was built expecting. ADR-0012 §4
seeds `d_moc = 0` from the map tier; D5 moved that tier from `wiki/<domain>/<domain>-moc.md` into
`wiki/maps/**`, and the fixture's coverage table had measured the loss of those seeds at **347/347
lines — the whole corpus**. What actually happened is that the flip *relocated* the seeds without
changing which notes are seeds: `wiki/maps/` holds exactly the three maps that
`wiki/<domain>/<domain>-moc.md` held, each with the same explicit `children:` list, so every seed's
`d_moc`, every MOC-label token union and every in-degree survives the move unchanged.

The 423 lines are therefore about everything the flip did *besides* re-seeding:

| what changed | why it is in the listing |
| --- | --- |
| the `type:` mirror | ADR-0041 D2.5 retires the v1 four-value enum; a `theme` is now a `concept` |
| six basenames | D5 drops the `-moc` suffix; D2.6 merges dailies into one journal per `run_date` |
| the three journals' prose | D2.6's merge rewrites the H1 and prepends a `## <contributor>` section |
| the header | two records, two layouts, two corpus directories |

**Stated plainly, as D5 requires:** *nothing* in this listing is the seed rule. Every score in it
belongs to the ADR-0041 D2.6 journal merge; every appearance/disappearance is one of six renames;
every remaining line is the `type` mirror or the header.

### The evidence for that claim, since it is the load-bearing one

`test_the_flip_listing_is_the_journal_merge_and_the_renames_not_the_seed_rule` builds the corpus in
the **schema-2 layout**, then restores only the three journals' v1 H1 title and v1 body — undoing
D2.6 as *content* while leaving every path, every frontmatter key and the whole D5 seed rule exactly
as the flip left them. The record that comes back reproduces `golden_v1`'s **every** `score`,
`rank`, `match_reason`, `anchor`, `line` and `excerpt`; what survives is the `type` mirror, the six
renames and the header. Had the seed rule moved one number, reverting a journal's prose could not
have restored it.

The listing shows the same thing on its face: **every map appears at exactly the rank its MOC
held** — `finance-moc` rank 2 → `finance` rank 2, `engineering-moc` rank 1 → `engineering` rank 1,
all 41 pairs per column, no exceptions — which is what a preserved `d_moc = 0` seed looks like from
the outside.

---

## The listing

### `fm` on — the live ADR-0012 §8 mode (423 lines)

<!-- listing:fm-on -->
```text
header: repo 'rank-golden-v1' -> 'rank-golden-v2'
header: kb_schema_version 1 -> 2
q146-1: unbilled-receivables-recognition score 0.97173 -> 0.971617 (-0.000113)
q146-1: unbilled-receivables-recognition type 'theme' -> 'concept'
q146-1: finance-moc dropped (was rank 2, score 0.867216)
q146-1: finance-2026-01-12 dropped (was rank 3, score 0.68708)
q146-1: unbilled-receivables-superseded rank 4 -> 3
q146-1: unbilled-receivables-superseded score 0.684718 -> 0.684598 (-0.000120)
q146-1: unbilled-receivables-superseded type 'theme' -> 'concept'
q146-1: 2026-01-12 appeared at rank 4, score 0.683654
q146-1: finance appeared at rank 2, score 0.867481
q146-2: index-fund-investing score 0.954652 -> 0.954448 (-0.000204)
q146-2: index-fund-investing type 'theme' -> 'concept'
q146-2: dividend-yield-vs-total-return type 'theme' -> 'concept'
q146-2: cooking-as-a-finance-habit score 0.787823 -> 0.787874 (+0.000051)
q146-2: cooking-as-a-finance-habit type 'theme' -> 'concept'
q146-2: finance-moc dropped (was rank 4, score 0.775326)
q146-2: expense-ratio-drag type 'theme' -> 'concept'
q146-2: dividend-tax-korea type 'theme' -> 'concept'
q146-2: cooking-moc dropped (was rank 7, score 0.624182)
q146-2: bond-ladder-basics type 'theme' -> 'concept'
q146-2: cooking appeared at rank 7, score 0.624617
q146-2: finance appeared at rank 4, score 0.775779
q146-3: cooking-as-a-finance-habit score 0.943922 -> 0.943793 (-0.000129)
q146-3: cooking-as-a-finance-habit type 'theme' -> 'concept'
q146-3: cooking-moc dropped (was rank 2, score 0.90461)
q146-3: weeknight-meal-prep score 0.816354 -> 0.816603 (+0.000249)
q146-3: weeknight-meal-prep type 'theme' -> 'concept'
q146-3: finance-moc dropped (was rank 4, score 0.763533)
q146-3: kimchi-stew-ratio type 'theme' -> 'concept'
q146-3: index score 0.751072 -> 0.75113 (+0.000058)
q146-3: stock-simmering-basics type 'theme' -> 'concept'
q146-3: index-fund-investing score 0.653179 -> 0.652808 (-0.000371)
q146-3: index-fund-investing type 'theme' -> 'concept'
q146-3: dividend-yield-vs-total-return type 'theme' -> 'concept'
q146-3: finance-2026-01-12 dropped (was rank 10, score 0.636537)
q146-3: cooking-2026-01-13 dropped (was rank 11, score 0.615746)
q146-3: expense-ratio-drag type 'theme' -> 'concept'
q146-3: bond-ladder-basics type 'theme' -> 'concept'
q146-3: dividend-tax-korea type 'theme' -> 'concept'
q146-3: 2026-01-12 appeared at rank 10, score 0.632404
q146-3: 2026-01-13 appeared at rank 11, score 0.611292
q146-3: cooking appeared at rank 2, score 0.904637
q146-3: finance appeared at rank 4, score 0.763457
q146-4: segment-reporting-thresholds score 0.965634 -> 0.965601 (-0.000033)
q146-4: segment-reporting-thresholds type 'theme' -> 'concept'
q146-4: finance-moc dropped (was rank 2, score 0.931265)
q146-4: quarterly-close-checklist type 'theme' -> 'concept'
q146-4: footnote-reading-korea type 'theme' -> 'concept'
q146-4: audit-materiality-threshold type 'theme' -> 'concept'
q146-4: hedge-accounting-stub type 'theme' -> 'concept'
q146-4: cooking-as-a-finance-habit score 0.691228 -> 0.691018 (-0.000210)
q146-4: cooking-as-a-finance-habit type 'theme' -> 'concept'
q146-4: index score 0.678242 -> 0.678322 (+0.000080)
q146-4: finance-2026-01-12 dropped (was rank 9, score 0.636537)
q146-4: engineering-moc dropped (was rank 10, score 0.627721)
q146-4: cooking-moc dropped (was rank 11, score 0.614395)
q146-4: circuit-breaker-stub type 'theme' -> 'concept'
q146-4: 2026-01-12 appeared at rank 9, score 0.632404
q146-4: cooking appeared at rank 11, score 0.614765
q146-4: engineering appeared at rank 10, score 0.628051
q146-4: finance appeared at rank 2, score 0.931317
p01: unbilled-receivables-recognition type 'theme' -> 'concept'
p01: deferred-revenue-basics score 0.909223 -> 0.909302 (+0.000079)
p01: deferred-revenue-basics type 'theme' -> 'concept'
p01: finance-moc dropped (was rank 3, score 0.880711)
p01: revenue-recognition-milestones score 0.84662 -> 0.846839 (+0.000219)
p01: revenue-recognition-milestones type 'theme' -> 'concept'
p01: expense-ratio-drag score 0.826689 -> 0.826944 (+0.000255)
p01: expense-ratio-drag type 'theme' -> 'concept'
p01: engineering-moc dropped (was rank 6, score 0.81465)
p01: cooking-as-a-finance-habit score 0.764702 -> 0.76503 (+0.000328)
p01: cooking-as-a-finance-habit type 'theme' -> 'concept'
p01: fermentation-safety-stub type 'theme' -> 'concept'
p01: hedge-accounting-stub type 'theme' -> 'concept'
p01: circuit-breaker-stub type 'theme' -> 'concept'
p01: idempotent-consumer-stub type 'theme' -> 'concept'
p01: retry-budget-stub type 'theme' -> 'concept'
p01: cooking-moc dropped (was rank 13, score 0.698963)
p01: weeknight-meal-prep type 'theme' -> 'concept'
p01: unbilled-receivables-superseded score 0.657216 -> 0.657378 (+0.000162)
p01: unbilled-receivables-superseded type 'theme' -> 'concept'
p01: audit-materiality-threshold score 0.635138 -> 0.635445 (+0.000307)
p01: audit-materiality-threshold type 'theme' -> 'concept'
p01: dividend-yield-vs-total-return rank 17 -> 18 (score unchanged)
p01: dividend-yield-vs-total-return type 'theme' -> 'concept'
p01: cash-flow-forecast-window rank 18 -> 17
p01: cash-flow-forecast-window score 0.623038 -> 0.627012 (+0.003974)
p01: cash-flow-forecast-window type 'theme' -> 'concept'
p01: braising-temperature-window score 0.62114 -> 0.62133 (+0.000190)
p01: braising-temperature-window type 'theme' -> 'concept'
p01: knife-sharpening-angles type 'theme' -> 'concept'
p01: cooking appeared at rank 13, score 0.699358
p01: engineering appeared at rank 6, score 0.814819
p01: finance appeared at rank 3, score 0.881037
p02: index-fund-investing score 0.963648 -> 0.963965 (+0.000317)
p02: index-fund-investing type 'theme' -> 'concept'
p02: braising-temperature-window score 0.831346 -> 0.831549 (+0.000203)
p02: braising-temperature-window type 'theme' -> 'concept'
p02: finance-moc dropped (was rank 3, score 0.826084)
p02: expense-ratio-drag score 0.811582 -> 0.811844 (+0.000262)
p02: expense-ratio-drag type 'theme' -> 'concept'
p02: finance appeared at rank 3, score 0.826527
p03: segment-reporting-thresholds score 0.951085 -> 0.951114 (+0.000029)
p03: segment-reporting-thresholds type 'theme' -> 'concept'
p03: revenue-recognition-milestones score 0.855549 -> 0.855764 (+0.000215)
p03: revenue-recognition-milestones type 'theme' -> 'concept'
p03: cooking-as-a-finance-habit type 'theme' -> 'concept'
p03: finance-moc dropped (was rank 4, score 0.75014)
p03: cooking-moc dropped (was rank 5, score 0.673929)
p03: write-ahead-log-recovery score 0.601068 -> 0.601306 (+0.000238)
p03: write-ahead-log-recovery type 'theme' -> 'concept'
p03: polling-interval-sizing-deprecated score 0.256347 -> 0.256731 (+0.000384)
p03: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p03: cooking appeared at rank 5, score 0.674389
p03: finance appeared at rank 4, score 0.750567
p04: deferred-revenue-basics score 0.980794 -> 0.98081 (+0.000016)
p04: deferred-revenue-basics type 'theme' -> 'concept'
p04: finance-moc dropped (was rank 2, score 0.869775)
p04: unbilled-receivables-recognition score 0.867472 -> 0.867527 (+0.000055)
p04: unbilled-receivables-recognition type 'theme' -> 'concept'
p04: braising-temperature-window type 'theme' -> 'concept'
p04: cooking-as-a-finance-habit score 0.784041 -> 0.784359 (+0.000318)
p04: cooking-as-a-finance-habit type 'theme' -> 'concept'
p04: segment-reporting-thresholds score 0.71694 -> 0.717288 (+0.000348)
p04: segment-reporting-thresholds type 'theme' -> 'concept'
p04: cooking-moc dropped (was rank 7, score 0.646766)
p04: audit-materiality-threshold score 0.635138 -> 0.635445 (+0.000307)
p04: audit-materiality-threshold type 'theme' -> 'concept'
p04: unbilled-receivables-superseded score 0.51678 -> 0.517035 (+0.000255)
p04: unbilled-receivables-superseded type 'theme' -> 'concept'
p04: polling-interval-sizing-deprecated score 0.241842 -> 0.242227 (+0.000385)
p04: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p04: cooking appeared at rank 7, score 0.647216
p04: finance appeared at rank 2, score 0.870139
p05: expense-ratio-drag score 0.998531 -> 0.99856 (+0.000029)
p05: expense-ratio-drag type 'theme' -> 'concept'
p05: index-fund-investing score 0.915819 -> 0.915781 (-0.000038)
p05: index-fund-investing type 'theme' -> 'concept'
p05: cooking-as-a-finance-habit type 'theme' -> 'concept'
p05: finance-moc dropped (was rank 4, score 0.809831)
p05: audit-materiality-threshold score 0.678189 -> 0.678482 (+0.000293)
p05: audit-materiality-threshold type 'theme' -> 'concept'
p05: cooking-moc dropped (was rank 6, score 0.673929)
p05: coffee-extraction-yield score 0.608535 -> 0.608728 (+0.000193)
p05: coffee-extraction-yield type 'theme' -> 'concept'
p05: write-ahead-log-recovery score 0.601068 -> 0.601306 (+0.000238)
p05: write-ahead-log-recovery type 'theme' -> 'concept'
p05: polling-interval-sizing-deprecated score 0.369221 -> 0.369541 (+0.000320)
p05: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p05: cooking appeared at rank 6, score 0.674389
p05: finance appeared at rank 4, score 0.81025
p06: dividend-yield-vs-total-return score 0.971968 -> 0.972005 (+0.000037)
p06: dividend-yield-vs-total-return type 'theme' -> 'concept'
p06: sourdough-starter-schedule score 0.853961 -> 0.85399 (+0.000029)
p06: sourdough-starter-schedule type 'theme' -> 'concept'
p06: quarterly-close-checklist type 'theme' -> 'concept'
p06: finance-moc dropped (was rank 4, score 0.83299)
p06: cooking-as-a-finance-habit score 0.764702 -> 0.76503 (+0.000328)
p06: cooking-as-a-finance-habit type 'theme' -> 'concept'
p06: cooking-moc dropped (was rank 6, score 0.7426)
p06: audit-materiality-threshold score 0.718848 -> 0.718885 (+0.000037)
p06: audit-materiality-threshold type 'theme' -> 'concept'
p06: segment-reporting-thresholds score 0.71694 -> 0.717288 (+0.000348)
p06: segment-reporting-thresholds type 'theme' -> 'concept'
p06: unbilled-receivables-recognition score 0.679389 -> 0.679456 (+0.000067)
p06: unbilled-receivables-recognition type 'theme' -> 'concept'
p06: weeknight-meal-prep type 'theme' -> 'concept'
p06: coffee-extraction-yield score 0.664377 -> 0.664394 (+0.000017)
p06: coffee-extraction-yield type 'theme' -> 'concept'
p06: cash-flow-forecast-window score 0.623038 -> 0.627012 (+0.003974)
p06: cash-flow-forecast-window type 'theme' -> 'concept'
p06: braising-temperature-window score 0.62114 -> 0.62133 (+0.000190)
p06: braising-temperature-window type 'theme' -> 'concept'
p06: engineering-moc dropped (was rank 14, score 0.619864)
p06: revenue-recognition-milestones score 0.613102 -> 0.61331 (+0.000208)
p06: revenue-recognition-milestones type 'theme' -> 'concept'
p06: expense-ratio-drag score 0.603312 -> 0.603538 (+0.000226)
p06: expense-ratio-drag type 'theme' -> 'concept'
p06: engineering-2026-01-14 dropped (was rank 17, score 0.601081)
p06: knife-sharpening-angles rank 18 -> 17 (score unchanged)
p06: knife-sharpening-angles type 'theme' -> 'concept'
p06: deferred-revenue-basics score 0.583389 -> 0.583586 (+0.000197)
p06: deferred-revenue-basics type 'theme' -> 'concept'
p06: clock-skew-drift-stub score 0.570551 -> 0.57062 (+0.000069)
p06: clock-skew-drift-stub type 'theme' -> 'concept'
p06: 2026-01-14 appeared at rank 18, score 0.596315
p06: cooking appeared at rank 6, score 0.743005
p06: engineering appeared at rank 14, score 0.620042
p06: finance appeared at rank 4, score 0.833348
p07: cash-flow-forecast-window score 0.986052 -> 0.986181 (+0.000129)
p07: cash-flow-forecast-window type 'theme' -> 'concept'
p07: braising-temperature-window score 0.863657 -> 0.863846 (+0.000189)
p07: braising-temperature-window type 'theme' -> 'concept'
p07: finance-moc dropped (was rank 3, score 0.853531)
p07: working-capital-cycle score 0.846474 -> 0.846509 (+0.000035)
p07: working-capital-cycle type 'theme' -> 'concept'
p07: deferred-revenue-basics type 'theme' -> 'concept'
p07: cooking-as-a-finance-habit score 0.820928 -> 0.820989 (+0.000061)
p07: cooking-as-a-finance-habit type 'theme' -> 'concept'
p07: sourdough-starter-schedule score 0.773383 -> 0.773585 (+0.000202)
p07: sourdough-starter-schedule type 'theme' -> 'concept'
p07: cooking-moc dropped (was rank 8, score 0.646766)
p07: polling-interval-sizing-deprecated score 0.228671 -> 0.229056 (+0.000385)
p07: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p07: cooking appeared at rank 8, score 0.647216
p07: finance appeared at rank 3, score 0.853882
p08: working-capital-cycle score 0.98327 -> 0.983287 (+0.000017)
p08: working-capital-cycle type 'theme' -> 'concept'
p08: finance-moc dropped (was rank 2, score 0.856031)
p08: expense-ratio-drag score 0.797629 -> 0.797895 (+0.000266)
p08: expense-ratio-drag type 'theme' -> 'concept'
p08: unbilled-receivables-recognition score 0.792304 -> 0.792651 (+0.000347)
p08: unbilled-receivables-recognition type 'theme' -> 'concept'
p08: clock-skew-drift-stub type 'theme' -> 'concept'
p08: polling-interval-sizing-deprecated score 0.363566 -> 0.363892 (+0.000326)
p08: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p08: finance appeared at rank 2, score 0.856434
p09: bond-ladder-basics score 0.724994 -> 0.724909 (-0.000085)
p09: bond-ladder-basics type 'theme' -> 'concept'
p09: cooking-2026-01-13 dropped (was rank 2, score 0.631546)
p09: polling-interval-sizing-deprecated score 0.292143 -> 0.292517 (+0.000374)
p09: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p09: 2026-01-13 appeared at rank 2, score 0.627182
p10: audit-materiality-threshold score 0.871889 -> 0.872633 (+0.000744)
p10: audit-materiality-threshold type 'theme' -> 'concept'
p10: revenue-recognition-milestones score 0.86354 -> 0.863402 (-0.000138)
p10: revenue-recognition-milestones type 'theme' -> 'concept'
p10: segment-reporting-thresholds score 0.845678 -> 0.845961 (+0.000283)
p10: segment-reporting-thresholds type 'theme' -> 'concept'
p10: deferred-revenue-basics score 0.81978 -> 0.819437 (-0.000343)
p10: deferred-revenue-basics type 'theme' -> 'concept'
p10: unbilled-receivables-recognition score 0.803818 -> 0.803904 (+0.000086)
p10: unbilled-receivables-recognition type 'theme' -> 'concept'
p10: working-capital-cycle score 0.742564 -> 0.742794 (+0.000230)
p10: working-capital-cycle type 'theme' -> 'concept'
p10: finance-moc dropped (was rank 7, score 0.705377)
p10: unbilled-receivables-superseded type 'theme' -> 'concept'
p10: finance appeared at rank 7, score 0.705754
p11: cooking-as-a-finance-habit score 0.887813 -> 0.888045 (+0.000232)
p11: cooking-as-a-finance-habit type 'theme' -> 'concept'
p11: pantry-staples-rotation score 0.847698 -> 0.847882 (+0.000184)
p11: pantry-staples-rotation type 'theme' -> 'concept'
p11: deferred-revenue-basics score 0.825921 -> 0.826119 (+0.000198)
p11: deferred-revenue-basics type 'theme' -> 'concept'
p12: weeknight-meal-prep type 'theme' -> 'concept'
p12: cooking-moc dropped (was rank 2, score 0.710165)
p12: cooking appeared at rank 2, score 0.710628
p13: braising-temperature-window score 0.994116 -> 0.994143 (+0.000027)
p13: braising-temperature-window type 'theme' -> 'concept'
p13: cooking-moc dropped (was rank 2, score 0.859329)
p13: weeknight-meal-prep type 'theme' -> 'concept'
p13: working-capital-cycle score 0.791947 -> 0.792163 (+0.000216)
p13: working-capital-cycle type 'theme' -> 'concept'
p13: segment-reporting-thresholds score 0.760632 -> 0.76097 (+0.000338)
p13: segment-reporting-thresholds type 'theme' -> 'concept'
p13: revenue-recognition-milestones score 0.751673 -> 0.751918 (+0.000245)
p13: revenue-recognition-milestones type 'theme' -> 'concept'
p13: pantry-staples-rotation type 'theme' -> 'concept'
p13: expense-ratio-drag score 0.739959 -> 0.740234 (+0.000275)
p13: expense-ratio-drag type 'theme' -> 'concept'
p13: schema-migration-locks type 'theme' -> 'concept'
p13: blue-green-deploy-window score 0.499446 -> 0.499687 (+0.000241)
p13: blue-green-deploy-window type 'theme' -> 'concept'
p13: bond-ladder-basics score 0.491962 -> 0.492223 (+0.000261)
p13: bond-ladder-basics type 'theme' -> 'concept'
p13: unbilled-receivables-superseded score 0.459255 -> 0.459519 (+0.000264)
p13: unbilled-receivables-superseded type 'theme' -> 'concept'
p13: polling-interval-sizing-deprecated score 0.249974 -> 0.250263 (+0.000289)
p13: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p13: cooking appeared at rank 2, score 0.859643
p14: pantry-staples-rotation score 0.976988 -> 0.977015 (+0.000027)
p14: pantry-staples-rotation type 'theme' -> 'concept'
p14: cooking-moc dropped (was rank 2, score 0.853526)
p14: cash-flow-forecast-window score 0.782035 -> 0.782281 (+0.000246)
p14: cash-flow-forecast-window type 'theme' -> 'concept'
p14: blue-green-deploy-window score 0.704396 -> 0.704429 (+0.000033)
p14: blue-green-deploy-window type 'theme' -> 'concept'
p14: polling-interval-sizing-deprecated score 0.320809 -> 0.321079 (+0.000270)
p14: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p14: cooking appeared at rank 2, score 0.853892
p15: write-ahead-log-recovery score 0.797164 -> 0.797194 (+0.000030)
p15: write-ahead-log-recovery type 'theme' -> 'concept'
p15: sourdough-starter-schedule score 0.773383 -> 0.773585 (+0.000202)
p15: sourdough-starter-schedule type 'theme' -> 'concept'
p15: cooking-as-a-finance-habit score 0.721607 -> 0.721946 (+0.000339)
p15: cooking-as-a-finance-habit type 'theme' -> 'concept'
p15: index-fund-investing score 0.720428 -> 0.72077 (+0.000342)
p15: index-fund-investing type 'theme' -> 'concept'
p15: blue-green-deploy-window score 0.543948 -> 0.544182 (+0.000234)
p15: blue-green-deploy-window type 'theme' -> 'concept'
p15: polling-interval-sizing-deprecated score 0.228671 -> 0.229056 (+0.000385)
p15: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p16: blue-green-deploy-window score 0.74548 -> 0.745539 (+0.000059)
p16: blue-green-deploy-window type 'theme' -> 'concept'
p17: dividend-tax-korea score 0.95828 -> 0.958403 (+0.000123)
p17: dividend-tax-korea type 'theme' -> 'concept'
p17: finance-moc dropped (was rank 2, score 0.82059)
p17: finance appeared at rank 2, score 0.820996
p18: footnote-reading-korea score 0.955046 -> 0.954969 (-0.000077)
p18: footnote-reading-korea type 'theme' -> 'concept'
p18: finance-moc dropped (was rank 2, score 0.858755)
p18: kimchi-stew-ratio score 0.738502 -> 0.738888 (+0.000386)
p18: kimchi-stew-ratio type 'theme' -> 'concept'
p18: backpressure-design-stub score 0.734094 -> 0.734323 (+0.000229)
p18: backpressure-design-stub type 'theme' -> 'concept'
p18: log-correlation-tracing score 0.646097 -> 0.646165 (+0.000068)
p18: log-correlation-tracing type 'theme' -> 'concept'
p18: finance appeared at rank 2, score 0.859125
p19: kimchi-stew-ratio score 0.992563 -> 0.992503 (-0.000060)
p19: kimchi-stew-ratio type 'theme' -> 'concept'
p19: cooking-moc dropped (was rank 2, score 0.941066)
p19: stock-simmering-basics score 0.764018 -> 0.764348 (+0.000330)
p19: stock-simmering-basics type 'theme' -> 'concept'
p19: cooking appeared at rank 2, score 0.941262
p20: stock-simmering-basics score 0.877641 -> 0.877161 (-0.000480)
p20: stock-simmering-basics type 'theme' -> 'concept'
p20: kimchi-stew-ratio score 0.782951 -> 0.783319 (+0.000368)
p20: kimchi-stew-ratio type 'theme' -> 'concept'
p20: cooking-moc dropped (was rank 3, score 0.710165)
p20: cooking appeared at rank 3, score 0.710628
p21: log-correlation-tracing score 0.781183 -> 0.781293 (+0.000110)
p21: log-correlation-tracing type 'theme' -> 'concept'
p22: backpressure-design-stub score 0.891497 -> 0.891557 (+0.000060)
p22: backpressure-design-stub type 'theme' -> 'concept'
p22: footnote-reading-korea score 0.765917 -> 0.76628 (+0.000363)
p22: footnote-reading-korea type 'theme' -> 'concept'
p23: kimchi-stew-ratio score 0.971031 -> 0.970838 (-0.000193)
p23: kimchi-stew-ratio type 'theme' -> 'concept'
p23: cooking-moc dropped (was rank 2, score 0.892571)
p23: expense-ratio-drag score 0.888972 -> 0.888564 (-0.000408)
p23: expense-ratio-drag type 'theme' -> 'concept'
p23: dividend-yield-vs-total-return score 0.790501 -> 0.790722 (+0.000221)
p23: dividend-yield-vs-total-return type 'theme' -> 'concept'
p23: index-fund-investing score 0.747892 -> 0.748228 (+0.000336)
p23: index-fund-investing type 'theme' -> 'concept'
p23: finance-moc dropped (was rank 6, score 0.629959)
p23: cooking appeared at rank 2, score 0.892853
p23: finance appeared at rank 6, score 0.630446
p24: dividend-tax-korea score 0.984774 -> 0.984721 (-0.000053)
p24: dividend-tax-korea type 'theme' -> 'concept'
p24: finance-moc dropped (was rank 2, score 0.938085)
p24: dividend-yield-vs-total-return score 0.875457 -> 0.87489 (-0.000567)
p24: dividend-yield-vs-total-return type 'theme' -> 'concept'
p24: finance appeared at rank 2, score 0.938301
p25: engineering-moc dropped (was rank 1, score 0.927651)
p25: write-ahead-log-recovery score 0.686515 -> 0.686175 (-0.000340)
p25: write-ahead-log-recovery type 'theme' -> 'concept'
p25: engineering-2026-01-14 dropped (was rank 3, score 0.643785)
p25: 2026-01-14 appeared at rank 3, score 0.641179
p25: engineering appeared at rank 1, score 0.92783
p26: finance-moc dropped (was rank 1, score 0.802939)
p26: unbilled-receivables-superseded score 0.727325 -> 0.727378 (+0.000053)
p26: unbilled-receivables-superseded type 'theme' -> 'concept'
p26: polling-interval-sizing-deprecated score 0.425587 -> 0.425672 (+0.000085)
p26: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p26: finance appeared at rank 1, score 0.803362
p27: expense-ratio-drag score 0.845184 -> 0.84543 (+0.000246)
p27: expense-ratio-drag type 'theme' -> 'concept'
p27: bond-ladder-basics type 'theme' -> 'concept'
p27: audit-materiality-threshold score 0.718848 -> 0.718885 (+0.000037)
p27: audit-materiality-threshold type 'theme' -> 'concept'
p27: segment-reporting-thresholds score 0.71694 -> 0.717288 (+0.000348)
p27: segment-reporting-thresholds type 'theme' -> 'concept'
p27: coffee-extraction-yield score 0.608535 -> 0.608728 (+0.000193)
p27: coffee-extraction-yield type 'theme' -> 'concept'
p27: finance-moc dropped (was rank 6, score 0.604299)
p27: blue-green-deploy-window score 0.543948 -> 0.544182 (+0.000234)
p27: blue-green-deploy-window type 'theme' -> 'concept'
p27: clock-skew-drift-stub score 0.503979 -> 0.504022 (+0.000043)
p27: clock-skew-drift-stub type 'theme' -> 'concept'
p27: polling-interval-sizing-deprecated score 0.228671 -> 0.229056 (+0.000385)
p27: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p27: finance appeared at rank 6, score 0.604765
p28: knife-sharpening-angles score 0.949072 -> 0.948857 (-0.000215)
p28: knife-sharpening-angles type 'theme' -> 'concept'
p28: cooking-moc dropped (was rank 2, score 0.854202)
p28: cooking-as-a-finance-habit score 0.697944 -> 0.698283 (+0.000339)
p28: cooking-as-a-finance-habit type 'theme' -> 'concept'
p28: index-fund-investing score 0.696763 -> 0.697104 (+0.000341)
p28: index-fund-investing type 'theme' -> 'concept'
p28: segment-reporting-thresholds score 0.693271 -> 0.693619 (+0.000348)
p28: segment-reporting-thresholds type 'theme' -> 'concept'
p28: schema-migration-locks score 0.568307 -> 0.568509 (+0.000202)
p28: schema-migration-locks type 'theme' -> 'concept'
p28: write-ahead-log-recovery score 0.551265 -> 0.551516 (+0.000251)
p28: write-ahead-log-recovery type 'theme' -> 'concept'
p28: blue-green-deploy-window score 0.520756 -> 0.520994 (+0.000238)
p28: blue-green-deploy-window type 'theme' -> 'concept'
p28: unbilled-receivables-superseded score 0.480635 -> 0.480897 (+0.000262)
p28: unbilled-receivables-superseded type 'theme' -> 'concept'
p28: polling-interval-sizing-deprecated score 0.205086 -> 0.205468 (+0.000382)
p28: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p28: cooking appeared at rank 2, score 0.854524
p29: quarterly-close-checklist type 'theme' -> 'concept'
p29: finance-moc dropped (was rank 2, score 0.856627)
p29: pantry-staples-rotation score 0.842896 -> 0.845749 (+0.002853)
p29: pantry-staples-rotation type 'theme' -> 'concept'
p29: weeknight-meal-prep score 0.773493 -> 0.773745 (+0.000252)
p29: weeknight-meal-prep type 'theme' -> 'concept'
p29: expense-ratio-drag score 0.729787 -> 0.730062 (+0.000275)
p29: expense-ratio-drag type 'theme' -> 'concept'
p29: finance-2026-01-12 dropped (was rank 6, score 0.725486)
p29: cooking-as-a-finance-habit score 0.666516 -> 0.66685 (+0.000334)
p29: cooking-as-a-finance-habit type 'theme' -> 'concept'
p29: index-fund-investing score 0.665351 -> 0.665687 (+0.000336)
p29: index-fund-investing type 'theme' -> 'concept'
p29: audit-materiality-threshold score 0.662322 -> 0.662623 (+0.000301)
p29: audit-materiality-threshold type 'theme' -> 'concept'
p29: segment-reporting-thresholds score 0.661912 -> 0.662255 (+0.000343)
p29: segment-reporting-thresholds type 'theme' -> 'concept'
p29: bond-ladder-basics score 0.531393 -> 0.531567 (+0.000174)
p29: bond-ladder-basics type 'theme' -> 'concept'
p29: write-ahead-log-recovery score 0.519743 -> 0.519996 (+0.000253)
p29: write-ahead-log-recovery type 'theme' -> 'concept'
p29: 2026-01-12 appeared at rank 6, score 0.722942
p29: finance appeared at rank 2, score 0.857006
p30: sourdough-starter-schedule score 0.874645 -> 0.874379 (-0.000266)
p30: sourdough-starter-schedule type 'theme' -> 'concept'
p30: revenue-recognition-milestones score 0.837896 -> 0.838119 (+0.000223)
p30: revenue-recognition-milestones type 'theme' -> 'concept'
p30: cooking-moc dropped (was rank 3, score 0.690381)
p30: cooking appeared at rank 3, score 0.690844
```

### `fm` off (423 lines)

<!-- listing:fm-off -->
```text
header: repo 'rank-golden-v1' -> 'rank-golden-v2'
header: kb_schema_version 1 -> 2
q146-1: unbilled-receivables-recognition score 0.87173 -> 0.871617 (-0.000113)
q146-1: unbilled-receivables-recognition type 'theme' -> 'concept'
q146-1: unbilled-receivables-superseded score 0.834718 -> 0.834598 (-0.000120)
q146-1: unbilled-receivables-superseded type 'theme' -> 'concept'
q146-1: finance-moc dropped (was rank 3, score 0.767216)
q146-1: finance-2026-01-12 dropped (was rank 4, score 0.58708)
q146-1: 2026-01-12 appeared at rank 4, score 0.583654
q146-1: finance appeared at rank 3, score 0.767481
q146-2: index-fund-investing score 0.854652 -> 0.854448 (-0.000204)
q146-2: index-fund-investing type 'theme' -> 'concept'
q146-2: dividend-yield-vs-total-return type 'theme' -> 'concept'
q146-2: cooking-as-a-finance-habit score 0.687823 -> 0.687874 (+0.000051)
q146-2: cooking-as-a-finance-habit type 'theme' -> 'concept'
q146-2: finance-moc dropped (was rank 4, score 0.675326)
q146-2: expense-ratio-drag type 'theme' -> 'concept'
q146-2: dividend-tax-korea type 'theme' -> 'concept'
q146-2: cooking-moc dropped (was rank 7, score 0.524182)
q146-2: bond-ladder-basics type 'theme' -> 'concept'
q146-2: cooking appeared at rank 7, score 0.524617
q146-2: finance appeared at rank 4, score 0.675779
q146-3: cooking-as-a-finance-habit score 0.843922 -> 0.843793 (-0.000129)
q146-3: cooking-as-a-finance-habit type 'theme' -> 'concept'
q146-3: cooking-moc dropped (was rank 2, score 0.80461)
q146-3: weeknight-meal-prep score 0.716354 -> 0.716603 (+0.000249)
q146-3: weeknight-meal-prep type 'theme' -> 'concept'
q146-3: finance-moc dropped (was rank 4, score 0.663533)
q146-3: kimchi-stew-ratio type 'theme' -> 'concept'
q146-3: index score 0.651072 -> 0.65113 (+0.000058)
q146-3: stock-simmering-basics type 'theme' -> 'concept'
q146-3: index-fund-investing score 0.553179 -> 0.552808 (-0.000371)
q146-3: index-fund-investing type 'theme' -> 'concept'
q146-3: dividend-yield-vs-total-return type 'theme' -> 'concept'
q146-3: finance-2026-01-12 dropped (was rank 10, score 0.536537)
q146-3: cooking-2026-01-13 dropped (was rank 11, score 0.515746)
q146-3: expense-ratio-drag type 'theme' -> 'concept'
q146-3: bond-ladder-basics type 'theme' -> 'concept'
q146-3: dividend-tax-korea type 'theme' -> 'concept'
q146-3: 2026-01-12 appeared at rank 10, score 0.532404
q146-3: 2026-01-13 appeared at rank 11, score 0.511292
q146-3: cooking appeared at rank 2, score 0.804637
q146-3: finance appeared at rank 4, score 0.663457
q146-4: segment-reporting-thresholds score 0.865634 -> 0.865601 (-0.000033)
q146-4: segment-reporting-thresholds type 'theme' -> 'concept'
q146-4: finance-moc dropped (was rank 2, score 0.831265)
q146-4: quarterly-close-checklist type 'theme' -> 'concept'
q146-4: audit-materiality-threshold type 'theme' -> 'concept'
q146-4: footnote-reading-korea type 'theme' -> 'concept'
q146-4: hedge-accounting-stub type 'theme' -> 'concept'
q146-4: cooking-as-a-finance-habit score 0.591228 -> 0.591018 (-0.000210)
q146-4: cooking-as-a-finance-habit type 'theme' -> 'concept'
q146-4: index score 0.578242 -> 0.578322 (+0.000080)
q146-4: circuit-breaker-stub type 'theme' -> 'concept'
q146-4: finance-2026-01-12 dropped (was rank 10, score 0.536537)
q146-4: engineering-moc dropped (was rank 11, score 0.527721)
q146-4: cooking-moc dropped (was rank 12, score 0.514395)
q146-4: 2026-01-12 appeared at rank 10, score 0.532404
q146-4: cooking appeared at rank 12, score 0.514765
q146-4: engineering appeared at rank 11, score 0.528051
q146-4: finance appeared at rank 2, score 0.831317
p01: unbilled-receivables-recognition score 0.908828 -> 0.908855 (+0.000027)
p01: unbilled-receivables-recognition type 'theme' -> 'concept'
p01: deferred-revenue-basics score 0.809223 -> 0.809302 (+0.000079)
p01: deferred-revenue-basics type 'theme' -> 'concept'
p01: unbilled-receivables-superseded score 0.807216 -> 0.807378 (+0.000162)
p01: unbilled-receivables-superseded type 'theme' -> 'concept'
p01: finance-moc dropped (was rank 4, score 0.780711)
p01: revenue-recognition-milestones score 0.74662 -> 0.746839 (+0.000219)
p01: revenue-recognition-milestones type 'theme' -> 'concept'
p01: expense-ratio-drag score 0.726689 -> 0.726944 (+0.000255)
p01: expense-ratio-drag type 'theme' -> 'concept'
p01: engineering-moc dropped (was rank 7, score 0.71465)
p01: fermentation-safety-stub type 'theme' -> 'concept'
p01: hedge-accounting-stub type 'theme' -> 'concept'
p01: circuit-breaker-stub type 'theme' -> 'concept'
p01: idempotent-consumer-stub type 'theme' -> 'concept'
p01: retry-budget-stub type 'theme' -> 'concept'
p01: cooking-as-a-finance-habit score 0.664702 -> 0.66503 (+0.000328)
p01: cooking-as-a-finance-habit type 'theme' -> 'concept'
p01: audit-materiality-threshold score 0.635138 -> 0.635445 (+0.000307)
p01: audit-materiality-threshold type 'theme' -> 'concept'
p01: cooking-moc dropped (was rank 15, score 0.598963)
p01: weeknight-meal-prep type 'theme' -> 'concept'
p01: polling-interval-sizing-deprecated score 0.533691 -> 0.533929 (+0.000238)
p01: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p01: dividend-yield-vs-total-return rank 18 -> 19 (score unchanged)
p01: dividend-yield-vs-total-return type 'theme' -> 'concept'
p01: cash-flow-forecast-window rank 19 -> 18
p01: cash-flow-forecast-window score 0.523038 -> 0.527012 (+0.003974)
p01: cash-flow-forecast-window type 'theme' -> 'concept'
p01: braising-temperature-window score 0.52114 -> 0.52133 (+0.000190)
p01: braising-temperature-window type 'theme' -> 'concept'
p01: cooking appeared at rank 15, score 0.599358
p01: engineering appeared at rank 7, score 0.714819
p01: finance appeared at rank 4, score 0.781037
p02: index-fund-investing score 0.863648 -> 0.863965 (+0.000317)
p02: index-fund-investing type 'theme' -> 'concept'
p02: braising-temperature-window score 0.731346 -> 0.731549 (+0.000203)
p02: braising-temperature-window type 'theme' -> 'concept'
p02: finance-moc dropped (was rank 3, score 0.726084)
p02: expense-ratio-drag score 0.711582 -> 0.711844 (+0.000262)
p02: expense-ratio-drag type 'theme' -> 'concept'
p02: finance appeared at rank 3, score 0.726527
p03: segment-reporting-thresholds score 0.851085 -> 0.851114 (+0.000029)
p03: segment-reporting-thresholds type 'theme' -> 'concept'
p03: revenue-recognition-milestones score 0.755549 -> 0.755764 (+0.000215)
p03: revenue-recognition-milestones type 'theme' -> 'concept'
p03: cooking-as-a-finance-habit type 'theme' -> 'concept'
p03: finance-moc dropped (was rank 4, score 0.65014)
p03: cooking-moc dropped (was rank 5, score 0.573929)
p03: write-ahead-log-recovery score 0.501068 -> 0.501306 (+0.000238)
p03: write-ahead-log-recovery type 'theme' -> 'concept'
p03: polling-interval-sizing-deprecated score 0.406347 -> 0.406731 (+0.000384)
p03: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p03: cooking appeared at rank 5, score 0.574389
p03: finance appeared at rank 4, score 0.650567
p04: deferred-revenue-basics score 0.880794 -> 0.88081 (+0.000016)
p04: deferred-revenue-basics type 'theme' -> 'concept'
p04: finance-moc dropped (was rank 2, score 0.769775)
p04: unbilled-receivables-recognition score 0.767472 -> 0.767527 (+0.000055)
p04: unbilled-receivables-recognition type 'theme' -> 'concept'
p04: braising-temperature-window type 'theme' -> 'concept'
p04: cooking-as-a-finance-habit score 0.684041 -> 0.684359 (+0.000318)
p04: cooking-as-a-finance-habit type 'theme' -> 'concept'
p04: unbilled-receivables-superseded score 0.66678 -> 0.667035 (+0.000255)
p04: unbilled-receivables-superseded type 'theme' -> 'concept'
p04: audit-materiality-threshold score 0.635138 -> 0.635445 (+0.000307)
p04: audit-materiality-threshold type 'theme' -> 'concept'
p04: segment-reporting-thresholds score 0.61694 -> 0.617288 (+0.000348)
p04: segment-reporting-thresholds type 'theme' -> 'concept'
p04: cooking-moc dropped (was rank 9, score 0.546766)
p04: polling-interval-sizing-deprecated score 0.391842 -> 0.392227 (+0.000385)
p04: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p04: cooking appeared at rank 9, score 0.547216
p04: finance appeared at rank 2, score 0.770139
p05: expense-ratio-drag score 0.898531 -> 0.89856 (+0.000029)
p05: expense-ratio-drag type 'theme' -> 'concept'
p05: index-fund-investing score 0.815819 -> 0.815781 (-0.000038)
p05: index-fund-investing type 'theme' -> 'concept'
p05: cooking-as-a-finance-habit type 'theme' -> 'concept'
p05: finance-moc dropped (was rank 4, score 0.709831)
p05: audit-materiality-threshold score 0.678189 -> 0.678482 (+0.000293)
p05: audit-materiality-threshold type 'theme' -> 'concept'
p05: cooking-moc dropped (was rank 6, score 0.573929)
p05: polling-interval-sizing-deprecated score 0.519221 -> 0.519541 (+0.000320)
p05: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p05: coffee-extraction-yield score 0.508535 -> 0.508728 (+0.000193)
p05: coffee-extraction-yield type 'theme' -> 'concept'
p05: write-ahead-log-recovery score 0.501068 -> 0.501306 (+0.000238)
p05: write-ahead-log-recovery type 'theme' -> 'concept'
p05: cooking appeared at rank 6, score 0.574389
p05: finance appeared at rank 4, score 0.71025
p06: dividend-yield-vs-total-return score 0.871968 -> 0.872005 (+0.000037)
p06: dividend-yield-vs-total-return type 'theme' -> 'concept'
p06: sourdough-starter-schedule score 0.753961 -> 0.75399 (+0.000029)
p06: sourdough-starter-schedule type 'theme' -> 'concept'
p06: quarterly-close-checklist type 'theme' -> 'concept'
p06: finance-moc dropped (was rank 4, score 0.73299)
p06: audit-materiality-threshold score 0.718848 -> 0.718885 (+0.000037)
p06: audit-materiality-threshold type 'theme' -> 'concept'
p06: cooking-as-a-finance-habit score 0.664702 -> 0.66503 (+0.000328)
p06: cooking-as-a-finance-habit type 'theme' -> 'concept'
p06: cooking-moc dropped (was rank 7, score 0.6426)
p06: segment-reporting-thresholds score 0.61694 -> 0.617288 (+0.000348)
p06: segment-reporting-thresholds type 'theme' -> 'concept'
p06: unbilled-receivables-recognition score 0.579389 -> 0.579456 (+0.000067)
p06: unbilled-receivables-recognition type 'theme' -> 'concept'
p06: weeknight-meal-prep type 'theme' -> 'concept'
p06: clock-skew-drift-stub score 0.570551 -> 0.57062 (+0.000069)
p06: clock-skew-drift-stub type 'theme' -> 'concept'
p06: coffee-extraction-yield score 0.564377 -> 0.564394 (+0.000017)
p06: coffee-extraction-yield type 'theme' -> 'concept'
p06: cash-flow-forecast-window score 0.523038 -> 0.527012 (+0.003974)
p06: cash-flow-forecast-window type 'theme' -> 'concept'
p06: braising-temperature-window score 0.52114 -> 0.52133 (+0.000190)
p06: braising-temperature-window type 'theme' -> 'concept'
p06: engineering-moc dropped (was rank 15, score 0.519864)
p06: fermentation-safety-stub type 'theme' -> 'concept'
p06: hedge-accounting-stub type 'theme' -> 'concept'
p06: circuit-breaker-stub type 'theme' -> 'concept'
p06: idempotent-consumer-stub type 'theme' -> 'concept'
p06: retry-budget-stub type 'theme' -> 'concept'
p06: cooking appeared at rank 7, score 0.643006
p06: engineering appeared at rank 15, score 0.520042
p06: finance appeared at rank 4, score 0.733348
p07: cash-flow-forecast-window score 0.886052 -> 0.886181 (+0.000129)
p07: cash-flow-forecast-window type 'theme' -> 'concept'
p07: braising-temperature-window score 0.763657 -> 0.763846 (+0.000189)
p07: braising-temperature-window type 'theme' -> 'concept'
p07: finance-moc dropped (was rank 3, score 0.753531)
p07: working-capital-cycle score 0.746474 -> 0.746509 (+0.000035)
p07: working-capital-cycle type 'theme' -> 'concept'
p07: deferred-revenue-basics type 'theme' -> 'concept'
p07: cooking-as-a-finance-habit score 0.720928 -> 0.720989 (+0.000061)
p07: cooking-as-a-finance-habit type 'theme' -> 'concept'
p07: sourdough-starter-schedule score 0.673383 -> 0.673585 (+0.000202)
p07: sourdough-starter-schedule type 'theme' -> 'concept'
p07: cooking-moc dropped (was rank 8, score 0.546766)
p07: polling-interval-sizing-deprecated score 0.378671 -> 0.379056 (+0.000385)
p07: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p07: cooking appeared at rank 8, score 0.547216
p07: finance appeared at rank 3, score 0.753883
p08: working-capital-cycle score 0.88327 -> 0.883287 (+0.000017)
p08: working-capital-cycle type 'theme' -> 'concept'
p08: finance-moc dropped (was rank 2, score 0.756031)
p08: expense-ratio-drag score 0.697629 -> 0.697896 (+0.000267)
p08: expense-ratio-drag type 'theme' -> 'concept'
p08: unbilled-receivables-recognition score 0.692304 -> 0.692651 (+0.000347)
p08: unbilled-receivables-recognition type 'theme' -> 'concept'
p08: polling-interval-sizing-deprecated score 0.513566 -> 0.513892 (+0.000326)
p08: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p08: clock-skew-drift-stub type 'theme' -> 'concept'
p08: finance appeared at rank 2, score 0.756434
p09: bond-ladder-basics score 0.624994 -> 0.624909 (-0.000085)
p09: bond-ladder-basics type 'theme' -> 'concept'
p09: cooking-2026-01-13 dropped (was rank 2, score 0.531546)
p09: polling-interval-sizing-deprecated score 0.442143 -> 0.442517 (+0.000374)
p09: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p09: 2026-01-13 appeared at rank 2, score 0.527182
p10: audit-materiality-threshold score 0.871889 -> 0.872633 (+0.000744)
p10: audit-materiality-threshold type 'theme' -> 'concept'
p10: revenue-recognition-milestones score 0.76354 -> 0.763402 (-0.000138)
p10: revenue-recognition-milestones type 'theme' -> 'concept'
p10: segment-reporting-thresholds score 0.745678 -> 0.745961 (+0.000283)
p10: segment-reporting-thresholds type 'theme' -> 'concept'
p10: deferred-revenue-basics score 0.71978 -> 0.719437 (-0.000343)
p10: deferred-revenue-basics type 'theme' -> 'concept'
p10: unbilled-receivables-recognition score 0.703818 -> 0.703904 (+0.000086)
p10: unbilled-receivables-recognition type 'theme' -> 'concept'
p10: working-capital-cycle score 0.642564 -> 0.642794 (+0.000230)
p10: working-capital-cycle type 'theme' -> 'concept'
p10: unbilled-receivables-superseded type 'theme' -> 'concept'
p10: finance-moc dropped (was rank 8, score 0.605377)
p10: finance appeared at rank 8, score 0.605754
p11: cooking-as-a-finance-habit score 0.787813 -> 0.788045 (+0.000232)
p11: cooking-as-a-finance-habit type 'theme' -> 'concept'
p11: pantry-staples-rotation score 0.747698 -> 0.747882 (+0.000184)
p11: pantry-staples-rotation type 'theme' -> 'concept'
p11: deferred-revenue-basics score 0.725921 -> 0.726119 (+0.000198)
p11: deferred-revenue-basics type 'theme' -> 'concept'
p12: weeknight-meal-prep score 0.92669 -> 0.926761 (+0.000071)
p12: weeknight-meal-prep type 'theme' -> 'concept'
p12: cooking-moc dropped (was rank 2, score 0.610165)
p12: cooking appeared at rank 2, score 0.610628
p13: braising-temperature-window score 0.894116 -> 0.894143 (+0.000027)
p13: braising-temperature-window type 'theme' -> 'concept'
p13: cooking-moc dropped (was rank 2, score 0.759329)
p13: weeknight-meal-prep type 'theme' -> 'concept'
p13: working-capital-cycle score 0.691947 -> 0.692163 (+0.000216)
p13: working-capital-cycle type 'theme' -> 'concept'
p13: segment-reporting-thresholds score 0.660632 -> 0.66097 (+0.000338)
p13: segment-reporting-thresholds type 'theme' -> 'concept'
p13: revenue-recognition-milestones score 0.651673 -> 0.651918 (+0.000245)
p13: revenue-recognition-milestones type 'theme' -> 'concept'
p13: pantry-staples-rotation type 'theme' -> 'concept'
p13: expense-ratio-drag score 0.639959 -> 0.640234 (+0.000275)
p13: expense-ratio-drag type 'theme' -> 'concept'
p13: unbilled-receivables-superseded score 0.609255 -> 0.609519 (+0.000264)
p13: unbilled-receivables-superseded type 'theme' -> 'concept'
p13: schema-migration-locks type 'theme' -> 'concept'
p13: polling-interval-sizing-deprecated score 0.399974 -> 0.400263 (+0.000289)
p13: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p13: blue-green-deploy-window score 0.399446 -> 0.399687 (+0.000241)
p13: blue-green-deploy-window type 'theme' -> 'concept'
p13: bond-ladder-basics score 0.391962 -> 0.392223 (+0.000261)
p13: bond-ladder-basics type 'theme' -> 'concept'
p13: cooking appeared at rank 2, score 0.759643
p14: pantry-staples-rotation score 0.876988 -> 0.877015 (+0.000027)
p14: pantry-staples-rotation type 'theme' -> 'concept'
p14: cooking-moc dropped (was rank 2, score 0.753526)
p14: cash-flow-forecast-window score 0.682035 -> 0.682281 (+0.000246)
p14: cash-flow-forecast-window type 'theme' -> 'concept'
p14: blue-green-deploy-window score 0.604396 -> 0.604429 (+0.000033)
p14: blue-green-deploy-window type 'theme' -> 'concept'
p14: polling-interval-sizing-deprecated score 0.470809 -> 0.471079 (+0.000270)
p14: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p14: cooking appeared at rank 2, score 0.753892
p15: write-ahead-log-recovery score 0.697164 -> 0.697194 (+0.000030)
p15: write-ahead-log-recovery type 'theme' -> 'concept'
p15: sourdough-starter-schedule score 0.673383 -> 0.673585 (+0.000202)
p15: sourdough-starter-schedule type 'theme' -> 'concept'
p15: cooking-as-a-finance-habit score 0.621607 -> 0.621946 (+0.000339)
p15: cooking-as-a-finance-habit type 'theme' -> 'concept'
p15: index-fund-investing score 0.620428 -> 0.62077 (+0.000342)
p15: index-fund-investing type 'theme' -> 'concept'
p15: blue-green-deploy-window score 0.443948 -> 0.444182 (+0.000234)
p15: blue-green-deploy-window type 'theme' -> 'concept'
p15: polling-interval-sizing-deprecated score 0.378671 -> 0.379056 (+0.000385)
p15: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p16: blue-green-deploy-window score 0.64548 -> 0.645539 (+0.000059)
p16: blue-green-deploy-window type 'theme' -> 'concept'
p17: dividend-tax-korea score 0.85828 -> 0.858403 (+0.000123)
p17: dividend-tax-korea type 'theme' -> 'concept'
p17: finance-moc dropped (was rank 2, score 0.72059)
p17: finance appeared at rank 2, score 0.720996
p18: footnote-reading-korea score 0.855046 -> 0.854969 (-0.000077)
p18: footnote-reading-korea type 'theme' -> 'concept'
p18: finance-moc dropped (was rank 2, score 0.758755)
p18: backpressure-design-stub score 0.734094 -> 0.734323 (+0.000229)
p18: backpressure-design-stub type 'theme' -> 'concept'
p18: kimchi-stew-ratio score 0.638502 -> 0.638888 (+0.000386)
p18: kimchi-stew-ratio type 'theme' -> 'concept'
p18: log-correlation-tracing score 0.546097 -> 0.546165 (+0.000068)
p18: log-correlation-tracing type 'theme' -> 'concept'
p18: finance appeared at rank 2, score 0.759125
p19: kimchi-stew-ratio score 0.892563 -> 0.892503 (-0.000060)
p19: kimchi-stew-ratio type 'theme' -> 'concept'
p19: cooking-moc dropped (was rank 2, score 0.841066)
p19: stock-simmering-basics score 0.664018 -> 0.664348 (+0.000330)
p19: stock-simmering-basics type 'theme' -> 'concept'
p19: cooking appeared at rank 2, score 0.841262
p20: stock-simmering-basics score 0.777641 -> 0.777161 (-0.000480)
p20: stock-simmering-basics type 'theme' -> 'concept'
p20: kimchi-stew-ratio score 0.682951 -> 0.683319 (+0.000368)
p20: kimchi-stew-ratio type 'theme' -> 'concept'
p20: cooking-moc dropped (was rank 3, score 0.610165)
p20: cooking appeared at rank 3, score 0.610628
p21: log-correlation-tracing score 0.681183 -> 0.681293 (+0.000110)
p21: log-correlation-tracing type 'theme' -> 'concept'
p22: backpressure-design-stub score 0.891497 -> 0.891557 (+0.000060)
p22: backpressure-design-stub type 'theme' -> 'concept'
p22: footnote-reading-korea score 0.665917 -> 0.66628 (+0.000363)
p22: footnote-reading-korea type 'theme' -> 'concept'
p23: kimchi-stew-ratio score 0.871031 -> 0.870838 (-0.000193)
p23: kimchi-stew-ratio type 'theme' -> 'concept'
p23: cooking-moc dropped (was rank 2, score 0.792571)
p23: expense-ratio-drag score 0.788972 -> 0.788564 (-0.000408)
p23: expense-ratio-drag type 'theme' -> 'concept'
p23: dividend-yield-vs-total-return score 0.690501 -> 0.690722 (+0.000221)
p23: dividend-yield-vs-total-return type 'theme' -> 'concept'
p23: index-fund-investing score 0.647892 -> 0.648228 (+0.000336)
p23: index-fund-investing type 'theme' -> 'concept'
p23: finance-moc dropped (was rank 6, score 0.529959)
p23: cooking appeared at rank 2, score 0.792853
p23: finance appeared at rank 6, score 0.530446
p24: dividend-tax-korea score 0.884774 -> 0.884721 (-0.000053)
p24: dividend-tax-korea type 'theme' -> 'concept'
p24: finance-moc dropped (was rank 2, score 0.838085)
p24: dividend-yield-vs-total-return score 0.775457 -> 0.77489 (-0.000567)
p24: dividend-yield-vs-total-return type 'theme' -> 'concept'
p24: finance appeared at rank 2, score 0.838301
p25: engineering-moc dropped (was rank 1, score 0.827651)
p25: write-ahead-log-recovery score 0.586515 -> 0.586175 (-0.000340)
p25: write-ahead-log-recovery type 'theme' -> 'concept'
p25: engineering-2026-01-14 dropped (was rank 3, score 0.543785)
p25: 2026-01-14 appeared at rank 3, score 0.541179
p25: engineering appeared at rank 1, score 0.82783
p26: unbilled-receivables-superseded score 0.877325 -> 0.877378 (+0.000053)
p26: unbilled-receivables-superseded type 'theme' -> 'concept'
p26: finance-moc dropped (was rank 2, score 0.702939)
p26: polling-interval-sizing-deprecated score 0.575587 -> 0.575672 (+0.000085)
p26: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p26: finance appeared at rank 2, score 0.703362
p27: expense-ratio-drag score 0.745184 -> 0.74543 (+0.000246)
p27: expense-ratio-drag type 'theme' -> 'concept'
p27: audit-materiality-threshold score 0.718848 -> 0.718885 (+0.000037)
p27: audit-materiality-threshold type 'theme' -> 'concept'
p27: bond-ladder-basics type 'theme' -> 'concept'
p27: segment-reporting-thresholds score 0.61694 -> 0.617288 (+0.000348)
p27: segment-reporting-thresholds type 'theme' -> 'concept'
p27: coffee-extraction-yield score 0.508535 -> 0.508728 (+0.000193)
p27: coffee-extraction-yield type 'theme' -> 'concept'
p27: finance-moc dropped (was rank 6, score 0.504299)
p27: clock-skew-drift-stub score 0.503979 -> 0.504022 (+0.000043)
p27: clock-skew-drift-stub type 'theme' -> 'concept'
p27: blue-green-deploy-window score 0.443948 -> 0.444182 (+0.000234)
p27: blue-green-deploy-window type 'theme' -> 'concept'
p27: polling-interval-sizing-deprecated score 0.378671 -> 0.379056 (+0.000385)
p27: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p27: finance appeared at rank 6, score 0.504765
p28: knife-sharpening-angles score 0.849072 -> 0.848857 (-0.000215)
p28: knife-sharpening-angles type 'theme' -> 'concept'
p28: cooking-moc dropped (was rank 2, score 0.754202)
p28: unbilled-receivables-superseded score 0.630635 -> 0.630897 (+0.000262)
p28: unbilled-receivables-superseded type 'theme' -> 'concept'
p28: cooking-as-a-finance-habit score 0.597944 -> 0.598283 (+0.000339)
p28: cooking-as-a-finance-habit type 'theme' -> 'concept'
p28: index-fund-investing score 0.596763 -> 0.597104 (+0.000341)
p28: index-fund-investing type 'theme' -> 'concept'
p28: segment-reporting-thresholds score 0.593271 -> 0.593619 (+0.000348)
p28: segment-reporting-thresholds type 'theme' -> 'concept'
p28: schema-migration-locks score 0.468307 -> 0.468509 (+0.000202)
p28: schema-migration-locks type 'theme' -> 'concept'
p28: write-ahead-log-recovery score 0.451265 -> 0.451516 (+0.000251)
p28: write-ahead-log-recovery type 'theme' -> 'concept'
p28: blue-green-deploy-window score 0.420756 -> 0.420994 (+0.000238)
p28: blue-green-deploy-window type 'theme' -> 'concept'
p28: polling-interval-sizing-deprecated score 0.355086 -> 0.355468 (+0.000382)
p28: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p28: cooking appeared at rank 2, score 0.754524
p29: quarterly-close-checklist score 0.920859 -> 0.920791 (-0.000068)
p29: quarterly-close-checklist type 'theme' -> 'concept'
p29: finance-moc dropped (was rank 2, score 0.756627)
p29: pantry-staples-rotation score 0.742896 -> 0.745749 (+0.002853)
p29: pantry-staples-rotation type 'theme' -> 'concept'
p29: weeknight-meal-prep score 0.673493 -> 0.673745 (+0.000252)
p29: weeknight-meal-prep type 'theme' -> 'concept'
p29: audit-materiality-threshold score 0.662322 -> 0.662623 (+0.000301)
p29: audit-materiality-threshold type 'theme' -> 'concept'
p29: expense-ratio-drag score 0.629787 -> 0.630062 (+0.000275)
p29: expense-ratio-drag type 'theme' -> 'concept'
p29: finance-2026-01-12 dropped (was rank 7, score 0.625486)
p29: cooking-as-a-finance-habit score 0.566516 -> 0.56685 (+0.000334)
p29: cooking-as-a-finance-habit type 'theme' -> 'concept'
p29: index-fund-investing score 0.565351 -> 0.565687 (+0.000336)
p29: index-fund-investing type 'theme' -> 'concept'
p29: segment-reporting-thresholds score 0.561912 -> 0.562255 (+0.000343)
p29: segment-reporting-thresholds type 'theme' -> 'concept'
p29: bond-ladder-basics score 0.431393 -> 0.431567 (+0.000174)
p29: bond-ladder-basics type 'theme' -> 'concept'
p29: write-ahead-log-recovery score 0.419743 -> 0.419996 (+0.000253)
p29: write-ahead-log-recovery type 'theme' -> 'concept'
p29: polling-interval-sizing-deprecated score 0.324154 -> 0.324526 (+0.000372)
p29: polling-interval-sizing-deprecated type 'theme' -> 'concept'
p29: 2026-01-12 appeared at rank 7, score 0.622942
p29: finance appeared at rank 2, score 0.757006
p30: sourdough-starter-schedule score 0.774645 -> 0.774379 (-0.000266)
p30: sourdough-starter-schedule type 'theme' -> 'concept'
p30: revenue-recognition-milestones score 0.737896 -> 0.738119 (+0.000223)
p30: revenue-recognition-milestones type 'theme' -> 'concept'
p30: cooking-moc dropped (was rank 3, score 0.590381)
p30: cooking appeared at rank 3, score 0.590844
```

---

## Per-category accounting

Every line of both listings falls into exactly one row. The counts are asserted to sum to the
listings above.

| category | fm-on | fm-off | what it is |
| --- | ---: | ---: | --- |
| header | 2 | 2 | `repo` and `kb_schema_version` — the two records name two layouts |
| `type` mirror `'theme' -> 'concept'` | 178 | 180 | D2.5: the kind replaces the v1 `type:` enum |
| rename — map dropped (`<domain>-moc`) | 41 | 41 | D5: the `-moc` suffix moved into the directory |
| rename — map appeared (`<domain>`) | 41 | 41 | the same note, re-entering under its new basename |
| rename — journal dropped (`<domain>-YYYY-MM-DD`) | 8 | 7 | D2.6: the domain no longer namespaces the date |
| rename — journal appeared (`YYYY-MM-DD`) | 8 | 7 | the same note, re-entering under its new basename |
| score delta | 141 | 143 | the journal merge shifting the BM25F corpus statistics |
| rank move annotated `(score unchanged)` | 2 | 1 | a note passively displaced by a neighbour that moved |
| rank move, score changed | 2 | 1 | explained per line below |
| `match_reason` change | 0 | 0 | no candidate crossed a §7 reason boundary |
| `status` / `expect` flip | 0 | 0 | no probe changed its ADR-0012 §5 answer |
| `title` / `anchor` / `line` / `excerpt` change | 0 | 0 | the §7 extraction contract is content-derived |
| **total** | **423** | **423** | the whole listing |

### 1 · header (2 / 2)

```text
header: repo 'rank-golden-v1' -> 'rank-golden-v2'
header: kb_schema_version 1 -> 2
```

`kb_schema_version` is the flip itself. `repo` is the corpus **directory name**, which lands in the
header because `Wiki.repo` is the layout root's name; the two records are two different trees on
disk and `regen` names them accordingly, so `diff_snapshots` reporting it is the reporter doing its
job. Neither line is a ranking quantity.

### 2 · the `type` mirror (178 / 180)

Every one of these is `type 'theme' -> 'concept'`. ADR-0041 D2.5 retires the closed v1 `type:` enum
(`index | moc | theme | daily`) in favour of the directory-derived kind, and `_hit_record` records a
hit's declared `type` precisely because *"the directory IS the kind" is the Stratum axis*. A hit
whose score is unchanged but whose type moved is exactly the silent regression the field was
recorded to surface; here it moved for every recorded concept, by design, and for nothing else.

The maps and journals do **not** appear in this category even though their kinds also changed
(`moc → map`, `daily → note`): they were renamed, so the listing reports them as a drop and an
appearance rather than as a field change on one row. The fm-off column carries two more of these
lines than fm-on for the ordinary reason the two columns differ at all: it records 180 concept hits
to fm-on's 178, because without the §8 boost the demoted `deprecated` near-duplicate and several
stubs rise back into the recorded top-20 (see README.md).

### 3 · the six renames (98 / 96)

Two families, both from ADR-0041, both closed and pinned by
`test_the_flip_renamed_exactly_the_maps_and_the_journals`:

* **D5, the maps.** `finance-moc → finance`, `cooking-moc → cooking`,
  `engineering-moc → engineering`. The kind marker moved out of the filename and into the
  directory, so the suffix is gone.
* **D2.6, the journals.** `finance-2026-01-12 → 2026-01-12`, `cooking-2026-01-13 → 2026-01-13`,
  `engineering-2026-01-14 → 2026-01-14`. v1 namespaced the date with the domain *because* bare
  dates would have collided across domains; schema 2 removed the domain from the path, so the
  collision reason is gone and the journal is one note per `run_date`, repo-wide.

The record is **basename-keyed** — deliberately, so it survives a change that renames every *path* —
which means a renamed *note* is the one thing it cannot see through: it leaves as `dropped` and
re-enters as `appeared`, and the pair must be read together. **All 41 map pairs sit at the same
rank on both sides of the flip, in both columns** — the direct visual evidence that the seed
survived the move. Two journal pairs do not (`q146-1` rank 3 → 4 and `p06` rank 17 → 18, fm-on
only), and both are explained per line under category 5.

`queries.yaml` still names the v1 basenames (`p25 → engineering-moc`, `p26 → finance-moc`), on
purpose: the query file records a *judgement* about which note should answer, and that judgement did
not change when the file moved. The translation happens once, in `regen.expected_note`, through the
`v2_basename` mapping the fixture builder exports for exactly this.

### 4 · score deltas (141 / 143)

All 141/143 come from ADR-0041 D2.6's journal merge, and only from it (see "the evidence" above).
The mechanism: the merge retitles each journal to the bare date and moves the contributing daily's
title into a `## <contributor title>` heading, which the parser counts in **both** the `headings`
field (weight 2.0) and `body` (1.0) instead of in `title` (3.0). That changes three notes' per-field
document lengths and therefore the corpus-wide `avgdl` denominators and per-field document
frequencies that BM25F normalizes against — which every note's `lex` reads.

The deltas are small and one-sided in magnitude, not in direction: **median 0.000234, maximum
0.003974**, with only three above 0.001 (`cash-flow-forecast-window` on `p01`/`p06` at +0.003974 and
`pantry-staples-rotation` on `p29` at +0.002853). Both columns carry the same deltas because the §8
boost is an additive constant.

Two facts worth stating rather than leaving implied: the deltas are **too small to move a rank**
almost everywhere (4 rank moves out of 141/143 score changes), and the three journals themselves are
not in this category at all — being renamed, their own score changes are inside the drop/appear
pairs (`finance-2026-01-12` 0.68708 → `2026-01-12` 0.683654, and so on).

### 5 · rank moves (4 / 2), and the tie-break artefacts that did not happen

D5 warned that *"7 recorded hits (13 in the `fm_off` column) sit in exact score ties broken by
`_order_key`'s `note.path` tail, and the flip changes every path, so some rank swaps **will** be
tie-break artefacts"*. **None did.** The corpus has 3 tie groups covering 7 hits (fm-on) and 5
groups covering 13 hits (fm-off) — exactly the numbers README.md records — and not one of them
reordered. The reason is that a tie group in this corpus never spans two v1 domains, so flattening
`wiki/<domain>/themes/<slug>.md` into `wiki/concepts/<slug>.md` leaves each group's internal path
order alone.

The two lines that `diff_snapshots` *does* annotate `(score unchanged)` are therefore not tie-break
artefacts but the reporter's other documented case — *"another note moving past it"*:

```text
p01: dividend-yield-vs-total-return rank 17 -> 18 (score unchanged)   [fm-on]
p01: dividend-yield-vs-total-return rank 18 -> 19 (score unchanged)   [fm-off]
p06: knife-sharpening-angles rank 18 -> 17 (score unchanged)          [fm-on]
```

Each is the passive half of a scored move explained per line in the next section.

---

## Per line — the D5 obligation

D5 requires a per-line explanation for exactly two classes.

### (a) every `match_reason` change — **there are none**

Zero in both columns. This is the fixture's most direct read-out of the structural term:
README.md's coverage table records that a `d_moc` change large enough to cross a reason boundary
shows up here as `linked-theme → lexical`, and the simulated seed loss produced **36** such lines
per column. The real flip produced none, which is the same finding as the headline arriving by a
second route — no candidate's `d_moc` moved, so no candidate changed which §7 reason admitted it.

### (b) every rank move not annotated `(score unchanged)` — **three**

**`q146-1: unbilled-receivables-superseded rank 4 -> 3`** (fm-on). The note's own score *fell*
(0.684718 → 0.684598, −0.000120) and it rose a rank anyway, because the journal above it fell
further: `finance-2026-01-12` at 0.68708 became `2026-01-12` at 0.683654 (−0.003426) and crossed
below it. Cause: category 4 — the D2.6 merge, acting on the journal. The fm-off column does not
carry this line because `unbilled-receivables-superseded` *is* the `deprecated` near-duplicate: with
the §8 demotion off it already sits at rank 2, above the journal, so the journal's fall cannot
displace it.

**`p01: cash-flow-forecast-window rank 18 -> 17`** (fm-on) / **`rank 19 -> 18`** (fm-off). This is
the corpus's largest single delta, +0.003974 (0.623038 → 0.627012), which lifts it past
`dividend-yield-vs-total-return` at an unchanged 0.623125 — the passive `(score unchanged)` line in
the same query. Cause: category 4. Nothing about `cash-flow-forecast-window` changed; the statistics
it is normalized against did.

**`p06: knife-sharpening-angles rank 18 -> 17 (score unchanged)`** is listed here for completeness
even though it *is* annotated: it is the passive half of a drop/appear pair rather than of another
rank line, so a reader scanning only the un-annotated lines would not otherwise meet it. The
engineering journal fell from 0.601081 to 0.596315 (−0.004766) and crossed below the knife note's
unchanged 0.600119. Cause: category 4.

---

## The 347/347 citation, and why this listing is larger

D5 requires citing the baseline *"so a materially larger listing is itself a signal that something
beyond the seed rule moved"*. README.md's coverage table records:

> | `_is_moc_path` always `False` (**the flip, simulated**) | **347 / 347** | the whole set |

This listing is **423 / 423** — materially larger. The signal fired, and the answer is that
something beyond the seed rule *did* move: the `type` vocabulary and six filenames, neither of which
the simulation touched, because a mutation of one predicate cannot rename a file or retire an enum.
Read as *ranking* movement, the listing is much **smaller** than the baseline, not larger:

| | simulated flip (347) | this flip (423) |
| --- | --- | --- |
| `score` lines | 169 / 168 | 141 / 143 |
| `rank` lines | 111 / 110 | 4 / 2 |
| `match_reason` lines | 36 / 36 | 0 / 0 |
| `anchor` / `line` / `excerpt` lines | 29 / 29 | 0 / 0 |
| `dropped` / `appeared` lines | 2 / 4 | 98 / 96 (all six renames) |
| `type` lines | 0 / 0 | 178 / 180 |

The 347 number is not a historical citation here — it is **live**.
`regen.frozen_baseline_drift()` measures it on every run and
`test_the_frozen_baseline_moved_by_exactly_the_lost_seeds` asserts it, because the two things turn
out to be the same measurement: simulating the flip meant forcing `_is_moc_path` to `False`, and the
schema-2 predicate returns exactly that for every **v1** path — a v1 directory names no kind
(ADR-0041 D5, "no shim"). So a schema-1 corpus read by today's build differs from the frozen
`golden_v1` record by exactly 347 lines per column, and that assertion is now the only thing
defending a record this build can no longer reproduce.

---

## Two records, one of them frozen

`regen` can only produce the *current* layout, so D5 requires the pre-flip records to be preserved
rather than overwritten before the listing is taken. They are:

| file | layout | status |
| --- | --- | --- |
| `golden_v1.json` · `golden_v1_fm_off.json` | ADR-0010 v1 | **frozen** — history; `regen.write_records` refuses to write it |
| `golden_v2.json` · `golden_v2_fm_off.json` | ADR-0041 kind-first | live — regenerated by `python -m tests.rank_golden.regen` |

The refusal is a `ValueError`, not a convention. A convention would have held right up until the
first person ran the obvious command.

## What this listing does not cover

Three limits, named rather than left to be rediscovered.

1. **One map per subject.** This corpus puts exactly one map in `wiki/maps/` per domain, which is
   what makes the seed population identical across the flip. D5 anticipated the other case —
   *"`wiki/maps/` may hold arbitrarily many maps, so the seed population, the `d_moc` distribution
   and `indeg_norm`'s denominator can all change without any scoring constant changing"* — and this
   fixture does **not** measure it. A repo that splits one subject across several maps, or files one
   map under several subjects, will move ranking in a way nothing here has recorded.
2. **The reader cache is not exercised.** `header["index_cache_used"]` is `false` in all four
   records (`kb_builder` runs no `git init`, so there is no curated commit and every number came
   from the full scan). The ADR-0012 §2 cache payload is keyed by repo-relative POSIX path — the one
   derived structure the flip is guaranteed to invalidate — and `CACHE_SCHEMA_VERSION` 2 → 3 is what
   handles it. Neither is covered here.
3. **The private KB is a separate measurement.** The committed golden covers a synthetic corpus,
   because that is the only corpus that can live in git. The owner-side `agora eval` snapshot over
   `~/knowledge-agora-dogfood` (README.md, "The owner-side dogfood snapshot") is where the flip's
   effect on a real, many-map KB gets read.
