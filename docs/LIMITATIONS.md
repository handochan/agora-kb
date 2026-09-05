# Agora — limitations and the data-safety contract (0.1.0b1)

> The one-page answer to "what may I put in this, and what can disappear?"
>
> This is a **contract**, not a feature tour. Each section states what is true today, when it bites,
> what you can do about it now, and where it is tracked. Every claim carries a file/line, an ADR, or
> an issue number that was checked against the tree at commit `a8906bf` (`agora 0.1.0b1`). Where a
> claim needed a run to prove it, the run is reproduced in §7.
>
> **§6 and §6a–§6c are newer than that commit** and were checked against the KB wiki schema 2 work
> now on `main` (`e9ba6bb`, ADR-0041 Accepted 2026-09-05) rather than against `a8906bf`.

Related, and deliberately not duplicated here:

| Doc | Covers |
|---|---|
| [`../CHANGELOG.md`](../CHANGELOG.md) → "Known limitations" | The short list this document expands (12 items) |
| [`ROADMAP.md`](ROADMAP.md) → "Not in 0.1.0-beta" | The **normative** list — if the two ever disagree, ROADMAP wins |
| [`../SECURITY.md`](../SECURITY.md) | Threat model, supported scope, private vulnerability reporting |
| [`DEPLOY-TEAM.md`](DEPLOY-TEAM.md) (**Korean**) | Running one KB for 2–10 people: hub topology, proxy auth, footguns |
| [`../deploy/README.md`](../deploy/README.md) | Always-on units — and the **SSOT** for the terminal-failure recovery procedure §7 summarizes |

