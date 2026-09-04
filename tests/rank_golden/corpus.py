"""The synthetic corpus the ranking golden is recorded over (Stratum UNIT 3, gate B).

46 notes across three domains — ``finance``, ``cooking``, ``engineering`` — chosen so the four
issue-#146 queries have DEFINED outcomes and so every shape that moves the ADR-0012 score is
present at least once. "Present" is not enough on its own: each shape below was kept only after a
MUTATION of the term it exercises was shown to redden the golden (the table in README.md records
which query is the evidence for which term, and the one term no output-level fixture can observe).

============================ ===============================================================
shape                        where
============================ ===============================================================
``note-<sha8>`` husks        three, one per domain — thin bodies with Korean titles, so the
                             basename falls through the production slugger to the canonical
                             body hash. Their slugs are DERIVED (no ``slug=``), never typed
the #146 label husk          the engineering husk carries ``moc_label`` — a MOC bullet whose
                             text shares NO token with the note it points at, which is the
                             only construction that reaches ``_passes_gate``'s ``d_moc == 0``
                             branch with ``lex == 0`` (query ``p25``)
Korean bodies (CJK bigrams)  ``dividend-tax-korea``, ``footnote-reading-korea``,
                             ``kimchi-stew-ratio``, ``stock-simmering-basics``,
                             ``backpressure-design-stub``, ``log-correlation-tracing``
a stubs-only MOC             ``engineering`` — its MOC links stubs and the husk, nothing else
orphan themes                ``bond-ladder-basics``, ``coffee-extraction-yield``,
                             ``clock-skew-drift-stub``, ``polling-interval-sizing-deprecated``,
                             plus every non-stub engineering theme
aliases                      ``expense-ratio-drag``, ``pantry-staples-rotation``, and
                             ``bond-ladder-basics`` — whose alias tokens appear in NO other
                             field of NO other note, so the §4 alias weight has rank-level
                             consequences (``p27``) instead of a 4th-decimal wobble
contested shape              ``audit-materiality-threshold``
deprecated shape             ``unbilled-receivables-superseded`` (MOC-linked near-duplicate of
                             an active note — ``p26``) and ``polling-interval-sizing-deprecated``
                             (orphan). ADR-0012 §8's −0.15 is the ranker's ONLY negative term
FLOOR-adjacent scores        the deprecated orphan lands at 0.205086 in ``p28`` (just OVER
                             FLOOR = 0.18) and 0.174154 in ``p29`` (just UNDER), which is what
                             bounds the floor's position to (0.174, 0.205]
dailies                      one per domain (2026-01-12 / -13 / -14)
============================ ===============================================================

CONTENT ONLY. Every path decision lives in :mod:`tests.support.kb_builder`, so UNIT 2 can
materialize this same corpus under the Stratum layout without touching a single note body — which
is the whole point: the ranking delta then belongs to the layout, not to the fixture.

All text is original synthetic prose. No real personal data, no copied source material.
"""

from __future__ import annotations

from tests.support.kb_builder import BUILDER_DATE, NoteSpec

__all__ = ["CORPUS", "DOMAINS"]

DOMAINS = ["cooking", "engineering", "finance"]


# --- the #57 / #146 husks ------------------------------------------------------------------------
#
# Their basenames are DERIVED, never typed: `NoteSpec.basename()` falls through `slugify` (the
# production slugger) to `hash_basename` (the production canonical `content_sha256` of the note
# body), so if either drifts this fixture drifts with it and the golden goes red. A hand-written
# `note-<sha8>` literal would look like the #57 shape while exercising none of it.
#
# `_ENGINEERING_HUSK` is the one that reaches the #146 code path. `core.wiki._passes_gate`'s
# `d_moc == 0` branch admits a candidate whose ONLY overlap is with `moc_label_tokens` — the MOC
# bullet's link TEXT, which is not one of the note's own scoring fields — and `_combined`'s #146
# guard is what stops such a `lex == 0` candidate from taking the full structural term (0.245,
# comfortably over FLOOR) and surfacing as a hit. That path is reachable only if some bullet label
# shares no token with the note it points at, which is why this husk carries `moc_label`.

_FINANCE_HUSK = NoteSpec(
    kind="theme",
    domain="finance",
    title="미수금 정산 메모",
    summary="정리되지 않은 메모.",
    status="stub",
    body="",
)

