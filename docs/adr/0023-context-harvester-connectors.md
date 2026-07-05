# ADR-0023 — Context-harvester connectors: taxonomy, safety envelope, OSS paths

**Status:** Accepted · 2026-07-05 (Step-0 ratified, #36) · Proposed 2026-06-24

Realizes the broader-source half of [ADR-0007](0007-memory-harvester-safety.md) (memory harvester
with provenance / gate / scope safety) for issues **#25** (harvest analyzes agent *sessions*, not
just `MEMORY.md`) and **#28** (diversify corporate work-context collection: folders, git, mail, chat,
calendar, meetings). Extends [ADR-0004](0004-pluggable-adapters.md) (read-adapter family) and
[ADR-0017](0017-harvester-file-connector-mechanics.md) (the file-connector mechanics this generalizes
from). Bound by [ADR-0005](0005-fully-oss-bom.md) (no AGPL/copyleft in the core; proprietary services
optional behind adapters — invariant #4), [ADR-0006](0006-repo-as-tenant-boundary.md) (repo = tenant
boundary — invariant #5), and [ADR-0002](0002-cqrs-single-writer-curator.md) (the inbox is the only
write path; the curator is the sole wiki writer — invariants #2/#3). Sibling backlog ADRs:
[ADR-0022](0022-curator-taxonomy-governance.md) (auto-domain + per-domain — filing for the
heterogeneous content these connectors capture), [ADR-0024](0024-bulk-processing-horizontal-curator-scale.md)
(parallel curators / repo sharding — the throughput pressure capture volume creates), and
[ADR-0025](0025-web-config-multiupload-extensions.md) (which owns the broadened extractor extension
table this ADR's `dir:` connector consumes). The KB→agent skill write-back (#25 second half) is
reserved as **ADR-0026** and is out of scope here (§9).

## Context
The harvester today reads exactly one shape of source. `harvester/connectors.py` defines a single
`Connector` Protocol (`name` / `agent` / `scope` / `scan(last_content_sha256)`, lines 134–156) and
**one** implementation, `FileConnector` (line 159), which globs markdown files and segments them via
`_segment` — one fact per top-level list item or prose paragraph, headings are context (ADR-0017 §2).
`build_connectors` (`harvester/harvester.py:200–223`) dispatches on `name.startswith("file:")` and
**fail-loudly raises `ConnectorError`** for any other type — `letta:`/`mem0:`/`session:` are
explicitly unimplemented. The orchestrator `Harvester.run` applies ADR-0007's three mandatory
mechanisms verbatim: the **candidate gate** (every fact written `kind=Kind.candidate` /
`confidence=Confidence.low`, `harvester.py:356–359`, so the curator's `GATE_ALLOWED_OPS =
{MERGE_INTO_THEME, MARK_CONTESTED, DROP}` at `plan.py:77` plus `is_gated` (`kind==candidate OR
confidence==low`, `bundle.py:16–18`) forbid a gated candidate from ever *originating* a theme —
`plan.py` check 10, lines 520–524), **provenance** (`source=harvest:<agent>`), and the fail-closed
**scope gate** (`check_scope`, `harvester.py:67–95`, keyed on the concrete `RepoConfig.kind`; an
absent/unknown kind is treated as `team` and refused). Config: `ConnectorSpec` (`config.py:332`, the
`<type>:<agent>` key + `scope`/`path`/`follow_links`) parsed fail-loud by `load_connector_specs`
(`config.py:349`); `HarvestPolicy` (`config.py:287`, opt-in `enabled=False` default, `config.py:302`).
The inbox `source` validator (`models.py:88–93`) already accepts the parametric `harvest:<agent>`
form (`_HARVEST_RE`, `models.py:34`) alongside the `FIXED_SOURCES` set — **no inbox-enum change is
needed** for any new source. The cursor is the fixed DATA-MODEL §6 schema (`HarvestCursor`,
`harvester.py:98`, `extra='forbid'`), reduced to a whole-source `last_content_sha256` no-op by
ADR-0017 §3. The Prometheus exporter (`faces/web/metrics.py:149–164`) already emits per-connector
`agora_harvester_{proposed,accepted,rejected}` counter families.

Two product asks now press on this single-shape harvester:

- **#25 — sessions.** Original intent (Korean): *세션(대화 로그)도 분석해서 에이전트가 발견했지만
  MEMORY.md에 안 적은 지식을 수확하고, 거꾸로 에이전트에게 스킬을 제안.* Agent SESSION transcripts
  (Claude Code stores per-project JSONL under `~/.claude/projects/**/*.jsonl`; Codex/Gemini/Hermes
  have analogues) hold durable knowledge an agent discovered but never wrote into its `MEMORY.md`. A
  transcript is a typed turn-stream (user/assistant/tool records, `sessionId`/`cwd`/`gitBranch`/
  `timestamp`) — a fundamentally noisier, larger, and **far higher-PII** shape than a hand-curated
  memory file (verbatim user prompts, pasted secrets, file contents, `cwd`), for which `_segment`'s
  top-level-bullet model is wrong. #25 also raises a second, *outbound* ask — SUGGEST skills back to
  the locally installed agents — which is a write into a user's `~/.claude/skills/`, a shape with
  **zero precedent** (confirmed: nothing in `src/agora_kb/` writes back to any agent install; the only
  reference is the commented connectors example at `config.py:263`). That half is reserved as ADR-0026
  (§9).

- **#28 — corporate context (the product north-star).** Original intent (Korean): *회사 업무 맥락
  수집을 다변화 — 작업 폴더·git·메일·채팅·캘린더·회의록 등 일이 벌어지는 곳에서 수집하고, 모은
  맥락을 실제로 어떻게 활용할지까지 설계.* Capture from where work lives: local working folders, git
  repos, mail, chat, calendar, meetings. The framing is deliberately *not* "bolt a dozen bespoke
  importers onto the core" — it is to confirm the **existing** read-adapter seam (this module) and
  input-extractor seam (`ingest/extractors/base.py`, whose entire surface is `_OFFICE_EXTS` at
  line 43 + pdf) are the right homes, to fix the **safety/scope/provenance posture** for sources far
  noisier and more PII-dense than a curated memory file, **and** to answer the issue's explicit second
  half: how the collected context is actually *used* (§O2 below).

Both asks are the *same* decision: a connector taxonomy + a safety envelope for harvesting beyond
`MEMORY.md`. The mechanics deserve an ADR because they touch load-bearing surfaces — the DATA-MODEL §6
cursor, the immutable-inbox write contract (invariant #3), privacy/tenancy (invariant #5), and OSS
purity (invariant #4) — and because getting the *distillation* wrong would either flood the curator
gate or introduce a second uncontrolled generation point outside the integrity boundary.

## Proposed decision
Establish a connector taxonomy and a mandatory safety envelope for non-`MEMORY.md` sources, **behind
the existing ADR-0004 `Connector` Protocol** — the orchestrator, cursor, candidate gate, and scope
gate are reused **unchanged**; only `build_connectors` (`harvester.py:200`) gains type branches and
`load_connector_specs`/`ConnectorSpec` gain optional per-type fields (keeping `extra='forbid'`
discipline). The recommended outcome is **Adopt**.

1. **Connector-type grammar `<type>:<agent>`.** Reserve and dispatch on
   `session:` / `dir:` / `git:` / `mail:` / `chat:` / `calendar:` alongside the shipped `file:`
   (the `<type>:<agent>` form is already enforced — `FileConnector.__init__:190`). Each maps its
   facts to `source=harvest:<agent>` (e.g. `session:claude-code`, `git:agora-kb`, `mail:gmail`), so
   **no inbox `source` enum change is required** (`models.py:89` already accepts `harvest:<agent>`).
   `build_connectors` keeps its fail-loud posture: an unimplemented type still raises `ConnectorError`.

2. **Distillation is DETERMINISTIC + model-free for v1.** Each connector is a **pure transform** that
   uses heuristic salience extraction (e.g. explicit lessons/decisions, files-touched, succeeded
   commands, error→fix pairs, "remember/note" markers for sessions; commit message + diff summary for
   git; per-message reduction for chat) to emit a bounded set of `HarvestedFact`s. The curator's two
   cognitive acts (plan / author) remain the **ONLY** delegated steps — the INGEST-CONTRACT governing
   principle ("the backend *decides* and *writes prose*; deterministic code owns all the rest"). An
   **LLM-digest** pre-curation stage is an **explicit opt-in future stage** (§O2/C), behind its own
   flag, that still feeds the candidate gate downstream — never the default, never inside the
   integrity boundary.

3. **Every fact enters the same gate.** All connectors write `kind=candidate` / `confidence=low` so
   the curator's keep/merge/drop gate (`GATE_ALLOWED_OPS`, `is_gated`) adjudicates them exactly as it
   does file facts. This is the load-bearing pollution control and it is *doubly* important here: a
   raw transcript or mail firehose has a far worse signal-to-noise ratio than a `MEMORY.md`, so
   reliable **DROP** behaviour is a **validation requirement**, not an assumption.

4. **Fail-closed personal scope; team/corporate-shared sources DEFERRED to Phase 4.** Every new
   connector defaults to `scope=personal` and is enforced by the existing `check_scope`
   (`harvester.py:67–95`, fail-closed on absent/unknown repo kind). SHARED sources (a team Slack, a
   shared mailbox) are inherently cross-tenant and `check_scope` is a single-process pre-write gate,
   **not** the core write boundary (ADR-0017 §6). Shared/team sources are therefore **deferred to
   Phase 4** (multi-tenant + auth + the core write boundary); only PERSONAL sources (own
   folders/git/sessions, own mailbox/calendar) are in scope before then.

5. **Connector-boundary deterministic redaction BEFORE the immutable inbox write — bound to the
   `session:` landing, not the first networked connector.** A shared, model-free redaction utility
   (`core/redact.py` — net-new; absent today) performs a regex secret/PII scan (+ optional allow/deny
   lists) and is invoked by **any PII-bearing source — local OR networked — before the fact is
   persisted**. The inbox is append-only and immutable (invariant #3): `Inbox.write`
   (`harvester.py:349`) records an event that **cannot be retroactively scrubbed**, so redaction must
   precede persistence. The first concrete trigger is the **`session:` connector**, NOT the first
   networked connector — session transcripts (and `dir:`/`git:` working folders) are the highest-PII
   *local* sources (verbatim prompts, pasted secrets, file contents, `cwd`). `core/redact.py` is
   therefore a **hard dependency of the `session:` connector merge** (implementation step 4), and the
   initial secret/PII policy is decided then, not deferred to the networked step. This is net-new
   privacy protection: ADR-0017's untrusted-input posture hardens the *engine* (sentinel-strip via
   `_AGORA_SENTINEL_RE` at `connectors.py:74`, size caps) but does **not** protect *privacy* (a chat
   line carrying an API key would otherwise flow verbatim into a candidate).

   **Redaction observability (because the inbox event is unscrubbable).** A silent redaction miss is a
   compliance incident with no signal, so the redaction path gets first-class observability:
   (a) a **redaction-event counter** (count of facts with ≥1 redaction, labelled by class) added as a
   new family alongside the existing `agora_harvester_{proposed,accepted,rejected}` in
   `faces/web/metrics.py:149–164`; (b) `agora harvest --dry-run` (the existing preview path,
   `harvester.py:315–325`) **prints what WOULD be redacted** before any source is relied upon. Both
   are **metadata-only** — they count and classify, they NEVER log the secret itself.

6. **Mandatory OSS path per source class (invariant #4 / ADR-0005).** Each capability class has a
   fully-OSS path so the core never *requires* a proprietary service: **mail** via IMAP/JMAP, **chat**
   via Matrix, **calendar** via CalDAV, **git** via plain local git, **folders/sessions** via local
   files. Proprietary SDKs (Gmail/Graph, Slack/Teams, Google/MS Calendar) are **optional extras only**,
   behind adapters, lazily imported like the existing `ingest` extra — and screened for AGPL/copyleft.

7. **Prompt-injection hardening on transcript/message content.** A whole transcript is a much larger
   injection surface than a `MEMORY.md` bullet. The connector applies the existing `_neutralize`
   sentinel-strip + `max_fact_bytes` caps **and** flattens per-turn role attribution so an embedded
   "assistant"/"system" turn (possibly crafted by hostile prior input) cannot impersonate engine
   structure inside the candidate bundle the planning brain reads.

8. **Cursor reuse, whole-source-hash no-op — explicit per-type semantics so `extra='forbid'` never
   blocks an implementer.** The fixed DATA-MODEL §6 cursor (`HarvestCursor`, `extra='forbid'`) is
   reused with NO schema change. Per connector type:
   - **`file:` / `dir:` / `session:`** — `last_content_sha256` is the whole-source hash (re-read on
     any byte change; `event_key=fact_key` idempotency absorbs the re-flood, `harvester.py:361–364`).
   - **`git:`** — the cursor is **still a whole-source content hash**: the hash of the concatenated
     since-cursor commit payloads (messages + diff summaries) scanned this run. There is **no
     "since a cursor SHA" field** — a raw commit-SHA cursor would need a net-new §6 field, which
     `extra='forbid'` rejects and which would require its own ADR. The whole-source hash keeps git
     v1-cheap and §6-clean; a per-offset/per-SHA cursor is revisited only if re-scan cost hurts.
   The re-scan cost is documented like ADR-0018 documented the sibling-read cost. A per-offset cursor
   is a real §6 schema change requiring its own ADR and is **premature** for v1.

9. **The KB→agent skill-suggestion WRITE-BACK (#25 second half) is EXPLICITLY DEFERRED to its own ADR
   — reserved as ADR-0026.** Writing into a user's agent install is an OUTBOUND side-effect with zero
   precedent and it reopens the reworded-loop reasoning (DATA-MODEL §7: the connector skips only facts
   whose origin trace points *verbatim* back to Agora). It is out of scope here; if built at all it
   must be **opt-in, dry-run/staging-only** (emit a proposed `SKILL.md` to stdout or `_kb/staging/`),
   **never auto-written** into `~/.claude/skills/`, gated behind explicit human confirmation, with a
   test asserting **no filesystem write outside the staging dir**. It does not touch the wiki/inbox so
   it does not violate invariant #2 directly, but it is a new trust surface and gets its own decision
   record (ADR-0026).

   **Loop-break responsibility is SHARED (write-back ⇄ session distiller).** The `session:` connector
   this ADR ships and a future ADR-0026 write-back together form a potential **KB→skill→session→KB
   cycle**: a `SKILL.md` derived from KB content, once a human installs it into `~/.claude/skills/`,
   becomes future agent session content that the `session:` connector re-harvests — a path the
   verbatim origin-marker skip (DATA-MODEL §7; ADR-0017 §5) **cannot catch, because it is reworded by
   construction**. The candidate gate (decision 3) remains the only general break, but write-back
   widens what it must absorb. Therefore ADR-0026 MUST stamp write-back-derived content with a
   provenance marker the **session distiller recognizes and drops**, and this responsibility is
   recorded in BOTH ADR-0026's preconditions and this ADR's `session:` distiller spec — not in the
   candidate gate alone.

### Open sub-decisions (Proposed — recorded with options + recommendation)

- **O1 — Distillation strategy (D3).** **(A)** raw straight through the gate; **(B)** deterministic
  model-free heuristic extraction inside each connector + connector-boundary redaction;
  **(C)** LLM-distillation inside the connector. **Recommendation: B for v1**, with C as an
  explicitly-opt-in future *digest* stage (O2) that still feeds the gate. C as the default would add a
  second uncontrolled generation point outside the integrity boundary, can hallucinate facts not in
  the source, and breaks the harvester-is-deterministic property; A overflows the per-run bundle cap
  or burns the curator on thousands of DROPs at corporate volume. **Validate DROP-reliability on a
  real corpus via `--dry-run` before relying on any source.**

- **O2 — High-volume consumption model (#28's "how do we USE the collected context" half).** This is
  the issue's genuinely-open second half. The deferral is bounded, not vague:
  **(1)** once curated, harvested context **becomes queryable wiki knowledge** via the *existing*
  read / `kb_query` / MOC / graph path (ADR-0009/0012 + ADR-0021 graph render) — that consumption
  path exists today and needs nothing new; **(2)** the genuinely-open piece is the **digest/clustering
  consumption stage** for high-volume low-signal sources: **(A)** straight through the gate;
  **(B)** connector-side summarization; **(C)** a dedicated pre-curation *digest* adapter stage that
  clusters+summarizes a window of low-signal captures into candidate facts (read-side; the gate still
  adjudicates); **(D)** a two-tier store keeping raw context un-promoted (reopens the markdown-SSOT
  question — invariant #1). **Recommendation: C**, with B as the pragmatic first cut for the noisiest
  sources. The digest-stage design fires on a **concrete evidence trigger** — an inbox backlog-depth
  threshold OR a per-run DROP-rate threshold drawn from #27's curator throughput metrics
  ([ADR-0024](0024-bulk-processing-horizontal-curator-scale.md)) — not "until corporate volume
  exists." That gives the open question a defined re-entry condition.

- **O3 — Shared/corporate scope routing.** **(A)** defer ALL shared/team sources to Phase 4, ship only
  personal sources now; **(B)** per-connector target-repo binding + consent record now (still
  single-process, pre-Phase-4 caveat); **(C)** build the core write-boundary as part of this work.
  **Recommendation: A** — pre-Phase-4 scope enforcement is single-process and bypassable (ADR-0017 §6);
  routing shared PII before multi-tenant landing is the worst risk here.

- **O4 — Session-format abstraction (#25).** **(A)** Claude Code JSONL only, `ConnectorError` for
  others; **(B)** an abstract `SessionReader` seam with one Claude Code JSONL reader, others as future
  readers; **(C)** defer sessions entirely. **Recommendation: B** — one concrete reader proves the flow
  while the seam honours tool-agnosticism (invariant #6); Codex/Gemini/Hermes (and #28's git/mail) slot
  in without touching the orchestrator.

- **O5 — Redaction placement.** **(A)** none (current posture); **(B)** connector-boundary redaction;
  **(C)** a shared core utility every connector calls; **(D)** curator-side. **Recommendation: B + C** —
  a shared deterministic `core/redact.py` (C) invoked at each connector boundary (B), because redaction
  must precede the immutable inbox write (D is too late, invariant #3). Bound to the `session:` landing
  (decision 5), with the metadata-only redaction counter + `--dry-run` preview as the observability
  envelope.

- **O6 — Extension breadth (owned by [ADR-0025](0025-web-config-multiupload-extensions.md), consumed
  here).** The broadened `extract()` dispatch (`.txt`/`.md` passthrough + markitdown's html/csv/json/
  epub long tail beyond the six `_OFFICE_EXTS`; OCR/audio deferred behind their own opt-in extra +
  ADR-0005 vetting) is **decided in ADR-0025**, not re-litigated here. This ADR's `dir:` connector is
  a **consumer** of that table for local-folder capture (#28); the decompression-bomb/size caps are
  part of ADR-0025's untrusted-input hardening pass.

## Alternatives considered
- **Reuse `FileConnector._segment` for transcripts (rejected).** A JSONL turn-stream has no top-level
  bullets or headings; the markdown segmenter (ADR-0017 §2) would mis-split it. Session ingestion needs
  a different reader + distiller, so it cannot reuse `_segment`. This is the core design tension and the
  reason a new connector type (not a new `file:` glob) is correct.
- **An LLM distiller inside the connector by default (rejected — see O1/C).** Higher recall, but it is
  a second uncontrolled generation point outside the integrity boundary, can hallucinate, and is hard
  to test deterministically — it breaks the property that the curator's two acts are the only delegated
  steps. Kept as an explicit opt-in future digest stage that still feeds the gate.
- **A raw commit-SHA cursor for `git:` (rejected for v1 — decision 8).** A SHA cursor is not one of the
  `extra='forbid'` §6 fields; adding it is a real schema change to a fail-closed/rebuildable surface,
  requiring its own ADR. The whole-source content hash of the concatenated since-cursor commit payloads
  is correct and cheap at personal scale. Revisit only if scan cost hurts.
- **Defer redaction to the first networked connector (rejected — decision 5).** This was the prior
  framing; it leaves a window where the highest-PII *local* source (`session:`, shipped in step 4)
  persists unredacted secrets into the immutable personal-repo inbox with no redaction policy defined.
  Binding `core/redact.py` to the `session:` landing closes it.
- **Ship shared/team corporate sources now (rejected — O3/A).** `check_scope` is a single-process
  pre-write gate the design itself flags as bypassable before Phase 4 (ADR-0017 §6); routing shared PII
  into a single-process gate is a tenancy/compliance risk, not a feature.
- **Make corporate SDKs (Gmail/Graph/Slack) the primary path (rejected — invariant #4).** Easiest to
  build, but it would make the core require a proprietary service and risks AGPL/copyleft. OSS protocol
  paths (IMAP/JMAP, Matrix, CalDAV, git) are mandatory; SDKs are optional extras only.
- **Build the skill write-back here (rejected — §9, reserved ADR-0026).** An outbound write to a user's
  agent install with zero precedent; widens the reworded loop (KB→skill→session→KB) and could clobber
  an install. Sequenced last, behind its own opt-in dry-run ADR.

## Consequences
- **+** ADR-0007's safety model (gate + scope + provenance) extends to a whole *family* of sources with
  **zero core change** — only `build_connectors` gains branches and `load_connector_specs`/
  `ConnectorSpec` gain optional fields; the orchestrator/cursor/gate/scope are untouched. The
  `Connector` Protocol proves out as the clean ADR-0004 extension seam ADR-0017 §Consequences promised.
- **+** Privacy posture improves: connector-boundary deterministic redaction (bound to the `session:`
  landing) closes the gap ADR-0017's engine-only hardening left open, runs *before* the immutable inbox
  write (invariant #3 honoured, not fought), and is *observable* via a metadata-only redaction counter
  + `--dry-run` preview — a silent redaction miss now produces a signal.
- **+** OSS purity holds (invariant #4): every capability class has an OSS protocol path; proprietary
  SDKs stay optional extras, screened for copyleft (ADR-0005).
- **+** The harvester stays a pure deterministic transform: the curator's two acts remain the only
  delegated generation, keeping the integrity boundary intact and the connectors unit-testable.
- **−** Noise pollution risk rises at corporate volume: without the O2 digest stage a firehose overflows
  the per-run bundle cap or burns the curator on low-value DROPs. **DROP-reliability must be validated**
  on a real corpus (the ADR-0017/0018 live-verification protocol) before any source is relied upon.
- **−** The reworded KB→agent-memory→harvest→KB loop (ADR-0017 §5 residual) **widens** on two fronts:
  an agent may restate KB content mid-session, then a `session:` connector re-harvests it; and a future
  ADR-0026 skill write-back would close a KB→skill→session→KB cycle the verbatim origin skip cannot
  catch (§9). The candidate gate remains the only general break; sessions make its reword-DROP
  behaviour more load-bearing. Not closed — a stated residual risk, with write-back loop-break
  responsibility shared into ADR-0026.
- **−** What stays **deferred**, on purpose: shared/team/corporate-shared scope routing (Phase 4 core
  write boundary + multi-tenant + auth); the O2 high-volume *digest* stage and any LLM-distillation
  (opt-in future, behind the gate, fired on the #27-metrics evidence trigger); the #25 skill-suggestion
  write-back (reserved ADR-0026, opt-in dry-run); the SSRF/decompression-bomb hardening (owned by
  ADR-0025's hardening pass; must land **alongside** any networked connector); and a per-offset/per-SHA
  cursor (only if re-scan cost hurts).
- **−** This feeds the forcing functions linking the backlog: capture volume pushes
  [ADR-0024](0024-bulk-processing-horizontal-curator-scale.md) (parallel curators / repo sharding) and
  #26 (search/index, implementing the already-Accepted [ADR-0012](0012-deterministic-query-ranking.md); invariant
  #1 requires any index stay rebuildable), and benefits from
  [ADR-0022](0022-curator-taxonomy-governance.md) (auto-domain + per-domain) for filing heterogeneous
  content.

## Implementation sketch (when adopted; ordered by risk)
1. **Reserve the grammar in docs first** (no code): DESIGN §6 reframe ("agent memory AND working-context
   sources"); DATA-MODEL §8 + ARCHITECTURE §3.3 reserve the `<type>:<agent>` namespace mapping to
   `source=harvest:<agent>`; ROADMAP harvester follow-on line (session connector + skill-suggestion
   sketch reserved as ADR-0026); INGEST-CONTRACT §6 noise/DROP-validation note.
2. **A `SessionReader` seam + a `ClaudeCodeJsonlReader`** (new `harvester/session_sources.py`): yields
   normalized `(role, text, timestamp, tool_name?)` turn records, tolerantly skipping operational lines.
   Pure transform, fully unit-testable, no model (O4/B).
3. **A shared deterministic redaction utility** (new `core/redact.py`): regex secret/PII scan + optional
   allow/deny, zero model, returns redacted text + per-class hit metadata (never the secret). Invoked at
   the connector boundary before persistence (O5/B+C); a hard dependency of the `session:` merge.
4. **Personal/local/no-network connectors first** — `dir:` (walk a local subtree, reuse FileConnector's
   `~`-expand / symlink-escape containment / size+count caps, route files through ADR-0025's broadened
   `extract()` dispatch), `git:` (recent commit messages + diff summaries; whole-source content hash of
   the concatenated since-cursor commit payloads as the §6 cursor — decision 8), `session:` (drive
   `SessionReader` + the deterministic salience heuristic, `_neutralize` + role-flattening, **mandatory
   `core/redact.py` pass**, `fact_key=content_sha256`, `domain`/`tags` left `None` so the curator
   decides). These exercise the whole taxonomy with zero tenancy/network/SDK risk. **Land the redaction
   counter + `--dry-run` "would-redact" preview in the same change (decision 5).**
5. **Wire `<type>:` into `build_connectors` + `load_connector_specs`/`ConnectorSpec`** (add only the
   minimal optional field, e.g. a distillation level; keep `extra='forbid'`); surface new types in
   `agora harvest`/`--dry-run`/`agora doctor` connectors table; update the commented `adapters.yaml`
   example.
6. **Networked-personal next** — `mail:` (IMAP/JMAP) + `calendar:` (CalDAV), Gmail/Graph/Google optional
   extras; mandatory redaction (already bound from step 4); depends on the O2 digest stage for volume.
7. **Adversarial + noise validation** on a real personal corpus (`agora harvest --dry-run` over
   `~/.claude/projects/**/*.jsonl`): confirm signal-to-noise + reliable DROP, no secrets/cwd/file
   contents leak past `scope=personal` or past redaction, transcript injection cannot impersonate engine
   structure (sentinel strip + role flatten), team-repo scope refusal, tolerable re-scan cost — the
   ADR-0017/0018 live-verification protocol.
8. **(Reserved ADR-0026, separate decision)** the #25 skill-suggestion dry-run/staging sketch with the
   shared write-back ⇄ session-distiller loop-break (§9); **(Phase 4)** shared/team `chat:` (Matrix OSS
   path; Slack/Teams optional) and all shared corporate sources.

## Addendum — Redaction v1 policy (#39, landed 2026-07-06)

Decision 5 mandated `core/redact.py` and its observability but left the concrete secret/PII policy to
be **decided with the module, not deferred** (§decision 5). Issue **#39** landed that module (plus the
`--dry-run` would-redact preview and a dormant metric); this addendum records the settled policy so
#25 (the `session:` connector), reserved **ADR-0026** (skill write-back), and reserved **ADR-0030**
(federation) do not re-litigate it.

### 1. v1 policy classes (`redact.DEFAULT_ON_CLASSES` / `KNOWN_CLASSES`)

Precision-FIRST for the default set: a false positive **corrupts unscrubbable curated content**, whereas
a false negative is still caught downstream (the candidate gate + curator, and the broad structural
coverage below). Every default-on pattern is a distinctive, structural secret shape — not a broad
heuristic. The table is the doc/code lockstep source (a test asserts the default-on rows equal
`DEFAULT_ON_CLASSES`, and default-on ∪ opt-in equals `KNOWN_CLASSES`).

| class | tier | pattern family | precision | notes |
| --- | --- | --- | --- | --- |
| `pem_private_key` | default-on | `-----BEGIN…PRIVATE KEY-----` … `-----END…` whole block | high | non-crossing gap (no over-redaction), unbounded+linear (no numeric cap — a cap would fail open on a large key body), substring-pregated |
| `aws_access_key_id` | default-on | `AKIA`/`ASIA` + 16 upper-alnum | high | leading boundary + maximal run |
| `github_token` | default-on | `gh[opsru]_…` / `github_pat_…` | high | distinctive prefix + length floor |
| `slack_token` | default-on | `xox[baprs]-…` | high | Slack-reserved shape |
| `google_api_key` | default-on | `AIza` + 35 | high | Google-reserved |
| `stripe_secret_key` | default-on | `[sr]k_(live\|test)_…` | high | ordered before `openai_anthropic_key` (disjoint, but pinned) |
| `openai_anthropic_key` | default-on | `sk-ant-`/`sk-proj-` (hyphens ok) or bare hyphen-free `sk-…` | medium | TIGHTENED so a kebab slug (`sk-learn-…`) is not a hit; a long hyphen-free `sk-` identifier is an accepted over-redaction |
| `jwt` | default-on | `eyJ….….…` three b64url segments | medium | a documented example JWT IS redacted (accepted) |
| `bearer_token` | default-on | `Authorization:… Bearer <token>` (group replace) | medium | context-anchored; header preserved |
| `generic_assigned_secret` | opt-in | secret-noun + `[:=]` + long value | medium | in the registry, NOT in `DEFAULT_POLICY`; `(?!\[REDACTED:)` keeps it idempotent; #25 may enable it |
| `aws_secret_access_key` | deferred | bare 40 b64 | — | no prefix → collides with hashes/base64; a keyword-anchored variant may land in v1.1 |
| `credit_card_pan` | deferred | 13–19 digits + Luhn | — | collides with order-IDs/versions on a curated KB |
| `email` | deferred | RFC-ish | — | abundant LEGITIMATE curated content; behind an explicit PII opt-in |
| `phone_number` | deferred | E.164 / grouped digits | — | catastrophic FP rate |
| `high_entropy_blob` | deferred | Shannon entropy | — | REJECTED: this KB is saturated with legit 64-hex `content_sha256`, commit SHAs, UUIDs, data-URIs |

### 2. Reversibility & no-retention (the load-bearing privacy guarantee)

Redaction is **irreversible by construction** — forced by invariant #3 (the inbox is unscrubbable, so
redaction must be a one-way gate before persistence). The matched bytes are discarded at substitution
time: no sidecar map, no reversible token, no offsets. The only survivors are per-class integer counts
+ class names + the redacted text carrying `[REDACTED:<class>]`. A reverse map would itself be a new
unscrubbable secret store, and **a SHA of a short/low-entropy credential is a brute-force reversal
oracle** — so when #25 wires the live write path, `fact_key = content_sha256(REDACTED text)`, never the
raw secret, and redaction runs **before** the hash. The module is locked by a six-surface
no-secret-retention test (text / hit fields / repr / metric label / dry-run note / logs).

### 3. Determinism, idempotence, placeholder

A fixed, ordered `_RULES` registry, no randomness/clock/set-iteration in the output path → byte-identical
across runs and processes. The placeholder `[REDACTED:<class>]` is a pure function of the class name (no
index/offset/count/hash), so a second pass yields byte-identical text (idempotence, incl. the opt-in
class). **Documented residual (tested):** the leading `(?<![A-Za-z0-9])` boundary skips a key glued to a
*preceding* word char (`wordAKIA…`); keys are ~always preceded by a separator, and dropping the boundary
would false-positive on a coincidental alnum-embedded prefix — #25 may revisit for the live path.

### 4. Sentinel canonical home (do NOT fork ADR-0027 §8)

The ADR-0027 §8 CONSUMER-duty machinery (span-drop + marker-strip) moved to a new `core/sentinel.py`
(the redaction module lives in `core/` and may not import from `harvester/`); `harvester/connectors.py`
re-exports it byte-identically. **The normative sentinel + loop-break contract remains ADR-0027 §8**
(cited here, not restated — §8 is the single source every consumer must cite). `redact.sanitize`
composes `core.sentinel.strip_agora_sentinels` (phase-0) then the secret scan (phase-1). The producer
duty stays with the emitter in `core/gold.py`.

### 5. #39 / #25 scope split (recorded so #25 does not re-decide)

**#39 (landed):** `core/sentinel.py` + `core/redact.py` + tests; the read-only `--dry-run` would-redact
preview (redacted preview text + metadata-only class×count notes); a **dormant**
`agora_harvester_redacted{connector,class}` counter. The shipped write path is **byte-identical** — no
`redact()` call was added between scan and `Inbox.write`.

**#25 (deferred):** the live write-path invocation (redact at the connector boundary, before
`content_sha256`, guarded by a kill-switch); the `harvest.redact.{enabled,pii,allow,deny}` repo.yaml
loader (fail-loud, mirroring `load_harvest_policy`); the persisted counter source (see §6 below); the
two loop-proof e2e (injection-pack round-trip strip; reworded residue landing as a gated candidate).

### 6. Observability & the staged `HarvestCursor.redacted` field

Decision 5's "counter labelled by class" is realized as the two-label family
`agora_harvester_redacted{connector,class}` (the extra `connector` label is for parity with the existing
per-connector `agora_harvester_{proposed,accepted,rejected}`); metadata-only — the label is the class
NAME, never the secret. It is **dormant** in #39 (no persisted source → no samples, an honest 0). #25
adds the source: a **`HarvestCursor.redacted: dict[str,int]`** field (a dict, to keep the class
dimension), incremented once per fact per class present, bumped beside `cursor.proposed`. This is a new
DATA-MODEL §6 cursor field, which decision 8 defaults to "no schema change / would require its own ADR";
it is **explicitly authorized here** as a decision-5-mandated observability field (ADR-0023's redaction
mandate IS the authorizing ADR), and #25 must update **both** DATA-MODEL §6 and the `HarvestCursor`
docstring's "no unbounded extension" note to enumerate it. No `schema_version` impact (git-ignored
`_kb/` cursor state only).
