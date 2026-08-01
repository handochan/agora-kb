# Changelog

All notable changes to Agora (`agora-kb`) are recorded here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); version strings are
[PEP 440](https://peps.python.org/pep-0440/).

## Versioning and tag convention

Fixed by [#101](https://github.com/handochan/agora-kb/issues/101) so that a bug report can name a
build and a maintainer can reproduce it:

- **One source of the version.** `src/agora_kb/__init__.py:__version__` is it; `pyproject.toml`
  declares `dynamic = ["version"]` and reads that file through `[tool.hatch.version]`. The runtime
  must be able to answer `agora --version` from a plain `git clone` with no install metadata, which
  is why the source file — not the packaging metadata — is authoritative. Never edit two places.
- **Tags are `v` + the exact PEP 440 string**: `0.1.0b1` → `v0.1.0b1`. Not SemVer's
  `0.1.0-beta.1` — PyPI normalizes that to `0.1.0b1`, and the wheel filename would then disagree
  with the tag it was built from.
- **Only a CI-green commit is tagged.** `.github/workflows/ci.yml` (#100) is the definition of
  green: `ubuntu-latest` and `macos-latest` are required; `windows-latest` is a `continue-on-error`
  progress signal for the Windows port (epic #85) and never gates a tag.
- **The commit immediately before a tag is a single `__version__` promotion commit** and contains
  nothing else, so the tag points at a commit whose only content is "this is now that version".
- **Betas use `bN`** (`0.1.0b1`, `0.1.0b2`, …), and **tags are never moved or deleted.** A tag that
  turns out to be wrong is superseded by the next one; rewriting it would break every checkout and
  every bug report that already cited it.

### Release status — read this before you trust the section below

**`v0.1.0b1` has not been cut.** The version *string* is set and `agora --version` reports it, but
no git tag exists yet, so `git checkout v0.1.0b1` will not resolve and there is no published
artifact. Installing today means installing from `main`, which moves.

The `0.1.0b1` notes below are the prepared release notes, not a shipped release. Remaining gates
(epic [#93](https://github.com/handochan/agora-kb/issues/93), Track A): the frontmatter
`body_status` invariant (#119) with its test-oracle repair (#121), the `schema_version` skew guard
(#98), PyPI name reservation (#102), a friendly Windows failure (#103), repo metadata
(description/topics, part of #106), and a clean-machine release smoke run by someone who is not the
author (#107) — plus the `windows-latest` gate ruling that decides whether Windows ships in b1 or
b2. Landed: [`SECURITY.md`](SECURITY.md) (#95), [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md)
(#104), [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) (#105), the README reconciliation (#106), and
the two `agora doctor` truthfulness fixes (#129).

**Security reports go through [`SECURITY.md`](SECURITY.md)** — private vulnerability reporting, not
a public issue.

## [Unreleased]

Nothing since the `0.1.0b1` notes below — the work in flight is the release-gate list above, not
new capability.

## [0.1.0b1] - unreleased

*Prepared, not tagged — see "Release status" above.*

The first release intended for someone other than the author: a developer dogfooding a personal
knowledge base on one machine, or a team of 2–10 evaluating whether the shape fits. Everything is
local, unauthenticated, and markdown-on-disk. Requires Python ≥ 3.12, git, and a curator brain
(see the known limitations).

### Added

- **The core loop — capture → curate → query, on plain markdown in git.** Many writers append
  immutable events to a per-writer inbox; exactly one curator process reads the queue, asks an LLM
  what to file where, and commits the result to `wiki/`. Nothing else writes the wiki. A run is
  transactional (claim → sandboxed worktree → validate → publish → finalize), so a hallucinating or
  failing brain cannot leave the wiki half-edited: the deterministic FINAL-DIFF gate rejects the
  whole run instead. Queries are *navigation* — deterministic BM25F ranking over the note graph
  with path/anchor citations, no vector database, no embedding service (ADR-0009/0012).
- **An MCP face for agents** (`agora serve`, FastMCP over stdio): `kb_remember`, `kb_query`,
  `kb_read`, `kb_neighbors`, `kb_context`, `kb_status`, `kb_curate`. Any MCP-speaking client
  (Claude Code, Codex, Gemini CLI, Qwen Code, Hermes, …) can both write knowledge and read it back;
  `kb_read` + `kb_neighbors` close the loop from a search hit to the note to its link ego-graph
  (#58).
- **A CLI face for humans** (`agora`): `repo init`, `import`, `status`, `curate`, `requeue`,
  `harvest`, `index`, `gold`, `sync`, `watch`, `serve`, `web`, `doctor`.
- **A pluggable curator brain.** The curator is not a model — it shells out to one, declared in
  `adapters.yaml`. `agora-ollama-brain` drives a local Ollama model (the default, zero API cost);
  `agora-cli-brain` (ADR-0016) drives *any* headless CLI agent as a pure text generator, in a
  throwaway working directory so the agent's own scratch files can never reach the published diff.
  `plan` and `author` route independently (ADR-0015), so a strong remote planner can pair with a
  local writer. Four real brains have been verified end-to-end (qwen + hermes via Ollama, claude +
  codex via the CLI shim).
- **A harvester that pulls other agents' memory in** (`agora harvest`, opt-in, off by default —
  ADR-0007/0017/0018/0023). File connectors segment an agent's on-disk `MEMORY.md` into facts and
  can follow its pointer links one hop to the sibling note that holds the actual content; a
  `session:` connector distills coding-session transcripts (ADR-0023). Everything arrives as a
  *gated candidate*, never as curated knowledge, and passes the curator's existing keep/merge/drop
  judgment — which is also the primary defense against a knowledge round-trip loop. Redaction runs
  at the connector boundary. A fail-closed scope gate refuses to harvest a personal source into a
  team repo.
- **A web face for humans** (`agora web`, optional `web` extra — ADR-0019/0020/0021). API-first
  FastAPI (`GET /api/{status,search,notes,notes/{path},graph,gold/{pack}}`, `POST /api/upload`)
  with a server-rendered HTMX UI over the same handlers: browse, search, drag-and-drop multi-upload,
  a read-only dashboard (KB health · curator · harvester), a Prometheus `/metrics` exporter that
  never lints on scrape, and an interactive knowledge graph plus a per-note local ego-graph drawn by
  a vendored MIT force-graph — no Node toolchain, no CDN. Note bodies render with raw HTML disabled.
- **Ingest extractors** (optional `ingest` extra): URL (trafilatura), PDF (pdfminer.six — MIT, not
  AGPL), office/html/csv/json/xml (markitdown), `.epub`, and dependency-free `.txt`/`.md`. An upload
  becomes an extraction plus an inbox capture; the curator stays the sole writer of `raw/`
  (ADR-0020). A missing optional dependency surfaces as a clean "extractor unavailable", not an
  import traceback.
- **Retrieval that works in Korean** (#56/#57): NFC normalization + CJK-bigram tokenization,
  `aliases`/`summary` as scoring fields, and a `note-<sha8>` filename fallback so a purely-Korean
  capture can no longer slugify to an empty string and get silently dropped.
- **Derived tiers that are always rebuildable** (invariant 1): `_kb/index/` reader cache
  (`agora index`, ADR-0012 §2) and `_kb/gold/` context packs (`agora gold`, ADR-0027) — a pure,
  token-budgeted function of (curated commit, pack spec), served byte-identically to `kb_context`,
  the `agora://gold/{pack}` MCP resource, and `GET /api/gold/{pack}`. Deleting **those two
  directories** costs recomputation, never knowledge. This does **not** extend to `_kb/` as a whole:
  the same tree holds the uncurated inbox, `_kb/failed/` events awaiting `agora requeue`, and
  curator state, none of which is derivable from the wiki — see known limitation 4.
- **A version you can quote in a bug report** (#101): `agora --version`, and an
  `agora doctor (agora <ver>, python <ver>)` header line built to be pasted into an issue. The
  string has exactly one home (`src/agora_kb/__init__.py`), which `pyproject.toml` derives from, so
  the runtime and the built artifact cannot disagree — see the tag convention at the top of this
  file.
- **No-loss capture** (#23, ADR-0022): a capture that matches no declared domain is filed under the
  first declared domain instead of being dropped. Repo-global curator thresholds
  (`curator.limits.{body_byte_bound,related_k}`, `curator.lint.max_orphans`) are honored at every
  run site.
- **Bounded batches** (#60, ADR-0024): `max_candidates_per_run` (default 32) slices the FIFO head so
  a large inbox does not blow a small model's context; the remainder waits for the next trigger.
  Batch size, cap, and queue-remaining are on the run log, `state.json`, and `/metrics`.
- **Always-on deployment** (#64/#65): `agora watch` (cron + threshold + idle triggers),
  `agora sync` push-only git backup with an opt-in auto-push after a published run, and
  launchd/systemd unit templates in `deploy/` — including a *separate* harvest schedule, because
  `watch` deliberately does not harvest. Team topology (reverse-proxy auth, SSH forced-command,
  footguns) is documented in [`docs/DEPLOY-TEAM.md`](docs/DEPLOY-TEAM.md) (#68).
- **Failure that is visible and reversible.** A failed `agora curate` prints the path of the
  `_kb/failed/<date>/<run>/error.json` that explains it and a bounded echo of the failing checks;
  `_kb/state.json` records `last_attempt`/`last_failure`, so a *non-terminal* failure — which
  returns its events to the inbox and bumps no counter — still shows up in `agora status` and
  `agora doctor` instead of leaving `last_run: never` (#96). `agora doctor` now probes the
  configured brain and refuses to report healthy when curate cannot possibly work. `agora watch`
  catches a raising tick, reports one bounded line, and backs off exponentially instead of
  converting a typo in `repo.yaml` into a ten-second crash loop under systemd/launchd (#97).
  `agora requeue (--run | --event | --all)` returns terminal events from `_kb/failed/` to the inbox
  by rename only — same bytes, same id, no second event minted — and the failure output now prints
  the exact command to run (#99).

### Security

- **Browser-mediated attack defense for the web face** (#94, ADR-0025 appendix): a `Host` allowlist
  (`web.security.allowed_hosts`, loopback-only by default) that closes DNS rebinding, an
  `Origin`/`Referer` guard on state-changing requests that must match the request's own authority,
  and framing refusal (`X-Frame-Options` + CSP `frame-ancestors`) against clickjacking. Before this,
  any page the operator happened to visit while `agora web` was running could POST into the inbox —
  a `multipart` form is a *simple* request, so no CORS preflight stood in the way — and DNS
  rebinding could read the whole KB.
- **Upload hardening** (#66/#53): an SSRF guard that blocks private-network fetches in the URL
  extractor (with a `web.upload.url_enabled` operator off-switch) and an actual-size cap that stops
  a zip decompression bomb.
- **Cross-platform path safety** (#108): `agora index build` no longer dies with a raw traceback in
  a repo whose directory name contains a space or Hangul (`~/My Knowledge`, `~/내지식`), and
  `agora import` can no longer be walked outside its destination by a `sources:` entry like
  `raw/../../etc/x` in an untrusted vault.
- **Curator isolation fixes** (#113/#114/#115): the Linux sandbox is fail-closed on a working
  `bwrap`, and a silently-failing authoring pass on Linux is now loud.
- **No event is destroyed by a failure** (#124): three curator paths returned events to the inbox
  through a mover that silently skips refusals and then deleted the processing directory anyway —
  so an event that could not be returned (an already-occupied inbox address was enough) was lost,
  counted by nothing. Markdown is the source of truth; a run that cannot return an event now fails
  loudly rather than quietly shrinking the KB.
- **`agora doctor` no longer reports green on a repo that cannot curate** (#129), in two cases that
  a reader had no way to tell apart from a healthy one:
  - Pinning the model with `--model` in the `adapters.yaml` argv skipped `/api/tags` — correctly,
    since a pinned run lists no models — but with it skipped *every* reachability check, so a repo
    whose Ollama daemon was down printed `status: healthy` and exited `0`. Liveness is now probed
    at `/` on every path (the model question stays skipped, because the run does not ask it), and
    a dead daemon is `unhealthy`. **Behavior change:** a pinned repo with a dead daemon now
    exits `1`.
  - `sandbox: seatbelt (ok)` proves the mechanism works on the host, which reads as "my brain is
    confined" — but a backend is confined only when its spec says `network: none`, and `repo init`
    writes `network: loopback`. A `confines this repo's brains:` line now answers that question
    directly (`NO` / `PARTIAL` / `yes`, naming each backend and its posture). Reporting only: an
    unconfined loopback brain is the designed default, not a fault.

### Known limitations

Read this list as "things that can bite you", not as a feature backlog. Every item was verified
against the code. The normative version lives in
[`docs/ROADMAP.md`](docs/ROADMAP.md) → "Not in 0.1.0-beta".

1. **No authentication and no authorization.** There is no login, no token, and no role: anyone who
   can reach the process is a full-rights operator. `agora web` binds `127.0.0.1` and the #94 Host
   allowlist is loopback-only, both by default; exposing it over a LAN, a tunnel, or the public
   internet means widening both, and that is outside the supported envelope until auth exists.
   Teams must put a reverse proxy in front and treat *the proxy* as
   the security boundary ([`docs/DEPLOY-TEAM.md`](docs/DEPLOY-TEAM.md)). `web.identity.trusted_header`
   (#67) threads a provenance *label* for the log — it isolates nothing and enforces nothing. Auth
   is Phase 4; ADR-0036 is still Proposed (`docs/adr/0036-authn-authz.md`). Remote MCP transport (Streamable HTTP) is coupled
   to that decision and is deliberately not shipped: stdio only.
2. **macOS and Linux only.** On native Windows not even `agora --help` runs — `curator/claim.py`
   imports `fcntl` unconditionally and that import sits on the path of every command. Windows is
   epic #85 (the import blocker is #86); `windows-latest` runs in CI purely as a progress signal.
   The WSL2 workaround has *zero* verification evidence in this repo — no test, no CI job, no doc —
   so it is a guess, not a supported path.
3. **Nothing can be deleted through a supported path.** The curator's op vocabulary is closed and
   contains no DELETE (`CREATE_THEME`, `APPEND_DAILY`, `MERGE_INTO_THEME`, `MARK_CONTESTED`, `DROP`,
   `NOOP` — and `DROP` discards an inbox event during planning, it does not remove curated content),
   and the inbox is append-only by invariant. Once something is curated into `wiki/`, retracting it
   means editing git by hand. Retention and right-to-delete are an unwritten ADR (#42).
   **Do not capture secrets, credentials, or other people's personal data.**
4. **Backup covers `wiki/`, not `_kb/` — and is push-only.** `agora repo init` writes a `.gitignore`
   containing `_kb/`, and `agora sync` (#64) pushes git history, so the *entire* operational spool
   is outside the backup: the uncurated inbox, harvest cursors, gold packs, `_kb/failed/`,
   `_kb/processed/`, curator state, and your `_kb/repo.yaml` policy. Only two of those are
   rebuildable by command (`_kb/index/`, `_kb/gold/`) — the rest are not, so anything captured and
   not yet curated, and anything sitting terminal in `_kb/failed/`, is not backed up (the full table
   is [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) §1). Sync also never pulls or merges: the
   remote is a backup target, never a second writer (ADR-0002), and two machines writing the same KB
   is unsupported (#46). Widening the backup contract is a separate decision, not a beta patch.
5. **You must supply a brain, and the out-of-box default assumes a local Ollama.** `repo init`
   writes a single backend (`qwen` → the `agora-ollama-brain` script), so a fresh install needs
   Ollama running with at least one model pulled. The name is a preference, not a requirement: the
   adapter picks the first Qwen it finds and otherwise falls back to whatever else is installed
   rather than stopping — so an unrelated model can end up curating your KB without saying so. The brain *is* pluggable — `agora-cli-brain`
   (ADR-0016) drives any headless CLI agent and `adapters.yaml` routes `plan`/`author` independently
   (ADR-0015) — but a hosted CLI agent sends KB content off your machine, which must stay an
   explicit operator decision and never a default (invariant 4). Run `agora doctor` first: it probes
   the configured brain and prints a copy-pasteable fix.
6. **A capture is not queryable until a curator run publishes it.** There is no read-your-own-write
   overlay (ADR-0033 is reserved and unauthored), so "I just saved it and search can't find it" is
   expected behavior, not a bug. Consistency is eventual, at the resolution of your trigger schedule.
7. **Failure recovery is supported but manual.** With the default `watch --interval 60`, a threshold
   trigger that fires every tick, and `max_attempts=3`, a brain that stops answering moves captures
   *terminally* into `_kb/failed/` in roughly three minutes. Nothing recovers them for you: the
   failure output prints `agora requeue --run <id>`, and you run it.
8. **The sandbox confines writes and network, not reads.** ADR-0013 confinement (macOS Seatbelt /
   Linux bwrap) wraps the *authoring* subprocess only; the default Ollama brain does inference
   outside it by design. On both platforms that subprocess can read the whole filesystem — it cannot
   write outside its temporary worktree and has no network, so a read alone cannot leave the
   machine, but read-hardening is a follow-up (#122), not shipped. The compensating control
   ADR-0013 promises for `allow_reduced_isolation` (forced review mode) is not implemented (#91).
   The real last line of defense is the deterministic FINAL-DIFF gate, not the kernel.
9. **No schema migration command.** `agora repo upgrade` is #63, still open, and `schema_version` is
   read but never compared against a supported range (#98). A repo initialized by this beta is
   expected to keep working — the format is what is stable, not `main` — but if the schema moves
   there is no command to move a repo with it.
10. **No embeddings and no semantic search.** Deterministic BM25F ranking is the contract
    (ADR-0009/0012), not a gap waiting to be filled; ADR-0032 is reserved and evidence-triggered.
    If you want vector recall, Agora is the wrong tool today.
11. **No contributor process yet.** `CONTRIBUTING.md` and `CODE_OF_CONDUCT.md` are deliberately
    post-beta; the beta's audience is dogfooders and evaluators, not contributors.

<!-- No version-compare link definitions here on purpose: Keep a Changelog's footer links point at
     git tags, and this repo has none yet (see "Release status"). They get added with the first tag,
     not before — a link to a ref that does not resolve is worse than no link. -->
