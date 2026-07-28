# deploy/ — always-on packaging (launchd / systemd) — issue #65

Example service units that keep a personal Agora deployment running unattended: start on
boot, restart on crash, and capture logs. Three long-running/periodic entries:

| Entry | What | launchd | systemd |
|---|---|---|---|
| **watch** | curator scheduler loop (`agora watch`) | `launchd/com.agora.watch.plist` | `systemd/agora-watch.service` |
| **web** | web face (`agora web`, loopback only) | `launchd/com.agora.web.plist` | `systemd/agora-web.service` |
| **harvest** | periodic `agora harvest` | `launchd/com.agora.harvest.plist` | `systemd/agora-harvest.service` + `agora-harvest.timer` |

> **Why is harvest a separate unit?** `agora watch` evaluates **only** the curation triggers
> (cron / threshold / idle) — it **never runs harvest** (`cli.py::_cmd_watch`,
> `curator/triggers.py`). Without a harvest unit, connectors (e.g. the #25 session connector)
> run only when you type `agora harvest` by hand. Harvest is opt-in and fail-closed
> (`harvest.enabled: false` by default), so the unit is a cheap no-op on an unconfigured repo —
> integrating harvest into the watch tick is deferred to the #51 supervisor design.

## Placeholders (substitute before installing)

All files use the same fixed tokens — grep for them to catch a missed substitution:

| Token | Meaning |
|---|---|
| `/ABSOLUTE/PATH/TO/uv` | the `uv` binary (`which uv`; commonly `~/.local/bin/uv` or `/opt/homebrew/bin/uv`) — service managers do not search your shell `PATH` |
| `/ABSOLUTE/PATH/TO/agora-kb` | this source checkout (`uv run --directory` is the current only run form; the package is unreleased, version 0.0.0) |
| `/ABSOLUTE/PATH/TO/knowledge-repo` | the knowledge repo (`--repo`) |
| `YOUR_USER` | your login user (launchd log paths, `loginctl enable-linger`) |

Check for leftovers after editing — no output means clean. The header comments keep the tokens
on purpose (as install instructions), so a raw `grep` over the whole file hits forever unless you
substituted with a whole-file `sed`; check the *effective* lines instead:

```bash
plutil -p ~/Library/LaunchAgents/com.agora.*.plist \
  | grep "ABSOLUTE/PATH/TO\|YOUR_USER"                              # macOS (parsed values only)
grep -rn "ABSOLUTE/PATH/TO\|YOUR_USER" ~/.config/systemd/user/agora-* \
  | grep -v ':#'                                                    # Linux (skip comment lines)
```

**Web-face prerequisite** (one-time): the web unit needs the `web`/`ingest`/`metrics` extras.
The unit argv passes `--extra` flags so `uv run` installs them itself, but sync once up front so
the first start does not depend on the network:

```bash
uv sync --directory /ABSOLUTE/PATH/TO/agora-kb --extra web --extra ingest --extra metrics
```

## macOS (launchd)

```bash
# 0. one-time: log directory (launchd creates log FILES, not directories)
mkdir -p ~/Library/Logs/agora

# 1. copy the plists and substitute the placeholders (edit in your editor, or sed)
cp deploy/launchd/com.agora.*.plist ~/Library/LaunchAgents/

# 2. syntax-check after editing
plutil -lint ~/Library/LaunchAgents/com.agora.*.plist

# 3. load into your user (gui) domain — starts now AND at every login
#    (LaunchAgents run in your login session, not pre-login at boot; a login-less
#    headless Mac needs a LaunchDaemon conversion instead)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.agora.watch.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.agora.web.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.agora.harvest.plist

# 4. verify
launchctl print gui/$(id -u)/com.agora.watch | head -20   # state = running
curl -s http://127.0.0.1:8000/api/status | head -1        # web face answers

# unload (e.g. before editing a plist; re-bootstrap afterwards)
launchctl bootout gui/$(id -u)/com.agora.watch
```

**Logs** land in `~/Library/Logs/agora/{watch,web,harvest}.{out,err}.log` (the
`StandardOutPath`/`StandardErrorPath` keys). Tail them when a unit dies silently:
`tail -f ~/Library/Logs/agora/watch.err.log`.

**Schedules**: `com.agora.watch` and `com.agora.web` are daemons (`KeepAlive` — relaunched on
*any* exit, crash or clean). `com.agora.harvest` runs at load and then every `StartInterval`
seconds (3600 = hourly; edit to taste).

**A failing watch tick no longer restarts the process** (#97): since a deterministic per-tick raise
(a corrupt `_kb/state.json`, a `repo.yaml` typo) recurs on every restart, `agora watch` now reports
one bounded `<stamp> tick failed: <Type>: <message>` line on **stderr**, backs off exponentially
(interval → 2× → 4× … capped at 15 min, and never faster than your `--interval`), and keeps
running; the next clean tick resets the backoff immediately, so a repaired repo recovers without a
restart. Exit code stays 0, so `KeepAlive`/`Restart=on-failure` no longer turn a 60 s scheduler
into a 10 s crash loop. The unit files are unchanged and still correct — those policies exist for a
genuine process death (OOM, SIGKILL). Set `AGORA_WATCH_TRACEBACK=1` to add a full traceback to that
stderr line when filing a bug.

## Linux (systemd, user units)

```bash
# 1. copy the units and substitute the placeholders
mkdir -p ~/.config/systemd/user
cp deploy/systemd/agora-*.{service,timer} ~/.config/systemd/user/

# 2. enable — the harvest entry enables the TIMER, not the service
systemctl --user daemon-reload
systemctl --user enable --now agora-watch.service agora-web.service agora-harvest.timer

# 3. user units normally stop at logout / start at login — for true boot-time start:
loginctl enable-linger YOUR_USER

# 4. verify
systemctl --user status agora-watch agora-web
systemctl --user list-timers agora-harvest.timer
curl -s http://127.0.0.1:8000/api/status | head -1
```

**Logs** go to journald: `journalctl --user -u agora-watch -f` (same for `agora-web`,
`agora-harvest`). `Restart=on-failure` restarts crashed daemons; the harvest service is
`Type=oneshot` with no `Restart=` — the timer owns the retry cadence.

## Per-user identity behind a reverse proxy (`web.identity`, issue #67)

> Deploying for a **team**? The full hub topology (reverse proxy, SSH MCP writes, read-only
> clones, secrets) is [`docs/DEPLOY-TEAM.md`](../docs/DEPLOY-TEAM.md) (issue #68) — the snippets
> below are the building blocks its §2 references.

For a **team** deployment fronted by an authenticating reverse proxy, set in the knowledge repo's
`_kb/repo.yaml`:

```yaml
web:
  identity:
    trusted_header: X-Remote-User   # opt-in: naming the header IS the trust declaration
    # strip_domain: true            # alice@example.com → alice (optional)
```

Uploads then stamp `source: web:<header value>` per authenticated user instead of one shared
`web:local` (ADR-0025 appendix). **Trust boundary — all three are mandatory:** the proxy must
(1) authenticate every request, (2) force-set the header from the authenticated user, and
(3) strip/override any client-supplied copy of it. The web face must be reachable **only through
the proxy** (keep the `127.0.0.1` bind); a directly reachable port with `trusted_header` set lets
anyone forge any teammate with `curl -H`. A present-but-invalid header value is refused with 400
(proxy misconfig / forgery signal), an absent header falls back to `--user`.

**Caddy** (basic auth; `{http.auth.user.id}` is the authenticated username):

```caddyfile
kb.example.com {
    basic_auth {
        alice $2a$14$...   # caddy hash-password
        bob   $2a$14$...
    }
    # reverse_proxy preserves the original Host (kb.example.com) — the #94 Host standard.
    reverse_proxy 127.0.0.1:8000 {
        header_up X-Remote-User {http.auth.user.id}   # force-set: overrides any client value
    }
}
```

**nginx** (basic auth; `$remote_user` is the authenticated username):

```nginx
server {
    listen 443 ssl;
    server_name kb.example.com;
    auth_basic           "Agora";
    auth_basic_user_file /etc/nginx/agora.htpasswd;   # htpasswd -B
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $http_host;             # #94 Host standard: preserve the client's
                                                      # Host VERBATIM, port included (nginx would
                                                      # otherwise send $proxy_host =
                                                      # 127.0.0.1:8000; $host drops the port,
                                                      # which breaks non-default public ports)
        proxy_set_header X-Remote-User $remote_user;  # force-set: replaces any client value
    }
}
```

Both directives *set* (not append) the header, which is what strips a spoofed client copy — verify
with `curl -u alice:pw -H 'X-Remote-User: mallory' https://kb.example.com/api/upload ...` and check
the receipt's `identity_source: "header"` plus the inbox event's `source: web:alice`. As defense in
depth the face itself refuses a request carrying the header **more than once** with 400 (an
append-mode directive such as Apache `RequestHeader append` would let the client's forged copy ride
alongside the authenticated one), so a set-vs-append misconfiguration surfaces loudly on the first
upload instead of silently mis-attributing writes.

## Browser-mediated attack defense (`web.security`, issue #94)

The `127.0.0.1` bind is a *network* boundary, and a browser walks straight through it: a page the
victim merely opens can auto-submit a cross-site multipart form into `POST /api/upload` (the write
lands in the append-only, undeletable inbox), and an attacker domain rebound to `127.0.0.1`
(DNS rebinding) can read the whole KB *same-origin*. Two guards close both, over one operator list:

```yaml
web:
  security:
    # An explicit list REPLACES the default (it does not extend it): keep 127.0.0.1 for hub-local
    # health checks / Prometheus and localhost for browsing through the SSH tunnel below.
    allowed_hosts: [kb.example.com, 127.0.0.1, localhost]   # default: [localhost, 127.0.0.1]
    # require_origin: true                                  # also refuse writes with NO Origin
```

- **Host allowlist** → starlette `TrustedHostMiddleware`: a `Host` outside the list is **400**
  (with a body that names this config key — matching is case-insensitive and never redirects).
  Bare hostnames only (exact, or a `*.example.com` subdomain wildcard) — the port is stripped
  before matching, so entries carry none. **A reverse proxy must pass the client's `Host` through
  verbatim** (Caddy does by default; nginx needs `proxy_set_header Host $http_host;` — both
  snippets above do it) and the public hostname must be in this list, or every proxied request
  400s — and browser uploads 403, since the Origin check below is anchored to it. IPv6 literals
  (`--host ::1`) are **not supported** — starlette matches on `Host.split(":")[0]`, which cannot
  express `[::1]`; bind IPv4 loopback (the default) or use a hostname. Putting `::1` in the list
  fails at startup with a `ConfigError` that says so.
- **Origin/Referer guard** on the three state-changing routes (`POST /api/upload`,
  `POST /api/upload-batch`, HTMX `POST /upload`): a request whose `Origin` (or, absent that,
  `Referer`) `host:port` is not the request's **own `Host`** is **403** and **nothing is
  appended**. The baseline is the request's Host, *not* this allowlist: the list carries entries
  that exist for other reasons (hub-local loopback for health checks, a subdomain wildcard), and
  trusting them for writes would let any page on any team member's loopback — or one XSS'd sibling
  subdomain — inject into the hub's inbox. The scheme is not compared (TLS termination); the port
  is. A request with **no** `Origin` passes by default — that is what keeps scripted/CI writers and
  the verification `curl` above working, and browsers always send `Origin` on cross-site writes, so
  refusing mismatches alone closes the browser path. Set `require_origin: true` to refuse
  header-less writes too (recommended for a team hub with no scripted uploads; then add
  `-H 'Origin: https://kb.example.com'` to any upload `curl`). GET health checks are never
  Origin-checked.
- **Framing denial** on every response (`X-Frame-Options: DENY` + CSP `frame-ancestors 'none'`): a
  click inside an iframe submits with the face's own origin, so the guard above cannot see it.

Quick check from the hub: `curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.com'
http://127.0.0.1:8000/api/status` must print **400**. Unknown keys in this block fail loud
(`ConfigError`) — a typo in a security opt-in must not read as "off".

This is **not** authentication (that is ROADMAP Phase 4 / ADR-0036); it is defense in depth on top
of the unauthenticated premise, and it closes *browser-mediated* attacks only.

## Rules & health

- **127.0.0.1 only.** The web units hard-code `--host 127.0.0.1`. The web face has **no
  authentication, no TLS** — never change the bind host to `0.0.0.0` or any non-loopback address.
  Remote access goes over an SSH tunnel (`ssh -L 8000:127.0.0.1:8000 host`) or the authenticating
  reverse proxy above (which keeps the loopback bind and adds per-user identity). The loopback
  bind is a *network* boundary only — the browser-mediated paths through it are closed by
  `web.security` above (issue #94), not by the bind.
- **backup.auto (#64) under a service manager** is non-interactive: use an ssh agent (macOS:
  `ssh-add --apple-use-keychain`) or a git credential helper. A prompting credential flow fails
  the push; curation is unaffected (check the `agora doctor` backup line). **Linux caveat:** a
  systemd user unit does NOT inherit `SSH_AUTH_SOCK` from your login shell — import it
  (`systemctl --user import-environment SSH_AUTH_SOCK`, or `~/.config/environment.d/`), or use a
  passphrase-less deploy key / credential helper instead. (macOS launchd injects
  `SSH_AUTH_SOCK` into the gui domain itself, so the agent route works as-is there.)
- **Health check**: `uv run --directory /ABSOLUTE/PATH/TO/agora-kb agora doctor --repo
  /ABSOLUTE/PATH/TO/knowledge-repo` — git/deps/sandbox self-test, routing + **brains** + connectors
  tables, backup + failures lines. Run it after installing units and whenever a log shows repeated
  failures.
  - **Since #96 the brain probe is part of the verdict**: doctor asks whether the configured
    backend's `argv[0]` is on PATH and (for `agora-ollama-brain`) whether the daemon answers
    `/api/tags`, and prints `status: unhealthy` + **exit 1** when it cannot be used. That is the
    point — a node whose curator cannot run should not report healthy — but it means **a node with
    no brain now fails a `doctor`-based gate**. Two ways out: fix the brain (doctor prints a
    copy-pasteable block naming any headless CLI agent already on your PATH — `claude` / `codex` /
    `gemini` via `agora-cli-brain`, ADR-0016 — with `ollama serve` + `ollama pull` as the
    heavier alternative), or run `agora doctor --skip-probe` on nodes that legitimately have none
    (web-only, CI, a hub whose curation happens elsewhere). `--skip-probe` makes the verdict ignore
    brain reachability and performs no daemon or PATH lookups; every other check is unchanged.
  - The probe is bounded (3 s per distinct routed brain) and never *executes* a brain.
- **Reboot check** (the #65 acceptance): reboot, wait a minute, then confirm
  (macOS: LaunchAgents start at *login* — log back in first, or nothing runs)
  `launchctl print gui/$(id -u)/com.agora.watch` shows `state = running` (macOS) or
  `systemctl --user status agora-watch` shows `active (running)` (Linux, requires linger),
  `curl http://127.0.0.1:8000/api/status` answers, and the harvest log/journal shows a run
  after boot (`RunAtLoad` / `OnBootSec=5min`).