Limits that are *capability* gaps rather than data-safety risks — native Windows (epic #85), no
embeddings/semantic search (ADR-0009/0012), no contributor process — are listed in the CHANGELOG
and are not repeated below. The absence of a schema-migration command (#63/#98) is the exception:
once a repo predates the wiki-schema flip it is a data-safety matter, so it is expanded here in §6a
rather than left to the short list.

---

## The 30-second version

1. **Do not put secrets, credentials, or other people's personal data into Agora.** There is no
   delete. Nothing you capture can be retracted through a supported path (§2).
2. **Run `agora curate` before you rely on `agora sync`.** `_kb/` is git-ignored, so everything
   captured but not yet curated is outside the backup (§1).
3. **Exactly one machine may curate a repo.** The single-writer lock is host-local; a second host
   is not prevented by anything in the code (§4).
4. **Do not expose `agora web`.** There is no authentication of any kind; keep the loopback bind and
   put an authenticating reverse proxy in front for a team (§8).
5. **Know which brain you wired.** The default is a local model and nothing leaves your machine. A
   `claude`/`codex`/`gemini` brain sends your KB to that vendor on every run (§9).
6. **A repo on the old wiki schema is read-only here.** Reads work; `curate`, `watch`, `requeue`,
   `harvest`, `kb_remember` and the web upload all refuse. There is no in-place upgrade — the
   crossing is a conversion into a new repo (§6a).
7. **`wiki/people/` is human-owned and searchable, not private.** It is kept out of gold packs and
   `kb_context`, but an agent that asks for it through `kb_query`/`kb_read` gets it (§6b).

---

## 1. Backup covers `wiki/`, not `_kb/`

**What is true.** `agora repo init` writes a `.gitignore` whose whole content is the operational
spool plus `.DS_Store` (`src/agora_kb/core/repo.py:52-54`):

```
# Agora operational spool — rebuildable, never canonical (ADR-0001).
_kb/
.DS_Store
```

`agora sync` is the only **git** operation in the codebase that leaves the machine, and it pushes the
curated branch, fast-forward only, never `--force` (`src/agora_kb/core/repo.py:324-346`, #64).
Therefore nothing under `_kb/` is backed up by Agora, by any command, ever. Verified on a freshly
initialized repo — `git ls-files` returns `.gitignore`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`,
`QWEN.md`, `_meta/taxonomy.yaml`, `_templates/*`, `adapters.yaml`, `index.md`, and nothing under
`_kb/`.

> **This is a statement about backup, not about egress.** Other things in Agora do open outbound
> sockets: the URL upload extractor fetches remote pages
> (`src/agora_kb/ingest/extractors/url.py:99-150`), the default brain POSTs the curator bundle to
> `$AGORA_OLLAMA_HOST` (`src/agora_kb/adapters/ollama_brain.py:845-852` — loopback by default but
> operator-settable to any host), and a brain routed to a **hosted** CLI agent sends KB content to
> that vendor on every run. See [`../SECURITY.md`](../SECURITY.md) §3(b) and §3(c), and §9 below.

**It is tempting — and wrong — to read `_kb/` as "derived, safe to delete".** Of the twelve entries
below, two are rebuildable by command and three more are expendable. **The other seven are not
recoverable from any backup Agora makes:**

| Path | Defined at | Holds | Rebuildable from `wiki/`? |
|---|---|---|---|
| `_kb/inbox/<writer>/` | `core/layout.py:104` | captures written but not yet curated | **No** |
| `_kb/processing/<run-id>/` | `core/layout.py:108` | the claimed events + manifest of a run in flight | **No** |
| `_kb/processed/<date>/` | `core/layout.py:112` | the immutable event archive of every delivered capture | **No** — the wiki holds the curator's *rendering*, not the event |
| `_kb/failed/<date>/<run-id>/` | `core/layout.py:116` | terminal events awaiting `agora requeue`, plus the `error.json` records that **are** the retry budget (§7) | **No** |
| `_kb/requeued/<date>/<run-id>/` | `core/layout.py:120` | retry records archived by `requeue --reset-attempts` | **No** |
| `_kb/harvest/` | `core/layout.py:148` | per-connector harvest cursors | In effect yes — "derived … rebuildable from git + `processed/`" ([`DATA-MODEL.md`](DATA-MODEL.md):369); losing one re-scans the source and the candidate gate absorbs the re-flood |
| `_kb/index/` | `core/layout.py:153` | the ADR-0012 §2 reader cache | **Yes** — `agora index build` |
| `_kb/gold/` | `core/layout.py:164` | ADR-0027 context packs | **Yes** — `agora gold build` |
| `_kb/state.json` | `core/layout.py:209` | counters, the `published_runs` double-publish guard, the `event_keys` idempotency cache, `last_attempt`/`last_failure` | **No** (`event_keys` alone "may be rebuilt from retained events", [`DATA-MODEL.md`](DATA-MODEL.md):321) |
| `_kb/repo.yaml` | `config.py:154-156` | **your configuration**: domains, `curator.max_attempts`, thresholds, `backup.remote`, `web.security` | **Your edits are not.** `agora repo init` regenerates a *default* file (`config.py:257-280`), so you get a working repo back — but every key you tuned is gone |
| `_kb/backup.json` | `core/layout.py:198` | the last `agora sync` result | Expendable (losing it blanks one `doctor` line) |
| `_kb/curator.lock` | `core/layout.py:213` | the `flock` file | Expendable |

Note the asymmetry that catches people: `adapters.yaml` — the brain wiring — lives at the **repo
root** (`config.py:308`) and *is* tracked by git. `_kb/repo.yaml` — the curator policy — is **not**.
Restoring a repo from a backup remote gives you your brain wiring back and a *regenerated default*
policy, not the policy you tuned. If you have changed anything under `curator:`, `harvest:`,
`backup:` or `web:`, keep a copy of that file somewhere your ordinary backup reaches.

**When it bites.** The disk dies, or `_kb/` is deleted, between a capture and the next curator run.
Everything captured in that window is gone, and `agora sync` never had it. The same applies to
anything sitting in `_kb/failed/` waiting for §7.

**What you can do now.**

- Curate before you rely on a backup:
  ```bash
  uv run agora curate --repo /ABSOLUTE/PATH/TO/knowledge-repo --force
  uv run agora sync   --repo /ABSOLUTE/PATH/TO/knowledge-repo
  ```
- Back `_kb/` up with your ordinary file backup (Time Machine, restic, borg, a snapshotting
  filesystem). No Agora command does this, and none is being added for the beta.
- Do not treat `agora sync` as replication. It is strictly push-only: nothing pulls, fetches, or
  merges, and a non-fast-forward rejection (the remote moved ahead) is reported and left alone
  (`core/repo.py:340-346`). The remote is a backup target, never a second writer (ADR-0002).

**Tracking.** Widening the backup contract is recorded in the CHANGELOG as a separate decision, not
a beta patch; no open issue proposes it. The multi-machine half is #46 (§4).

---

## 2. Deletion and correction do not exist

**What is true.** The curator's op vocabulary is **closed** and has six members —
`CREATE_THEME`, `APPEND_DAILY`, `MERGE_INTO_THEME`, `MARK_CONTESTED`, `DROP`, `NOOP`
(`src/agora_kb/curator/plan.py:50-60`). The comment above it is the design statement, not an
oversight (`curator/plan.py:48-49`):

> The six allowed ops. Hard deletion of curated content does NOT exist in the vocabulary; link/MOC/
> index maintenance is a deterministic side-effect of CREATE/MERGE/CONTEST, never a standalone op.

**`DROP` is not a delete, and misreading it is exactly why people believe deletion exists.** `DROP`
is a *plan-time* disposition: the curator declines to file an inbox event into the wiki. It writes
nothing and removes nothing (`curator/apply.py:625-626` — `DROP`/`NOOP` `continue`, no wiki edit),
and the event itself is still archived to `_kb/processed/<date>/` at finalization
(`curator/worker.py:1980-1996`). So `DROP` neither deletes curated content nor destroys the event.

**Do not read that as "a dropped capture is safe", though.** Compose it with §1: `_kb/processed/` is
git-ignored and not rebuildable from `wiki/`, so a dropped capture is absent from the wiki, absent
from every `agora sync` push, and present only on the local disk this whole section is about. The
one durable trace outside `_kb/` is a `- dropped: <candidate_id>` line in `log.md`
(`curator/worker.py:2041`) — an id, not the text. And the number you would use to notice is
imprecise: `counters.dropped` folds `NOOP` in with `DROP` (`curator/worker.py:1952`), so it is not a
count of discards.

Everything else that would let you retract something is also absent:

- The inbox is append-only ([`DESIGN.md`](DESIGN.md):104, §2.2; ADR-0002) and events are immutable:
  their lifecycle is represented by *location*, never by mutation
  ([`DATA-MODEL.md`](DATA-MODEL.md):33-37).
- No face offers a delete. `agora --help` lists `repo · import · capture · status · query · read ·
  neighbors · curate · requeue · harvest · index · gold · eval · sync · watch · serve · web ·
  doctor`. The MCP face exposes
  `kb_remember`, `kb_query`, `kb_read`, `kb_neighbors`, `kb_context`, `kb_status`, `kb_curate`
  (the `@mcp.tool` registrations in `faces/mcp_server.py`'s `build_server`). The web face has no
  delete route.
- `wiki/` is git history. Retracting a published note means rewriting that history by hand, on
  every clone and every backup remote that already has it.
- ADR-0031 (retention and right-to-delete) is **unwritten**: there is no `docs/adr/0031-*.md`.
  A future `agora prune`/`agora forget` would also need its own ADR — it cannot satisfy clause C5
  of the ADR-0002 spool-custodian appendix, which forbids a non-curator process from removing spool
  content (`docs/adr/0002-cqrs-single-writer-curator.md:71-76`).

> **Do not capture secrets, credentials, API tokens, or other people's personal data.**

**Redaction does not cover you here.** `core/redact.py` runs at the **connector boundary** only —
`harvester/harvester.py:287` and `harvester/session_connector.py:238` (ADR-0023 §5). It does not
inspect what you send through `kb_remember`, the web upload, or `agora import`.

**When it bites.** A capture that quotes an API key, a colleague's home address, or a customer
record. It is in `wiki/` after the next curator run, in git history forever, and on every backup
remote after the next `agora sync`.

**What you can do now.** Only prevention: keep secrets out of the sources you capture and out of the
memory files/session transcripts you point the harvester at. If something already landed, you are in
manual git-history-rewrite territory across every copy.

**Tracking.** [#42](https://github.com/handochan/agora-kb/issues/42) — ADR-0031, retention and
right-to-delete.

---

## 3. A capture is queryable only after a curator run publishes it

**What is true.** Writes land in the inbox; reads serve the curated commit. The upload route says so
in its own contract — "the item is searchable only after the next curator run (eventual consistency,
DESIGN §2.2)" (`faces/web/app.py:533-534`). There is no read-your-own-write overlay: ADR-0033 is
reserved and unauthored (no `docs/adr/0033-*.md`).

How long "eventual" is depends entirely on your trigger configuration — the `cron`, `threshold`, and
`idle_minutes` keys under `curator.triggers` in `_kb/repo.yaml`, evaluated by `agora watch` each
tick. A repo with no `watch` running is only consistent when you type `agora curate`.

**When it bites.** "I just saved it and search cannot find it." That is expected behavior, not a
bug. It bites harder when the curator is *failing*: the capture is not lost, but it is not queryable
either, and after `curator.max_attempts` failures it is in `_kb/failed/` (§7).

**What you can do now.** Force a run and watch the status line:

```bash
uv run agora curate --repo /ABSOLUTE/PATH/TO/knowledge-repo --force
uv run agora status --repo /ABSOLUTE/PATH/TO/knowledge-repo
```

`inbox depth: 0` with a fresh `last_run`/`last_commit` means everything captured has been published.

**Tracking.** ADR-0033 is reserved and evidence-triggered, not planned for beta
([`ROADMAP.md`](ROADMAP.md):323-325).

---

## 4. Exactly one machine may curate a repo

**What is true.** The single-writer guarantee is a non-blocking `fcntl.flock` on
`_kb/curator.lock` (`curator/claim.py:57-77`). `fcntl` locks are **host-local**. Two consequences,
and only one of them is safe:

- Two `agora curate` invocations **on the same host** are safe: the loser raises `LockHeld` and
  returns a clean no-op (`curator/claim.py:47-53`). Its captures stay in the inbox for the next
  trigger; nothing is corrupted.
- Two hosts curating **the same repo** (via a clone, a network share, or a synced folder) are not
  prevented by anything in the code ([`DEPLOY-TEAM.md`](DEPLOY-TEAM.md):37-42). A non-fast-forward
  rejection from `agora sync` is the *symptom* of it having already happened, not a guard against
  it (`core/repo.py:340-346`).

Note also that a clone carries no inbox at all — `_kb/` is git-ignored (§1) — so "share the KB by
cloning it" is a read-only distribution model, never a write path.

**When it bites.** A laptop and a desktop both running `agora watch` against the same synced
directory; or a team that clones the hub repo and each runs a curator.

**What you can do now.** Nominate one curation host and run `agora watch` only there. For teams,
follow the hub topology in [`DEPLOY-TEAM.md`](DEPLOY-TEAM.md) (Korean) — all writes land on one host's local
filesystem, and clones are read-only.

**Tracking.** [#46](https://github.com/handochan/agora-kb/issues/46) — the multi-machine topology
ADR. Until it is written, multi-machine curation is unsupported, not merely undocumented.

---

## 5. Do not enable `curator.allow_reduced_isolation`

> **First, the scope — because it is narrower than it looks.** A backend is confined only when its
> spec declares `network: none` (`curator/subprocess_backend.py:371`). The `adapters.yaml` that
> `agora repo init` writes declares **`network: loopback`** (`config.py:323`), and so does the
> documented CLI-agent brain. **In the default configuration neither curator pass runs inside the
> kernel sandbox at all**, and the deterministic FINAL-DIFF gate is the entire boundary. This is by
> design, not an oversight — the Ollama shim couples inference and file-writing and needs loopback
> to reach the daemon (ADR-0013 Context) — but it means `agora doctor`'s `sandbox: seatbelt (ok)`
> line tells you *the mechanism works on this host*, **not** *your brain is confined*. Since #129
> doctor answers the second question on its own line, per act, so you never have to infer it:
>
> ```
>   sandbox: seatbelt (ok)
>     …
>     confines this repo's brains: NO — outside: plan=qwen (network: loopback), author=qwen (network: loopback) (only a network: none author is confined; PASS-1 never is)
> ```
>
> **That line can never read `yes`**, in any configuration. `SubprocessBackend.plan` passes
> `confine=False` unconditionally, so PASS-1 is outside the sandbox even for a `network: none`
> backend; `PARTIAL` is the strongest true answer. It also reads `NO` — not `PARTIAL` — when the
> host has no kernel sandbox, when the self-test failed, or when the flag below selected the
> `restricted` fallback.
>
> Everything below is therefore about the configuration you opt into with `network: none`, plus the
> flag that weakens it further.

**What is true.** The flag defaults to `false` and the default posture is fail-closed: with no
usable kernel sandbox, backend selection raises `SandboxUnavailable` and the run fails rather than
proceeding unconfined (`curator/isolation/restricted.py:19-21`, `curator/isolation/base.py:149-151`).
Setting it to `true` selects the restricted fallback, which loses two protections *during the run*
and does not pretend otherwise (`curator/isolation/restricted.py:11-17`):

1. **Network egress is not blocked.** A malicious backend can reach the network and exfiltrate
   captured content.
2. **Out-of-worktree writes are not confined.** A malicious backend can write anywhere the invoking
   user can — `~/.ssh/authorized_keys`, `~/.bashrc`, another repo's files.

The ADR-0008 post-run validator still diffs the worktree, and it catches **neither** of those.

**The compensating controls ADR-0013 promised are not implemented — all three of them.**
ADR-0013:431-434 states that when the flag is explicitly enabled, "the run is **FORCED into
review-mode** (publish to a branch/PR, never direct CAS) regardless of `repo.yaml review_mode` …
`reduced_isolation=True` is recorded in the manifest". Checked against the tree:

- `grep -rn reduced_isolation src/agora_kb/curator/worker.py` → **0 matches**. The worker never sees
  the flag (#91).
- `RunManifest` is frozen with `extra="forbid"` and its field set is `run_id`, `base_commit`,
  `event_ids`, `phase`, `prose_complete`, `schema_version`, `published_commit`, `started`
  (`curator/manifest.py:61-70`) — there is no `reduced_isolation` field to record.
- The `WARNING` text that "MUST" reach logs, the manifest, and `kb_status`
  (`curator/isolation/restricted.py:43-56`) is referenced nowhere outside its own module and one
  text-assertion test. It is never emitted to an operator.
- There is no review mode at all. `grep -rn review_mode src/agora_kb/` matches exactly one line
  outside the wiki-schema template — `config.py:263`, the `review_mode: direct` key that
  `repo init` writes into `_kb/repo.yaml`. Nothing reads it. Every published run advances the
  branch by compare-and-swap.

So the warning string promises a safeguard that does not exist, and it would not be printed even if
it did.

**When it bites.** A host without a usable sandbox (native Windows, a Linux box with user
namespaces disabled and no AppArmor remedy, an old kernel) where the flag looks like the obvious way
to make curation run at all.

**What you can do now.** Leave it `false` for the whole beta. If a host has no sandbox, curate
somewhere else, or run the backend as a throwaway low-privilege OS user or inside a container —
the hardening the fallback itself recommends (`curator/isolation/restricted.py:53-55`). The real
last line of defense is the deterministic FINAL-DIFF gate, not the kernel.

Related, and true even with the sandbox working: **it confines writes and network, not reads.** On
both platforms the authoring subprocess can read the whole filesystem; it cannot write outside its
temporary worktree and has no network, so a read alone cannot leave the machine
([`ROADMAP.md`](ROADMAP.md):334-343).

**Tracking.** [#91](https://github.com/handochan/agora-kb/issues/91) (the compensating control),
[#122](https://github.com/handochan/agora-kb/issues/122) (read-hardening).

---

## 6. An upload's original bytes are kept — and now reachable, ungraded and unevenly redacted

**What is true.** Since the schema-2 work (ADR-0041 D1.4/D4.2, Stratum wave W2.5 — commits
`37dd56e` · `d244e5e` · `7a30124`), an upload's original bytes travel with its inbox event as a
content-addressed attachment (`Inbox.write(..., attachments=[(filename, media_type, data)])` stages
`_kb/inbox/<writer>/_attach/<sha256>.<ext>` *before* the event that names it), and APPLY — the
curator's deterministic pass, never the brain — materialises them as `raw/_blob/<ab>/<sha256>.<ext>`
plus a `<file>.meta.yaml` sidecar, immutable and git-tracked (`.gitattributes` pins
`raw/_blob/** -text -diff -merge` so EOL translation can never move a file off its own content
address). The derived note cites the **blob** in `sources:` beside the extracted-markdown evidence; a
DROPped or NOOPed candidate still gets its blob (the model's judgement never discards the user's
artefact); an identical re-upload is re-cited, never rewritten. `agora capture --file PATH` is the
no-server way in. The bundle the brain sees carries a text summary only — a test asserts no bytes
reach it. The extracted markdown remains the searchable body; the blob is evidence, not corpus.

**The read side landed (#169 wave A).** A `sources:` string is now followable on every read face,
through one shared seam (`AgoraHandlers.raw()`), so the three faces cannot describe one capture
differently:

- **Web** — `GET /raw/{path}` (a page) and `GET /api/raw/{path}` (JSON), where `{path}` is the
  citation with its stored `raw/` prefix dropped: `sources: raw/general/psa-hca.md` →
  `/raw/general/psa-hca.md`. A note page's `sources:` rows are now anchors — but only the ones the
  route would actually serve; the server computes each row with the same predicate the route
  enforces, so a link that is offered always opens and anything else stays the plain text it was.
- **MCP** — `kb_read` takes a `sources:` string as well as a note path; the tool count is still
  seven. A `raw/` answer is marked by a `resource: "raw"` key and carries
  `raw_kind` (`"text"` | `"blob"`), `path`, `bytes`, and either `text` + `truncated` or `meta`.
- **CLI** — `agora read <path>` prints the same payload (`--json` prints it verbatim, byte-identical
  to what the MCP tool returns), alongside the two other new read verbs `agora query` and
  `agora neighbors`.
- **Kill switch** — `web.features.raw_enabled` (default `true`, `_kb/repo.yaml`). Off → both routes
  404, `sources:` render as plain text again, and a body link into `raw/` is left verbatim rather
  than pointed at a disabled route. It is a switch, not an access control: the face still has no
  authentication of any kind (§8).

**When it bites.**
1. **What you now reach is ungraded content.** `raw/` is the capture, not the curated answer: it
   passed none of the curator's PLAN/APPLY grading, no lint rule grades its content (L1-8 only
   checks that a cited path *exists*), and the read faces hand it to you exactly as stored.
   Concretely, the web page renders a text capture into an **escaped `<pre>`** and deliberately
   *not* through the markdown renderer, so the
   pre-flip relative links these captures carry stay inert instead of being rewritten into `/note/`
   hrefs — plain text now is reversible, enrolling uncurated text in the site's link graph is not.
2. **Redaction is asymmetric, and reaching `raw/` makes that visible.** Of the five producers that
   put text into `raw/`, exactly **one** redacts: the `session:` harvest connector, which runs
   `core/redact.py` at its connector boundary before the fact is hashed or persisted
   (`harvester/session_connector.py`). The `file:` connector never redacts (byte-identical write
   path, #39), and the web upload, `kb_remember` and `agora capture` never pass a redactor at all —
   `Inbox.write` has no redaction step. The `redact` call in `harvester/harvester.py` is
   `_redaction_preview`, reached only on the `--dry-run` branch. Nothing redacts on the way *out*
   either, and nothing will: the redactor is one-way with no reverse map, so read-time redaction
   would make the served bytes disagree with the digest the note cites. **Never read "`raw/` has
   already been redacted" into anything here.**
3. **Blob bytes never leave over MCP, by decision.** `kb_read("raw/_blob/…")` returns the sidecar's
   capture facts (`meta`: `sha256`, `ext`, `media_type`, `bytes`, `filename`, `captured_at`,
   `writer`, `source`, `event_id` — only the keys the sidecar actually has) and a note pointing at
   the web face. There is no base64 channel and no `bytes: true` parameter. The download is the web
   route, which serves every blob as `application/octet-stream` + `X-Content-Type-Options: nosniff`
   + `Content-Disposition: attachment` + `Content-Security-Policy: default-src 'none';
   frame-ancestors 'none'`, **never** as the `media_type` the sidecar records — that field and the
   stored `<ext>` are both uploader-chosen, so an uploaded `.html` or `.svg` is a download, never a
   same-origin document. A PDF or an image therefore does not preview in the browser; that is the
   trade, taken deliberately.
   A `*.meta.yaml` sidecar is also not addressable on its own (404 naming the artefact instead) —
   the URL space matches the citation space, where lint L1-8b already refuses a sidecar citation.
4. **A large text capture is truncated on read.** Both faces cap a text read at
   `core.rawstore.MAX_RAW_TEXT_BYTES` (1 MiB) and report `truncated: true` with the artefact's true
   on-disk `bytes`; the rest is in the repo, not in the response.
5. **`raw/` authorship is weaker than it looks.** The curator being "the sole writer of `raw/`" is a
   property of *one curator run* — `raw_writes` is that run's FINAL-DIFF allowlist
   (`curator/apply.py`), not a repo-wide invariant. A human can commit into `raw/` (including
   `raw/_blob/`) directly, and nothing detects it. Note also what the route does and does not do:
   `raw/` is git-tracked and `agora sync` already pushes it to the backup remote, so `/raw` does not
   *create* an exposure — it makes an existing git-level one reachable over HTTP, which matters the
   moment the face is behind anything more than loopback.
6. **`assets/` is still unserved.** `assets/` is a curator-writable prefix
   (`curator/constants.py` `ALLOWLIST_DIR_PREFIXES = ("wiki/", "assets/")`) that the schema template
   tells authors to use, but no face serves it and the body-link rewriter skips image embeds by
   construction, so a relative `<img src>` into `assets/` renders broken. Same defect shape as this
   section had, and explicitly out of scope for #169 — it has no issue of its own yet.
7. **One size tier, one cap.** Every attachment is stored inline in git and refused above
   `web.upload.max_bytes` — one constant, shared by the web upload and `agora capture`. There is no
   large-file pointer/LFS tier, so a repo that receives big originals grows its git history by
   exactly that much.
8. **No drift tooling.** The sidecar records the digest; nothing re-fetches a `source_url` or diffs
   a re-extraction against the kept bytes.
9. **Retention is unchanged.** An artefact kept forever in git history meets the unwritten
   right-to-delete ADR-0031 head-on (#42) — §2 applies to blobs exactly as to notes.

**What you can do now.** Upload freely — the bytes are kept, cited, and now followable: click a
`sources:` row on the note page, or run `agora read <the sources: string>`. Read (1)–(3) before you
expose the face to anyone: a capture is evidence, not a reviewed artefact, and it is the least
filtered content in the repo. `web.features.raw_enabled: false` turns the **web** surface off for a
team deployment — both routes, the linked `sources:` rows, and the body-link rewrite
([`DEPLOY-TEAM.md`](DEPLOY-TEAM.md)). It does **not** gate `kb_read` or `agora read`: there is no
MCP/CLI kill switch, only the choice not to expose the MCP face. Keep your own copy of anything large or
sensitive until (7) and (9) are decided.

**Tracking.** The read-side gap is closed by
[#169](https://github.com/handochan/agora-kb/issues/169) wave A — the `/raw` + `/api/raw` routes,
linked `sources:`, the `kb_read` bridge, and the `agora query`/`read`/`neighbors` verbs. What #169
still owes is **wave B**: a curator-*emitted* inline citation so Obsidian can click through from the
note body (today `sources:` is a plain frontmatter list, which Obsidian does not linkify). The
undesigned control over what these read paths may emit is ADR-0041 residual risk R1
([#166](https://github.com/handochan/agora-kb/issues/166)); the unimplemented repo-internal `file:`
connector fence is [#165](https://github.com/handochan/agora-kb/issues/165) (§6b).
[#48](https://github.com/handochan/agora-kb/issues/48) keeps its remainder — the size-tier/LFS
policy and the dedup-union rule, co-designed with ADR-0031 (#42).

---

## 6a. Schema-1 repos are read-only in this build

**What is true.** This build writes **KB wiki schema 2** ([ADR-0041](adr/0041-stratum-kind-first-layout.md),
Proposed): the first directory under `wiki/` is the note's kind (`concepts/`, `summaries/`, `notes/`,
`maps/`, `entities/`, `people/`) and the topic lives in the note's `subjects:` frontmatter.
`agora repo init` creates schema 2 by default. `config.SUPPORTED_KB_SCHEMA_VERSIONS` is `{1, 2}`, so
a repo created by an earlier release still **reads**: `agora query`/`status`/`browse`/`doctor`, the
MCP read tools, and the web read routes all work on it. Every **write** path refuses, with one
message naming the conversion — `agora curate`, `agora watch`, `agora requeue`, `agora harvest`, the
`kb_curate` MCP tool, and `Inbox.write` itself, which is the single gate covering `kb_remember` and
the web upload. The refusal is `ReadOnlySchemaVersionError`, a subclass of
`UnsupportedSchemaVersionError`, so it is distinguishable from "this build cannot read your repo".

**`agora doctor` runs, and still fails the verdict — know this before you gate a unit on it.**
Doctor keeps its diagnostic exemption: it is the one command that reaches such a repo and explains
why the others refuse, printing a `write: READ-ONLY …` line naming `--from-kb`. Running is not
passing, though. The overall run ends `status: unhealthy` with **exit 1**, on the same judgement
that made an unrunnable curator unhealthy in #96 — a KB that can accept nothing new is not a healthy
deployment — and `--skip-probe` does not silence it, because the cause is the repo, not the brain.
So **a launchd/systemd health gate on `agora doctor` goes red on a schema-1 repo**, with a
documented cause ([`../deploy/README.md`](../deploy/README.md) "Health check") and one fix: the
conversion below, not a flag.

**Why a refusal rather than a warning.** DESIGN §10 V9's posture for a new binary on an old repo is
"read-works / write-warns", and a warn assumes the write is merely suboptimal. Here it would be
corrupting: APPLY would commit schema-2 paths and schema-2 frontmatter into a schema-1 tree,
producing a repo that is neither, that no lint ruleset can gate, and whose damage is already in git
history. The inbox write refuses too, for a related reason — an inbox that can never drain, and that
a re-import into a new repo would orphan, is silent data loss dressed as success.

**There is no in-place migrator, and there will not be one for this bump.** No `agora repo upgrade`
(#63), no dual-layout reader, no compatibility shim. The sanctioned crossing is a **conversion into
a NEW repo**, and it is one command:

```bash
agora import --from-kb <old-repo> <new-repo>
```

The source repo is **never modified**. The conversion implements ADR-0041 D6 rules 1–7: each note's
`type:` becomes its `kind:` and the note moves into that kind's directory; the path domain becomes
`subjects: [<domain>]` whenever that domain is declared in the source taxonomy — the v1 path domain
is a real curator assertion and is never silently discarded; a domain the source taxonomy no longer
declares converts with `subjects: []` **and a per-note warning**, because lint L1-5 grades
`subjects:` against the copied taxonomy and writing an undeclared one would mint a repo that fails
its own lint (a note with no path domain at all, such as the root `index.md`, gets the same
`subjects: []` structurally and needs no warning — there was never a subject to carry);
`wiki/<d>/<d>-moc.md` becomes `wiki/maps/<d>.md` with every `[[<d>-moc]]` and body link rewritten; same-date dailies from different domains **merge**
into the one `wiki/notes/<yyyy>/<mm>/<date>.md` journal, sections concatenated in domain order,
`sources:` unioned, each merged section keeping its origin domain as a `subjects:` entry; `raw/` is
copied **byte-identically** and `sources:` strings are **not** rewritten (which is the whole reason
the conversion is cheap); and `_meta/kb.yaml` is minted with a **new** `kb_id` stamped into every
note, because the destination is a new KB rather than a continuation. It prints a conversion report
— the note, rename, merge and `raw/` counts, every renamed basename, every merge, and every
top-level path in the source it did NOT carry over (a `README.md`, a `docs/` folder, `.obsidian/`:
left exactly where they are, never copied and never deleted). A basename collision the conversion
would introduce is a **hard failure with the colliding names listed**, exit 1, never a silent rename: a converter that renames
silently is a converter that loses `[[basename]]` edges.

**When it bites.** You have a KB created by an earlier Agora release. Your first `agora curate` or
`kb_remember` on it refuses, and `agora doctor` / `agora status` print a READ-ONLY line pointing at
`--from-kb` — `doctor` additionally ends `status: unhealthy` and exits 1, which is the expected
verdict for such a repo rather than a second fault to chase. On the web face, an upload into such a repo comes back as a **per-file receipt error**
naming the same remedy — not a 500 — and the dashboard's KB-health panel carries a
`write: READ-ONLY` stat beside `lint`, with `agora_kb_schema_writable 0` on `/metrics`, so a repo
that lints clean while refusing every capture cannot show as green.

**What you can do now.** Read from it normally, and convert when you are ready. Do not point a
capture path at it and assume the events will drain later — they will not. Keep the old repo: the
conversion reads it and writes a new tree, so nothing is destroyed by waiting, and nothing is
destroyed by converting either.

**Two things a schema-1 repo silently loses on the READ side, and they are worth knowing:**

- **Ranking degrades — on a schema-1 repo specifically.** `core/wiki.py`'s map predicate is now
  `wiki/maps/…` unconditionally, with no schema-1 branch, so a schema-1 repo's
  `wiki/<domain>/<domain>-moc.md` files are no longer level-0 seeds. They are still reached as
  children of the root `index.md` (at `d_moc = 1`), and their own children one hop further out, so
  the structural term shifts rather than disappears — but hit order will not match what an earlier
  release returned for the same question. (This is *not* what the flip does to a converted corpus:
  measured on the `tests/rank_golden/` fixture, the seed rule contributed **zero** lines to the
  before/after diff, because `wiki/maps/` ends up holding exactly the maps the v1 MOCs held —
  `tests/rank_golden/FLIP-DIFF.md`.) `agora eval` is how to see it on your own corpus.
- **Gold packs and `kb_context` still work**, because `Note.kind` is derived for *both* schemas — a
  schema-1 `type: theme` maps to `concept` through the frozen ADR-0041 D2.5 table — so the
  claim-bearing population a pack draws from is the same set of notes it always was.

**Tracking.** ADR-0041 D6; the schema-version guard is #98.

---

## 6b. `wiki/people/` is excluded from gold packs and `kb_context` — but not from agent reads

**What is true.** `wiki/people/**` is a human-owned namespace: the curator may never write it (any
add, modify, rename or delete under it in a curated diff fails the run), `lint()` permanently
excludes it from its graded population for *every* caller, and its basenames are outside the global
`[[basename]]` identity space — a people note is addressed by path, never by `[[basename]]`. Read is
first class: it is indexed and returned by search like any other note.

On day 1 it is **excluded from every gold pack and from `kb_context`**, because the outbound
redaction boundary for a human-owned read corpus is undesigned. The ADR-0023 connector-boundary
redaction and a *read-corpus* boundary are not the same boundary, and shipping human notes into
every agent session's standing context before that is designed would be an unreviewed egress. This
is a default, not a permanent rule; lifting it needs the boundary design, not a config flag.

**The gap this does NOT close, stated rather than papered over.** The exclusion covers the **push**
surface only. `kb_query`, `kb_read` and `kb_neighbors` will return people content to an agent on
demand — a pull-shaped, agent-initiated read — and since #169 wave A so will the `raw/` bridge on
the same surface: `/raw`, `/api/raw` and `agora read` (§6). The control for that surface is
**distinct and still undesigned** (ADR-0041 residual risk R1; it amends ADR-0027 §8's scope
sentence rather than claiming the push exclusion covers it).

**Two further fences ADR-0041 specifies are NOT implemented yet.** (a) The `file:` connector fence —
"a connector glob on a path inside the repo may cover `wiki/people/**` and nothing else" — has no
code: `harvester/connectors.py` guards only the gold directory (`_is_within_gold`). Until it lands,
do not point a `file:` connector at a glob inside your own KB: the curator's own output can be fed
back to it as candidates, and a glob that covers `wiki/people/**` carries human-owned content into
the inbox and thence into `raw/`, where the #169 read bridge will serve it (§6). (b) `raw/_pages/` is a reserved prefix with no writer and no gate
exception.

**When it bites.** You file something personal under `wiki/people/you/` expecting it to stay out of
agent context. It stays out of packs; it does not stay out of an agent that asks for it.

**What you can do now.** Treat `wiki/people/**` as *human-owned and searchable*, not as *private*.
The repo is still the only security boundary (§8), and §2 still applies: there is no delete.

**Tracking.** ADR-0041 D3.3 + residual risk R1.

---

## 6c. Long documents, `summaries/` and `entities/` have containers but no contents

**What is true.** Schema 2 ships two kinds that nothing produces. `wiki/summaries/` and
`wiki/entities/` have directories (created at `repo init`, each with a `.gitkeep`), declared
frontmatter shapes, and lint rules — and **no producer**: no curator op creates one, the importer has
no rule that emits one, and there is no human authoring route with a `people/`-style carve-out. That
is deliberate: shipping the container before the contract avoids a second migration, while inventing
a producer would create a population no ADR governs (ADR-0041 OD-7/OD-8).

The long-document contract those tiers are waiting on — **ADR-0040** — **has not been written**. The
`raw/_pages/` prefix reserved for it is likewise empty and unprivileged.

**When it bites.** You expect to file a long document, or a person/product/project as an entity page,
and find there is no command that does it.

**What you can do now.** File it as a concept, or as your own note under `wiki/people/`. Nothing you
write by hand into `summaries/` or `entities/` is *rejected* — those directories are inside the
closed kind set — but nothing produces or maintains such a note either.

**Tracking.** ADR-0041 OD-7 / OD-8; ADR-0040 is reserved and unauthored.

---

## 7. Recovering terminal failures — `agora requeue`

This is the section that decides whether a broken week costs you knowledge. It does not, if you
follow it.

### What `agora status` shows, and what it does not

A failing curator is **visible** as of [#96](https://github.com/handochan/agora-kb/issues/96).
`agora status` prints three failure lines beyond the counters (`cli.py:536-541`):

- `last_attempt:` — when a run last *started*, whether or not it published. `last_run` remains "last
  successful publish", so a repo whose curator fails every run shows `last_run: never` while
  `last_attempt` moves.
- `last_failure:` — verdict word first, so `agora status | grep UNRESOLVED` works
  (`cli.py:550-570`). `UNRESOLVED` means no successful publish has happened since; `superseded`
  means one has. It is **sticky** — a later success never clears it, it only downgrades the verdict
  (`core/state.py:100-102, 190-203`).
- `failed_events:` — the live count of terminal events sitting in `_kb/failed/` right now, derived
  by one shared helper so the CLI, the MCP face and the requeue report cannot disagree
  (`core/inbox.py:195-212`).

Three things it still does not tell you:

- **The exit code is not a failure signal.** A `status: failed` run returns **0**, deliberately: a
  CAS conflict and a within-budget retry are normal self-healing, and making them non-zero would
  trip `Restart=on-failure` on a supervising unit (`cli.py:616-622`). Check the `status:` line, or
  `agora status`, never `$?`.
- **`counters.failed` over-counts.** It is a lifetime tally of terminal dispositions and is never
  decremented; `agora requeue` does not write `state.json` at all. One event that fails, is
  requeued, and fails again reads `failed=2`. `failed_events:` is the exact live number.
- **It names no events.** `last_failure` carries the run id, the phase, and the *first* reason. To
  see which captures are stuck you read `_kb/failed/` (below) or run `agora requeue --dry-run`.

`agora doctor` adds the cause side: since #96 it probes the configured brain, contributes to the
verdict (**exit 1** when the brain is unreachable), supports `--skip-probe` for hosts that
legitimately have no brain, and prints a copy-pasteable remediation that leads with a CLI agent
already on your PATH before suggesting an Ollama pull.

### How a capture becomes terminal

`curator.max_attempts` defaults to **3** (`curator/constants.py:37`, `config.py:128`). Each failing
run writes one `_kb/failed/<date>/<run-id>/error.json` and returns the event to the inbox while the
budget lasts; on the attempt that exhausts it, the event `.md` is moved next to the record and stays
there (`curator/worker.py:1148-1167`). With `agora watch --interval 60` and a threshold trigger that
re-fires each tick, a brain that stops answering takes captures terminal in two to three minutes.

The record is the durable, un-truncated explanation — `agora status` and `agora curate` only echo a
bounded head of it. Verbatim from the reproduction below:

```json
{
  "run_id": "2026-07-31T16-22-44.409Z--e19beb",
  "base_commit": "498b0fc4cfa6755e0f7db68412e6d951524846b7",
  "event_ids": [
    "2026-07-31T16-22-42.213Z--58cfbb"
  ],
  "phase": "claimed",
  "failed_checks": [
    "PLAN-BACKEND: backend 'qwen' ('agora-ollama-brain-typo') could not be executed: [Errno 2] No such file or directory: 'agora-ollama-brain-typo'; check adapters.yaml and that the program is installed and on PATH"
  ]
}
```

`event_ids` is the list of captures the run was holding — that is how you find out *what* is stuck.

### The procedure

Never hand-move files inside `_kb/`. `agora requeue` is the supported back-edge and it is
rename-only under the curator lock: same bytes, same id, no second event minted, an occupied inbox
slot reported rather than clobbered, nothing deleted — the five clauses C1–C5 of the ADR-0002
spool-custodian appendix, each test-locked (`docs/adr/0002-cqrs-single-writer-curator.md:52-76`).
Because it deletes nothing, there is nothing to back up before running it; the thing that *would*
need a backup is a hand edit, which is why you should not make one.

From a source checkout, prefix each command with
`uv run --directory /ABSOLUTE/PATH/TO/agora-kb`.

```bash
# 1. see it
agora status --repo /ABSOLUTE/PATH/TO/knowledge-repo

# 2. read the cause (doctor exits 1 when the brain is unreachable, and prints the fix)
agora doctor --repo /ABSOLUTE/PATH/TO/knowledge-repo

# 3. FIX THE CAUSE. Then prove it — see "what green does not prove" below.

# 4. preview — changes not one byte. RUN_ID is the id `agora curate` printed as
#    `failed_requeue:`; use --all instead to see the whole backlog.
agora requeue --repo /ABSOLUTE/PATH/TO/knowledge-repo --run RUN_ID --dry-run

# 5. move them back. --reset-attempts restores the retry budget and is correct ONLY
#    because step 3 actually fixed the cause (see below).
agora requeue --repo /ABSOLUTE/PATH/TO/knowledge-repo --run RUN_ID --reset-attempts

# 6. consolidate now, or let the watch loop pick them up
agora curate --repo /ABSOLUTE/PATH/TO/knowledge-repo --force
agora status --repo /ABSOLUTE/PATH/TO/knowledge-repo
```

**What a green `agora doctor` does and does not prove.** Step 3 is the step everything else depends
on, so be precise about the evidence. Doctor's brain check establishes **presence**, not function:

- For a non-Ollama argv it asks only whether `argv[0]` resolves and is executable — a brain that is
  installed but broken reads `status: healthy`.
- For an Ollama backend it establishes that the daemon **answers**, on every path including a
  `--model` pin in the argv (#129). What it does not establish is that the *pinned tag is
  installed*: `/api/tags` returns fully-qualified `name:tag` while Ollama resolves an unqualified
  name to `:latest`, so exact membership is not a sound existence test. The line says so itself
  with `the pin is NOT verified installed`; `ollama show '<tag>'` is the check that answers it.

That matters here because step 5 spends a resource. The only thing that proves the cause is fixed is
**a run that reaches `status: published`**. So: fix, then `agora requeue --dry-run` to see the
backlog, then requeue *one* run's events without `--reset-attempts` and let them prove the brain
answers — or accept that `--reset-attempts` on a still-broken curator costs a full budget, not one
tick.

**Step 3 is not politeness — it is the retry-budget trap.** The retry count is not stored in the
event. It is *derived* from the number of retained `_kb/failed/**/error.json` records that reference
the event id (`curator/worker.py:1217-1231`). A plain `agora requeue` therefore returns an event
that has already spent its budget, and it gets exactly **one** more run: if that run publishes, the
capture is saved; if it fails, the event goes straight back to `_kb/failed/` without retrying.
Requeueing into a still-broken curator costs one tick of noise, not three.

`--reset-attempts` restores the full budget by **archiving** those records to `_kb/requeued/`
(`core/layout.py:120-145`) — never rewriting or deleting them, and never editing `state.json`. It
scopes *records*, not events: a record is released once none of the events it lists is still in
`_kb/failed/`. Every released record is printed on an `archived:` line. Use it after a real fix; it
is the wrong flag before one.

Two more behaviors worth knowing before you type `--all`:

- **A large `--all` head-of-line-blocks new captures.** Requeued events keep their old ids, so they
  sort to the FIFO head and the claim's `max_candidates_per_run` cap (default 32,
  `curator/constants.py:54`) feeds them first. Requeueing N events delays new captures by
  `ceil(N / 32)` runs — and **`--reset-attempts` roughly triples that** if the cause is not actually
  fixed, since each event now burns three runs instead of one before going terminal again
  ([`../deploy/README.md`](../deploy/README.md) → "Recovering terminal failures"). Prefer
  `--run <id>` in batches.
- **The "still UNRESOLVED" warning fires even after you have fixed the cause.** `last_failure` is
  sticky and only downgrades to `superseded` after a *successful publish*, so the preflight warning
  is expected on a legitimate recovery — it is a warning, never a guard (`cli.py:824-832`), and it
  is printed only for `--all`, not for the narrow `--run`/`--event` selectors.

### Reproduction (throwaway repo, macOS 26.4, `agora 0.1.0b1`)

A fresh `agora repo init` repo with one capture in the inbox; `adapters.yaml` pointed at a brain
executable that does not exist (`agora-ollama-brain-typo`), then repaired to a **real, answering**
brain (`agora-ollama-brain --model qwen3.6:35b-a3b`) — so the `status: published` at the end is a
genuine consequence of the fix, not a separate run spliced in. Output is byte-for-byte real except
for edits made for width, all of which are marked: the throwaway repo path is elided as `…/demo-kb`,
repeated `repo:`/`inbox depth:`/`should_run:`/`reason:` header lines and unrelated `doctor` lines
are cut and marked `…`, and over-long strings end in `…`.

```console
$ for i in 1 2 3; do agora curate --repo …/demo-kb --force; done
…
status: failed
counts: failed=0, retried=1
failed_record: _kb/failed/2026-07-31/2026-07-31T16-22-43.964Z--ba862b/error.json
failed_checks: PLAN-BACKEND: backend 'qwen' ('agora-ollama-brain-typo') could not be executed: …
…
status: failed
counts: failed=0, retried=1
failed_record: _kb/failed/2026-07-31/2026-07-31T16-22-44.187Z--409d84/error.json
…
status: failed
counts: failed=1, retried=0
failed_record: _kb/failed/2026-07-31/2026-07-31T16-22-44.409Z--e19beb/error.json
failed_requeue: agora requeue --run 2026-07-31T16-22-44.409Z--e19beb
# exit code: 0 for all three — see "the exit code is not a failure signal" above

$ agora status --repo …/demo-kb
inbox depth: 0
last_run: never
last_commit: -
counters: ingested=0 merged=0 dropped=0 failed=1
last_attempt: 2026-07-31T16:22:44Z
last_failure: UNRESOLVED 2026-07-31T16:22:44Z run=2026-07-31T16-22-44.409Z--e19beb phase=claimed reasons=1 record=_kb/failed/2026-07-31/2026-07-31T16-22-44.409Z--e19beb/error.json first=PLAN-BACKEND: backend 'qwen' ('agora-ollama-brain-typo') could not be executed: …
failed_events: 1

$ agora doctor --repo …/demo-kb
agora doctor (agora 0.1.0b1, python 3.12.13)
…
  brain qwen: 'agora-ollama-brain-typo' NOT FOUND on PATH — install it or fix the adapters.yaml argv
    fix (no download — 'claude', 'codex', 'gemini' are already installed): add to adapters.yaml
      backends:          # merge into the existing key, do not add a second one
        claude: { argv: [agora-cli-brain, --, claude, -p], network: loopback }
      then set  curator.backend: claude  in _kb/repo.yaml (ADR-0016) — i.e.
      curator:           # merge into the existing key
        backend: claude
    fix (local model instead): ollama serve  &&  ollama pull qwen3.6:35b-a3b
…
  failures: events=1 last_attempt=2026-07-31T16:22:44Z last_failure=UNRESOLVED run=2026-07-31T16-22-44.409Z--e19beb …
  requeue: 1 terminal event in _kb/failed/ — fix the cause above, then 'agora requeue --all' returns the backlog to the inbox
status: unhealthy
# exit code: 1

# --- THE CAUSE IS FIXED HERE: adapters.yaml now names a brain that answers ---
# ---  argv: [agora-ollama-brain, --model, qwen3.6:35b-a3b]                  ---
$ agora doctor --repo …/demo-kb
…
  brain qwen: ollama http://localhost:11434 reachable, model pinned to 'qwen3.6:35b-a3b' by adapters.yaml argv (no /api/tags probe — the run lists no models either; the pin is NOT verified installed)
…
  requeue: 1 terminal event in _kb/failed/ — fix the cause above, then 'agora requeue --all' returns the backlog to the inbox
status: healthy
# exit code: 0 — the daemon answered, but note the parenthetical: nothing here proves that
#                PINNED TAG is installed. Green proves presence, not function. See "what a
#                green doctor proves".

$ agora requeue --repo …/demo-kb --all --dry-run
warning: the last curator failure is still UNRESOLVED (run=2026-07-31T16-22-44.409Z--e19beb record=…) — run 'agora doctor' first; a requeued event goes terminal again on the next failing run
requeue [dry-run]: selector=all matched=1
  2026-07-31T16-22-42.213Z--58cfbb: would requeue -> _kb/inbox/local/2026-07-31T16-22-42.213Z--58cfbb.md
would requeue: 1
would skip: 0
failed_events: 0
note: fix the cause before curating again — 'agora status' shows the last failure, 'agora doctor' checks the brain

$ agora requeue --repo …/demo-kb --all --reset-attempts
warning: the last curator failure is still UNRESOLVED (run=2026-07-31T16-22-44.409Z--e19beb record=…) — run 'agora doctor' first; a requeued event goes terminal again on the next failing run
requeue: selector=all matched=1
  2026-07-31T16-22-42.213Z--58cfbb: requeued -> _kb/inbox/local/2026-07-31T16-22-42.213Z--58cfbb.md
requeued: 1
skipped: 0
reset_attempts: archived=3 kept=0
  archived: _kb/failed/2026-07-31/2026-07-31T16-22-43.964Z--ba862b/error.json -> _kb/requeued/2026-07-31/2026-07-31T16-22-43.964Z--ba862b/error.json
  archived: _kb/failed/2026-07-31/2026-07-31T16-22-44.187Z--409d84/error.json -> _kb/requeued/2026-07-31/2026-07-31T16-22-44.187Z--409d84/error.json
  archived: _kb/failed/2026-07-31/2026-07-31T16-22-44.409Z--e19beb/error.json -> _kb/requeued/2026-07-31/2026-07-31T16-22-44.409Z--e19beb/error.json
failed_events: 0
note: fix the cause before curating again — 'agora status' shows the last failure, 'agora doctor' checks the brain

$ agora curate --repo …/demo-kb --force
…
status: published
published_commit: 8b598c410d21ce36d759ce2f2887d1ef8ffef89d
counts: CREATE_THEME=1, candidates=1, claimed=1, inbox_remaining=0, prose_pending=0, prose_regions=1
# 1m30s wall clock — this is the run that actually proves the brain answers.

$ agora status --repo …/demo-kb
inbox depth: 0
last_run: 2026-07-31T16:22:45Z
last_commit: 8b598c410d21ce36d759ce2f2887d1ef8ffef89d
counters: ingested=1 merged=0 dropped=0 failed=1
last_attempt: 2026-07-31T16:22:45Z
last_failure: superseded 2026-07-31T16:22:44Z run=… record=_kb/requeued/2026-07-31/2026-07-31T16-22-44.409Z--e19beb/error.json …
failed_events: 0
```

Three details in that tail are the contract working as designed: `counters.failed` stays at `1`
while `failed_events` is `0` (the lifetime tally is not decremented); `last_failure` survives the
successful run but downgrades to `superseded`; and its `record=` pointer follows the archived twin
into `_kb/requeued/` instead of dangling (`cli.py:573-585`).

A separate run of the same scenario confirmed the budget trap directly: requeueing without
`--reset-attempts` into a still-broken curator produced `counts: failed=1, retried=0` on the very
next run — one attempt, straight back to terminal.

**Tracking.** [#99](https://github.com/handochan/agora-kb/issues/99) shipped `agora requeue`;
[#96](https://github.com/handochan/agora-kb/issues/96) shipped the failure surface;
[#124](https://github.com/handochan/agora-kb/issues/124) closed the paths where a run that could not
return an event deleted it anyway. Longer-term retention/pruning of `_kb/failed/` is unaddressed —
the tree is never pruned (`core/inbox.py:205-207`).

---

## 8. The web face has no authentication

**What shipped.** [#94](https://github.com/handochan/agora-kb/issues/94) closed the
browser-mediated attack surface. Three middlewares are registered in `build_app`
(`faces/web/app.py:442-446`):

- **A `Host` allowlist** — a request whose `Host` is not in `web.security.allowed_hosts` gets a 400.
  The default is loopback only: `("localhost", "127.0.0.1")` (`config.py:754`). This is what makes
  DNS rebinding fail: a rebound request still carries the attacker's DNS name in `Host`.
- **An `Origin`/`Referer` guard** on every state-changing method, applied before FastAPI parses the
  multipart body and before any inbox append. A present-and-mismatched origin (including `null`) is
  refused with 403 (`faces/web/app.py:243-306`).
- **Framing refusal** — `X-Frame-Options` plus CSP `frame-ancestors`, so a clickjacked UI cannot
  borrow your same-origin position (`faces/web/app.py:375-398`).

**What is still true, and is the reason this section exists.**

- **There is no authentication and no authorization at all.** No login, no token, no role: anything
  that can reach the process is a full-rights operator. ADR-0036 is *Proposed* and Phase 4
  (`docs/adr/0036-authn-authz.md:8`). `web.identity.trusted_header` (#67) threads a provenance
  *label* into the log; it isolates nothing and enforces nothing.
- **A write with no `Origin`/`Referer` header is accepted by default.** `require_origin` defaults to
  `false` (`config.py:791`), because scripted writers — `curl`, CI, the documented upload procedures
  — send no `Origin`, and refusing them by default would break documented operations for no browser
  gain (`faces/web/app.py:262-267`, `config.py:781-785`). The consequence is precise: the guard stops
  a *browser* being used against you; it does not stop any local process or anything else that
  reaches the bound port. Set `web.security.require_origin: true` if your deployment has no scripted
  writers.
- **Whatever is injected is permanent.** An inbox append becomes wiki content at the next curator
  run, and §2 applies: there is no delete.

**What you can do now.**

- Keep the loopback bind. `agora web` defaults to `--host 127.0.0.1` (`cli.py:341`); do not change
  it without an authenticating proxy in front.
- For a team, front it with a reverse proxy that does TLS and auth, treat *the proxy* as the
  security boundary, and add the public hostname to `web.security.allowed_hosts` — a proxy must pass
  the client's `Host` through verbatim or the Origin guard's baseline breaks
  ([`DEPLOY-TEAM.md`](DEPLOY-TEAM.md) §2).
- Widening `allowed_hosts` without putting auth in front is outside the supported envelope
  ([`ROADMAP.md`](ROADMAP.md):306-309).

**This section is revised when authentication lands.** ADR-0036 was authored under
[#69](https://github.com/handochan/agora-kb/issues/69) and is *Proposed*; nothing implements it, and
implementation is Phase 4. Until then every statement above holds. Remote MCP transport (Streamable
HTTP) is coupled to the same decision and is deliberately not shipped — stdio only.

---

## 9. A hosted CLI-agent brain sends your KB off the machine

**What is true.** The curator's brain is pluggable (ADR-0015/0016), and one of the two paths
[`GETTING-STARTED.md`](GETTING-STARTED.md) §1.2 documents is `agora-cli-brain` driving `claude`,
`codex`, or `gemini`. Those are **hosted services**. On every run the shim pipes the curator bundle —
the captured facts being filed plus the wiki text they are merged into — to that vendor's API on
stdin (`adapters/cli_agent_brain.py:86-113`). Nothing about that is hidden or accidental; it is the
documented, supported way to run without a local model. But it is a different data posture from the
default, and it is the one place where §2's "do not capture secrets" rule and your brain choice
interact.

The default is the other way round: `agora repo init` wires the local Ollama shim
(`config.py:288-330`), and an OSS, fully-local path is mandatory by invariant 4 — a cloud brain can
never be the default ([`ROADMAP.md`](ROADMAP.md) → "Not in 0.1.0-beta" → *Cloud brains*).

**When it bites.** You picked Path B during onboarding because it needed no 23 GB download, then
later captured something you would not have pasted into that vendor's web UI. The transfer already
happened, on the run that filed it. There is no retention setting, no redaction on this path
(redaction runs only at the harvest boundary, §2), and no way to un-send it.

**What you can do now.**

- Decide this *before* you capture, not after. `agora doctor`'s `routing:` line names the brain each
  act runs on; `_kb/repo.yaml` `curator.backend` and `adapters.yaml` `routing:` are where it is set.
- For a fully-local posture use Path A (a local Ollama model). It is the default for this reason.
- If you want a hosted brain for *quality* but not for *everything*, note that routing is per-act
  (ADR-0015): `plan` and `author` can run on different brains. Both still see KB content, so this
  narrows nothing by itself — it is a knob, not a mitigation.

**Tracking.** Not a defect and not tracked as one — it is a documented operator decision
([`../SECURITY.md`](../SECURITY.md) §3(c) "Known limits").

---

## Maintaining this document

The short list lives in [`../CHANGELOG.md`](../CHANGELOG.md) → "Known limitations"; the normative
list lives in [`ROADMAP.md`](ROADMAP.md) → "Not in 0.1.0-beta". When code closes one of these, the
fix is to update all three in the same change — a limitations page that outlives the limitation is
worse than none, because a reader who has been told "there is no defense" will not go looking for
one.
