# Getting started — from an empty machine to your first curated note

The onboarding path for `agora 0.1.0b1`: what has to exist before Agora can do anything, how to
install it so the pieces you need are actually present, and a five-step run that ends with a wiki
note the curator wrote and a query that cites it.

Every command below was run on the host described in [§1.3](#13-the-measuring-host) against commit
`a8906bf`, and every block of output is pasted from that run — nothing here is illustrative. Two
edits are made to pasted output, and nowhere else: the absolute path of the source checkout is
rewritten to `/ABSOLUTE/PATH/TO/agora-kb`, and where lines are cut from the middle of a block the
cut is marked `…`. Every factual claim carries a `file:line`, an ADR, or an issue number.

**Every measured number and every pasted output in this document is macOS.** The mechanisms are the
same on Linux; the one place the two platforms genuinely differ is the curator sandbox, which
[§1.1](#11-python-uv-git) covers.

Read this in the order it is written, with one exception the text repeats where it matters:
[§3](#3-pin-the-model-path-a-only) writes into a repo that [§4 step 1](#step-1--create-the-repo)
creates, so run §4 step 1 before you paste anything from §3. The single most common way to fail at
step 4 is to skip [§1.2](#12-a-curator-brain-pick-one).

| Doc | Covers |
|---|---|
| [`LIMITATIONS.md`](LIMITATIONS.md) | **Read before you put anything you cannot re-create into a repo.** What can disappear, and what is not backed up |
| [`../SECURITY.md`](../SECURITY.md) | Threat model, supported scope, and how to report a hole privately. Relevant here for [§1.2 Path B](#path-b--a-headless-cli-agent-no-model-download-adr-0016) (a hosted brain) and for the web face |
| [`../CHANGELOG.md`](../CHANGELOG.md) | What is in `0.1.0b1`, and what still gates the tag |
| [`../deploy/README.md`](../deploy/README.md) | Running `watch` / `web` / `harvest` unattended (launchd, systemd), and the SSOT for the terminal-failure recovery procedure |
| [`DEPLOY-TEAM.md`](DEPLOY-TEAM.md) | Sharing one KB with 2–10 people (hub topology, proxy auth) — **written in Korean** |
| [`DESIGN.md`](DESIGN.md) | Why any of this is shaped the way it is |

## 0. Before you start

**There is no release artifact.** `v0.1.0b1` is not tagged, so `git checkout v0.1.0b1` does not
resolve; the PyPI name is not reserved either — that is still an open release gate
([#102](https://github.com/handochan/agora-kb/issues/102), `CHANGELOG.md:36`) — so do not expect
`pip install agora-kb` to install this project. You install from `main`, which moves
(`CHANGELOG.md`, "Release status"). Placeholders in this document use the `deploy/README.md` token
convention: `/ABSOLUTE/PATH/TO/agora-kb` is your source checkout.

**macOS and Linux only.** On native Windows not even `agora --help` runs: `curator/claim.py`
imports `fcntl` unconditionally and that import is on the path of every command
(`CHANGELOG.md:176-180`). Native Windows is epic
[#85](https://github.com/handochan/agora-kb/issues/85); packaging and platform docs for it are
[#92](https://github.com/handochan/agora-kb/issues/92). The WSL2 workaround has no verification
evidence in this repo, so it is not documented here as a path.

## 1. Prerequisites

### 1.1 Python, uv, git

```bash
python3 --version     # need >= 3.12
uv --version
git --version
```

Observed on the measuring host:

```
Python 3.12.13
uv 0.11.19 (Homebrew 2026-06-03 aarch64-apple-darwin)
git version 2.50.1 (Apple Git-155)
```

`git` is not optional: the curated markdown *is* the source of truth and every curator run ends in a
commit (ADR-0001), so `agora doctor` fails the health verdict without it (`cli.py:1719-1723`).

**On Linux, also install bubblewrap.**

```bash
sudo apt-get install -y bubblewrap      # Debian/Ubuntu; `bwrap` is the binary
```

This is the Linux half of the ADR-0013 curator sandbox (`curator/isolation/bwrap.py`; macOS uses
the built-in `sandbox-exec`, `curator/isolation/seatbelt.py`). Be precise about what it does and
does not gate, because the two are easy to confuse:

- **The default configuration does not require it.** A backend is confined only when its spec
  declares `network: none` (`curator/subprocess_backend.py:371`), and both brains this document
  configures declare `network: loopback`. Without bubblewrap, `agora doctor` prints
  `sandbox: unavailable — fail-closed for network:none backends (…)` and **still returns
  `status: healthy`** (`cli.py:2391-2393`) — curation runs.
- **It is required the moment you configure a `network: none` backend.** Selection is fail-closed:
  with no usable kernel sandbox `select_backend_isolation` raises `SandboxUnavailable` and
  `agora curate` refuses rather than running unconfined (`curator/isolation/__init__.py:239-244`).
- On Ubuntu 24.04+ installing the package is **necessary but not sufficient**: the kernel ships
  `kernel.apparmor_restrict_unprivileged_userns=1`, which fails bubblewrap's user-namespace probe
  anyway. Check with the same probe CI uses (`.github/workflows/ci.yml:74-80`):

  ```bash
  bwrap --unshare-all --die-with-parent --ro-bind / / --proc /proc --dev /dev -- true \
    && echo "bwrap userns probe: OK"
  ```

  If it fails, the documented remedy is an AppArmor profile granting userns to `/usr/bin/bwrap`, or
  running the curator inside a container that permits userns (`USERNS_REMEDIATION`,
  `curator/isolation/bwrap.py:179-185`). Relaxing the sysctl host-wide is what CI does because its
  runners are ephemeral and single-tenant; it is not a recommendation for a real machine.

The macOS `sandbox: seatbelt (ok)` block pasted in [§4 step 2](#step-2--health-check) reads
`sandbox: bwrap (ok)` on a Linux host with a working bubblewrap.

### 1.2 A curator brain (pick one)

`agora curate` does not think for itself. It asks an LLM what to file where, and that LLM is a
**required runtime dependency** — not an optional enhancement. `agora repo init` writes an
`adapters.yaml` whose only backend shells the `agora-ollama-brain` console script
(`config.py:288-330`) and a `_kb/repo.yaml` with `curator.backend: qwen` (`config.py:96`), so out of
the box Agora expects a local Ollama daemon with a Qwen-family model.

That default is a *default*, not a requirement (invariant 6). There are two supported paths. Pick
one now.

#### Path A — a local model through Ollama

Zero API cost, fully offline, and the family this repo has actually been run against (ADR-0005,
`docs/INGEST-CONTRACT.md` §8). It costs disk and RAM; see the measured table below.

```bash
# 1. install Ollama — https://ollama.com/download  (macOS: `brew install ollama`)
ollama --version

# 2. the daemon must be listening on the loopback endpoint the shim defaults to
#    (`adapters/ollama_brain.py:79`); start it with `ollama serve` if this fails
curl -fsS http://localhost:11434/api/tags >/dev/null && echo "ollama daemon: up"

# 3. pull the model this repo is verified against
ollama pull qwen3.6:35b-a3b

# 4. confirm it is installed
ollama list
```

Observed:

```
ollama version is 0.32.5
ollama daemon: up
NAME                     ID              SIZE     MODIFIED
qwen3.6-hermes:latest    42dc987f1c6e    23 GB    7 weeks ago
qwen3.6:35b-a3b          07d35212591f    23 GB    7 weeks ago
```

**Measured cost of the model.** Disk is the `SIZE` column of `ollama list`. Resident memory is the
`SIZE` column of `ollama ps` taken immediately after a one-token generation
(`ollama run <tag> "Reply with exactly: ok"`), which is the loaded footprint Ollama reports while
the model is actually resident — not an estimate, and not a number taken from anywhere else.

| Model tag | On disk | Resident while loaded | Context Ollama loaded it with | Processor |
|---|---|---|---|---|
| `qwen3.6:35b-a3b` | 23 GB | **29 GB** | 262144 | 100% GPU |
| `qwen3.6-hermes:latest` | 23 GB | **25 GB** | 131072 | 100% GPU |

Resident memory tracks the context window Ollama chooses, so the two rows differ by more than the
identical disk sizes suggest.

**Read the resident column as a hardware requirement, not as a statistic.** A 23 GB weight file has
to be *in memory* to run at usable speed. On a host with less RAM Ollama will load with a smaller
context, which lowers the resident figure somewhat — but it cannot shrink the weights, so below
roughly the resident figure above the model spills to CPU/swap and a run goes from minutes to tens
of minutes, or fails to load at all. On the 64 GB host in §1.3 it fits with room to spare; on a
16 GB laptop `qwen3.6:35b-a3b` is not a usable choice. You also need ~23 GB of free disk for the
pull itself.

This repository has verified exactly the two tags above and **no smaller one** — the only model tag
named anywhere in `src/` is `qwen3.6:35b-a3b` (`cli.py:1705`, `adapters/ollama_brain.py:174`). A smaller Qwen-family
model may well work; nobody here has measured it, so this document will not claim a floor it has not
tested. If your machine cannot hold this model, **Path B is the supported route**, and the sizing
note in [§3](#3-pin-the-model-path-a-only) applies to any small local model you do try.

A curation run on this host took **2m20s** wall clock for 3 captured facts with
`qwen3.6:35b-a3b` (§4 step 4).

#### Path B — a headless CLI agent, no model download (ADR-0016)

> **This path sends your knowledge base to a third party.** `claude`, `codex`, and `gemini` are
> hosted services. On **every** curator run the shim pipes the curator bundle — the facts you
> captured plus the wiki text they are being merged into — to that vendor's API. Path A is the only
> fully-offline option. That is an explicit operator decision and never a default
> (invariant 4; [`../SECURITY.md`](../SECURITY.md) §3(c);
> [`ROADMAP.md`](ROADMAP.md) → "Not in 0.1.0-beta" → *Cloud brains*). If your KB holds anything you
> would not paste into that vendor's web UI, use Path A.

If you already have `claude`, `codex`, or `gemini` on your PATH, you do not need Ollama at all.
`agora-cli-brain` drives any of them as a **pure text generator**: the shim reads the curator bundle
itself, feeds a prompt on stdin, and takes only the agent's stdout text
(`adapters/cli_agent_brain.py:86-113`). The agent runs in a throwaway scratch cwd, so it never
touches your repo's files — a filesystem guarantee, not a confidentiality one.

The exact invocations live in one place — `KNOWN_CLI_AGENTS`
(`adapters/cli_agent_brain.py:55-70`) — which is also what `agora doctor` reads when it prints a
remediation snippet. For a freshly initialized repo, replace `adapters.yaml` with:

```yaml
backends:
  claude: { argv: [agora-cli-brain, --, claude, -p], network: loopback }
default_backend: claude
```

and change `curator.backend` in `_kb/repo.yaml` from `qwen` to `claude`. The two other invocations
from the same table go under the same `backends:` key, with `default_backend` naming whichever one
you want:

```yaml
  codex:  { argv: [agora-cli-brain, --, codex, exec, --skip-git-repo-check, --sandbox, read-only], network: loopback }
  gemini: { argv: [agora-cli-brain, --, gemini, -p, ""], network: loopback }
```

`cwd`, `prompt`, `sandbox` and `timeout_s` are omitted on purpose — `BackendSpec` defaults them to
`{worktree}` / `stdin` / `strict` / none (`curator/backends.py:62-69`). `network: loopback` is
required, not decoration: the curator confines a backend inside the ADR-0013 sandbox **only** when
`network` is `none` (`curator/subprocess_backend.py:371`), and a sandboxed agent cannot reach its
own API.

Verified end to end on the measuring host — a repo wired exactly as above, one capture, one run.
These are **two** commands, so they are shown as two blocks; `agora doctor` never prints a
`status: published` line.

```bash
uv run agora doctor --repo /tmp/my-kb
```

```
…
  routing: plan=claude (network: loopback)  author=claude (network: loopback)
  brain claude: 'agora-cli-brain' on PATH (/ABSOLUTE/PATH/TO/agora-kb/.venv/bin/agora-cli-brain)
…
status: healthy
```

```bash
uv run agora curate --repo /tmp/my-kb --force
```

```
…
status: published
counts: CREATE_THEME=1, candidates=1, claimed=1, inbox_remaining=0, prose_pending=0, prose_regions=1
```

That run took **37s** and never contacted Ollama.

### 1.3 The measuring host

Every timing and memory number in this document was taken on:

| | |
|---|---|
| Machine | Apple M5 Pro, 64 GB unified memory |
| OS | macOS 26.4 (build 25E246), arm64 |
| Python | 3.12.13 |
| Ollama | 0.32.5 |
| Agora | `agora 0.1.0b1` at commit `a8906bf` |

## 2. Install

```bash
git clone https://github.com/handochan/agora-kb.git
cd agora-kb
```

**A bare `uv sync` installs neither `dev` nor `web`/`ingest`/`metrics` — and on an environment that
already has them, it *removes* them.** `pytest`, `ruff` and `mypy` live under
`[project.optional-dependencies] dev` (`pyproject.toml:42-48`), not in a `[dependency-groups]`
table, so they are extras like any other, and `uv sync` is an exact sync: it prunes anything the
requested extras do not name. Check without changing anything:

```bash
uv sync --dry-run
```

On an environment that had every extra installed, that printed:

```
Would uninstall 55 packages
 - fastapi==0.136.3
 ...
 - mypy==2.1.0
 - pytest==9.0.3
 - ruff==0.15.17
```

| Extra | What you lose without it | Install it when |
|---|---|---|
| *(none)* | — | Core API, the `agora` CLI, and the MCP face are all in the base install (`pyproject.toml:18-22`: `fastmcp`, `pyyaml`, `pydantic`) |
| `web` | `agora web` — the browse/search/upload UI, the JSON API, `/graph`, `/dashboard` (`pyproject.toml:25-31`) | You want the human-facing face, not just the agent one |
| `ingest` | URL / PDF / docx / xlsx / pptx upload extraction (`pyproject.toml:32-38`) | You will upload documents through the web face |
| `metrics` | `GET /metrics`, the Prometheus exporter (`pyproject.toml:39-41`) | You scrape the deployment |
| `dev` | `pytest`, `ruff`, `mypy` (`pyproject.toml:42-48`) | You will run the test suite or contribute |

One command that gets all of it:

```bash
uv sync --extra web --extra ingest --extra metrics --extra dev
```

(`uv sync --all-extras` is equivalent today — there are exactly four extras — and both resolve to
the same environment; verified with `--dry-run` on this host: *"Would make no changes"*.)

Verify:

```bash
uv run agora --version
```

```
agora 0.1.0b1
```

Every `agora` command in this document is run from inside the checkout as `uv run agora …`. From
anywhere else, use `uv run --directory /ABSOLUTE/PATH/TO/agora-kb agora …`.

## 3. Pin the model (Path A only)

> **Run [§4 step 1](#step-1--create-the-repo) first.** Everything in this section edits a repo that
> `agora repo init` has already created; pasted as-is into a shell before that, the heredoc below
> fails with `no such file or directory`. Read §3 now, apply it between §4 step 1 and §4 step 2 —
> step 2 sends you back here at the exact moment it matters.

`select_model` resolves the Ollama model in this order (`adapters/ollama_brain.py:160-181`):

1. an explicit `--model` in the `adapters.yaml` argv,
2. `$AGORA_OLLAMA_MODEL`,
3. the **first of `sorted(available)` whose name contains `qwen`**,
4. `sorted(available)[0]`.

Steps 3 and 4 happen **without any log line during a run**. On the measuring host, two Qwen models
are installed and step 3 picks `qwen3.6-hermes:latest` — the alphabetically first — even though
`qwen3.6:35b-a3b` is the tag §1.2 told you to pull. Nothing in `agora curate`'s output says which
model produced the wiki text.

`agora doctor` is where you see it (#96). Unpinned, on this host:

```
  brain qwen: ollama http://localhost:11434 reachable, 2 models, would use 'qwen3.6-hermes:latest'
```

Note what doctor does *not* do here: it prints a `WARNING … this is the alphabetical fallback` only
when **no** qwen-family model is installed at all (`cli.py:2193-2201`). With two qwen models it
simply names its pick and stays quiet, so read that line rather than the verdict word.

Pin it. On a freshly initialized repo (see §4 step 1), overwrite `adapters.yaml`:

```bash
cat > /tmp/my-kb/adapters.yaml <<'YAML'
backends:
  qwen:
    argv: [agora-ollama-brain, --model, qwen3.6:35b-a3b]
    cwd: '{worktree}'
    prompt: stdin
    sandbox: strict
    network: loopback
    timeout_s: 600
default_backend: qwen
YAML
```

On a repo whose `adapters.yaml` you have already customised, add the two tokens to the existing
`argv:` list instead — the emitted file is idempotent and `repo init` never rewrites it
(`config.py:308-311`), so a re-init will not undo your edit either.

Verify:

```bash
uv run agora doctor --repo /tmp/my-kb | grep "brain qwen"
```

```
  brain qwen: ollama http://localhost:11434, model pinned to 'qwen3.6:35b-a3b' by adapters.yaml argv (no /api/tags probe — the run lists no models either; reachability NOT checked)
```

### The trade-off the argv pin makes — read this before you choose

**Pinning in the argv turns off doctor's reachability probe.** Read the parenthetical in that line
literally: an explicit `--model` short-circuits `/api/tags` *entirely*, so doctor returns a passing
verdict without contacting the daemon at all (`cli.py:2150-2156`). Verified on this host with the
heredoc above applied and nothing listening:

```bash
AGORA_OLLAMA_HOST=http://localhost:1 uv run agora doctor --repo /tmp/my-kb
```

```
  brain qwen: ollama http://localhost:1, model pinned to 'qwen3.6:35b-a3b' by adapters.yaml argv (no /api/tags probe — the run lists no models either; reachability NOT checked)
status: healthy          # exit 0 — with the daemon completely down
```

That is deliberate, not a bug (#96): on this path "the daemon is up" is a fact the *run* never
establishes either, so claiming it would be a lie. But the consequence is yours to manage, and it is
the opposite of what [§4 step 2](#step-2--health-check) can otherwise do for you.

The two pin forms are **not** equivalent:

| | Travels with the repo | Doctor still probes reachability |
|---|---|---|
| `--model <tag>` in the `adapters.yaml` argv | **yes** — `agora watch` under launchd/systemd sees it | **no** |
| `$AGORA_OLLAMA_MODEL` | no — it is process environment | **yes** (`_resolve_model` calls `list_ollama_models` first, `cli.py:2131-2134`) |

Pick on that basis. The argv form is still the right default for an unattended deployment — a pin
the scheduler cannot see is worse than a blind spot — but then **doctor is no longer your daemon
check**, and you should verify the daemon separately:

```bash
curl -fsS "${AGORA_OLLAMA_HOST:-http://localhost:11434}/api/tags" >/dev/null && echo "ollama: up"
```

(`$AGORA_OLLAMA_MODEL` reaches the shim at all because the default `qwen` backend declares
`network: loopback`, which means it is *not* run inside the sandbox and inherits the process
environment minus credential-shaped names — `curator/subprocess_backend.py:371-381`,
`curator/isolation/__init__.py:74-94`.)

### Size the batch for the model you actually run

The claim cap `max_candidates_per_run` defaults to **32** (`curator/constants.py:54`), and that is a
frontier-model number. A small model's effective attention collapses well before its nominal context
window, and an oversized PASS-1 prompt **degrades plan quality silently — no error, just worse
keep/merge/drop calls** ([`INGEST-CONTRACT.md`](INGEST-CONTRACT.md) §1.3). `qwen3.6:35b-a3b` is a
~30B MoE, i.e. squarely in the band that wants a lower cap. In `_kb/repo.yaml`:

```yaml
curator:
  limits:
    max_candidates_per_run: 24    # ~30B MoE: 16–24 · ≤8B dense: 8–12 · frontier/API: 32
```

Nothing is lost to a lower cap — a capped backlog drains across successive triggers in FIFO slices.
This matters most right after `agora import`, which queues an entire vault at once.

## 4. Your first knowledge base

Five steps, each with a verification command and the output it produced.

> **`/tmp/my-kb` is a scratch path, on purpose.** It keeps every command below literally
> copy-pasteable and every pasted output honest. **Do not keep a knowledge base there** — macOS
> reaps `/tmp`, and `systemd-tmpfiles` defaults to clearing it after 10 days on Linux, so the repo
> disappears with no error and no warning. For a KB you intend to keep, substitute a durable path
> (the README uses `~/my-kb`) everywhere below — including in [§5](#5-register-the-mcp-face-with-your-agent),
> which registers the path *permanently* with your agent.

### Step 1 — create the repo

```bash
uv run agora repo init /tmp/my-kb --name my-kb --domain general
```

```
adapters: /tmp/my-kb/adapters.yaml
9b3b1dcacdebf8d38576dcb17c81f595fc39be52
```

The second line is the sha of the admin commit that seeded the schema. (It is `agora repo init`,
not `agora init`.)

**Verify:**

```bash
git -C /tmp/my-kb log --oneline && cat /tmp/my-kb/.gitignore
```

```
9b3b1dc chore: emit KB schema + repo config
ea2a51d chore: initialize agora knowledge repo
# Agora operational spool — rebuildable, never canonical (ADR-0001).
_kb/
.DS_Store
```

`_kb/` — the inbox, curator state, cursors, caches and gold packs — is **git-ignored by design**.
Only curated markdown is versioned. What that means for backups is
[`LIMITATIONS.md`](LIMITATIONS.md); read it before this repo holds anything you care about.

**The repo now exists, so §3 applies from here on.** If you chose Path A, apply the
[§3](#3-pin-the-model-path-a-only) pin now — its heredoc targets exactly the `/tmp/my-kb` this step
created, and step 2 below is where you see whether it took. If you chose Path B in §1.2, apply its
`adapters.yaml` and `_kb/repo.yaml` edits instead.

### Step 2 — health check

```bash
uv run agora doctor --repo /tmp/my-kb
```

```
agora doctor (agora 0.1.0b1, python 3.12.13)
  git: ok (/usr/bin/git)
  python: 3.12.13 (ok)
  dep pydantic: ok
  dep fastmcp: ok
  dep yaml: ok
  repo /tmp/my-kb: initialized
  sandbox: seatbelt (ok)
    write-inside=True write-outside-denied=True apple-shim=True
    network-denied=True
  routing: plan=qwen (network: loopback)  author=qwen (network: loopback)
  brain qwen: ollama http://localhost:11434 reachable, 2 models, would use 'qwen3.6-hermes:latest'
  harvest: disabled (scope_lock=personal)
  connectors: none configured
  index: enabled=True cache=absent
  gold: pack=absent (harvester scan excludes _kb/gold/, §8)
  backup: no remote configured (push-only backup off — set backup.remote, #64)
  failures: events=0 last_attempt=never last_failure=none
status: healthy
```

**Verify:** the last line reads `status: healthy` and the exit code is `0`.

Read the `brain qwen:` line before moving on. Here it says `would use 'qwen3.6-hermes:latest'` — the
§3 fallback, choosing a model this walkthrough never asked for. **This is the moment to apply the
§3 pin**, then re-run doctor until that line reads:

```
  brain qwen: ollama http://localhost:11434, model pinned to 'qwen3.6:35b-a3b' by adapters.yaml argv (no /api/tags probe — the run lists no models either; reachability NOT checked)
```

Doctor probes the configured brain and that probe **counts toward the verdict** (`cli.py:2281-2373`,
#96) — **for an unpinned backend**. Once you apply the argv pin, that line is the pin's own report
and no longer a reachability check; see [§3](#3-pin-the-model-path-a-only). Run the pin *and* the
`curl` check there if you want both.

On an unpinned backend a red verdict is correct behaviour, not a false alarm — it means
`agora curate` cannot run. With Ollama down (the six lines between the remediation block and the
verdict are unrelated to the brain and are cut here — the block is otherwise verbatim):

```
  brain qwen: ollama http://localhost:1 UNREACHABLE (could not list Ollama models at http://localhost:1/api/tags: <urlopen error [Errno 61] Connection refused>; is the Ollama daemon running?)
    fix (no download — 'claude', 'codex', 'gemini' are already installed): add to adapters.yaml
      backends:          # merge into the existing key, do not add a second one
        claude: { argv: [agora-cli-brain, --, claude, -p], network: loopback }
      then set  curator.backend: claude  in _kb/repo.yaml (ADR-0016) — i.e.
      curator:           # merge into the existing key
        backend: claude
    fix (local model instead): ollama serve  &&  ollama pull qwen3.6:35b-a3b
…
status: unhealthy
```

Exit code `1`. The remediation block leads with a CLI agent already on your PATH because that costs
no download (`cli.py:2242-2278`) — but re-read the Path B warning in
[§1.2](#path-b--a-headless-cli-agent-no-model-download-adr-0016) before taking it: those agents are
hosted, and doctor's convenience ranking is not a privacy recommendation. Ollama is the fully-local
fallback.

On a host with no brain at all — a machine that only reads the KB, or CI — use `--skip-probe`:

```bash
uv run agora doctor --repo /tmp/my-kb --skip-probe | grep -E "brains|status:"
```

```
  brains: probe skipped (--skip-probe)
status: healthy
```

The verdict then ignores brain reachability, which is the point: you asked it not to look.

### Step 3 — capture a fact

Capture is a **face** operation, not a CLI one: there is no `agora capture` or `agora remember`
subcommand (`uv run agora --help`). The two write paths are the MCP tool `kb_remember`
(`faces/mcp_server.py:1022-1033`) and the web face's upload page.

**If you have an MCP client, do [§5](#5-register-the-mcp-face-with-your-agent) now and come back.**
Registration is two commands, it is the path you will use daily, and it makes everything below
unnecessary. The raw JSON-RPC below exists so this walkthrough completes on a machine with no MCP
client at all — it is a fallback, not the recommended way to capture anything.

Driving the stdio server directly is one `initialize`, one `notifications/initialized`, one
`tools/call`:

```bash
{ printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"shell","version":"0"}}}' \
    '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"kb_remember","arguments":{"text":"Only the curator process edits wiki/; every other face appends immutable events to the per-writer inbox.","domain":"general"}}}'
  python3 -c 'import time; time.sleep(2)'
} | uv run agora serve --repo /tmp/my-kb 2>/dev/null | tail -1
```

```
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\"id\":\"2026-07-31T15-48-51.416Z--a253be\",\"queued\":true,\"inbox_depth\":1}"}],"structuredContent":{"id":"2026-07-31T15-48-51.416Z--a253be","queued":true,"inbox_depth":1},"isError":false}}
```

The `sleep` keeps stdin open long enough for the server to answer before the pipe closes; without it
the process exits after the handshake.

Run it twice more with a different `text` each time — the curator has more to work with when there
is more than one fact. The two used for the outputs below were:

> The curator brain is swappable through adapters.yaml. The default backend qwen shells the
> agora-ollama-brain console script and reaches Ollama over loopback.

> Retrieval in Agora is navigation, not vector search: read the index, follow markdown links, then
> grep. There is no vector database in the design.

**Verify:**

```bash
uv run agora status --repo /tmp/my-kb
```

```
repo: /tmp/my-kb
inbox depth: 3
last_run: never
last_commit: -
counters: ingested=0 merged=0 dropped=0 failed=0
last_attempt: never
last_failure: none
failed_events: 0
```

`inbox depth: 3` — three immutable events are queued and nothing has been consolidated yet. Read the
depth as a **delta**: it should rise by exactly one per `tools/call`. If it jumps by two, the pipe
replayed the frame — the duplicate is byte-identical, and the claim's content dedup collapses the
pair into one candidate, so it costs you nothing but a confusing number here.

### Step 4 — curate

```bash
uv run agora curate --repo /tmp/my-kb --force
```

> **This command prints four lines and then goes silent until the model finishes.** There is no
> progress output. Three captures took **2m20s–2m54s** across repeated runs on the 64 GB host in
> §1.3, and it will take substantially longer on a machine where the model does not fit in memory
> ([§1.2](#path-a--a-local-model-through-ollama)). Do not interrupt it.

`--force` treats the run as due regardless of the cron/threshold/idle triggers. **It is not required
here** — it only makes the walkthrough deterministic. A repo that has never curated has
`last_run: None`, which makes the cron trigger due immediately (`curator/cron.py:176-178`: "when
`last_run` is `None` (never run), any fire within the window counts as due"), so a fresh repo *with*
captures curates on its own and prints `reason: cron` — verified. The only thing that reliably
yields `should_run: False` is an **empty** inbox: cron never runs over one
(`curator/triggers.py:128`).

```
repo: /tmp/my-kb
inbox depth: 3
should_run: True
reason: force
status: published
published_commit: e0c685da16ac84777c6beb21e9d1a19e24218dbc
counts: CREATE_THEME=2, DROP=1, candidates=3, claimed=3, inbox_remaining=0, prose_pending=0, prose_regions=2
```

2m20s on the measuring host. `status: published` and a non-empty `published_commit` are what matter.
**The `counts:` breakdown will not match this line exactly on your run** — the plan is a model
judgement, so it varies between runs on identical input; a repeat of this exact walkthrough produced
`CREATE_THEME=3` with no `DROP`. That is fine. What is not fine is a `status:` other than
`published`.

**`DROP=n` is normal when it appears, and it is worth understanding rather than skipping.** It means
the curator judged that many captures not worth filing. It is a *plan-time* disposition, not a
delete — nothing is
removed and the event is still archived — but the practical consequences are: that text is **not in
`wiki/`**, and its only remaining copy is in `_kb/processed/<date>/`, which is git-ignored and so is
never carried by `agora sync` or any clone ([`LIMITATIONS.md`](LIMITATIONS.md) §1). `log.md` records
the dropped candidate's **id**, not its text (`curator/worker.py:2041`). If a capture matters, check
that it landed rather than trusting the count — and note that the `dropped` counter in
`agora status` folds `NOOP` in with `DROP` (`curator/worker.py:1952`), so it is not a count of
discards either.

**Verify:**

```bash
uv run agora status --repo /tmp/my-kb && git -C /tmp/my-kb log --oneline -1
```

```
repo: /tmp/my-kb
inbox depth: 0
last_run: 2026-07-31T15:49:11Z
last_commit: e0c685da16ac84777c6beb21e9d1a19e24218dbc
counters: ingested=2 merged=0 dropped=1 failed=0
last_attempt: 2026-07-31T15:49:11Z
last_failure: none
failed_events: 0
e0c685d curate: run 2026-07-31T15-49-11.945Z--d8e4e9 (CREATE_THEME=2, DROP=1)
```

The inbox drained to `0` and the curator made a commit. `last_attempt`, `last_failure` and
`failed_events` are the failure surface added by #96 — a run that fails *without* exhausting its
retry budget leaves `last_run` at its old value, so those three lines are where you see it
(`cli.py:536-541`). A healthy repo reads `last_failure: none`.

### Step 5 — query it back

```bash
{ printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"shell","version":"0"}}}' \
    '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"kb_query","arguments":{"question":"who edits the wiki"}}}'
  python3 -c 'import time; time.sleep(2)'
} | uv run agora serve --repo /tmp/my-kb 2>/dev/null | tail -1 \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["result"]["structuredContent"], indent=2, ensure_ascii=False))'
```

```json
{
  "query": "who edits the wiki",
  "status": "ok",
  "hits": [
    {
      "repo": "my-kb",
      "path": "wiki/general/themes/wiki-editing-protocol.md",
      "anchor": "wiki-editing-protocol",
      "line": 1,
      "excerpt": "Wiki Editing Protocol Defines the strict boundary governing wiki modification and event logging operations.",
      "match_reason": "linked-theme",
      "score": 0.876434
    },
    {
      "repo": "my-kb",
      "path": "wiki/general/general-moc.md",
      "anchor": "",
      "line": 2,
      "excerpt": "Wiki Editing Protocol",
      "match_reason": "lexical",
      "score": 0.715079
    }
  ]
}
```

**Verify:** `status: "ok"` with at least one hit. Hits are citations into `wiki/` — path, anchor and
line — not synthesized prose (ADR-0009/0012). The file is plain markdown; open it and read it:

```bash
cat /tmp/my-kb/wiki/general/themes/wiki-editing-protocol.md
```

That is the whole loop: capture → curate → query. Everything else in Agora is a face over it.

## 5. Register the MCP face with your agent

> **Substitute a durable path here.** This registration is permanent; `/tmp/my-kb` is not. If you
> followed §4 literally, point the registration at `~/my-kb` (or wherever you intend to keep the KB)
> and re-run §4 step 1 against it — the OS will delete a `/tmp` repo out from under a registered
> server with no error.

```bash
claude mcp add agora-kb -- uv run --directory /ABSOLUTE/PATH/TO/agora-kb agora serve --repo /tmp/my-kb
```

```
Added stdio MCP server agora-kb with command: uv run --directory /ABSOLUTE/PATH/TO/agora-kb agora serve --repo /tmp/my-kb to local config
```

**Verify:**

```bash
claude mcp get agora-kb
```

```
agora-kb:
  Scope: Local config (private to you in this project)
  Status: ✔ Connected
  Type: stdio
  Command: uv
  Args: run --directory /ABSOLUTE/PATH/TO/agora-kb agora serve --repo /tmp/my-kb
…
```

(`claude mcp get` also prints an `Environment:` line and a `claude mcp remove …` hint after the
`Args:` line; both are cut above.)

Any other MCP client is the same server over **stdio** — point it at
`uv run --directory /ABSOLUTE/PATH/TO/agora-kb agora serve --repo /tmp/my-kb` with no HTTP
transport; remote transport is deliberately not shipped in this beta (`CHANGELOG.md:174-175`). Seven
tools appear, confirmed live via `tools/list`:

```
['kb_remember', 'kb_query', 'kb_read', 'kb_neighbors', 'kb_context', 'kb_status', 'kb_curate']
```

## 6. Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `agora curate` fails and the brain's stderr reads *"no Ollama models available; pull one (e.g. `ollama pull qwen3.6:35b-a3b`) and ensure the daemon is running"* | The daemon answered but has zero models installed — `select_model` raises rather than guess (`adapters/ollama_brain.py:173-176`) | `ollama pull qwen3.6:35b-a3b`, then re-check with `uv run agora doctor --repo /tmp/my-kb \| grep "brain "` |
| Curation output reads oddly and you cannot tell which model produced it | Two independent causes. (1) `select_model` falls back to the alphabetically first qwen-family model with **no run-time log line**, and doctor only prints `WARNING … alphabetical fallback` when *no* qwen model exists at all (`adapters/ollama_brain.py:160-181`, `cli.py:2193-2201`). (2) The batch is too large for the model: over-filling PASS-1 **degrades plan quality silently — no error** ([`INGEST-CONTRACT.md`](INGEST-CONTRACT.md) §1.3), and the default cap 32 is frontier-sized (`curator/constants.py:54`) | `ollama list` to see what is actually installed, then pin explicitly per [§3](#3-pin-the-model-path-a-only); and lower `curator.limits.max_candidates_per_run` to 16–24 for a ~30B MoE, 8–12 for ≤8B |
| `agora doctor` prints `brain qwen: ollama … UNREACHABLE (… Connection refused …)` and exits `1` | No Ollama daemon on the configured host (`$AGORA_OLLAMA_HOST`, default `http://localhost:11434` — `adapters/ollama_brain.py:79,1315`) | Start it (`ollama serve`), **or** wire a CLI agent per [§1.2 Path B](#path-b--a-headless-cli-agent-no-model-download-adr-0016) — reading its hosted-service warning first. On a deliberately brain-less host, `--skip-probe` |
| `agora curate` fails on a host where `agora doctor` said `status: healthy` | Doctor proved **presence**, not function. With an argv `--model` pin it never contacts the daemon at all ([§3](#3-pin-the-model-path-a-only), `cli.py:2150-2156`); for a non-Ollama backend the probe is a `shutil.which` PATH lookup and nothing more (`cli.py:2242-2278`) | `curl -fsS "$AGORA_OLLAMA_HOST/api/tags"` for the daemon; otherwise treat a `agora curate --force` that reaches `status: published` as the only real proof the brain answers |
| `agora web` dies with a traceback ending `ModuleNotFoundError: No module named 'fastapi'` | The `web` extra is not installed. The clean message `_cmd_web` intends (`cli.py:1517-1527`) does **not** fire: `uvicorn` arrives transitively with `fastmcp`, so both guarded imports succeed and the real failure lands later at `build_app` (verified in a core-only venv). No issue tracks this yet; it is a cosmetic guard, not a functional defect — and the message it would print names a `pip install 'agora-kb[web]'` that cannot work anyway, since there is no PyPI distribution ([§0](#0-before-you-start)) | `uv sync --extra web --extra ingest --extra metrics` — see [§2](#2-install) |
| `agora curate` prints `status: failed` with a `failed_record:` path, and the exit code is still `0` | A failing run is normal self-healing, not a crash: within-budget events go back to `inbox/` and `agora curate` returns `0` deliberately so cron/systemd do not manufacture a restart loop (`cli.py:618-625`). The cause is on stdout and in `_kb/failed/<date>/<run-id>/error.json` | Read the `failed_checks:` line, then `uv run agora status --repo /tmp/my-kb` for `last_failure: UNRESOLVED …`. Once the budget is exhausted the events are terminal; return them with `agora requeue` — **never hand-move files inside `_kb/`**. The full lifecycle, including `--reset-attempts` and `_kb/requeued/`, is [`LIMITATIONS.md`](LIMITATIONS.md) |
| `agora curate` prints `should_run: False` / `reason: none` / `note: no consolidation run was due` | **The inbox is empty.** No trigger fires over depth 0 — threshold needs `depth >= threshold` (min 1), idle needs `depth > 0`, and cron explicitly refuses an empty inbox (`curator/triggers.py:116-130`). On a repo that *has* captures this is not the message you get: a never-run repo fires `reason: cron` immediately | Capture something (step 3) and re-run. `--force` still prints `should_run: True` but a run over an empty inbox changes nothing |
| `agora doctor` says `status: healthy` in a directory that is not a knowledge repo | Deliberate: an **absent** `adapters.yaml` is "setup not done", not a misconfiguration, so it passes with `brains: not probed (no adapters.yaml — no backend configured)` (`cli.py:2315-2322`) | Check the `repo …: not initialized (run 'agora repo init')` line — that is the one telling you where you are |
| `pytest` or `ruff` is suddenly "command not found" after an install step | A bare `uv sync` pruned them; `dev` is an *extra*, not a dependency group (`pyproject.toml:42-48`) | Re-run the full command in [§2](#2-install). Confirm before you sync next time with `uv sync --dry-run` |

## 7. Next steps

- [`LIMITATIONS.md`](LIMITATIONS.md) — **the data-safety contract.** What is not backed up, what
  cannot be deleted, what "eventual consistency" means here, and how terminal failures come back.
  Read it before this repo holds anything you cannot re-create.
- [`../SECURITY.md`](../SECURITY.md) — the threat model: what the unauthenticated web face does and
  does not defend against, what the curator sandbox actually confines, and how to report a hole
  privately. Read it before you expose anything or route a brain to a hosted agent.
- [`../deploy/README.md`](../deploy/README.md) — launchd/systemd units for `agora watch`,
  `agora web` and a periodic `agora harvest`, so consolidation stops depending on you typing it.
  It is also the SSOT for the terminal-failure recovery procedure.
- [`DEPLOY-TEAM.md`](DEPLOY-TEAM.md) (**Korean**) — one KB shared by 2–10 people: hub topology,
  TLS + proxy auth, read-only clones. There is no authentication in this beta; that guide is how you
  compensate.
- [`DESIGN.md`](DESIGN.md) and [`adr/`](adr/) — why the inbox is append-only, why exactly one
  process writes the wiki, and why retrieval is navigation instead of vector search.
- `uv run agora --help` — the commands this walkthrough did not touch: `import` (normalize an
  existing Obsidian vault), `harvest`, `index`, `gold`, `sync`, `watch`, `web`.