_COOKING_HUSK = NoteSpec(
    kind="theme",
    domain="cooking",
    title="냉장고 정리 규칙",
    summary="정리되지 않은 메모.",
    status="stub",
    body="",
)

_ENGINEERING_HUSK = NoteSpec(
    kind="theme",
    domain="engineering",
    title="미정리 메모",
    # Shares no token with the note's title, summary, tags, headings or body — so a query written
    # from the LABEL leaves the husk at lex == 0 and it can only be admitted by the gate's second
    # branch. Reverting the #146 guard resurfaces it as a hit; that is what query `p25` pins.
    moc_label="Deadlock recovery playbook",
    summary="정리되지 않은 메모.",
    status="stub",
    body="",
)

# The ADR-0012 §8 NEGATIVE frontmatter term (−0.15) — the only one, and the half of the §8 table a
# corpus of active/stub/contested notes leaves unpinned. Deliberately a near-duplicate of the
# active `unbilled-receivables-recognition`, MOC-linked (so both sit at d_moc == 0 and the
# structural term cancels) and sharing its vocabulary, so the demotion is what separates them.
_DEPRECATED_RECEIVABLES = NoteSpec(
    kind="theme",
    domain="finance",
    slug="unbilled-receivables-superseded",
    title="Unbilled receivables recognition, superseded note",
    summary="Superseded — an older reading of when an unbilled receivable is recognised.",
    tags=["revenue-recognition", "receivables"],
    status="deprecated",
    body=(
        "An earlier note on unbilled receivables recognition, kept only so links to it still\n"
        "resolve. It treated the unbilled balance as a trade receivable from the moment the\n"
        "work was delivered, which is the reading the current note replaces.\n"
        "\n"
        "## Why it was superseded\n"
        "\n"
        "Recognition follows the satisfied obligation, but the balance is a contract asset\n"
        "until the invoice is raised — a distinction this note collapsed.\n"
    ),
)


# --- finance ------------------------------------------------------------------------------------

