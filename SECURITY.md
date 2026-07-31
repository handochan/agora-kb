# Security Policy

<!-- Placement: repo root. GitHub recognizes SECURITY.md in the root, `.github/`, or `docs/`. The
     root is chosen because it is the copy a reader sees without opening a directory, and
     `.github/` currently holds only the CI workflow (#100). -->

> **Do not report a security vulnerability in a public issue, a pull request, or a discussion.**
> This repository is public (`private: false`) and its issue tracker has no private mode — a public
> issue is a disclosure, not a report.

This document says how to report a vulnerability, what Agora defends against today, and — just as
importantly — what it does **not** defend against, so a reporter can tell a *bug* from a
*documented design limit* before spending time on it. Every claim below carries a file, line, ADR,
or issue reference that was checked against the tree at commit `a8906bf`.

---

## 1. Reporting a vulnerability

Use GitHub's **private vulnerability reporting**. It opens a draft security advisory visible only
to you and the maintainer:

**https://github.com/handochan/agora-kb/security/advisories/new**

(also reachable from the repository's **Security** tab → *Report a vulnerability*)

There is no email fallback. A single private channel is deliberate: it keeps the report, the fix,
and the eventual advisory in one thread, and it avoids publishing a mailbox address on a repo
maintained by one person.

### Identify the build you tested

There is no git tag and no published artifact — `git tag` is empty and `v0.1.0b1` has **not** been
cut ([`CHANGELOG.md`](CHANGELOG.md) → "Release status"). So a report must name a **commit**, not a
release:

```bash
uv run agora --version    # → agora 0.1.0b1   (src/agora_kb/__init__.py:24, the one version source)
git rev-parse HEAD        # → the exact build you tested
```

### What to include

- The two lines above (version string + commit sha).
- OS and architecture — `sw_vers` on macOS, `uname -a` on Linux.
- Which faces were running: `agora serve` (MCP, stdio), `agora web`, `agora watch`, `agora harvest`.
- Non-default config, verbatim: the `web:`, `curator:`, and `harvest:` blocks of `_kb/repo.yaml`
  and any `adapters.yaml` `routing:` — **with secrets removed** (see the deletion limit in §5).
- Reproduction steps and what you observed versus what you expected.
- Your assessment of impact, in the vocabulary of the threat model in §3 if it fits one of the axes.

Please do **not** attach a copy of a real knowledge repo. Nothing written into a KB can be deleted
through a supported path — the curator's op vocabulary is closed and contains no DELETE
(`src/agora_kb/curator/plan.py:58-60`) and the inbox is append-only by invariant — so a repro
fixture built from scratch is both safer and more useful.

### Response targets

These are what one part-time maintainer can hold. They are commitments about *communication*, not
about fix time.

| Stage | Target |
|---|---|
| Acknowledgement that the report arrived | within **7 days** |
| First assessment — is it a vulnerability, which axis, rough severity | within **14 days** |
| Status update while the report stays open | at least every **30 days** |
| Default coordinated-disclosure window | **90 days** from acknowledgement, extendable by agreement |

- **No bug bounty.** There is no money, and there is no swag.
- **No CVE promise.** A CVE can be requested through the GitHub advisory when a report warrants it;
  this is not guaranteed in advance.
- **If 14 days pass with no acknowledgement**, it is fine to nudge by opening a public issue whose
  entire body is `I filed a private advisory on YYYY-MM-DD and have had no reply.` — no technical
  detail, no hint of the class of bug.

---

## 2. Supported scope

| Scope | Status | Basis |
|---|---|---|
| `main`, version `0.1.0b1` | **Supported** — the only thing there is | No tag exists; installing means installing from `main`, which moves ([`CHANGELOG.md`](CHANGELOG.md) → "Release status") |
| Any tagged release, wheel, or PyPI artifact | **Does not exist** | `git tag` is empty; PyPI name reservation is [#102](https://github.com/handochan/agora-kb/issues/102), open |
| Older commits on `main` | **Not supported** | Pre-release; there is no branch to backport to. Re-test on current `main` before reporting |
| macOS (Seatbelt sandbox) | **Supported** | `src/agora_kb/curator/isolation/seatbelt.py`; selected at `src/agora_kb/curator/isolation/__init__.py:246` |
| Linux (bubblewrap sandbox) | **Supported** | `src/agora_kb/curator/isolation/bwrap.py`; ADR-0013 appendix (2026-07-27, [#115](https://github.com/handochan/agora-kb/issues/115)) |
| Native Windows | **Not supported — cannot run** | `src/agora_kb/curator/claim.py:30` imports `fcntl` unconditionally, on the path of every command. Port is epic [#85](https://github.com/handochan/agora-kb/issues/85); `windows-latest` is `continue-on-error` in CI (`.github/workflows/ci.yml:33`) |
| Optional extras `web` / `ingest` / `metrics` | **Supported when installed** | `pyproject.toml:24-41`; lazily imported, so a core-only install has no such surface |
| Third-party parsers (`trafilatura`, `pdfminer.six`, `markitdown`) | **Routed upstream** | A defect *inside* a parser belongs to that project. How Agora *invokes* it — what it feeds, what caps it applies, what it does with the output — is in scope here |
| Python < 3.12 | **Not supported** | `pyproject.toml:11` (`requires-python = ">=3.12"`) |

> **This table gets rewritten at the first tag.** Once `v0.1.0b1` is cut the top row becomes a
> version range instead of "`main` only". That rewrite is part of cutting the tag; it is not
> separately tracked today.

---

## 3. Threat model

Four axes. Each states the defense that is actually implemented and the limit that is actually
there. **Nothing below describes a planned defense.**

### (a) The web face is unauthenticated

**There is no login, no token, and no role.** Anyone who can reach the process is a full-rights
operator. `agora web` binds `127.0.0.1` by default (`src/agora_kb/cli.py:341`) and that network
boundary is the *only* boundary. Authentication is Phase 4; ADR-0036
([`docs/adr/0036-authn-authz.md`](docs/adr/0036-authn-authz.md)) is **Proposed**, not implemented.
Remote MCP transport is deliberately coupled to that decision — the MCP face is stdio only
(`src/agora_kb/faces/mcp_server.py:1162`).

A loopback bind is a *network* boundary, and a browser walks straight through it. So the face
defends the browser-mediated paths ([#94](https://github.com/handochan/agora-kb/issues/94),
**closed**; ADR-0025 appendix; `src/agora_kb/faces/web/app.py:39-49`):

| Defense | Where | Default |
|---|---|---|
| **Host allowlist** — rejects a `Host` outside the list with 400, closing DNS rebinding | `_HostAllowlistMiddleware`, `src/agora_kb/faces/web/app.py:314` | `["localhost", "127.0.0.1"]` (`src/agora_kb/config.py:754`) |
| **Origin/Referer guard** — a state-changing request whose stated authority is not this deployment's own is refused with 403 *before* anything reaches the inbox | `_OriginGuardMiddleware`, `src/agora_kb/faces/web/app.py:243` | on, **for present-and-mismatched headers only** — a request with no `Origin`/`Referer` still passes (`require_origin` defaults `false`) |
| **Framing denial** — `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'`, so a clickjacked UI cannot borrow the user's same-origin position | `_SecurityHeadersMiddleware`, `src/agora_kb/faces/web/app.py:397-398` | on |

**Known limits.**

- These are defense in depth on top of an unauthenticated premise. They are **not** authentication.
- A request with **no** `Origin` and no `Referer` — `curl`, CI, a scripted client — still passes the
  Origin guard. Closing that costs you those clients and is opt-in:
  `web.security.require_origin: true` (`src/agora_kb/config.py:791`, default `False`).
- An explicit `allowed_hosts` list **replaces** the loopback default rather than extending it
  (`src/agora_kb/config.py:960-973`). Behind a reverse proxy the client `Host` must be passed
  through verbatim and the public hostname added, or every request 400s.
- `web.identity.trusted_header` ([#67](https://github.com/handochan/agora-kb/issues/67)) threads a
  *provenance label* (`web:<user>`), not a permission. It separates writers in the log; it isolates
  nothing and enforces nothing (`src/agora_kb/faces/web/app.py:30-37`).
- Exposing the face beyond loopback without an authenticating reverse proxy is outside the
  supported envelope — see [`docs/DEPLOY-TEAM.md`](docs/DEPLOY-TEAM.md) (written in **Korean**),
  where **the proxy**, not Agora, is the security boundary.
- There is no TLS, no rate limit, and no CAPTCHA anywhere in the face.

### (b) Arbitrary files and URLs reach third-party parsers

The upload surface takes files, URLs, and text from whoever can reach it and hands them to
extractors (`src/agora_kb/faces/web/app.py:404` `build_app`). What is implemented:

| Defense | Where |
|---|---|
| **SSRF guard on server-side URL fetches** — non-`http(s)` schemes refused; every resolved address checked, rejecting loopback, RFC1918 private, link-local (including `169.254.169.254`), IPv6 unique-local and unspecified; **re-validated on every redirect hop**, max 5 hops | `_fetch_url_guarded`, `src/agora_kb/ingest/extractors/url.py:99-150`; classification at `:219-225` ([#66](https://github.com/handochan/agora-kb/issues/66)) |
| **URL fetch kill switch** — server-side fetching can be turned off entirely | `web.upload.url_enabled`, `src/agora_kb/config.py:681` |
| **Response body cap** — 25 MiB per fetch | `_MAX_FETCH_BYTES`, `src/agora_kb/ingest/extractors/url.py:40` |
| **Upload size cap** — 25 MiB per file, before extraction | `MAX_UPLOAD_BYTES`, `src/agora_kb/faces/web/app.py:88` |
| **Zip decompression-bomb cap** — two layers: a declared-total pre-check that rejects honest bombs without decompressing, then a streamed pass that measures the *actual* decompressed length and aborts mid-entry. Default 250 MiB | `_guard_zip_bomb`, `src/agora_kb/ingest/extractors/office.py:88-101`; default at `:77`; operator override `web.upload.max_uncompressed_bytes` (`src/agora_kb/config.py:680`) ([#53](https://github.com/handochan/agora-kb/issues/53)) |
| **Closed format dispatch** — anything the router cannot place raises rather than being guessed at; contradictory MIME/extension pairs are refused as ambiguous | `src/agora_kb/ingest/extractors/base.py:200-258` |
| **Path containment on imports** — a `sources:` entry like `raw/../../etc/x` looks like a `raw/` reference but escapes the destination; both ends of every copy run through the same containment predicate | `_contained_raw_ref`, `src/agora_kb/ingest/vault_import.py:636`, enforced at `:707` and `:763` ([#108](https://github.com/handochan/agora-kb/issues/108), closed) |

**Known limits.**

- **A defect inside `trafilatura`, `pdfminer.six`, or `markitdown` is routed upstream.** Agora pins
  them (`pyproject.toml:32-38`) and bounds what they are fed; it does not audit their parsing.
  Report *those* to *them*. Report to us if Agora's invocation is what makes them exploitable.
- The **face-level extension allowlist is opt-in and off by default**: `web.extensions.allowed`
  defaults to `None`, which means "use the extractor's built-in supported set" rather than an
  operator-narrowed list (`src/agora_kb/config.py:694`, gate at
  `src/agora_kb/faces/web/app.py:1107`). The built-in set is itself closed, but it is wider than
  most deployments need.
- The SSRF guard runs on the *server-side fetch* path. `allow_private=True` deliberately bypasses it
  for a local CLI caller (`src/agora_kb/ingest/extractors/url.py:44`).
- Original binaries are not preserved; only extracted markdown lands in the repo
  ([#48](https://github.com/handochan/agora-kb/issues/48), open).

### (c) The curator runs an unattended subprocess over untrusted text

The curator invokes a local model or a headless CLI agent on a schedule, with no human in the loop,
and its input is captured text nobody vetted. It is assumed adversarial (ADR-0013 Context). Three
layers, and **the second one is the real boundary** — the kernel is defense in depth, not the
guarantee:

1. **OS sandbox (ADR-0013)** — [`docs/adr/0013-curator-sandbox-mechanism.md`](docs/adr/0013-curator-sandbox-mechanism.md),
   implemented in `src/agora_kb/curator/isolation/` (`seatbelt.py` on macOS, `bwrap.py` on Linux).
   Writes are confined to a throwaway worktree; outbound network is denied. Selection is
   **fail-closed**: with no usable kernel sandbox, `select_backend_isolation` raises
   `SandboxUnavailable` rather than running a `sandbox: strict` backend unconfined
   (`src/agora_kb/curator/isolation/__init__.py:239-244`).

   > **Know the scope before you rely on this layer.** A backend is wrapped only when its spec
   > declares **`network: none`** (`src/agora_kb/curator/subprocess_backend.py:371`). The
   > `adapters.yaml` that `agora repo init` emits declares `network: loopback`
   > (`src/agora_kb/config.py:323`), because the Ollama shim couples inference and file-writing and
   > needs loopback to reach the daemon. **In the shipped default configuration neither curator pass
   > runs inside the kernel sandbox**, and layer 2 below is the entire boundary. The same is true of
   > the documented CLI-agent brain (also `network: loopback`). This layer becomes load-bearing when
   > an operator configures a `network: none` backend — which is also the only configuration whose
   > absence of a sandbox makes `agora curate` refuse to run.

   The *mechanism* is **proven at runtime, not assumed** — though read the doctor line as "this
   host's sandbox works", not "your brain is confined" (see the scope note above). `agora doctor`
   runs the ADR-0013 self-test against a throwaway worktree — write-inside must succeed,
   write-outside must be refused with a mechanism-specific errno, and the network deny is only
   accepted against a *reachable* target so "blocked" cannot silently mean "nothing was listening"
   (`src/agora_kb/curator/isolation/selftest.py:1-30`, printed by `_doctor_sandbox`,
   `src/agora_kb/cli.py:2376-2425`):

   ```bash
   uv run agora doctor --repo /ABSOLUTE/PATH/TO/knowledge-repo
   ```

   ```
     sandbox: seatbelt (ok)
       write-inside=True write-outside-denied=True apple-shim=True
       network-denied=True
   ```

   A sandbox whose self-test **fails** makes `agora doctor` exit non-zero — a confinement that lies
   is treated as worse than none (`src/agora_kb/cli.py:2382-2383`; the verdict is folded in at
   `src/agora_kb/cli.py:1753` and returned at `:1791`).

2. **The deterministic FINAL-DIFF gate (ADR-0008 step 4)** — `_assert_final_diff_allowlisted`,
   called at `src/agora_kb/curator/worker.py:773`, implemented at `:1528`. Everything the
   subprocess touched is staged and the diff must consist **only** of canonical-allowlist paths. Any
   off-allowlist file, any introduced or modified symlink, any `..` component, any tracked change
   under the scratch dir, any mutation of a schema symlink → the whole worktree is discarded, no
   commit-and-swap happens, and the run fails. `_kb/`, `_meta/`, `_templates/`, git internals, and
   hooks stay rejected. This gate is model-free: it does not care what the brain intended.

3. **Event-id validation at both trust re-entry points** — an inbox event id is interpolated
   straight into a destination path, so an unvalidated `id: ../../../wiki/PWNED` in hand-placed
   frontmatter escapes into the git-tracked read model that only the curator may write. Both places
   where a file on disk becomes an input are guarded with the same canonical-format check
   (`is_valid_event_id`, `src/agora_kb/core/ids.py:48-50`): the curator's claim scan
   (`src/agora_kb/curator/claim.py:183-191`) and the `agora requeue` inbox-return verdict
   (`src/agora_kb/core/inbox.py:289-295`), which matters because `_kb/failed/` is an
   operator-editable directory ([#124](https://github.com/handochan/agora-kb/issues/124), closed).
   A malformed id is skipped, not repaired — the event stays where it is and stays counted.
   The same traversal guard covers derived cache addressing
   (`index_notes_path`, `src/agora_kb/core/layout.py:239-259`) and harvest cursor paths
   (`src/agora_kb/core/layout.py:235-236`).

**Known limits.**

- **The sandbox confines writes and network, not reads.** On both platforms the authoring
  subprocess can read the whole filesystem. It cannot write outside its temporary worktree and has
  no network, so a read alone cannot leave the machine — but read-hardening is
  [#122](https://github.com/handochan/agora-kb/issues/122), **open and not shipped**.
- **In the default configuration the sandbox wraps neither pass.** A strict no-network profile also
  blocks loopback to the host daemon, so the default Ollama brain declares `network: loopback` and
  does *both* inference and authoring outside the sandbox by design (ADR-0013 Context;
  `src/agora_kb/curator/subprocess_backend.py:257-262`). Confinement of the authoring subprocess is
  what you get when you configure a `network: none` backend, and PASS-1 is never confined on any
  path. Out of the box, layer 2 is the boundary.
- `curator.allow_reduced_isolation: true` is an **explicit operator opt-in that abandons kernel
  isolation**: network egress and out-of-worktree writes are then *not* prevented
  (`src/agora_kb/config.py:129-133`). The compensating control ADR-0013 promises for that mode —
  forced review-mode — is **not implemented**; it is
  [#91](https://github.com/handochan/agora-kb/issues/91), open. In that configuration the FINAL-DIFF
  gate is the *only* thing standing between an adversarial brain and your repo.
- On Linux, `bwrap` currently drops `spec.env` wholesale via `--clearenv`, diverging from the
  adapter contract and from Seatbelt —
  [#118](https://github.com/handochan/agora-kb/issues/118), open.
- Routing `plan`/`author` to a **hosted** CLI agent (ADR-0015/0016) is supported and **sends KB
  content off your machine**. That is an operator decision, never a default (invariant 4).

### (d) Indirect prompt injection — a stated non-goal

A knowledge base is text that agents read. Text that arrives from a harvested memory file, a
session transcript, a web page, or an uploaded document can contain instructions aimed at whatever
model reads it next. Agora ships two mechanisms here, and **neither is a general injection
defense**:

- **The outbound sentinel + loop-break contract** (ADR-0027 §8, normative —
  `docs/adr/0027-gold-context-packs.md:157-160`).
  Every Agora→agent emission is wrapped in an `<!-- agora:pack … -->` span, and every consumer that
  reads agent memory back in drops those spans whole
  (`src/agora_kb/core/sentinel.py:59` / `:71`). Gold packs additionally exclude harvest-origin notes
  by default (`_is_harvest_provenance`, `src/agora_kb/core/gold.py:341`), so a pack cannot become an
  injection amplifier. **This closes the *verbatim* half of a KB→agent-memory→KB loop. It is a
  loop break, not a content filter.**
- **Connector-boundary redaction (ADR-0023 decision 5).** `src/agora_kb/core/redact.py` is a
  deterministic, model-free secret/PII scrubber applied *before* a harvested fact is persisted —
  because the inbox is immutable and cannot be scrubbed afterwards. It runs at exactly two call
  sites, both inside the harvester (`src/agora_kb/harvester/harvester.py:287`,
  `src/agora_kb/harvester/session_connector.py:238`). **Redaction exists only at the harvest
  boundary** ([`docs/DEPLOY-TEAM.md`](docs/DEPLOY-TEAM.md) §5): `kb_remember` and web upload are
  unfiltered write paths.

The harvester also gates what may enter at all: harvested facts land as `kind=candidate`,
`confidence=low` and a **fail-closed scope gate** requires `connector scope == harvest.scope_lock ==
repo kind`, treating an undeclared repo kind as `team`, so a personal source can never bleed into a
team repo (`check_scope`, `src/agora_kb/harvester/harvester.py:69-96`). Harvest is off by default —
a missing `harvest:` block yields `enabled=False` (`src/agora_kb/config.py:402`).

**Known limits — stated as design boundaries, not as bugs.**

- **A curated note can carry injected text out through `kb_query`, `kb_read`, `kb_context`, or a
  gold pack.** Agora does not classify, sanitize, or neutralize instruction-shaped prose on the read
  path. If you capture a document that says "ignore your previous instructions", a downstream agent
  can read that sentence back verbatim. **No issue tracks this, because it is not filed as a
  defect** — it is the documented boundary of the two mechanisms above. The reworded
  KB→memory→KB round trip is likewise explicitly *not claimed closed*
  (`docs/adr/0017-harvester-file-connector-mechanics.md:59-65`).
- The transcript-connector hardening is scoped to *impersonating engine structure* — sentinel strip,
  size caps, and per-turn role flattening so an embedded "system" turn cannot fake engine structure
  in the bundle the planning brain reads (`docs/adr/0023-context-harvester-connectors.md:143-147`).
  It does not judge content.
- Redaction is one-way and pattern-based. It has no reverse map by construction
  (`src/agora_kb/core/redact.py:13-17`), and a secret that matches no pattern passes through.
- `mail:` / `chat:` connectors are **prohibited** until the retention / right-to-delete ADR is
  accepted ([#42](https://github.com/handochan/agora-kb/issues/42), open;
  [`docs/DEPLOY-TEAM.md`](docs/DEPLOY-TEAM.md) §5).

---

## 4. Explicitly out of scope

A report about any of the following is closed with a pointer back to this section. These are
documented properties of the current design, not defects.

1. **Operator-widened network exposure.** Binding `agora web` to a non-loopback address, or putting
   it on a LAN/tunnel/public address without an authenticating reverse proxy in front. The default
   is `127.0.0.1` (`src/agora_kb/cli.py:341`) and the supported team topology puts the proxy in the
   boundary role ([`docs/DEPLOY-TEAM.md`](docs/DEPLOY-TEAM.md) §2). "No auth on the web face" is
   §3(a), a stated property, not a vulnerability report.
2. **Isolation bypass under `curator.allow_reduced_isolation: true`.** That flag is the documented
   opt-in that abandons kernel confinement (`src/agora_kb/config.py:129-133`), and the raise it
   suppresses says so verbatim (`src/agora_kb/curator/isolation/__init__.py:239-244`). A bypass
   *with the flag at its default `False`* **is** in scope — report it.
3. **Forged `X-Remote-User` where the proxy does not set/strip it.** `web.identity.trusted_header`
   is opt-in and its entire contract is "the proxy owns this header"
   ([#67](https://github.com/handochan/agora-kb/issues/67);
   `src/agora_kb/faces/web/app.py:30-37`). A deployment that forwards a client-supplied value has
   declared a trust boundary it does not enforce. A forgery accepted with `trusted_header` **unset**
   would be in scope.
4. **Steering your own agent with content you put in your own KB.** Single-operator self-injection
   is the non-goal in §3(d), not a vulnerability.
5. **Anything on native Windows.** No `agora` command runs there at all
   (`src/agora_kb/curator/claim.py:30`); the port is [#85](https://github.com/handochan/agora-kb/issues/85).
6. **Vulnerabilities inside a pinned third-party parser.** Route to that project; see §3(b).
7. **Missing repository hardening settings** — secret scanning, push protection, Dependabot,
   branch protection. Worth enabling, but they are repository administration, not a code
   vulnerability. Open a normal issue.
8. **Absence of a not-yet-built feature**, including: authentication and authorization (ADR-0036,
   Proposed, Phase 4), remote MCP transport, deletion / right-to-delete
   ([#42](https://github.com/handochan/agora-kb/issues/42)), schema migration
   ([#63](https://github.com/handochan/agora-kb/issues/63)), and multi-machine sync
   ([#46](https://github.com/handochan/agora-kb/issues/46)). The full list a beta user must not
   assume lives in [`docs/ROADMAP.md`](docs/ROADMAP.md) → "Not in 0.1.0-beta" and in
   [`CHANGELOG.md`](CHANGELOG.md) → "Known limitations".
9. **Denial of service against your own single-user localhost process** — filling your disk with
   uploads, spinning the curator, or exhausting your own model. There is no rate limit and none is
   claimed.
10. **Reports produced by an automated scanner with no verified impact on Agora**, and reports
    against a commit older than current `main`.

---

## 5. Operator rules that carry real security weight

Not a policy section — the three things that most often turn a supported configuration into an
unsupported one.

- **Do not capture secrets, credentials, or other people's personal data.** Nothing written can be
  retracted through a supported path (`src/agora_kb/curator/plan.py:58-60`; inbox append-only,
  invariant 3). A secret pasted through `kb_remember` or the web upload is unfiltered (§3(d)) and
  becomes permanent git history — replicated to every clone and every backup push.
- **Run `agora doctor` before you trust a deployment.** It exercises the sandbox mechanism rather
  than assuming it, and it exits non-zero when the host is unhealthy (§3(c)). Read its brain line
  carefully: with a `--model` pin in the `adapters.yaml` argv the verdict never contacts the daemon
  (`src/agora_kb/cli.py:2150-2156`), so green proves presence, not function.
- **Keep the web face on loopback unless an authenticating proxy is in front of it.** That is the
  entire boundary (§3(a)).

The operator-facing expansion of the first rule — what is and is not backed up, what `DROP` really
does, and how terminal failures come back — is
[`docs/LIMITATIONS.md`](docs/LIMITATIONS.md), the data-safety contract. This document is the
attacker's-eye view; that one is the owner's.

---

## 6. Disclosure

When a report is resolved, the fix lands on `main` and a GitHub security advisory is published from
the same draft the report was filed in, crediting the reporter unless they ask otherwise. Because
there is no tagged release yet (§2), an advisory's "patched version" reads as a **commit**, not a
version number, until `v0.1.0b1` is cut.
