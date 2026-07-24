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

## Rules & health

- **127.0.0.1 only.** The web units hard-code `--host 127.0.0.1`. The web face has **no
  authentication, no TLS** — never change the bind host to `0.0.0.0` or any non-loopback address.
  Remote access goes over an SSH tunnel (`ssh -L 8000:127.0.0.1:8000 host`) or the authenticating
  reverse proxy above (which keeps the loopback bind and adds per-user identity).
- **backup.auto (#64) under a service manager** is non-interactive: use an ssh agent (macOS:
  `ssh-add --apple-use-keychain`) or a git credential helper. A prompting credential flow fails
  the push; curation is unaffected (check the `agora doctor` backup line). **Linux caveat:** a
  systemd user unit does NOT inherit `SSH_AUTH_SOCK` from your login shell — import it
  (`systemctl --user import-environment SSH_AUTH_SOCK`, or `~/.config/environment.d/`), or use a
  passphrase-less deploy key / credential helper instead. (macOS launchd injects
  `SSH_AUTH_SOCK` into the gui domain itself, so the agent route works as-is there.)
- **Health check**: `uv run --directory /ABSOLUTE/PATH/TO/agora-kb agora doctor --repo
  /ABSOLUTE/PATH/TO/knowledge-repo` — git/deps/sandbox self-test, routing + connectors tables,
  backup line. Run it after installing units and whenever a log shows repeated failures.
- **Reboot check** (the #65 acceptance): reboot, wait a minute, then confirm
  (macOS: LaunchAgents start at *login* — log back in first, or nothing runs)
  `launchctl print gui/$(id -u)/com.agora.watch` shows `state = running` (macOS) or
  `systemctl --user status agora-watch` shows `active (running)` (Linux, requires linger),
  `curl http://127.0.0.1:8000/api/status` answers, and the harvest log/journal shows a run
  after boot (`RunAtLoad` / `OnBootSec=5min`).
