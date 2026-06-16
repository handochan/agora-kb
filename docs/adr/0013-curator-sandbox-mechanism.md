# ADR-0013 — Curator sandbox mechanism (macOS Seatbelt + cross-platform plan)

**Status:** Accepted · 2026-06-13

## Context
ADR-0008 mandates that the curator's cognitive INGEST step (its step 2-3) runs "inside an OS sandbox
with no network by default and with the repo as its only writable mount," but it deliberately leaves
the *mechanism* unspecified. The backend processes untrusted, prompt-injection-laden captured content
and may be any swappable brain (local Qwen via Ollama by default; or `claude` / `codex` / `qwen` /
`hermes` headless CLIs). We must assume it is fully adversarial: it could try to corrupt the wiki,
escape the tenant repo, exfiltrate captured secrets, steal git credentials, or tamper with the
operational spool to gain persistence. This ADR specifies the OS-level sandbox that confines that
step, and nothing else — the transaction itself (claim/manifest/worktree/validate/commit/CAS/finalize)
remains deterministic orchestration owned by `curator/worker.py` per ADR-0008.

The mechanism must satisfy the project invariants: writes confined to the per-tenant worktree
(integrity, tenant isolation #5), all outbound network denied (no exfiltration), git credentials never
present (no credential theft), and the main repo's `.git/{config,hooks,worktrees}` plus `_kb/` left
untouchable (no operational tampering). It must have an OSS path with no copyleft into the core library
(#4), and must be swappable so no single OS facility is hard-coded (#6). The sandbox is the *first*
layer of defense; ADR-0008's deterministic post-run validator (allowlist `wiki/`, `index.md`,
`log.md`, schema paths; reject symlinks/path-escapes/changes to `_kb`/git-config/hooks) is the
*second*. Neither is trusted alone.

Empirical reproduction on the target host (macOS 26.4, arm64; git 2.50.1 Apple Git-155;
`/usr/bin/sandbox-exec` present; Ollama listening on `127.0.0.1:11434`) surfaced seven non-obvious
failures that a naive design would ship broken. Most importantly, a *linked* git worktree's `.git` is
a 64-byte `gitdir:` pointer **file**, not a directory: the real hooks/config/objects and `_kb/` live
in the **main** repo *outside* the worktree, so carve-outs keyed on `{WORKTREE}/.git/hooks` are
no-ops. A strict no-network sandbox also blocks loopback to the host Ollama daemon, so the default
local-model brain only works if inference happens *outside* the sandbox. Apple-shimmed CLIs (git via
`xcrun`) fatally crash unless `/dev/null` is writable. And `/tmp` is a symlink to `/private/tmp`, so
Seatbelt subpath grants keyed on unresolved paths silently fail to grant the write. This ADR encodes
the verified fixes for all of these.

## Decision
Adopt the macOS `sandbox-exec` + deny-default SBPL profile as the Phase-1 default, behind a swappable
`BackendIsolation` adapter, with: a hard non-nested-worktree invariant asserted in code; a throwaway
scratch dir pinned *outside* the worktree (excluded from the ADR-0008 diff); explicit
`(deny network*)` / `(deny system-socket)` belt-and-suspenders; a `/dev/null` write-allow for
Apple-shimmed CLIs; a hardened self-test that asserts on `EPERM` specifically, probes the reachable
host Ollama, and exercises an Apple-shimmed binary; an opt-in, fail-closed restricted-fallback whose
*full* loss surface (network **and** out-of-worktree writes) is documented and which forces
review-mode/PR; a Linux `bwrap --unshare-all` plan; per-backend parameters sourced from
`adapters.yaml`; and Ollama inference kept *outside* the sandbox.

### Scope
This ADR is **only the "run the backend in a sandbox" box** inside ADR-0008 step 2, plus the self-test
that proves the box works. The transaction (lock, claim, manifest, worktree creation, post-run
validation, commit, compare-and-swap, finalize) belongs to ADR-0008. The cognitive backend registry
(the "brain": qwen/claude/codex/hermes) belongs to ADR-0004 and is unchanged here.

### Threat model and goals

| Goal | Mechanism | Failure if absent |
|------|-----------|-------------------|
| G1 Integrity | writes confined to the worktree; deterministic post-hoc allowlist validation of the worktree tree (ADR-0008) | backend corrupts wiki / escapes tenant repo |
| G2 No exfiltration | all outbound network **explicitly** denied (default-deny + `(deny network*)`/`(deny system-socket)`); no proxy | captured secrets leak out |
| G3 No credential theft | env scrubbed; git creds never in the sandbox; commit/CAS happen in deterministic code outside; backend never invokes git inside the sandbox | backend steals tokens / pushes |
| G4 No operational tampering | the **main** repo's `.git/{config,hooks,worktrees/<id>}` and `_kb/` live outside the worktree and are denied/non-bound; the worktree's own `.git` pointer file is locked | backend edits spool/hooks/objects for persistence |
| G5 Tenant isolation (#5) | only THIS repo's worktree is the writable content mount; tmp scratch is a throwaway outside any repo | curator touches another repo |

**Critical scope rule (G1):** the validator diffs ONLY the worktree's git-tracked tree; the separate
tmp scratch is never part of the diff.

### Adapter surface

Per invariant #6 and ADR-0008, the mechanism hides behind one adapter interface, swappable without
touching `core`.

```
src/agora_kb/curator/
  worker.py          # owns the transaction; asserts non-nested worktree; calls isolation.run(...)
  backends.py        # WRITE-adapter registry (the "brain": qwen/claude/codex/hermes) — UNCHANGED
  isolation/
    __init__.py      # select_backend_isolation() -> BackendIsolation
    base.py          # BackendIsolation Protocol + SandboxSpec/SandboxResult/SelfTestReport
    seatbelt.py      # macOS adapter (Phase 1 default)
    bwrap.py         # Linux adapter
    restricted.py    # opt-in reduced-isolation fallback
    selftest.py      # runtime self-test + capability detection (agora doctor)
    profiles/
      base.sbpl      # static SBPL allowlist (Codex-derived)
```

```python
# isolation/base.py  (type hints required on public fns)
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

NetworkPosture = Literal["none", "localhost-ollama"]   # "none" is the only Phase-1 default

@dataclass(frozen=True)
class SandboxSpec:
    argv: list[str]              # backend command, shell=False, NEVER concatenated
    worktree: Path               # MUST already be realpath-resolved by caller (see §realpath)
    tmp_dir: Path                # SEPARATE throwaway scratch OUTSIDE the worktree; HOME/TMPDIR point here
    read_roots: list[Path]       # runtime/model paths the backend must read (per-backend)
    stdin_data: bytes | None     # long prompt via stdin
    env: dict[str, str]          # ALREADY scrubbed; isolation only adds HOME/TMPDIR/PATH
    timeout_s: int = 600         # per-backend override from adapters.yaml
    network: NetworkPosture = "none"

@dataclass(frozen=True)
class SandboxResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    mechanism: str               # "seatbelt" | "bwrap" | "restricted"
    reduced_isolation: bool      # True only for restricted fallback

@dataclass(frozen=True)
class SelfTestReport:
    passed: bool
    write_inside_ok: bool
    write_outside_denied: bool   # asserted via EPERM, not "any error"
    network_denied: bool         # asserted via EPERM to a REACHABLE target
    apple_shim_ok: bool          # Apple-shimmed binary (git) runs without dev/null fatal
    mechanism: str

class BackendIsolation(Protocol):
    name: str
    def available(self) -> bool: ...          # cheap capability probe
    def self_test(self, throwaway_worktree: Path, throwaway_tmp: Path) -> "SelfTestReport": ...
    def run(self, spec: SandboxSpec) -> SandboxResult: ...
```

**Selection order** (`select_backend_isolation`): `darwin -> seatbelt`;
`linux -> bwrap (if available()) else restricted`; anything else `-> restricted`. `restricted` is only
returned if `config.curator.allow_reduced_isolation is True`; otherwise selection raises
`SandboxUnavailable` (fail-closed).

**Invariants the caller (`worker.py`) MUST uphold before calling `run`:**
1. Every path in the spec is already `Path(...).resolve(strict=True)` (realpath).
2. The temporary worktree is **non-nested** relative to the main repo checkout (asserted in code).
3. `tmp_dir` is a distinct realpath-resolved directory that is **not** inside `worktree` and **not**
   inside any repo.

#### adapters.yaml → SandboxSpec mapping

`adapters.yaml` (DATA-MODEL §8) already declares each backend with `sandbox: strict` and
`prompt: stdin`. This ADR defines exactly how those fields (plus a few additions) become a
`SandboxSpec`:

```yaml
backends:
  qwen:   { argv: ["qwen", "--headless"], cwd: "{worktree}", prompt: stdin,
            sandbox: strict, network: none, timeout_s: 1200,
            read_roots: ["{venv}", "{interpreter}"] }      # Ollama inference is OUTSIDE the sandbox
  claude: { argv: ["claude", "--headless"], cwd: "{worktree}", prompt: stdin,
            sandbox: strict, network: none, timeout_s: 600,
            read_roots: ["{venv}", "{interpreter}"] }
  codex:  { argv: ["codex", "exec"], cwd: "{worktree}", prompt: stdin,
            sandbox: strict, network: none, timeout_s: 600,
            read_roots: ["{venv}", "{interpreter}"] }
```

Rules:
- `sandbox: strict` means "**OS BackendIsolation required, fail-closed**" — with no usable kernel
  sandbox and `allow_reduced_isolation=False`, the run raises `SandboxUnavailable`. `strict` may NOT
  be satisfied by the restricted-fallback unless the operator has separately opted in.
- Mechanism is **OS-driven** (`select_backend_isolation`); parameters are **backend-driven**
  (`read_roots`, `timeout_s`, `network`). `worker.py` resolves `{venv}` / `{interpreter}` /
  `{worktree}` placeholders, realpath-resolves the result, and fills the `SandboxSpec`.
- A backend requesting `network` other than `none` (only `localhost-ollama` is recognized) is
  **rejected** unless it is the documented future Ollama-inside-sandbox alternative; Phase-1 default
  backends are all `network: none`.
- `timeout_s` is per-backend (local Qwen defaults higher at 1200s; hosted CLIs 600s). On timeout the
  adapter sends SIGTERM, waits a 10s grace, then SIGKILL on the process group, then tears down the
  worktree + tmp.

### macOS Phase-1 mechanism — exact invocation

Pin the absolute binary so a tampered `PATH` cannot substitute it:

```python
SANDBOX_EXEC = "/usr/bin/sandbox-exec"   # pinned; verify exists+executable in available()
```

Invocation (`-p <policy>`, then `-D KEY=value` params, then `--`, then verbatim argv):

```python
cmd = [
    SANDBOX_EXEC,
    "-p", full_policy_text,                 # base.sbpl + dynamic read roots
    f"-DWORKTREE={spec.worktree}",          # realpath-resolved
    f"-DTMP={spec.tmp_dir}",                # realpath-resolved scratch OUTSIDE worktree
    f"-DMAIN_GIT={main_git_realpath}",      # realpath of main repo's .git (to deny)
    f"-DMAIN_KB={main_kb_realpath}",        # realpath of main repo's _kb (to deny)
    "--",
    *spec.argv,                             # backend command, appended verbatim, NO shell
]
subprocess.run(
    cmd,
    input=spec.stdin_data,                  # long prompt via stdin
    env=spec.env | {"HOME": str(spec.tmp_dir), "TMPDIR": str(spec.tmp_dir)},   # scratch OUTSIDE worktree
    cwd=spec.worktree,
    capture_output=True,
    timeout=spec.timeout_s,
    shell=False,                            # MANDATORY
)
```

- Read roots are injected as additional `-DREADABLE_ROOT_n=...` params AND a matching
  `(allow file-read* (subpath (param "READABLE_ROOT_n")))` line appended to the policy text — never
  string-interpolated into the policy body. (Phase-1 keeps a broad `(allow file-read*)`.)
- `WORKTREE` / `TMP` / `MAIN_GIT` / `MAIN_KB` / `READABLE_ROOT_n` are referenced inside the SBPL via
  `(param "KEY")`, never interpolated. `(string-append (param "WORKTREE") "/.git")` parses and matches
  correctly on this host.

### macOS deny-default SBPL profile — `profiles/base.sbpl`

```scheme
(version 1)

;; ── deny everything by default: this makes the profile an ALLOWLIST.
;;    A later (deny ...) ALWAYS wins over any (allow ...), regardless of order. [V: deny-wins]
(deny default)

;; ── process model: the backend is CPython and/or a CLI agent spawning children
(allow process-exec)
(allow process-fork)
(allow signal     (target same-sandbox))
(allow process-info* (target same-sandbox))

;; ── reads: Phase-1 broad read (tightened later). Runtime/model/interpreter.
(allow file-read*)

;; ── writes: DENY-DEFAULT. Only /dev/null + the worktree + the separate scratch are writable.
;;    /dev/null write is REQUIRED so Apple-shimmed CLIs (git via xcrun) don't fatally die. [V]
(allow file-write*
  (require-all (path "/dev/null") (vnode-type CHARACTER-DEVICE)))
(allow file-write* (subpath (param "WORKTREE")))
(allow file-write* (subpath (param "TMP")))      ;; scratch lives OUTSIDE the worktree

;; ── G4: lock the worktree's OWN .git POINTER FILE (a linked worktree's .git is a 64-byte
;;    "gitdir:" pointer file, NOT a directory). Without this the backend could repoint it
;;    at the main repo's objects/hooks. [V: write to this path returns EPERM]
(deny file-write* (path (string-append (param "WORKTREE") "/.git")))

;; ── G4/G5: deny the MAIN repo's operational paths by their REALPATH. The real hooks/config,
;;    .git/worktrees/<id>, and _kb/ live in the MAIN repo OUTSIDE {WORKTREE}; deny them
;;    explicitly so protection does not silently depend on sibling placement luck.
(deny file-write* (subpath (param "MAIN_GIT")))   ;; main/.git (objects, hooks, config, worktrees/*)
(deny file-write* (subpath (param "MAIN_KB")))    ;; main/_kb (inbox/processing/processed/state/lock)

;; ── CPython / PyTorch / libomp need POSIX sem + shm
(allow ipc-posix-sem)
(allow ipc-posix-shm)

;; ── safe sysctl reads only (Codex allowlist) — NOT a blanket sysctl-read
(allow sysctl-read
  (sysctl-name-prefix "hw.")
  (sysctl-name-prefix "kern.")
  (sysctl-name "net.routetable.0")   ;; route lookup; harmless with explicit network deny below
  (sysctl-name-prefix "kern.os"))

;; ── pseudo-tty so CLI backends that expect a tty don't crash
(allow file-read* file-write* (regex #"^/dev/ttys[0-9]+$"))
(allow file-read* file-write* (path "/dev/ptmx"))
(allow pseudo-tty)

;; ── minimal name lookups some runtimes do at startup. Each enumerated; none proxies egress:
;;    opendirectoryd.libinfo = local user/group (getpwuid) lookups, NOT a network resolver path.
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
(allow user-preference-read)   ;; cfprefs reads (no write); read-only, cannot egress

;; ── NETWORK: belt-and-suspenders. Default-deny already blocks egress, but add EXPLICIT denies
;;    that a future stray (allow ...) cannot accidentally override (deny-wins). DNS via
;;    mDNSResponder is also denied: with no network-outbound, getaddrinfo cannot reach the resolver.
(deny network*)        ;; [V: outbound TCP -> PermissionError EPERM with this in place]
(deny system-socket)   ;; [V]
```

**Dynamic tail** (appended by `seatbelt.py`, one line per read root):

```scheme
(allow file-read* (subpath (param "READABLE_ROOT_0")))   ;; e.g. the venv
(allow file-read* (subpath (param "READABLE_ROOT_1")))   ;; e.g. ~/.ollama if a backend reads models
```

**Read posture.** Phase-1 ships the broad `(allow file-read*)` above (matching Codex/Claude-Code
defaults). With G2 (no network) a broad read cannot leak. Phase-2 hardening (documented, not Phase-1)
replaces it with a deny-list for secret dirs, or a strict `READABLE_ROOT` allowlist (runtime + venv +
model dir only):

```scheme
(deny file-read* (subpath (string-append (param "HOME") "/.ssh")))
(deny file-read* (subpath (string-append (param "HOME") "/.aws")))
(deny file-read* (subpath (string-append (param "HOME") "/.config/gh")))
```

#### Proven behavior (verified on macOS 26.4, arm64, git Apple Git-155)

| Probe | Expected | Verified |
|-------|----------|----------|
| write `{WORKTREE}/inside.txt` (realpath `/private/tmp/...`) | succeeds | [V] WROTE_INSIDE |
| write `{TMP}/x` (separate scratch, outside worktree) | succeeds | [V] |
| write `~/agora-selftest-probe` (outside both mounts) | EPERM | [V] |
| write `{WORKTREE}/.git` (the pointer file) | EPERM | [V] `Operation not permitted` |
| write inside `{MAIN_GIT}` / `{MAIN_KB}` | EPERM (denied subpath) | [V] |
| `python3 socket.create_connection(("1.1.1.1",443))` | PermissionError EPERM | [V] |
| connect `127.0.0.1:11434` (host Ollama, reachable) | PermissionError EPERM (proves blocked, not unreachable) | [V] |
| Apple-shimmed `git --version` WITHOUT `/dev/null` write | FATAL: `could not open '/dev/null'` | [V] reproduced |
| Apple-shimmed `git --version` WITH `/dev/null` write + `TMPDIR`→scratch | succeeds (xcrun_db warns, non-fatal) | [V] `git version 2.50.1 (Apple Git-155)` |

The backend **never invokes git inside the sandbox** (G3). It only edits files; the orchestrator
commits + compare-and-swaps *outside* the sandbox (ADR-0008). The Apple-shim rows exist only because a
CLI backend may *incidentally* shell out to an Apple-shimmed tool during reasoning, which must not
fatally crash — not because git is part of the curation transaction.

### Realpath normalization + tmp_dir pinned OUTSIDE the worktree

**(a) Realpath.** `/tmp` is a symlink to `/private/tmp`. Seatbelt matches the **resolved vnode path**,
so a `(subpath (param "WORKTREE"))` keyed on an unresolved `/tmp/agora-...` path **silently fails to
grant the write** (EPERM with no policy error). Every path is resolved before use:

```python
worktree   = Path(worktree).resolve(strict=True)   # realpath; /tmp -> /private/tmp
tmp_dir    = Path(tmp_dir).resolve(strict=True)
read_roots = [p.resolve(strict=True) for p in read_roots]
main_git   = (main_repo / ".git").resolve(strict=True)
main_kb    = (main_repo / "_kb").resolve(strict=True)
```

**(b) tmp_dir is a SEPARATE directory OUTSIDE the worktree.** `HOME` / `TMPDIR` point at `tmp_dir` so
the backend's dotfiles, caches, and scratch land THERE — never inside the worktree. If scratch were
inside the worktree, every stray HOME/cache file would appear in the worktree diff and FAIL the
ADR-0008 allowlist on **every** real run. The ADR-0008 validator diffs ONLY the worktree's git-tracked
tree and NEVER inspects `tmp_dir`. Both `(allow file-write* (subpath (param "WORKTREE")))` and
`(allow file-write* (subpath (param "TMP")))` are present so the true two-writable-mount topology is
exercised — including by the self-test, which uses `tmp_dir != worktree`.

**HARD invariant — worktree MUST be non-nested relative to the main repo.** ADR-0008 step 2 does not
pin the worktree's location. If it were created *nested* inside the main repo, `{WORKTREE}` would
transitively include the main `.git/` and the backend would gain write access to objects/hooks/config.
Therefore `worker.py` MUST assert, before building any `SandboxSpec`:

```python
import os
wt   = os.path.realpath(worktree)
main = os.path.realpath(main_repo)
# neither may contain the other
assert os.path.commonpath([wt, main]) not in (wt, main), \
    "curator worktree must be created OUTSIDE / non-nested relative to the main repo"
```

Combined with the explicit denies of `MAIN_GIT` / `MAIN_KB` realpaths, G4/G5 protection is enforced by
the profile, not by luck of sibling placement.

**Required unit tests (all four):**
1. A worktree under a `/tmp`-symlinked path: a write-inside probe under the seatbelt adapter SUCCEEDS
   (guards the realpath bug). `[V: pattern reproduced]`
2. A linked-worktree backend CANNOT write `{WORKTREE}/.git` (its own pointer file). `[V]`
3. A linked-worktree backend CANNOT write `main/.git/hooks/pre-commit`,
   `main/.git/worktrees/<id>/`, or anything under `main/_kb`. `[V via MAIN_GIT/MAIN_KB deny]`
4. `worker.py` rejects (raises) a nested worktree path via the `commonpath` assertion.

### Linux plan — bubblewrap (+ optional Landlock/seccomp)

Primary network block is the **network namespace** (kernel-enforced, loopback-only), not a proxy.
`tmp_dir` is a SEPARATE rw-bind OUTSIDE the worktree, mirroring macOS.

```bash
bwrap \
  --unshare-all              `# == --unshare-user-try --unshare-ipc --unshare-pid --unshare-net --unshare-uts --unshare-cgroup-try` \
  --die-with-parent \
  --new-session              `# setsid; blocks TIOCSTI keystroke injection` \
  --clearenv \
  --setenv HOME "$TMP" --setenv TMPDIR "$TMP" --setenv PATH "/usr/bin:/bin" \
  --proc /proc --dev /dev --tmpfs /tmp \
  --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
  --ro-bind "$VENV" "$VENV"          `# interpreter + site-packages` \
  --ro-bind "$MODEL_DIR" "$MODEL_DIR" `# only if backend reads model files directly` \
  --bind "$WORKTREE" "$WORKTREE"     `# the only writable CONTENT mount` \
  --bind "$TMP" "$TMP"               `# separate writable scratch OUTSIDE the worktree (= HOME/TMPDIR)` \
  --chdir "$WORKTREE" \
  -- <argv...>                       `# verbatim, no shell`
```

- `--unshare-net` is the authoritative network deny (loopback-only netns).
- **G4/G5 on Linux is by OMISSION, cleaner than macOS:** the main repo's
  `.git/{config,hooks,worktrees}` and `_kb/` are simply **never bind-mounted**, so they do not exist
  in the sandbox namespace at all — no carve-out needed. We bind ONLY the worktree checkout dir and
  the separate `$TMP`. (A linked worktree's `.git` is a pointer file; if a backend rewrites it, the
  repointed target path does not exist in the namespace, so it resolves to nothing — and the post-hoc
  validator still rejects the modified pointer file as an out-of-allowlist change.)
- **Optional Landlock layer** (kernel ≥ 5.13): supplementary FS hardening via `LANDLOCK_ACCESS_FS_*`
  + `PR_SET_NO_NEW_PRIVS`. Landlock CANNOT fully block network (only port-level BIND/CONNECT_TCP at
  ABI v4), so `--unshare-net` remains the primary network block; Landlock is FS defense-in-depth only.
- **Optional seccomp layer:** compiled cBPF passed via `--seccomp FD`, blocking
  `connect`/`bind`/`socket`/`TIOCSTI` (exact filter open; ship as later hardening).

**Unprivileged-userns caveat (documented + detected):** Ubuntu 24.04+ and many containers set
`kernel.apparmor_restrict_unprivileged_userns=1`, blocking `bwrap`'s user namespace. Remediation = an
AppArmor profile granting userns to `/usr/bin/bwrap`. If unavailable, fall through to
restricted-fallback (opt-in only) or fail closed. The self-test detects this.

### Restricted-fallback (opt-in, fail-closed default)

For platforms with no usable kernel sandbox (native Windows; Linux with userns disabled and no remedy;
old kernels). It CANNOT kernel-block network or out-of-worktree writes during the run.

**What it STILL does** (transaction integrity does not depend on the OS sandbox):
- run the backend with `cwd = worktree`, `HOME` / `TMPDIR = tmp_dir` (outside the worktree);
- scrub env + withhold ALL credentials/tokens;
- pass prompts as argv/stdin (never shell);
- and the ADR-0008 deterministic post-run validator STILL runs (allowlist + reject
  symlinks/path-escapes/`_kb`/git-config/hooks) over the worktree tree before commit + CAS.

**What is LOST during the run (BOTH):**
1. **Network deny.** A malicious backend CAN reach the network and exfiltrate captured secrets. The
   post-hoc validator does NOT see network egress.
2. **Out-of-worktree WRITE confinement.** A malicious backend CAN write ANYWHERE the invoking user
   can — e.g. overwrite `~/.ssh/authorized_keys`, `~/.bashrc`, or another repo's files. The post-hoc
   validator only diffs the WORKTREE tree, so it will **NOT** catch direct out-of-worktree filesystem
   writes the backend performed.

**Therefore:**
- `config.curator.allow_reduced_isolation` defaults to **False** → selection raises
  `SandboxUnavailable` → run fails closed.
- A backend marked `sandbox: strict` in adapters.yaml is **never** satisfied by restricted-fallback
  unless this flag is explicitly set.
- When explicitly enabled, the run is **FORCED into review-mode** (publish to a branch/PR, never
  direct CAS) regardless of `repo.yaml review_mode`, so a human sees the diff before it can advance the
  curated branch. `reduced_isolation=True` is recorded in the manifest and overrides
  `review_mode: direct`.
- Recommended additional hardening (documented, operator-configurable): run the backend as a throwaway
  low-privilege OS user, or inside a container (Docker/Podman) where available, to recover *some* of
  the lost write/network confinement.
- Emit a **loud, repeated** warning that BOTH exfiltration AND out-of-worktree writes are NOT
  prevented, into `_kb/logs`, the run manifest (`reduced_isolation: true`), and `kb_status` output.
  The warning text MUST enumerate both lost guarantees, not only network.
- `SandboxResult.reduced_isolation = True` propagates so `worker.py` records it and forces review-mode.

**Best-effort Linux ordering:** `bwrap + --unshare-net (+Landlock)` → if userns blocked, surface
AppArmor remediation → else restricted-fallback (only if opt-in, forced review-mode) → else fail
closed.

#### Env scrubbing (all mechanisms)

Strip from `spec.env` BEFORE handing to any adapter:

```
ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, GITHUB_TOKEN, GH_TOKEN,
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, GOOGLE_APPLICATION_CREDENTIALS,
GIT_ASKPASS, SSH_AUTH_SOCK, and anything matching (?i)(token|secret|key|password|cred)
```

Set `HOME` / `TMPDIR` to the throwaway `tmp_dir` (outside the worktree). git commit + compare-and-swap
run in deterministic code OUTSIDE the sandbox — the backend only edits files and never invokes git nor
holds git credentials (ADR-0008).

### Runtime self-test (`agora doctor` / curator startup) + capability detection

Runs the **real** sandbox config against a throwaway worktree AND a separate throwaway tmp_dir before
any real run trusts the sandbox. Doubles as the platform-capability detector that picks
`{seatbelt | bwrap(+landlock) | restricted}`. Three hardenings: (a) the network assertion is
EPERM-specific and uses a REACHABLE target; (b) it runs an Apple-shimmed binary; (c) read_roots come
from the real configured backend.

```python
def self_test(isolation, throwaway_worktree: Path, throwaway_tmp: Path,
              backend_read_roots: list[Path]) -> SelfTestReport:
    wt  = throwaway_worktree.resolve(strict=True)
    tmp = throwaway_tmp.resolve(strict=True)          # DISTINCT dir, NOT inside wt
    assert tmp != wt and not str(tmp).startswith(str(wt) + "/")
    inside  = wt / "probe.txt"
    outside = Path("~/agora-selftest-probe").expanduser()   # OUTSIDE both mounts
    # Probe 1: filesystem + network, asserting EPERM specifically and using a REACHABLE target.
    probe_fs_net = textwrap.dedent(f'''
        import errno, socket, sys
        open({str(inside)!r}, "w").write("ok")                       # (a) inside worktree -> OK
        try:                                                          # (b) outside -> must be EPERM
            open({str(outside)!r}, "w").write("x"); print("OUTSIDE_WRITE_OK"); sys.exit(2)
        except PermissionError: pass
        # (c) network: target a REACHABLE host (the user's own Ollama 127.0.0.1:11434, listening)
        #     so "blocked" provably means EPERM, NOT "host unreachable / connection refused".
        try:
            socket.create_connection(("127.0.0.1", 11434), timeout=2); print("NET_OK"); sys.exit(3)
        except PermissionError: pass                                  # the ONLY accepted "denied"
        except OSError as e:
            if e.errno == errno.EPERM: pass
            else: print(f"NET_AMBIGUOUS:{{e.errno}}"); sys.exit(4)    # refused/unreachable != proven block
        print("SELFTEST_FS_NET_PASS")
    ''')
    spec1 = SandboxSpec(argv=[sys.executable, "-c", probe_fs_net], worktree=wt, tmp_dir=tmp,
                        read_roots=backend_read_roots, stdin_data=None, env=scrub(os.environ))
    r1 = isolation.run(spec1)

    # Probe 2 (macOS especially): run an Apple-shimmed binary to catch the xcrun_db/dev-null fatal.
    # Without (allow file-write* /dev/null) this FATALLY fails; with it, git succeeds (xcrun warns).
    spec2 = SandboxSpec(argv=["/usr/bin/git", "--version"], worktree=wt, tmp_dir=tmp,
                        read_roots=backend_read_roots, stdin_data=None, env=scrub(os.environ))
    r2 = isolation.run(spec2)

    return SelfTestReport(
        passed = (r1.returncode == 0 and b"SELFTEST_FS_NET_PASS" in r1.stdout
                  and r2.returncode == 0 and b"git version" in r2.stdout),
        write_inside_ok      = inside.exists(),
        write_outside_denied = b"OUTSIDE_WRITE_OK" not in r1.stdout,
        network_denied       = (b"NET_OK" not in r1.stdout and b"NET_AMBIGUOUS" not in r1.stdout),
        apple_shim_ok        = (r2.returncode == 0 and b"git version" in r2.stdout),
        mechanism            = isolation.name,
    )
```

Assertions that MUST hold: **write-inside OK**, **write-outside EPERM**, **outbound TCP to a reachable
target EPERM** (not merely "unreachable"), and on macOS **Apple-shimmed binary runs without the
dev/null fatal**.

- All pass → sandbox trustworthy; cache `{mechanism, OS build, sandbox-exec/bwrap mtime}` in
  `_kb/state.json`; re-run only when that key changes (resolves per-startup-vs-cache).
- `NET_AMBIGUOUS` (a non-EPERM OSError) → treat as NOT proven blocked → fail the self-test. This
  prevents the bug where "any OSError == denied" could pass on a host with no route.
- Any fail → sandbox NOT trustworthy → **fail closed** (default), unless `allow_reduced_isolation` →
  drop to restricted-fallback with the warning + forced review-mode.
- `[V]` On this host: write-inside OK; write-outside EPERM; `127.0.0.1:11434` (Ollama listening)
  connect → PermissionError EPERM; `git --version` fatal without `/dev/null` write, succeeds with it.

`agora doctor` prints the report (mechanism, the four assertions, and the reduced-isolation flag) so
operators verify isolation rather than assume it.

### Prompt / path passing (no shell — ever)

| Datum | How it is passed | Never |
|-------|------------------|-------|
| backend command | `subprocess.run([...], shell=False)`; appended verbatim after `--` | `sh -c "..."`, string concat |
| short prompt | one argv element (`qwen -p <prompt>` / `claude -p` / `codex exec`) | concatenated into a shell line |
| long prompt | **stdin** (`input=spec.stdin_data`) | argv length limits / shell heredoc |
| inbox item bodies | **read-only files inside the worktree**; their paths passed as argv elements | mounting them writable; interpolating content |
| worktree / tmp / read roots / main-git / main-kb | `-D KEY=value` params (macOS) / `--bind`/omission (Linux), referenced via `(param "KEY")` | string-interpolated into the SBPL/bwrap text |

Captured content is treated as hostile input, never as code.

### Ollama / local-model boundary

A strict no-network sandbox blocks even loopback to the host Ollama daemon (`127.0.0.1:11434`) — `[V]`
confirmed EPERM with the explicit deny in place. Resolution consistent with ADR-0008:

- **PREFERRED (Phase-1 default, `network: none`):** do the LLM inference **outside the sandbox**. The
  deterministic orchestrator (`worker.py` / `backends.py`) performs model I/O (prompt in → proposed
  edits/diff out). Only the **file-writing / tool-execution** step runs inside the sandbox, applying +
  validating the model's proposed edits. This keeps the sandbox network-free and the default Ollama
  brain working with zero exceptions. (On this host: ollama at `/opt/homebrew/bin/ollama`, models in
  `~/.ollama`, daemon listening on `127.0.0.1:11434`.) Because the in-sandbox `network: none` posture
  blocks loopback, this outside-the-sandbox path is the ONLY way the Phase-1 default backend reaches
  the model — by design.
- **ALTERNATIVE (`network: localhost-ollama`, only if a backend CLI must run sandboxed AND call the
  local model):** grant a narrowly-scoped exception to ONLY the local Ollama endpoint and nothing
  else — macOS: a localhost-only network allow scoped to `127.0.0.1:11434`; Linux: bind-mount the
  daemon's unix socket while keeping `--unshare-net` for all external egress (Ollama defaults to TCP,
  so a unix-socket bridge or localhost-only allow is required). Still NO external network, NO API
  credentials. Represented in adapters.yaml via the backend's `network: localhost-ollama` field and in
  `SandboxSpec.network`; rejected for any backend not explicitly marked. Documented as future work,
  not Phase-1 default.

Read paths the backend needs (covered by the Phase-1 broad read, or scoped `READABLE_ROOT` later):
interpreter + venv (`/usr/bin`, `/opt/homebrew`, the venv) and, for the alternative, `~/.ollama`.

### OSS-license posture (satisfies invariant #4)

| Dependency | License | How used | Copyleft into core? |
|------------|---------|----------|---------------------|
| `sandbox-exec` / Seatbelt (macOS) | Apple OS facility | invoked as `/usr/bin/sandbox-exec` subprocess | No (OS facility) |
| `bubblewrap` (`bwrap`) | LGPL-2.0 | invoked as a **separate executable subprocess** (never linked) — same posture as calling `/usr/bin/git` | **No** — weak copyleft AND not linked |
| `socat` (only if a future unix-socket Ollama bridge is used) | GPL-2.0 | separate executable subprocess | No (separate process, not linked) |
| Landlock / seccomp (Linux) | Linux OS facility | syscalls via the OS | No |
| OpenAI Codex SBPL policy + invocation pattern | Apache-2.0 | reference / reimplementation guide | No (permissive) |
| `@anthropic-ai/sandbox-runtime` | Apache-2.0 | reference | No (permissive) |

Declare `bubblewrap` (and optional `socat`) as optional SYSTEM prerequisites (like `git`), not
vendored Python deps. The "LGPL-via-subprocess → no obligation attaches to the Python core" reasoning
is recorded here and in ADR-0008. Phase-1 default stack = `sandbox-exec` (macOS) + `bwrap` (Linux),
both subprocesses; all orchestration/validation in permissively-licensed Python. Invariant #4
satisfied with zero AGPL/copyleft exposure.

### Implementation checklist (Phase-1, macOS first)

1. `isolation/base.py` — `BackendIsolation` Protocol + `SandboxSpec` / `SandboxResult` /
   `SelfTestReport` dataclasses (incl. `network`, per-backend `timeout_s`).
2. `isolation/profiles/base.sbpl` — the profile above verbatim (incl. `/dev/null` write, worktree
   `.git` pointer deny, `MAIN_GIT` / `MAIN_KB` denies, explicit `(deny network*)` /
   `(deny system-socket)`).
3. `isolation/seatbelt.py` — `available()` (pin + stat `/usr/bin/sandbox-exec`), `run()` (the
   invocation above, inject `MAIN_GIT` / `MAIN_KB`, append read roots), realpath-assert, HOME/TMPDIR →
   tmp_dir.
4. `isolation/selftest.py` — the hardened probes (EPERM-specific; reachable-target `127.0.0.1:11434`;
   Apple-shim git run; per-backend read_roots); capability detection; `_kb/state.json` cache keyed by
   `{mechanism, OS build, binary mtime}`; `agora doctor` output.
5. `isolation/restricted.py` — the fallback, default-off, FORCES review-mode, loud warning enumerating
   BOTH lost guarantees (network + out-of-worktree writes) into logs + manifest + kb_status.
6. `isolation/__init__.py` — `select_backend_isolation()` (selection order, fail-closed unless
   opt-in); maps adapters.yaml backend → `SandboxSpec` params.
7. `worker.py` wiring — assert non-nested worktree via `os.path.commonpath`; create tmp_dir as a
   DISTINCT throwaway OUTSIDE the worktree; realpath all paths; scrub env; build `SandboxSpec`; call
   `isolation.run()`; keep commit + CAS OUTSIDE sandbox; validator diffs ONLY the worktree tree, never
   tmp_dir; on timeout SIGTERM → 10s grace → SIGKILL process group → teardown worktree + tmp.
8. Tests: `/tmp`-symlink worktree write-inside SUCCEEDS; backend CANNOT write `{WORKTREE}/.git`
   pointer file; backend CANNOT write `main/.git/hooks`, `main/.git/worktrees/<id>`, or `main/_kb`;
   `worker.py` raises on a nested worktree path; tmp_dir scratch is writable and NOT in the worktree
   diff; write-outside EPERM; outbound TCP to reachable `127.0.0.1:11434` EPERM; Apple-shimmed
   `git --version` runs under the sandbox; env-scrub strips creds; argv passing never invokes a shell;
   selection fails closed when no sandbox + `allow_reduced_isolation=False`, and `sandbox: strict` is
   not satisfied by restricted-fallback unless opted-in; restricted-fallback sets
   `reduced_isolation=True`, FORCES review-mode, and emits the two-guarantee warning.
9. `bwrap.py` (Linux) — the bwrap plan (separate `$TMP` rw-bind; main `.git`/`_kb` never bound) + the
   AppArmor-userns doctor remediation message (can follow macOS).

### Open questions
- **Read-path tightening timing:** ship the broad `(allow file-read*)` for Phase-1 and add the
  `~/.ssh` / `~/.aws` deny-list (or `READABLE_ROOT` allowlist) in Phase 2, or harden reads from day
  one? This ADR assumes broad-reads-Phase-1; needs product sign-off.
- **Windows posture:** native Windows has no seatbelt/bwrap equivalent. Is Phase-1 "WSL2 only (bwrap
  there)" or "native Windows = restricted-fallback (opt-in, forced review-mode) with warning"? Affects
  whether a future Windows AppContainer/Job-Object adapter is documented.
- **Exact seccomp filter contents** for the Linux optional layer (which syscalls beyond
  connect/bind/socket/TIOCSTI) — left as optional hardening; needs a concrete cBPF allowlist first.
- **Future `network: localhost-ollama` transport:** standardize on a unix-socket bridge (socat,
  GPL-2.0 subprocess) vs a narrowly-scoped localhost network allow scoped to `127.0.0.1:11434` — both
  documented; a default should be chosen if/when that backend type is introduced.
- **Self-test fidelity:** should `agora doctor`'s Apple-shim probe run the configured backend's REAL
  launcher (codex/claude/qwen) rather than `git --version` as a proxy, to also validate the backend's
  own read_roots and startup? This ADR uses git as a cheap, always-present stand-in.
- **Restricted-mode extra hardening** (throwaway low-priv OS user / container) is recommended but not
  specified — should Phase-1 ship a concrete Podman/Docker wrapper, or leave it as operator guidance
  with only the forced-review-mode safeguard in code?

## Consequences
- **+** ADR-0008's "OS sandbox, worktree-only writable mount, no network, no credentials/hooks/spool"
  becomes a concrete, empirically-verified mechanism rather than a promise.
- **+** Defense-in-depth: the kernel sandbox (this ADR) and the deterministic post-run validator
  (ADR-0008) are independent layers, so a gap in one does not breach G1.
- **+** G4/G5 are enforced by the profile (worktree `.git` pointer deny + `MAIN_GIT`/`MAIN_KB` realpath
  denies on macOS; bind-mount omission on Linux) plus a hard non-nested-worktree assertion — not by
  fragile sibling-placement luck.
- **+** The default local Qwen-via-Ollama brain keeps working at zero API cost (invariant #4) because
  inference runs outside the sandbox while only file/tool execution is confined.
- **+** Swappable `BackendIsolation` adapter keeps the mechanism OS-driven and the parameters
  backend-driven (invariant #6), with `adapters.yaml` as the single source for `read_roots` /
  `timeout_s` / `network`.
- **+** A trustworthy self-test (EPERM-specific, reachable-target, Apple-shim) lets `agora doctor`
  *prove* isolation, and its cached result avoids per-startup overhead.
- **+** OSS-clean: every facility is an OS feature or a separate subprocess (LGPL `bwrap` / GPL
  `socat` never linked), so no copyleft attaches to the permissively-licensed core.
- **−** macOS relies on the deprecated `sandbox-exec`; no non-deprecated replacement exists for
  applying Seatbelt to unsigned subprocesses. Mitigated by pinning the absolute path and hiding it
  behind the swappable adapter (Codex/Claude Code/Chromium still rely on it).
- **−** Phase-1 keeps `file-read*` broad, so the backend can read arbitrary local files (e.g. `~/.ssh`)
  during a run; acceptable only because G2 (no network) means a read alone cannot leak. Read-hardening
  is deferred.
- **−** Keeping Ollama inference outside the sandbox means the model-I/O step is not itself sandboxed
  (it is deterministic orchestrator code doing prompt-in/edits-out and runs no untrusted execution).
- **−** The `/dev/null` write-allow is a minor write-surface expansion, accepted because the device
  discards all writes (no persistence/exfiltration) and Apple-shimmed git fatally crashes without it.
- **−** Restricted-fallback cannot kernel-prevent network egress OR out-of-worktree writes during the
  run, and the post-hoc validator catches neither; mitigated by strict opt-in, default fail-closed,
  forced review-mode/PR, a recommended low-priv user/container, and a loud warning enumerating both
  lost guarantees in logs + manifest + kb_status.
- **−** Pinning tmp_dir outside the worktree adds a second writable mount and throwaway dir per run;
  accepted because scratch inside the worktree would fail ADR-0008 allowlist validation on stray
  HOME/cache files on every real run.
- **−** The non-nested-worktree invariant constrains where `worker.py` may place the temp worktree (a
  sibling/external path, never a repo subdir); cheap, and it makes G4/G5 explicit rather than
  placement-dependent.
- **−** Linux `bwrap` depends on unprivileged user namespaces, which Ubuntu 24.04+ / hardened hosts
  disable by default; mitigated by self-test detection + an AppArmor-profile remediation message, with
  fail-closed or opt-in restricted-fallback otherwise.
- **−** Temporary worktrees + per-run sandbox setup add disk and process-spawn overhead (already noted
  in ADR-0008); acceptable because curation is batch/background, and the self-test result is cached in
  `_kb/state.json` keyed by `{mechanism, OS build, binary mtime}`.