_FINANCE: list[NoteSpec] = [
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="unbilled-receivables-recognition",
        title="Unbilled receivables recognition",
        summary="Delivered work that has not been invoiced yet sits in unbilled receivables.",
        tags=["revenue-recognition", "receivables"],
        related=["revenue-recognition-milestones"],
        body=(
            "Work that has been delivered but not yet invoiced is carried as an unbilled\n"
            "receivable: the obligation is satisfied, the invoice simply has not been raised.\n"
            "It is a contract asset, not a trade receivable, because collection still depends\n"
            "on raising the bill rather than on the customer paying it.\n"
            "\n"
            "## When the balance clears\n"
            "\n"
            "The unbilled balance clears the moment the invoice is issued, which reclassifies\n"
            "it into ordinary receivables. A balance that never clears is usually a billing\n"
            "backlog rather than a recognition problem.\n"
            "\n"
            "## Why the ageing matters\n"
            "\n"
            "Ageing an unbilled balance separates a slow billing pipeline from work that was\n"
            "recognised too early. See [Revenue recognition milestones]"
            "(revenue-recognition-milestones.md).\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="index-fund-investing",
        title="Index fund investing basics",
        summary="A broad fund buys the whole market instead of trying to pick the winners.",
        tags=["investing", "passive-funds"],
        related=["expense-ratio-drag"],
        body=(
            "Investing through a broad fund means owning every listed company in a chosen\n"
            "market in proportion to its size, rather than betting on a handful of names.\n"
            "The bet is that the average outcome, held cheaply for a long time, beats the\n"
            "average active attempt after costs.\n"
            "\n"
            "## Why the whole market\n"
            "\n"
            "A tracker fund never has to be right about which company wins; it only has to\n"
            "stay invested. Concentration risk falls to whatever the market itself carries.\n"
            "\n"
            "## What still matters\n"
            "\n"
            "Cost and contribution rate, not cleverness. See [Expense ratio drag on long "
            "horizons](expense-ratio-drag.md).\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="segment-reporting-thresholds",
        title="Segment reporting thresholds",
        summary="A segment becomes reportable once it crosses one of the quantitative tests.",
        tags=["reporting", "segments"],
        body=(
            "An operating segment has to be reported separately once it is big enough to\n"
            "change how a reader understands the business. The test is mechanical, and it is\n"
            "applied segment by segment before any aggregation is considered.\n"
            "\n"
            "## Quantitative thresholds\n"
            "\n"
            "Three thresholds are applied, and crossing any one of them is enough: a tenth of\n"
            "combined revenue, a tenth of the larger of combined profit or combined loss, or\n"
            "a tenth of combined assets. Segments below every threshold may still be reported\n"
            "voluntarily.\n"
            "\n"
            "## Aggregation rules\n"
            "\n"
            "Two segments may be combined only when they share economic characteristics and\n"
            "the same kind of customer; aggregation is never a way to duck a threshold.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="revenue-recognition-milestones",
        title="Revenue recognition milestones",
        summary="Milestone billing recognises revenue as each obligation is satisfied.",
        tags=["revenue-recognition"],
        body=(
            "A milestone contract splits one long engagement into separately satisfiable\n"
            "obligations. Revenue attaches to the obligation, not to the payment schedule,\n"
            "so an early payment is a liability and late billing is an asset.\n"
            "\n"
            "## Ordering the milestones\n"
            "\n"
            "Milestones that cannot be delivered independently are a single obligation, no\n"
            "matter how the payment plan is written.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="deferred-revenue-basics",
        title="Deferred revenue basics",
        summary="Cash collected before delivery is a liability until the work is done.",
        tags=["revenue-recognition"],
        body=(
            "Money taken before the work is delivered is not income; it is a promise. The\n"
            "balance sits as a liability and is released as the promise is kept, which is why\n"
            "a growing deferred balance can be good news and a shrinking one bad.\n"
            "\n"
            "## Release profile\n"
            "\n"
            "A subscription releases evenly; a delivery contract releases in steps.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="dividend-yield-vs-total-return",
        title="Dividend yield versus total return",
        summary="A high payout is not the same thing as a high return.",
        tags=["investing"],
        body=(
            "Yield measures what a holding pays out; total return measures what the holding\n"
            "actually earned, payout and price change together. Chasing the first while\n"
            "ignoring the second is how a shrinking company looks generous.\n"
            "\n"
            "## The trap\n"
            "\n"
            "A payout ratio above earnings is a liquidation on a slow timer.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="expense-ratio-drag",
        title="Expense ratio drag on long horizons",
        summary="A small annual fee compounds into a large share of the final balance.",
        tags=["investing", "fund-costs"],
        aliases=["fund-fee-drag"],
        body=(
            "A fee is charged on the whole balance every year, including on the growth the\n"
            "earlier fees already took. Over a working life the gap between a cheap fund and\n"
            "an expensive one is not the fee itself but everything the fee never got to\n"
            "compound.\n"
            "\n"
            "## Reading the number\n"
            "\n"
            "The headline ratio omits trading costs, so the real drag is a little worse.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="quarterly-close-checklist",
        title="Quarterly close checklist",
        summary="The close is a fixed sequence, run the same way every quarter.",
        tags=["reporting"],
        body=(
            "The close is boring on purpose: cut-off, accruals, reconciliations, review,\n"
            "sign-off, in that order. Every quarter that skipped a step found out later which\n"
            "step it skipped.\n"
            "\n"
            "## Cut-off first\n"
            "\n"
            "Nothing else can be trusted until the period boundary is fixed.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="cash-flow-forecast-window",
        title="Cash flow forecast window",
        summary="A thirteen week window is short enough to be honest and long enough to act on.",
        tags=["treasury"],
        body=(
            "A rolling thirteen week cash forecast is the working horizon: far enough ahead\n"
            "that a gap can still be fixed, near enough that the numbers are real receipts\n"
            "and real payments rather than an annual plan restated weekly.\n"
            "\n"
            "## Rolling, not restated\n"
            "\n"
            "Each week drops off the front and a new week joins the back.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="working-capital-cycle",
        title="Working capital cycle",
        summary="Cash is trapped between paying suppliers and being paid by customers.",
        tags=["treasury"],
        body=(
            "The cycle is the number of days between paying for inputs and collecting from\n"
            "customers. Shortening it releases cash without earning a single extra unit of\n"
            "revenue, which is why it is the cheapest funding available.\n"
            "\n"
            "## Three levers\n"
            "\n"
            "Collect sooner, hold less stock, pay on the agreed day rather than early.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="dividend-tax-korea",
        title="배당소득세 원천징수 구조",
        summary="배당금은 지급 시점에 원천징수되고 금융소득 합계에 따라 종합과세로 넘어간다.",
        tags=["investing", "tax-korea"],
        body=(
            "배당금은 계좌에 들어오기 전에 이미 세금이 떼여 있다. 지급하는 쪽이 원천징수를 하고\n"
            "남은 금액만 입금하기 때문에, 통장에 찍힌 숫자는 세후 금액이다.\n"
            "\n"
            "## 종합과세 기준\n"
            "\n"
            "이자와 배당을 합한 금융소득이 기준 금액을 넘으면 다른 소득과 합산해 다시 계산한다.\n"
            "기준을 넘지 않으면 원천징수로 납세 의무가 끝난다.\n"
            "\n"
            "## 해외 배당\n"
            "\n"
            "해외에서 이미 낸 세금은 이중과세를 피하기 위해 일정 범위 안에서 공제한다.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="footnote-reading-korea",
        title="재무제표 주석 읽는 순서",
        summary="주석은 숫자보다 늦게 읽되 반드시 읽어야 하는 부분이다.",
        tags=["reporting"],
        body=(
            "재무제표에서 가장 많은 정보가 들어 있는 곳은 본문이 아니라 주석이다. 회계정책,\n"
            "우발부채, 특수관계자 거래가 모두 여기에 적혀 있다.\n"
            "\n"
            "## 읽는 순서\n"
            "\n"
            "회계정책 변경을 먼저 보고, 그다음 우발부채와 약정을 본다. 마지막으로 특수관계자\n"
            "거래를 확인하면 숫자가 왜 그렇게 나왔는지 대부분 설명된다.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="audit-materiality-threshold",
        title="Audit materiality threshold",
        summary="Two teams set the same threshold from different bases and disagree.",
        tags=["reporting"],
        status="contested",
        sources=[
            "raw/finance/2026-01-08-materiality-memo.md",
            "raw/finance/2026-01-09-materiality-review.md",
        ],
        extra_frontmatter={
            "contested_by": ["reporting-team", "audit-team"],
            "contested_at": BUILDER_DATE,
        },
        body=(
            "> [!contested] Two live readings of the same threshold.\n"
            "\n"
            "Materiality fixes how large a misstatement has to be before it changes a\n"
            "reader's decision. The number itself is judgement dressed as arithmetic.\n"
            "\n"
            "## Reading A — profit basis\n"
            "\n"
            "Anchoring on profit keeps the threshold close to what a reader reacts to, but it\n"
            "swings wildly for a business near break-even.\n"
            "\n"
            "## Reading B — revenue basis\n"
            "\n"
            "Anchoring on revenue is stable year to year, at the cost of being loose for a\n"
            "high volume, thin margin business.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="hedge-accounting-stub",
        title="Hedge accounting",
        summary="Placeholder — hedge designation and effectiveness testing are not written yet.",
        tags=["reporting"],
        status="stub",
        body="",
    ),
    _FINANCE_HUSK,
    _DEPRECATED_RECEIVABLES,
    NoteSpec(
        kind="theme",
        domain="finance",
        slug="bond-ladder-basics",
        title="Bond ladder basics",
        summary="Staggered maturities trade a little yield for a lot of flexibility.",
        tags=["investing"],
        # An alias whose tokens appear in NO other field of NO other note, on an ORPHAN. The #56
        # addendum gives `aliases` title-equivalent weight 3.0 AND exempts it from length
        # normalization (`FIELD_B['aliases'] = 0`); this is the note that makes either of those
        # load-bearing, because reaching it through the alias is the only way it can outrank a
        # MOC-linked competitor whose structural term is four times its own.
        aliases=["gilt-staircase"],
        body=(
            "A ladder holds bonds maturing in consecutive years, so something matures every\n"
            "year regardless of where rates went. Reinvesting each rung at the far end keeps\n"
            "the average maturity stable without ever forcing a sale.\n"
            "\n"
            "## Why not one maturity\n"
            "\n"
            "A single date concentrates every reinvestment decision into one bad morning.\n"
        ),
    ),
]

# --- cooking ------------------------------------------------------------------------------------

_COOKING: list[NoteSpec] = [
    NoteSpec(
        kind="theme",
        domain="cooking",
        slug="cooking-as-a-finance-habit",
        title="Cooking as a finance habit",
        summary="Investing planning time each week pays back the way a finance habit does.",
        tags=["meal-planning", "personal-finance"],
        related=["weeknight-meal-prep"],
        body=(
            "Cooking at home is usually filed under taste or health, but the reliable part is\n"
            "financial: the same week of meals bought as ingredients rather than as finished\n"
            "dishes costs a fraction, every single week, forever.\n"
            "\n"
            "## Treat the plan as a contribution\n"
            "\n"
            "Investing forty minutes on a Sunday behaves like any other automatic transfer —\n"
            "small, dull, and only visible after a year of it. The finance framing helps\n"
            "because it stops the plan being renegotiated on a tired Wednesday.\n"
            "\n"
            "## What to measure\n"
            "\n"
            "Count meals cooked, not money saved; the saving follows and is easier to fake.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="cooking",
        slug="weeknight-meal-prep",
        title="Weeknight meal prep blocks",
        summary="Prepare components, not finished meals, so a tired evening still has options.",
        tags=["meal-planning"],
        body=(
            "Batch a grain, a braise and a bright pickle on the weekend and every weeknight\n"
            "becomes assembly rather than cooking. Finished dishes reheat badly and bore\n"
            "faster; components recombine into four different dinners.\n"
            "\n"
            "## The three block rule\n"
            "\n"
            "One starch, one protein, one acid. Anything else is a bonus.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="cooking",
        slug="sourdough-starter-schedule",
        title="Sourdough starter schedule",
        summary="Feed on a rhythm the flour and the room temperature can actually keep.",
        tags=["baking"],
        body=(
            "A starter is a colony on a feeding schedule. Twice a day at warm room\n"
            "temperature, once a day when cooler, and a refrigerated jar can wait a week.\n"
            "\n"
            "## Reading the peak\n"
            "\n"
            "Use it when it has just crested, not after it has fallen back.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="cooking",
        slug="knife-sharpening-angles",
        title="Knife sharpening angles",
        summary="A narrower angle cuts better and chips sooner; pick per knife, not per kitchen.",
        tags=["kitchen-tools"],
        body=(
            "The bevel angle trades keenness against durability. A thin slicing edge wants a\n"
            "narrow angle; a cleaver that meets bone wants a wide one.\n"
            "\n"
            "## Keeping the angle\n"
            "\n"
            "Consistency beats the exact number: a steady wrong angle out-cuts a wandering\n"
            "right one.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="cooking",
        slug="braising-temperature-window",
        title="Braising temperature window",
        summary="Hold below a simmer so collagen melts before the muscle fibres dry out.",
        tags=["technique"],
        body=(
            "A braise wants a bare shiver, not a rolling boil. Too hot and the fibres tighten\n"
            "and squeeze out their moisture faster than the connective tissue can soften.\n"
            "\n"
            "## Collagen and time\n"
            "\n"
            "Collagen becomes gelatin slowly, and slowly is the whole method.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="cooking",
        slug="pantry-staples-rotation",
        title="Pantry staples rotation",
        summary="Shelve new stock behind old stock so the oldest jar is always the nearest one.",
        tags=["meal-planning"],
        aliases=["pantry-first-in-first-out"],
        body=(
            "Rotation is the entire discipline: new tins go behind, old tins come forward,\n"
            "and nothing has to be remembered. A pantry that is never rotated is a slow way\n"
            "of throwing food away.\n"
            "\n"
            "## The quarterly sweep\n"
            "\n"
            "Once a quarter, pull everything forward and cook whatever surfaced.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="cooking",
        slug="kimchi-stew-ratio",
        title="김치찌개 황금 비율",
        summary="잘 익은 김치와 국물의 비율이 맛의 대부분을 결정한다.",
        tags=["korean-cooking"],
        body=(
            "김치찌개는 재료가 적은 대신 비율이 전부다. 잘 익은 김치를 먼저 기름에 볶아\n"
            "신맛을 눌러 준 다음 국물을 붓는 순서만 지켜도 맛이 달라진다.\n"
            "\n"
            "## 비율 잡기\n"
            "\n"
            "김치 한 컵에 국물 두 컵이 기본이고, 김치가 덜 익었으면 국물을 조금 줄인다.\n"
            "설탕은 신맛을 가리는 용도이지 단맛을 내는 용도가 아니다.\n"
            "\n"
            "## 마지막 단계\n"
            "\n"
            "두부는 마지막에 넣어야 부서지지 않는다.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="cooking",
        slug="stock-simmering-basics",
        title="육수 우려내기 기본",
        summary="센 불로 끓이면 탁해지므로 약한 불에서 오래 우려낸다.",
        tags=["korean-cooking", "technique"],
        body=(
            "육수는 끓이는 것이 아니라 우려내는 것이다. 물이 크게 끓으면 단백질이 부서져\n"
            "국물이 탁해지고 쓴맛이 돈다.\n"
            "\n"
            "## 불 조절\n"
            "\n"
            "표면이 살짝 흔들릴 정도의 약한 불을 유지하고, 떠오르는 거품은 걷어 낸다.\n"
            "다시마는 물이 끓기 직전에 건져야 미끈한 맛이 남지 않는다.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="cooking",
        slug="fermentation-safety-stub",
        title="Fermentation safety",
        summary="Placeholder — brine strength and temperature limits are not written yet.",
        status="stub",
        body="",
    ),
    _COOKING_HUSK,
    NoteSpec(
        kind="theme",
        domain="cooking",
        slug="coffee-extraction-yield",
        title="Coffee extraction yield",
        summary="Grind and contact time move yield; taste tells you which way you moved it.",
        tags=["brewing"],
        body=(
            "Extraction yield is the share of the ground coffee that ends up dissolved in the\n"
            "cup. Under-extracted tastes sour and thin, over-extracted tastes dry and bitter.\n"
            "\n"
            "## Moving one lever\n"
            "\n"
            "Change the grind or the time, never both, or the cup teaches you nothing.\n"
        ),
    ),
]

# --- engineering (its MOC links ONLY stubs; every substantive note here is an orphan) ------------

_ENGINEERING: list[NoteSpec] = [
    NoteSpec(
        kind="theme",
        domain="engineering",
        slug="retry-budget-stub",
        title="Retry budget",
        summary="Placeholder — the budget shape and its exhaustion behaviour are not written yet.",
        status="stub",
        body="",
    ),
    NoteSpec(
        kind="theme",
        domain="engineering",
        slug="idempotent-consumer-stub",
        title="Idempotent consumer",
        summary="Placeholder — dedup key choice and its retention are not written yet.",
        status="stub",
        body="",
    ),
    NoteSpec(
        kind="theme",
        domain="engineering",
        slug="circuit-breaker-stub",
        title="Circuit breaker tripping",
        summary="Placeholder — trip thresholds and half-open probing are not written yet.",
        status="stub",
        body="",
    ),
    NoteSpec(
        kind="theme",
        domain="engineering",
        slug="backpressure-design-stub",
        title="백프레셔 설계 원칙",
        summary="큐가 넘치기 전에 생산자를 늦추는 설계 원칙 메모.",
        status="stub",
        body=(
            "큐가 길어지는 것은 문제가 아니라 증상이다. 소비자가 따라오지 못하면 생산자를\n"
            "먼저 늦춰야 하고, 그 신호를 어디에서 만들지가 설계의 핵심이다.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="engineering",
        slug="write-ahead-log-recovery",
        title="Write ahead log recovery",
        summary="Replay the log from the last checkpoint and stop at the first torn record.",
        tags=["durability"],
        related=["schema-migration-locks"],
        body=(
            "Recovery reads the log forward from the newest checkpoint, reapplying every\n"
            "committed record and discarding anything after the first record whose checksum\n"
            "does not match. A torn tail is normal after a crash; a torn middle is corruption.\n"
            "\n"
            "## Replay order\n"
            "\n"
            "Records replay in write order, which is why the log is append-only.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="engineering",
        slug="schema-migration-locks",
        title="Schema migration locks",
        summary="Take the lock last and hold it for as little of the migration as possible.",
        tags=["durability"],
        body=(
            "A migration that copies data under a lock is an outage with extra steps. Copy\n"
            "first without the lock, then take it only to swap the pointer.\n"
            "\n"
            "## Backfill then swap\n"
            "\n"
            "The lock window should be measured in milliseconds, not minutes.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="engineering",
        slug="blue-green-deploy-window",
        title="Blue green deploy window",
        summary="Keep the old fleet warm until the new one has served real traffic.",
        tags=["deployment"],
        body=(
            "Two fleets, one live. The new fleet takes traffic while the old one stays warm\n"
            "and unchanged, so a rollback is a routing change rather than a rebuild.\n"
            "\n"
            "## Closing the window\n"
            "\n"
            "Retire the old fleet only after a full traffic cycle, including the quiet hours.\n"
        ),
    ),
    NoteSpec(
        kind="theme",
        domain="engineering",
        slug="log-correlation-tracing",
        title="로그 상관관계 추적",
        summary="요청 하나에 붙은 식별자를 모든 서비스가 그대로 넘겨야 추적이 가능하다.",
        tags=["observability"],
        body=(
            "분산 시스템에서 로그가 쓸모없어지는 이유는 양이 아니라 연결이 끊기기 때문이다.\n"
            "요청 시작 지점에서 만든 식별자를 모든 하위 호출이 그대로 전달해야 한다.\n"
            "\n"
            "## 전파 규칙\n"
            "\n"
            "식별자를 새로 만드는 지점은 단 하나여야 하고, 나머지는 받은 값을 그대로 넘긴다.\n"
            "큐를 거치는 경우에도 메시지 헤더에 실어 보낸다.\n"
        ),
    ),
    _ENGINEERING_HUSK,
    # The FLOOR probe (ADR-0012 §5). An orphan (d_moc = 3 → struct ≈ 0.175) STUB (fm = 0) is the
    # only shape in this corpus whose combined score can land NEAR 0.18 at all: a MOC-linked note
    # takes 0.35 * 0.7 ≈ 0.245 from structure alone, and an active orphan starts at ≈ 0.161 from
    # the frontmatter boost. Its vocabulary is deliberately shared with nothing else, so a query
    # written from it has exactly one candidate and the recorded status IS the floor's verdict.
    NoteSpec(
        kind="theme",
        domain="engineering",
        slug="clock-skew-drift-stub",
        title="Clock skew drift",
        summary="Placeholder — bounding the skew between two hosts is not written up yet.",
        status="stub",
        body=(
            "Two machines disagree about the time by more than the operation takes, and the\n"
            "ordering you thought you had is gone. Bounding that disagreement, rather than\n"
            "pretending it is zero, is the entire job.\n"
        ),
    ),
    # The other FLOOR probe, and the second ADR-0012 §8 `deprecated` note. An orphan carrying the
    # −0.15 demotion starts at 0.65 * lex − 0.089, which is the ONLY band in this corpus that
    # straddles FLOOR = 0.18: `p28` lands a candidate just above it and `p29` lands one just below,
    # so moving the floor 0.05 in either direction reddens the golden (see README.md).
    NoteSpec(
        kind="theme",
        domain="engineering",
        slug="polling-interval-sizing-deprecated",
        title="Polling interval sizing",
        summary="Superseded — the queue is watched by an event now, not by a timer.",
        tags=["observability"],
        status="deprecated",
        body=(
            "The old approach checked the queue on a timer and changed the interval by hand\n"
            "after every incident. It worked, but the interval was always wrong between two\n"
            "incidents, and nobody could say what the right one was.\n"
            "\n"
            "## Why it was replaced\n"
            "\n"
            "An event arrives when the work does, so there is no interval left to size and\n"
            "no reason to keep a timer running for a queue that is usually empty. A poll that\n"
            "is still cheap at one queue is not cheap at forty, and the cost is paid whether\n"
            "or not anything arrived. The interval also has to be guessed twice: once for the\n"
            "quiet hours and once for the busy ones, and neither guess survives the week.\n"
            "\n"
            "## What replaced it\n"
            "\n"
            "The producer signals, the consumer wakes, and nothing is asked twice. The note\n"
            "is kept only so the old links still resolve.\n"
        ),
    ),
]

# --- dailies ------------------------------------------------------------------------------------

_DAILIES: list[NoteSpec] = [
    NoteSpec(
        kind="daily",
        domain="finance",
        slug="finance-2026-01-12",
        title="finance daily 2026-01-12",
        summary="Consolidation journal for the finance domain.",
        extra_frontmatter={"date": "2026-01-12"},
        body=(
            "Reviewed the billing backlog and split it from the recognition question.\n"
            "\n"
            "- [Unbilled receivables recognition](../themes/unbilled-receivables-recognition.md)\n"
            "- [Quarterly close checklist](../themes/quarterly-close-checklist.md)\n"
        ),
    ),
    NoteSpec(
        kind="daily",
        domain="cooking",
        slug="cooking-2026-01-13",
        title="cooking daily 2026-01-13",
        summary="Consolidation journal for the cooking domain.",
        extra_frontmatter={"date": "2026-01-13"},
        body=(
            "Tested the component approach for four consecutive evenings.\n"
            "\n"
            "- [Weeknight meal prep blocks](../themes/weeknight-meal-prep.md)\n"
            "- [Braising temperature window](../themes/braising-temperature-window.md)\n"
        ),
    ),
    NoteSpec(
        kind="daily",
        domain="engineering",
        slug="engineering-2026-01-14",
        title="engineering daily 2026-01-14",
        summary="Consolidation journal for the engineering domain.",
        extra_frontmatter={"date": "2026-01-14"},
        body=(
            "Walked a crash recovery by hand and wrote down what the log actually replayed.\n"
            "\n"
            "- [Write ahead log recovery](../themes/write-ahead-log-recovery.md)\n"
        ),
    ),
]

# --- navigation overrides -------------------------------------------------------------------------
#
# Each MOC names its children EXPLICITLY, which is how orphans are declared: a theme absent from
# this list has no inbound link at all (the engineering MOC lists only its four stubs, so every
# substantive engineering note is an orphan).

_NAVIGATION: list[NoteSpec] = [
    NoteSpec(
        kind="moc",
        domain="finance",
        title="finance MOC",
        summary="Map of content for the finance domain.",
        body="Recognition, reporting and the household side of investing.",
        children=[
            "unbilled-receivables-recognition",
            "index-fund-investing",
            "segment-reporting-thresholds",
            "revenue-recognition-milestones",
            "deferred-revenue-basics",
            "dividend-yield-vs-total-return",
            "expense-ratio-drag",
            "quarterly-close-checklist",
            "cash-flow-forecast-window",
            "working-capital-cycle",
            "dividend-tax-korea",
            "footnote-reading-korea",
            "audit-materiality-threshold",
            "hedge-accounting-stub",
            _FINANCE_HUSK.basename(),
            _DEPRECATED_RECEIVABLES.basename(),
        ],
    ),
    NoteSpec(
        kind="moc",
        domain="cooking",
        title="cooking MOC",
        summary="Map of content for the cooking domain.",
        body="Technique, planning, and the recurring weeknight problem.",
        children=[
            "cooking-as-a-finance-habit",
            "weeknight-meal-prep",
            "sourdough-starter-schedule",
            "knife-sharpening-angles",
            "braising-temperature-window",
            "pantry-staples-rotation",
            "kimchi-stew-ratio",
            "stock-simmering-basics",
            "fermentation-safety-stub",
            _COOKING_HUSK.basename(),
        ],
    ),
    NoteSpec(
        kind="moc",
        domain="engineering",
        title="engineering MOC",
        summary="Map of content for the engineering domain.",
        body="Reliability notes. Everything catalogued here is still a stub.",
        children=[
            "retry-budget-stub",
            "idempotent-consumer-stub",
            "circuit-breaker-stub",
            "backpressure-design-stub",
            _ENGINEERING_HUSK.basename(),
        ],
    ),
    NoteSpec(
        kind="index",
        domain="",
        title="Knowledge base",
        summary="Root map of every domain in this knowledge base.",
        body="",
    ),
]

#: The full corpus: 46 notes (39 themes, 3 dailies, 3 MOCs, 1 index).
CORPUS: list[NoteSpec] = [*_FINANCE, *_COOKING, *_ENGINEERING, *_DAILIES, *_NAVIGATION]
