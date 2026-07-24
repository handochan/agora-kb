# ADR-0036 — AuthN/AuthZ: Forgejo-delegated identity + repo-boundary authorization

<!-- Numbering: docs/adr/README.md reserves 0028–0035 for earlier evidence-triggered spines, so
     the next free number is 0036. This assignment is made by #69 and is INDEPENDENT of the #55
     decision-1 number allocations (query-v2 / typed relations / usage records / security classes
     receive their own later numbers when authored — none of them claims 0036). -->

**Status:** Proposed · 2026-07-25 (#69 — the Phase-4 gate ADR; authored before any Phase-4 code)

Decides how Agora authenticates callers and authorizes their reads/writes when the system goes
multi-tenant and networked (Phases 4–5). Materializes the caller → (repos, roles, domains)
delegation model of [ADR-0006](0006-repo-as-tenant-boundary.md); adopts the #55 strategy-review
security conclusion (repo boundary = the only real wall) normatively; supersedes the transitional
perimeter mechanics of [`docs/DEPLOY-TEAM.md`](../DEPLOY-TEAM.md) to the extent enumerated in this
ADR's §4 table (and only there). Bound by [ADR-0001](0001-markdown-git-source-of-truth.md) (secrets never in the SSOT),
[ADR-0002](0002-cqrs-single-writer-curator.md)/[ADR-0003](0003-one-core-many-faces.md) (auth is
enforced at the core boundary so every face inherits it; auth decides *who* writes, never *how*),
[ADR-0005](0005-fully-oss-bom.md) (OSS path for every auth component), and
[ADR-0006](0006-repo-as-tenant-boundary.md) (repo = tenant). Phase-4-coupled consumers: reserved
ADR-0030 (federation scope profiles), #28 team-scope connectors, the ADR-0022 team
`taxonomy_policy` default, and the #51 web control-plane stages 2–3 — all block on this decision.

## Context

**The gap.** AuthN/authz is the gate to Phases 4 and 5, yet it has had **zero dedicated ADRs**:
"Forgejo delegation → OpenFGA 2-tier" exists only as prose (DESIGN §7, ARCHITECTURE §2/§4), and
`src/agora_kb/auth/` is a two-line-docstring stub (`identity.py`/`policy.py` named but unwritten).
Every other load-bearing choice in Phases 1–3 got an ADR first; this one accumulated as design
debt while the Phase-4-coupled spine (ADR-0030, #28, governed CREATE_DOMAIN team defaults,
#51 stages 2–3) piled up behind it. Issue #69 tracks closing that debt — this document.

**What exists today (all identity is deployment-supplied, none app-verified).** The application
itself has no network auth. The pre-Phase-4 hub topology ([DEPLOY-TEAM.md](../DEPLOY-TEAM.md),
#68) supplies identity at the perimeter:

- **Web:** the face binds `127.0.0.1` only; an authenticating reverse proxy (Caddy/nginx basic
  auth + TLS) fronts it and **force-sets** `X-Remote-User`; the opt-in
  `web.identity.trusted_header` (#67) threads that username into a per-user `web:<user>` writer
  namespace + provenance. The proxy also path-blocks `/metrics`, `/dashboard`, `/api/dashboard`
  (operator-internal).
- **MCP:** stdio-over-SSH with **forced-command** keys — the server pins argv and `--writer` per
  key (`restrict`). DEPLOY-TEAM states plainly that this identity is **provenance-level**, not
  OS-level (all keys land on one OS account; one sshd misconfig = full-FS exposure), and that
  `kb_curate` (with `force`) is exposed to every key-holder with no rate limit.
- **Git reads:** a push-only mirror (`agora sync`, #64); read access is the hosting's own
  permission. The MCP write channel and the git read channel are deliberately separate.

**Tenancy today is process topology, not adjudication.** One process serves one repo
(`build_app(repo_path)`; `agora serve --repo`), so invariant #5 has been *inherited* from
deployment shape — no code ever asks "may this caller touch this repo?". Phase 4's shared
network endpoint serving multiple repos makes tenancy a **per-request decision**. Auth is
therefore the first component that will *enforce* invariant #5 rather than inherit it.

**The #55 security conclusion (adopted here, not merely cited).** The 2026-07 strategy review
(issue #55, security axis) concluded: *the only real wall is the repo boundary.* A domain-level
ACL inside one repo leaks through ≥10 channels, of which at least three are unblockable by any
server code — **(1)** the gold-pack file-`@include` (`@<repo>/_kb/gold/default.md` is assembled
from the whole repo and read by the local filesystem, ADR-0027), **(2)** `git clone` of the
mirror (whole-repo bytes leave together), and **(3)** the `_kb/` spool on the hub host (inbox
events readable to anyone with host access). The review's verdict: domain ACL must be demoted to
a **"convenience filter"** and note labels to **"hygiene"** — and per-note ACL sits on the #55
**permanent-rejection list**. This ADR accepts that verdict head-on rather than restating the
DESIGN §7 aspiration ("optionally per-domain ACL") that predates it.

**Transport landscape (external constraint).** The MCP specification ties authorization to
transport: HTTP-based transports (Streamable HTTP) use the MCP authorization spec, which is
**OAuth 2.1-based** (the 2025-06-18 revision classifies MCP servers as OAuth protected resources
with RFC 8707 resource indicators); **stdio transports are explicitly out of its scope** —
credentials come from the environment/process. This cleanly matches Agora's split: stdio =
process identity (unchanged forever), HTTP = token now, OAuth 2.1 later.

**License facts (verified 2026-07-25).** Forgejo is GPL-3.0-or-later from v9.0 (2024-10;
≤v8 remains MIT); Gitea remains MIT and is API-compatible. OpenFGA is Apache-2.0 (CNCF
incubating, Zanzibar-style ReBAC). Keycloak/Ory are Apache-2.0; Authentik core is MIT. All are
**external services reached over HTTP** — never linked into the core library — so invariant #4's
"no copyleft deps in the core library" is untouched by any of them (the same posture as the
Grafana AGPL sidecar, DESIGN §8).

## Proposed decision

The recommended outcome is **Adopt** (with the §OD sub-decisions ratified by the user).

### 1. AuthN — Phase 4 delegates identity to the git host; Agora issues no tokens

- **Identity source of record = Forgejo** (Gitea API-compatible; the same delegation posture as
  ADR-0006 tier 1: "repos, teams, roles, and PRs already exist there — reuse, don't reinvent").
  A caller authenticates to any Agora network surface with a **Forgejo personal access token**
  presented as `Authorization: token <PAT>`. Agora verifies it against the Forgejo API
  (`GET /api/v1/user` → the caller's login) and never sees a password.
- **Token model.** Issuance, lifetime, rotation, and revocation are **Forgejo-side** (scoped
  PATs; recommended minimal scopes: `read:user` + `read:repository` — **even writers present a
  read-only PAT**: the write role is *read out of* the `permissions` object (decision 2), and
  every authorized write flows through the inbox, never through the caller's token (invariant #2),
  so the PAT never needs `write:repository`; a write-scoped PAT would only widen blast radius by
  handing Agora a token that can push directly to the host). Revocation is immediate at the source;
  Agora bounds its own staleness with a short in-memory verification-cache TTL (default 60 s — a
  revoked token, **or a downgraded/removed repo permission (decision 2)**, reflects within one TTL).
- **Storage discipline (normative).** A token, password hash, or session secret **never lands in
  a KB repo** — not in `wiki/`, not in `_meta/`, and not even in git-ignored `_kb/` (the spool
  is per-repo and disk-persisted; auth state is caller-scoped service state, not repo state).
  Server-side verification results live in process memory only. Client-side, the token lives in
  the agent/MCP client config or environment, outside every repo — the same separation
  DEPLOY-TEAM §5 already demands for the direct write path (secrets in git history are
  unremovable; ADR-0031 right-to-delete is unwritten). This is the markdown-SSOT invariant
  applied to credentials: the repo carries knowledge, never the keys to it.
- **Stage-transition criteria (what observed → move).**
  - *Stay in Phase 4 (PAT delegation)* while every caller is a known teammate on a private
    network (Tailscale or equivalent) and the deployment already runs a git host for the mirror.
  - *Trigger the self-issued-token fallback (§OD-1 B)* only if a real deployment refuses to run
    any git host — recorded as a fallback, not a parallel default.
  - *Trigger Phase 5 (OAuth 2.1 + PKCE)* when any of: third-party or browser-based MCP clients
    must connect; the endpoint leaves the private network; or multi-org federation (ADR-0030)
    needs consent screens/audience separation. Phase 5 uses an OSS IdP (Keycloak / Authentik /
    Ory — DESIGN §8) or Forgejo's own OIDC provider capability as a middle step, aligned with
    the MCP authorization spec (OAuth 2.1 + PKCE; RFC 8707 resource indicators per repo).

### 2. AuthZ — repo permission is the ONLY security boundary; Forgejo repo-permission mirror

- **Materialization of ADR-0006's caller → (repos, roles, domains).** Phase 4 resolves the
  caller's role on a repo by **mirroring the Forgejo repo permission** (the `permissions`
  object of `GET /api/v1/repos/{owner}/{repo}`):

  | Forgejo permission | Agora role | ADR-0006 name | Grants |
  |---|---|---|---|
  | `pull` | **reader** | reader | `kb_query`/`kb_read`/`kb_neighbors`/`kb_context`, web read API, graph, gold |
  | `push` | **writer** | editor | reader + `kb_remember`/upload → that repo's inbox (writer ns = verified login) |
  | `admin` | **curator-admin** | owner | writer + `kb_curate`, config/control surfaces (#51 stages 2–3) |

  Team/org membership comes free from Forgejo teams. The role names refine ADR-0006's
  `owner > editor > reader` into operation-shaped names; the lattice is identical.
- **The `push` bit is dual-use — the read-distribution mirror MUST protect its curated
  branch (normative).** A Forgejo `push` permission grants both the Agora *writer* role (above,
  read out of the `permissions` object) **and** raw `git push` to that repo. Since the same
  Forgejo repo is the read-distribution mirror (`agora sync` target; team members `pull --ff-only`),
  an unguarded mirror lets a writer `git push` curator-bypassed bytes straight onto the curated
  branch — replicated team-wide, defeating invariant #2 (all writes via inbox → curator). So the
  mirror's curated branch is a **protected branch** whose only writer is the hub's `agora sync`
  deploy key; human `push` holders map to the Agora writer role but cannot write the curated
  branch directly. Their writes flow `kb_remember`/upload → inbox → curator → hub → sync. This is
  the git-layer counterpart of "faces never write the wiki" (ADR-0002) and is a hard precondition
  of the same-host mirror posture, not an optional hardening.
- **The #55 demotion, adopted normatively.** **Repo-level permission is promoted to the sole
  security boundary.** Domain-level ACL is a **convenience filter** (read-scope narrowing for
  UI/query ergonomics — never sold as confidentiality), and note-level labels are **hygiene**.
  Rationale restated concretely: the gold `@include`, `git clone`, and the `_kb/` spool all move
  whole-repo bytes past any server-side domain check by construction. Consequence rule:
  **data whose audience differs belongs in a different repo** (ADR-0006), never behind a domain
  filter. The `domains` member of the ADR-0006 tuple is retained as *filter input only*.
- **Phase-5 OpenFGA 2-tier transition — evidence triggers, not calendar.** Move authorization
  resolution behind OpenFGA when any of:
  - **(a)** authorization questions become relationship queries the per-repo mirror cannot
    answer efficiently — e.g. "list every repo/pack this caller may read" across many repos for
    ADR-0030 scope-profile composition (reverse-index queries, not per-repo checks);
  - **(b)** a second identity/hosting backend must coexist (policy must outlive one host's ACL
    model);
  - **(c)** measured administration failure of per-repo Forgejo permissions at team scale
    (delegation churn, orphaned grants).
  Even under OpenFGA, domain rules **stay convenience filters** — the #55 demotion survives the
  engine swap, because the three unblockable channels are structural, not implementational.

### 3. Surface-by-surface application

| Surface | Today (pre-Phase-4) | Phase 4 | Phase 5 |
|---|---|---|---|
| Web JSON API + UI + upload | loopback bind + proxy basic auth + `trusted_header` | **app-level token verification** (FastAPI dependency → core); write stamps `web:<login>` | OIDC session / OAuth bearer |
| `/metrics` · `/dashboard` · `/api/dashboard` | proxy path-block (403) | stay operator-internal: hub-local or curator-admin-gated (§OD-4) | unchanged |
| MCP **stdio** | process identity (forced-command `--writer`) | **no change, ever** — stdio is out of the MCP auth spec's scope; process identity *is* the credential | unchanged |
| MCP **Streamable HTTP** | not shipped | **never ships without token auth** — transport + verification are coupled: the two Phase-4 ROADMAP bullets (Streamable HTTP + this auth) land together; Forgejo PAT over private network | MCP-spec OAuth 2.1 + PKCE |
| HTTP inbox API (V12, future) | not shipped | **born inside** the same token verification; writer ns = verified login; no anonymous write path is ever exposed | same, OAuth-carried |
| Git read mirror | hosting read permission | delegated to hosting ACL (already the boundary — stays so). **One-knob rule** (holds only when the mirror is hosted on the identity Forgejo): the mirror's read bit and Agora's reader role derive from one permission — a split mirror host (e.g. GitHub private, DEPLOY-TEAM §4) forfeits the single knob (see Consequences) | unchanged |

Enforcement point: per ADR-0003/DESIGN §2.1 and the ARCHITECTURE §3.1/§3.5 flow diagrams
(`auth: caller may write target repo? (auth/policy)`), verification is consulted at the **core
boundary** (`core.write`/`core.read`), faces only extract and forward the credential — so every
tenant-data face, present and future, inherits the same adjudication. The operator surfaces
(`/metrics` · `/dashboard` · `/api/dashboard`) are the deliberate exception: under §OD-4 A they
take **no** core-boundary auth in Phase 4 and lean on the proxy path-block alone, so a lost
path-block or direct host access exposes their operator/KB-derived aggregations without app auth
(until §OD-4 flips to a `curator-admin` gate). Fail-closed: an unresolvable caller, token, or
repo → refuse.

### 4. Hub-topology transition — what is temporary, what carries

Migration of each [DEPLOY-TEAM.md](../DEPLOY-TEAM.md) mechanism (this table is the §4
supersession scope referenced in the header):

| Mechanism | Today | Phase 4 | Phase 5 | Verdict |
|---|---|---|---|---|
| Reverse-proxy **basic auth** | the only web authn | superseded by app-level token verification; proxy stays for TLS (+ optional defense-in-depth basic auth) | proxy = TLS only (or oauth2-proxy) | **temporary** |
| **`web.identity.trusted_header`** (#67) | the only per-user web identity | the *seam carries, the trust moves*: default = app-verified identity; header mode remains a supported config surface for SSO-proxy deployments (precedence: §OD-3) | header mode = oauth2-proxy-style deployments stay legitimate | **config surface carries; trust source replaced** |
| **SSH forced-command** (per-key `--writer`) | the only remote MCP write path | **survives** — it *is* MCP stdio with process identity, fully supported; recommended path becomes Streamable HTTP + token once deployed for that team | supported fallback (e.g. no-inbound-HTTP environments) | **carries** (not deprecated by this ADR) |
| `agora sync` mirror + hosting read ACL | read distribution | carries unchanged — and becomes more central (same Forgejo = identity + authz + mirror + Phase-5 PR review) | unchanged | **carries** |
| Loopback bind (`127.0.0.1`) | hard rule | **carries** — app-level auth does not license direct exposure; TLS termination stays at the proxy | unchanged | **carries** |
| Proxy path-block of `/metrics` etc. | hard rule | carries (or curator-admin gate, §OD-4) | unchanged | **carries** |

### 5. Invariant conformance

1. **Markdown+git SSOT (#1).** No credential, hash, or session state in any KB repo (decision 1
   storage discipline). Auth adds **no** canonical store: Phase 4's only state is an in-memory
   cache; Phase 5's IdP/OpenFGA are external services whose data is theirs, not the KB's.
2. **All writes via the inbox (#2).** Auth decides *who may write*, never *how writes happen* —
   every authorized write still lands as an inbox append; no face gains a `wiki/` write; the
   single-writer curator is untouched.
3. **Append-only inbox (#3).** Identity only stamps provenance (writer namespace,
   `source: web:<login>` / per-key `--writer`); nothing edits or reorders inbox items.
4. **OSS path (#4).** Forgejo GPL-3.0-or-later (v9.0+; ≤v8 MIT) and Gitea (MIT) are external
   services over HTTP — never linked into the core, so the no-copyleft-in-core rule holds;
   OpenFGA Apache-2.0; Keycloak/Ory Apache-2.0, Authentik MIT. Proprietary hosts
   (GitHub/GitLab) can slot behind the same delegation seam later as optional adapters
   (ADR-0004/0005), out of scope here.
5. **Tenant isolation (#5).** Auth is the **first component to enforce isolation** rather than
   inherit it: today process = tenant (one process, one repo); Phase 4 makes request = tenant
   adjudication at the core boundary *before* any repo path is resolved. Structural isolation
   (separate git repos, ADR-0006) remains the backstop beneath the adjudication.
6. **Tool-agnostic (#6).** A bearer token in a header works for every MCP/HTTP client; nothing
   is agent-specific. The identity source itself is a seam (`auth/identity.py` /
   `auth/policy.py`): Forgejo first, Gitea API-compatible; the policy resolver must not
   hard-code one host.

## Alternatives considered

- **Own full-stack IdP (Agora-issued accounts/passwords/sessions) — rejected.** Reinvents
  Keycloak inside a knowledge tool, adds credential storage and password-handling liability to a
  codebase whose SSOT discipline exists to *avoid* holding secrets, and contradicts ADR-0006's
  founding posture ("reuse, not reinvent, access control").
- **Tokens/ACL files inside the KB repo ("JWT-in-repo") — rejected hard.** Secrets in git
  history are unremovable without history rewrite (DEPLOY-TEAM §5; ADR-0031 right-to-delete is
  unwritten), the mirror push replicates them team-wide, and invariant #1 makes the repo a
  knowledge SSOT, never a credential store. Even git-ignored `_kb/` is out (decision 1).
- **Per-note ACL — permanently rejected** (#55 permanent-rejection list; re-litigation barred
  there). **Per-domain ACL as a *security* boundary — rejected on the same evidence**: the gold
  `@include`, `git clone`, and `_kb/` spool channels are unblockable by server code; a domain
  ACL that claims confidentiality is a false promise. Demoted to convenience filter
  (decision 2), which is a *feature we keep*, honestly labeled.
- **Continuing no-auth network exposure (perimeter-only, indefinitely) — rejected.** The
  DEPLOY-TEAM perimeter is explicitly transitional: identity is provenance-level, `kb_curate`
  (+`force`) is open to every key-holder without rate limit, and one proxy/sshd misconfig
  equals full-filesystem exposure — acceptable for 2–10 trusted people as documented, not as
  the end state.
- **OpenFGA-first (skip the Forgejo mirror) — rejected for Phase 4.** A second stateful service
  and a bespoke policy model with zero multi-tenant users repeats the mistake #55 warns about
  (writing the federation constitution before users exist). The mirror needs no new store,
  covers a small team, and OpenFGA has concrete evidence triggers (decision 2).
- **Tailscale/network identity as the auth story — rejected as sufficient.** Network-layer
  identity hardens transport (and stays recommended for Phase 4) but does not answer *which
  caller, which role, which repo*; it composes with, never replaces, token verification.

## Consequences

- **+** The Phase-4-coupled spine unblocks: ADR-0030 scope profiles get a caller→readable-repos
  resolver to compose against; #28 team-scope connectors get a real team-repo write boundary;
  ADR-0022's repo-kind-aware `taxonomy_policy` default gets an enforced notion of "team repo";
  #51 stages 2–3 get a `curator-admin` role for privileged control surfaces.
- **+** One knob for a small team: the same Forgejo instance supplies identity (PATs), authz
  (repo permissions), read distribution (mirror), and later PR review — "run Forgejo + run
  Agora" stays the whole ops story.
- **+** The security story becomes honest: repo boundary promoted to the wall, domain filter and
  labels demoted to convenience/hygiene — documentation stops overpromising what server code
  can enforce (#55 adopted, not deferred).
- **+** stdio users (Phase 1–3 solo posture) are untouched forever; the MCP auth spec's own
  stdio carve-out confirms process identity as the right model there.
- **−** A networked deployment now requires a Forgejo/Gitea (or the §OD-1 B fallback). Solo and
  local-only users are unaffected.
- **−** Verification adds a network hop to the git host (mitigated by the TTL cache; the cache
  bounds revocation **and permission-change** staleness to one TTL — an explicit, small trade).
- **−** The `push` permission is **dual-use**: it names the Agora writer role *and* grants raw
  `git push` to the mirror repo. The §2 protected-branch premise (hub deploy key is the curated
  branch's only writer) is what keeps a writer from bypassing the curator at the git layer; if an
  operator misconfigures the mirror without branch protection, invariant #2 is defeated out-of-band
  (curator-bypassed bytes replicate via `pull --ff-only`) with no Agora-side signal. `agora doctor`
  cannot see the remote's branch-protection state — this is an operator obligation the deploy guide
  must check, not something server code enforces.
- **−** A **split mirror host** (mirror on a *different* system than the identity Forgejo — e.g.
  GitHub private, which DEPLOY-TEAM §4 permits) breaks the one-knob rule: revoking a caller's
  permission on the identity Forgejo does not revoke their clone/read on the mirror host, opening a
  revocation-mismatch window the identity host cannot close (and already-cloned bytes are
  unrecoverable — channel 2). Same-host Forgejo (the recommended posture) avoids it.
- **−** A **bearer PAT has no holder-binding** (no mTLS/DPoP): a token leaked from client
  config/env/logs, or reused by an insider, impersonates the caller until *manual* revocation. The
  TTL bounds revocation *propagation*, not the theft→detection window, and there is no
  rate-limit/anomaly signal (DEPLOY-TEAM §3 already notes `kb_curate` has none). Tailscale hardens
  external transport only; blast radius is bounded by the minimal read-only PAT scope (decision 1).
- **−** The recommended default host is copyleft (Forgejo GPLv3+ since v9). Fine as an external
  service (invariant #4 concerns the core library), and Gitea (MIT) remains the drop-in
  permissive alternative — DESIGN §8 already lists both.
- **−** On Accept, prose must be reconciled: **ADR-0006** ("optional per-domain ACL" + "OpenFGA
  when per-domain rules are needed" → the demoted convenience-filter reading; annotate its
  index/README relationship), DESIGN §7 ("optionally per-domain ACL" → the demoted
  convenience-filter wording; the two-tier paragraph → cite this ADR) and ROADMAP Phase 5
  ("OpenFGA for fine-grained, per-domain ACL" → "OpenFGA behind the decision-2 evidence triggers;
  domain rules remain convenience filters"). The `auth/` package skeleton (`identity.py`/`policy.py`)
  follows this ADR, not the other way around.

## Open sub-decisions (Proposed; recommendations carried but not yet ratified)

1. **OD-1 — Identity source of record: Forgejo delegation vs self-issued tokens.**
   - *A) Forgejo PAT delegation* (decision 1 as written) — no issuance code, revocation/rotation
     live where accounts already live; requires a running git host.
   - *B) Agora-issued static tokens* (a tokens file on the hub, outside every repo) — removes
     the git-host requirement but makes Agora an issuer (rotation, revocation, storage burden
     land on us; a second credential system to audit).
   - *C) Both from day one* — rejected: two auth paths to secure and test, before any user needs
     the second.
   - **Recommendation: A.** B is recorded as the documented fallback with a concrete trigger (a
     real deployment that refuses any git host) and would be authored as an addendum then.
2. **OD-2 — Token scope granularity (what one presented token reaches).**
   - *A) Token = caller identity; server-side authz decides per repo* — one token, every repo
     the caller may see; matches Forgejo PAT semantics; simplest client story.
   - *B) Per-repo tokens* — token names one repo, presenting it elsewhere fails; finer blast
     radius, N-token client burden.
   - *C) Scoped claims inside the token* — needs issuer control; arrives naturally with Phase-5
     OAuth (RFC 8707 resource indicators per repo).
   - **Recommendation: A for Phase 4; C is the Phase-5 shape** (B only if a deployment demands
     blast-radius separation before Phase 5).
3. **OD-3 — `trusted_header` precedence once app verification exists.**
   - *A) Mode-exclusive:* the operator configures exactly one of {token verification,
     `trusted_header`}; both set → `ConfigError` — fail-loud, matching the #67 posture (a typo'd
     security key never silently changes trust).
   - *B) Token wins, header fallback* — flexible but creates ambient ambiguity about which
     identity was believed on any given request.
   - **Recommendation: A.**
4. **OD-4 — Operator surfaces (`/metrics`, `/dashboard`) placement under app auth.**
   - *A) Stay hub-local / proxy-blocked* (today's DEPLOY-TEAM §2 posture) — zero new code.
   - *B) Gate behind `curator-admin`* — needed the moment #51 stage 2 puts *controls* (not just
     status) on the web surface.
   - **Recommendation: A until #51 stage 2 lands; that landing flips to B as part of its own
     change.**

## Implementation sketch (when adopted; Phase-4 order)

1. **`auth/identity.py`** — Forgejo PAT verification (`GET /api/v1/user`) + in-memory TTL cache;
   fail-closed on any host error. **`auth/policy.py`** — repo-permission mirror
   (`GET /api/v1/repos/{owner}/{repo}` `permissions` → reader/writer/curator-admin) under the
   same short TTL cache (so a permission downgrade/removal — not just a token revocation — reflects
   within one TTL), the caller→(repos, roles) resolver of ADR-0006. Auth configuration lives in **server-level
   config** (the multi-repo serving config Phase 4 introduces — host URL, mode, TTL), never
   inside a KB repo.
2. **Core-boundary enforcement** — consult `auth/policy` in `core.write`/`core.read` (the
   ARCHITECTURE §3.1/§3.5 hook points); faces only extract the `Authorization` header. Web face
   wires it as a FastAPI dependency; `trusted_header` precedence per §OD-3.
3. **MCP Streamable HTTP + token verification land as one change** (the two coupled ROADMAP
   Phase-4 bullets) — the transport never exists unauthenticated.
4. **e2e (the ROADMAP Phase-4 exit):** two users, two team repos + personal repos, over the
   network — cross-tenant read and write refusals test-locked; revoked-token death within one
   TTL test-locked.
5. **Phase 5 (later, behind the decision-1/2 triggers):** OIDC/OAuth 2.1 + PKCE via an OSS IdP
   (or Forgejo-as-OIDC-provider), OpenFGA behind the same `auth/policy` seam — the seam is the
   contract; the engines swap behind it.
