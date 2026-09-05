# ADR-0037 — Local multi-KB federation (registry · profile · banded reads · S1 read-only attach)

**Status:** Proposed · 2026-09-05

**Scope note.** This ADR is **auth-independent**. It ships before [ADR-0036](0036-authn-authz.md)
(Proposed, Phase-4) and may not define, consume or anticipate a `Principal`, a token, or a
permission object. Everything here is a **local operator declaration** about paths on **this**
machine, evaluated in-process, with no network authority and no adjudication. It also owns **no
outbound pack composition** — see [§ What this ADR does NOT own](#what-this-adr-does-not-own).

---

## Context

Agora today is structurally **one process, one repo**. `Repo.resolve(root)` builds a repo handle
with no git calls and no validation (`src/agora_kb/core/repo.py:289-292`); `build_app` and
`build_server` each resolve exactly one repo and close every route/tool over one `AgoraHandlers`
(`src/agora_kb/faces/web/app.py:470-509`, `src/agora_kb/faces/mcp_server.py:1461-1485`); and 19
CLI subparsers each declare their own `--repo` with `default="."`. ADR-0036 names this precisely:
*"Tenancy today is process topology, not adjudication. One process serves one repo … so invariant
#5 has been *inherited* from deployment shape"* (`docs/adr/0036-authn-authz.md:48-52`).

That shape is now the binding constraint on three separate things the owner has already decided to
build:

1. **A hub.** An operator with a personal KB, a team KB and one or more attached read-only KBs
   wants one agent session, one web tab and one CLI to reach all of them. Restarting a process per
   KB is the only way today.
2. **Read composition without merging.** DESIGN §7 already reserves the shape — *"A client-side
   scope profile (`~/.agora/profile.yaml`, outside every repo) lists the repos the caller reads,
   and queries/gold packs are *composed* at read time"* (`docs/DESIGN.md:465-471`) — but attributes
   it wholesale to reserved ADR-0030. Under the 0030/0037 split the profile half is **this** ADR's.
3. **Identity that survives the crossing.** ADR-0041 D1.5 minted `kb_id` into `_meta/kb.yaml` and
   routed *"the registry, aliasing and attach semantics that consume `kb_id`"* here
   (`docs/adr/0041-stratum-kind-first-layout.md:275-277`).

Four prerequisites are met. `_meta/kb.yaml` identity exists and is validated
(`src/agora_kb/config.py:576-624`). Wave A of #169 landed the CLI read verbs `agora query` /
`read` / `neighbors` (`src/agora_kb/cli.py:313`, `:331`, `:342`) and `doctor --agents`
(`src/agora_kb/cli.py:563-570`) — the CLI surface the bands feed. ADR-0006 must be amended **in
this same unit**, because everything below stands on *"one process reads N KBs"* and on
*"repo = security / audience / custody boundary, not an account"*, which revises invariant 5.
And #166 states the one `wiki/people/**` clause that blocks H1.

**Three facts about today's code make this ADR necessary rather than convenient**, and each is
verifiable:

- **A clone of someone else's KB is fully writable.** `load_repo_config`'s own docstring blesses
  the case — *"A missing `repo.yaml` is NOT an error (a freshly-cloned or pre-config repo)"*
  (`src/agora_kb/config.py:192-202`) — and `_GITIGNORE` is only `_kb/` + `.DS_Store`
  (`src/agora_kb/core/repo.py:52-54`), so every clone takes that defaults branch. Nothing in
  `_cmd_curate` (`src/agora_kb/cli.py:1601-1622`) asks whose KB this is. **Without a defence,
  `curate` on a clone simply succeeds** — and the reader pass proved it end to end, including
  `agora sync` from a clone pushing the clone's curator-authored history back into the source repo.
- **A mirror carries `raw/` in full.** Only `_kb/` is git-ignored; `.gitattributes` explicitly
  preserves `raw/_blob/**` bytes (`src/agora_kb/core/repo.py:64-79`). Any claim that an attached
  KB's `raw/` "is not fetched or not resolvable" is **false**, and Wave A gave `raw/` a fourth
  emission path (`AgoraHandlers.raw()`, `src/agora_kb/faces/mcp_server.py:271-277`). A refusal must
  be **written**, not assumed.
- **`wiki/people/**` is open on every pull surface.** The people rules that exist are grading and
  identity rules, not read rules: `is_people_path` keeps people out of the `[[basename]]` space
  (`src/agora_kb/core/wiki.py:689-716`), gold excludes them at the population stage
  (`src/agora_kb/core/gold.py:519-531`), and the curator may never write them
  (`src/agora_kb/curator/constants.py:33-40`). Nothing filters them out of `Wiki.query`,
  `AgoraHandlers.note`, `graph()` or the reader cache. #166's clause is therefore **net-new work**,
  not a property to inherit.

---

## Decision

Fifteen decisions, D1–D15.

### A. Identity and names

#### D1 — `$AGORA_HOME` resolution, and an observably identical single-repo fallback

`agora_home()` resolves in **one order and nowhere else**:

1. the environment variable `AGORA_HOME`, `.strip()`ed, with an **empty value treated as UNSET**
   (the `_FALSEY` posture already established at `src/agora_kb/cli.py:2387-2389`); when set it is
   `os.path.expanduser`ed and **MUST then be absolute** — a relative `AGORA_HOME` is refused, on
   the connector rule's reason that a containment root must be a declared tree and never the
   ambient CWD (`src/agora_kb/harvester/connectors.py:198-210`);
2. otherwise `Path.home() / ".agora"`.

There is **no XDG fallback**, on the strength of `docs/DESIGN.md:469` already naming `~/.agora/` —
**not** on "the codebase has no XDG precedent", which proves nothing either way: `grep -rn AGORA_HOME
src/` and `grep -rn 'Path.home()' src/` are both empty, so there is no precedent for **any**
user-level path. The default location of the project's first user-level state directory, and its
**Windows** spelling (`%LOCALAPPDATA%` is the platform convention, and Track B / #85 is live —
`docs/ROADMAP.md:332`, `:153`), are therefore **OD-14**, not a settled clause.

**HUB MODE is entered iff `$AGORA_HOME/registry.yaml` is a readable file declaring at least one
KB.** In every other case — no directory, an empty directory, a registry with an empty `kbs:` —
agora is in **SINGLE-REPO MODE**.

**The mode probe is bounded, normatively: ONE `stat` plus ONE parse of
`$AGORA_HOME/registry.yaml`, and nothing else**, executed once per invocation inside the single
resolution point below. No other user-level path is touched, no directory is walked, and no
`profile.yaml` is read until a hub-mode command actually needs a band list. This is stated because
it is a genuinely new global dependency: `grep -rn AGORA_HOME src/` and `grep -rn 'Path.home()'
src/` are both **empty** in the frozen tree, so nothing in `src/` reads a user-level path today.

**Four probe outcomes, all decided here** (T11 covers every row):

| `$AGORA_HOME/registry.yaml` | mode | behaviour |
|---|---|---|
| absent (or `$AGORA_HOME` absent) | SINGLE-REPO | the pre-ADR path, observably identical |
| present, parses, `kbs:` empty | SINGLE-REPO | the pre-ADR path; **not** a "healthy hub with zero KBs" |
| present, parses, ≥ 1 KB | HUB | the ladder below |
| present but **unreadable (EACCES) or malformed** | **REFUSE** | every command, including one that names `--repo` explicitly |

The last row is the uncomfortable one and is decided rather than left to fall out of D2's
fail-loud posture: a registry the operator cannot read is a registry whose contents cannot be
assumed empty, and silently degrading to single-repo mode would run a write verb against `.` while
the operator believes a profile is selecting the target. The refusal **names the registry path and
the escape hatch** in one sentence:

```
agora: $AGORA_HOME/registry.yaml exists but could not be read (<exc>). agora refuses to guess
whether this machine is a hub. Fix or remove the file, or run this command with AGORA_HOME= (empty
= unset) to force single-repo mode (ADR-0037 D1).
```

`AGORA_HOME=` (empty ⇒ unset, per the `_FALSEY` rule above) is therefore a real escape hatch and
not an accident of parsing.

**In SINGLE-REPO MODE agora is OBSERVABLY IDENTICAL to this build, not textually unchanged.** The
distinction matters because this ADR's own D6 and D15 edit the very functions an earlier draft
claimed were untouched. What actually changes, named so T11 pins behaviour rather than an untrue
source-level claim:

- all 19 `--repo` declarations become `default=None` (see the sentinel decision below);
- the three MCP read tools defined inside `build_server`'s closure gain a `kb: str = ""` parameter
  (`kb_query` `src/agora_kb/faces/mcp_server.py:1508`, `kb_read` `:1516`, `kb_neighbors` `:1559`;
  `build_server` body from `:1461`);
- the web read routes defined inside `build_app`'s closure (`src/agora_kb/faces/web/app.py:470`;
  `grep -c '@app\.\(get\|post\)'` → 26) gain `?kb=`, a per-request `_handlers_for(kb)` and one
  Jinja global `kb_qs` (D15).

The guarantee is therefore: **with no registry, every CLI payload, MCP tool payload and HTTP
response is byte-identical to the pre-ADR build**, and no file outside the repo is read (T11).

**One resolution point, normatively.** An alias is resolved **exactly once**, by rewriting
`args.repo` in `main()` immediately before `_schema_version_guard(args)`
(`src/agora_kb/cli.py:581-598`, guard at `:602-654`). That reuses the one structural,
parser-tree-tested hook over `args.repo`/`schema_guard_attr`, whose own docstring states the rule
this ADR relies on: *"a command is guarded IFF it names a repo with `--repo` and has not opted
out … the failure mode that made this issue P0 is a check somebody forgets to wire"*
(`src/agora_kb/cli.py:607-610`). **Per-command alias resolution is forbidden**; with 19
independent `--repo` declarations and no shared helper, a per-command design will drift into 19
divergent implementations.

**The sentinel, decided normatively — `default=None` at all 19 sites.** Today every `--repo` is
`add_argument("--repo", default=".")` — verified, 19 of 19
(`src/agora_kb/cli.py:283`, `:306`, `:315`, `:333`, `:344`, `:363`, `:383`, `:426`, `:448`, `:453`,
`:458`, `:466`, `:477`, `:489`, `:513`, `:520`, `:534`, `:540`, `:556`). With that default,
`args.repo == "."` after parsing is **indistinguishable** between "the operator omitted `--repo`"
and "the operator typed `--repo .`" — argparse always populates the attribute — so tiers 3 and 4 of
the ladder below would be unreachable and the ladder would be decorative. **All 19 declarations
therefore change to `default=None`, and the `"."` fallback moves into the one resolver in
`main()`.** This is the one source-level change the single-repo claim above excludes; it is
observably identical because `None → "."` is applied before `_schema_version_guard` reads
`getattr(args, "repo", None)` (`src/agora_kb/cli.py:637`). The alternative — `parser.get_default`
comparison — is rejected: it reads as "unset" for an explicit `--repo .`, which is the same
ambiguity wearing a different hat.

**`--repo` stays a PATH; the alias is a separate `--kb <alias>` flag.** If `--repo` accepted both,
a directory literally named `general` in the CWD would silently shadow the alias `general`.
`--kb <alias>` is declared **at all 19 `--repo`-bearing sites** — the same edit that changes the
default — because the single-resolution-point design has no other way to make the ladder's write-verb
tier meaningful, and because a write verb an operator cannot aim at an alias would force them back
to typing mirror paths, which is exactly the bare-path habit D9 exists to make safe. (D6 restricts
only the two **read-only spellings** — `--kb all` and `--profile` — to `query`/`read`/`neighbors`.)
Precedence, in ADR-0015's style:

> `--kb <alias>` > `--repo <path>` > `profile.yaml write:` (write verbs) / `profile.yaml read[0]`
> (read verbs) > `.`

`--kb` together with `--repo` is a **refusal**, never a merge (argparse mutually exclusive —
verified: argparse refuses `--kb general --repo .`).

**What a bare command targets in HUB MODE is an open sub-decision, not a precedence-line
side effect.** Tier 3 says a bare `agora curate` inside a KB directory targets `profile.yaml
write:` rather than the CWD, which is a large and irreversible UX choice. It is promoted to
**OD-11** with a recommended default (`.` when the CWD is an initialized Agora repo, the profile
otherwise) rather than settled here by a `>` sign.

`AGORA_HOME` is **not** scrubbed from a curator-brain subprocess today: the name is neither in
`_SCRUB_NAMES` nor matched by the `(?i)(token|secret|key|password|cred)` regex
(`src/agora_kb/curator/isolation/__init__.py:74-113`). This is deliberate-and-inert — the brain
runs in a throwaway cwd and reads no registry — and is recorded here so that any future "the brain
may read the registry" change is a visible ADR amendment rather than an inherited environment
variable.

#### D2 — Registry grammar: `$AGORA_HOME/registry.yaml`

```yaml
# $AGORA_HOME/registry.yaml   — LOCAL · never git-tracked · never leaves this machine
version: 1                                  # int, REQUIRED; an unrecognised value REFUSES
kbs:                                        # mapping: alias -> entry. Non-mapping REFUSES.
  general:                                  # key = LOCAL alias (see grammar below)
    path: "/Users/me/knowledge"             # local KB; mutually exclusive with `transport`
    role: owner
    kb_id: "01J8Z…"                         # OPTIONAL CACHE of the tree's own _meta/kb.yaml
                                            #   value; display/join only (D3). The TREE wins (D4).
  hana-research:
    transport: "git+ssh://git@forge.example/hana/kb.git"
    mirror: "/Users/me/.agora/remotes/hana-research"
    pinned_commit: "9f1c2d0…"               # 40-hex, the attach pin
    role: reader                            # attach = structurally read-only (D9)
    kb_id: "01J9B…"
```

**Unknown-key posture: TOLERANT, with three named loud exceptions.** The repo already states the
rule that decides which posture a file gets: *"`_kb/repo.yaml` is git-IGNORED operator-local
policy, so an unknown key there is a local typo; `_meta/kb.yaml` is git-TRACKED and therefore
travels with a clone, so an unknown key there arrived from whoever authored the repo"*
(`src/agora_kb/config.py:537-543`). The registry never leaves this machine, so it takes
`repo.yaml`'s tolerant posture — the **opposite** of `_meta/kb.yaml`'s closed key set. An unknown
entry key loads without effect.

**Three refusals**, each on `load_backup_policy`'s reasoning that a silently-defaulted value is
discovered too late (`src/agora_kb/config.py:1041-1049`):

1. an **absent or unrecognised `version:`**;
2. **`path` and `transport` both present, or both absent**;
3. an **unrecognised `role:` value**. `role:` has no tolerant reading — a typo there would widen
   access. **The absent-`role:` default is keyed on the entry SHAPE, which is where the trust
   boundary actually is:** a `path:` entry (a local directory this operator typed) defaults to
   `role: owner`; a `transport:` entry (a remote clone) defaults to `role: reader`, fail-safe,
   mirroring `scope` defaulting to the most restrictive value
   (`src/agora_kb/config.py:1112-1113`). A single global `reader` default was the earlier draft's
   rule and is **rejected**: with it, an operator who hand-writes their own KB into the registry and
   omits one line gets a hub whose `profile.yaml write:` is refused at LOAD time (D5) with no verb
   to fix it — a fail-*stuck* posture, not a fail-safe one.

**A verb writes a local owner entry, so hand-editing is never the only way into HUB MODE.** The
OD-9 verb group is `agora kb add|attach|detach|refresh|list`: **`agora kb add <alias> <path>`**
registers a LOCAL KB (writes a `path:` entry, `role: owner`), while `agora kb attach` is the
commit-pinned fetch of a REMOTE (writes a `transport:`/`mirror:` entry, `role: reader`). Both refuse
rather than default when `--role` contradicts the entry shape.

**Four loader rules that are load-bearing, not cosmetic:**

- **A present-but-non-mapping `kbs:` REFUSES.** `_read_yaml_mapping` degrades an absent, empty **or
  non-mapping** file to `{}` (`src/agora_kb/config.py:1705-1720`), so a malformed or
  wrongly-indented registry would otherwise present as a *healthy hub with zero KBs attached*.
- **A duplicate alias key REFUSES.** `yaml.safe_load` silently keeps the **last** of duplicate
  mapping keys, so two entries for one alias differing in `role:` would load as the wider one with
  no diagnostic. `yaml.safe_load` alone cannot see this; use a duplicate-detecting constructor or a
  `yaml.compose()` node walk.
- **A non-string alias key REFUSES.** Under YAML 1.1 an alias spelled `no`/`yes`/`on`/`off` parses
  to a Python `bool`, and `_sub_mapping`'s `str(k)` (`src/agora_kb/config.py:1722-1729`) would
  silently rename it `"False"`/`"True"` — a KB appearing under a name the operator never typed.
- **`path:` and `mirror:` are `expanduser`ed and then REQUIRED to be absolute.** `RepoLayout`
  normalises with `Path(...).absolute()` only — no expanduser, no symlink resolution, no `..`
  collapsing (`src/agora_kb/core/layout.py:293-296`) — so a `path: "~/knowledge"` copied verbatim
  from the H1 sketch resolves to `<cwd>/~/knowledge`. Reuse `_require_source_path`'s rule and its
  wording (`src/agora_kb/harvester/connectors.py:198-210`).

**Alias grammar: REUSE `core.pathsafe.is_safe_component` at the default `max_bytes=180`**
(the predicate at `src/agora_kb/core/pathsafe.py:204-211`; `DEFAULT_MAX_BYTES = 180` at `:58`). The
alias is interpolated into `$AGORA_HOME/remotes/<alias>`, into a `?kb=` URL parameter and into a
shell argument, so it needs **one** closed Unicode-category allowlist, not three — and reusing the
predicate that already governs note basenames and plan paths keeps one charset decision in the
repo. It admits `general`, `hana-research`, `hana_research`, `내지식`; it rejects `..`, `.hidden`,
`a/b`, `a b`, `x-`, `con`.

**The alias grammar deliberately does NOT use `is_safe_filename_stem`**
(`src/agora_kb/core/pathsafe.py:214-248`), the **union** predicate whose whole purpose is to *admit*
what `is_safe_component` rejects — its own docstring says so: *"`is_safe_component` alone would
newly REJECT `con`/`CON`/`nul`/`com1`/`aux` … and `foo-`/`foo.`"* (`:226-230`). That union is
correct for a derived cache stem, where rejecting a legacy name silently costs a cache; it is wrong
for an alias, where `con` and `foo-` are exactly the names that must not become a directory. Citing
the union predicate's line range for the alias rule would ship the opposite of the rule.

Three **alias-only** rules the predicate does not cover:

- **Reject an alias for which `ids.is_ulid(alias)` is True.** A ULID passes `is_safe_component`, and
  a ULID-shaped alias is indistinguishable from a `kb_id` inside the D4 badge.
- **Decide collisions case-insensitively** (`str.casefold()`), even though `pathsafe` explicitly
  *"does not decide case"* and *"does not defend against homographs"*
  (`src/agora_kb/core/pathsafe.py:35-39`; the case note also at `:172`): the alias names a mirror
  directory, and `Hana`/`hana` are two registry keys and one directory on a case-insensitive
  filesystem.
- **Require the YAML key to be a `str`** before anything else (see the loader rules above).

The alias grammar is **not** the legacy inbox-writer charset `\A[A-Za-z0-9][A-Za-z0-9._-]*\Z`
(`src/agora_kb/core/layout.py:47-48`, `:133-156`), which remains a separate, still-live guard on a
different concept.

**A naming collision is being created deliberately and is recorded, not discovered later.**
"Registry" already means the adapters `BackendRegistry` in this codebase
(`src/agora_kb/curator/backends.py:107`), and `agora doctor` — the very command that gains
`--registry` (D12) — already holds a local `registry`/`registry_error` pair that **is** the backend
registry (`src/agora_kb/cli.py:2907`, `:2918`, `:2943`; the parameter is typed
`registry: BackendRegistry | None` at `:3171`). So `doctor --registry` would print KBs while
`doctor`'s routing and `--agents` tables print the other registry. **Decision for H1: the two
registries coexist under one name, and the same unit renames doctor's local variable to
`backend_registry`/`backend_registry_error` so no single function holds two things called
`registry`.** The alternative spelling (`kbs.yaml` + `doctor --kbs` + `agora kb list`) is
**OD-13**.

#### D3 — `kb_id` is display and join identity, never an authorisation input

`kb_id` is display and join identity. It is **never an authorisation input**, never the key of a
local decision, and never a substitute for the alias. [ADR-0041 D1.5](0041-stratum-kind-first-layout.md)
decided this and residual risk **R3** records why it cannot be closed; this ADR **cites** those and
does not restate them.

Concretely, and testably — stated as the rule it is, because an earlier draft's absolute ("no code
path may branch on the VALUE of a `kb_id`") is contradicted by D4 two paragraphs later, where the
fingerprint comparison **is** a branch on the value:

> **`kb_id` is never an AUTHORISATION input. Equality comparison for attach-collision detection
> (D4) and rendering inside the three-part badge are the ONLY permitted uses, and no code path may
> grant, widen, route or scope access on its value.**

The audit baseline is recorded so the property is a regression test rather than an assertion. Today
`kb_id` is read at exactly four kinds of site and none of them is authorisation: APPLY **stamps**
`kb:` and fails loud when the file is absent (`src/agora_kb/curator/apply.py:255-283`, `:1150`);
`repo init` refuses on the file's **ABSENCE** in a schema-≥2 tree
(`not layout.kb_meta_file.is_file()`, `src/agora_kb/cli.py:836`, message at `:834-857`) while
`agora import` refuses on its **PRESENCE**, in neither case on its value
(`src/agora_kb/ingest/vault_import.py:1360-1372`); `Repo.init` refuses schema ≥ 2 without one and
interpolates it into the seed map (`src/agora_kb/core/repo.py:199`, `:437-440`, `:454`); and
`agora import --from-kb` **mints and stamps a NEW one** at the destination
(`src/agora_kb/ingest/kb_convert.py:1032`, `:1112`, `:1139` — D6 rule 6). No face, no ranker and no
gold path reads it (`grep -rn kb_id src/agora_kb/faces src/agora_kb/core/gold.py` → empty, verified).

**The regression test is scoped to where the grep is meaningful** (see [Test plan](#test-plan), T9):
the emptiness assertion covers `src/agora_kb/faces` and `src/agora_kb/core/gold.py` only. The
earlier draft also grepped `src/agora_kb/hub`, which would fail on **this ADR's own mandated code** —
D4 requires the hub to compare `kb_id` values. `hub/` is pinned instead by a **positive** test: the
only `kb_id` reads under `src/agora_kb/hub/**` are the D4 fingerprint comparison and the badge
renderer, each named in the test.

#### D4 — Attach fingerprint, collisions, and the three-part badge

**An attachment's fingerprint is the compound `(kb_id, transport_id)`.** `transport_id` is the
resolved absolute path for a local entry, spelled `local:<resolved path>`; for a remote entry it is
the normalised `transport + url`: trailing `/` stripped, trailing `.git` stripped, scheme and host
casefolded, **path left case-sensitive** (a forge path is), and any value containing whitespace or a
control character refused — reusing `_REMOTE_BAD_CHARS` verbatim
(`src/agora_kb/core/repo.py:228-232`) rather than writing a second regex. The `local:` spelling is
not cosmetic: a bare literal `"local"` would make the badge's third part identical for every local
KB, so two local KBs would badge alike while D4's fingerprint already distinguishes them by
resolved path. **A fingerprint is never hashed into an opaque token**: it is shown as its two parts,
because its whole purpose is to be read by a human deciding whether two KBs are the same KB.

**Where `kb_id` comes from, normatively.** The fingerprint's first component is read **from the
candidate KB's own `_meta/kb.yaml`**, never from the operator-typed registry key. The registry's
`kb_id:` is a cached display value only; if it disagrees with the tree, the tree wins and `agora kb
attach|refresh` rewrites the registry line. Were it operator-typed, every collision rule below would
be defeated by omitting one key.

**It is read by a NEW tolerant reader in `hub/`, not by `config.load_kb_identity`.** The shipped
loader is deliberately closed: absent ⇒ `None`, but a present file **raises `ConfigError`** on a
non-ULID `kb_id`, on any unknown key and on any policy key
(`src/agora_kb/config.py:622-658`; the ULID validator at `:600-607`). That strictness is right for
the local repo's own file and wrong for a remote's, because it turns a *display* concern into an
*availability* failure for a KB that reads perfectly. `hub.read_remote_kb_id(layout) -> str | None`
therefore returns the raw `kb_id` string when the file parses as a mapping and carries one, `None`
when the file is absent, and `None` on any parse/validation failure — never raising, and never
reused for the local repo, whose loader is unchanged.

Three fingerprint shapes follow from that, and each has a badge form:

| what the mirror declares | fingerprint | badge |
|---|---|---|
| a canonical ULID `kb_id` | `(kb_id, transport_id)` | `alias · 01J9B… · git+ssh://…` |
| a non-ULID `kb_id` | `(kb_id, transport_id)` | `alias · kb_id: unverifiable · git+ssh://…` |
| **no `_meta/kb.yaml`** (schema 1) or an unreadable one | **`transport_id` alone** | `alias · kb_id: none (schema 1) · git+ssh://…` |

The third row is not hypothetical: `_meta/kb.yaml` does not exist on schema 1 at all
(`load_kb_identity` returns `None` when absent, `src/agora_kb/config.py:642-643`;
`tests/support/kb_builder.py:655-660` refuses `kb_id`/`kb_name` on a schema-1 build), and **D12
deliberately ALLOWS attaching a schema-1 KB**. An identity-less KB is attachable, is fingerprinted
on transport alone, and is never presented with a borrowed or invented `kb_id`.

**An unreadable or invalid `_meta/kb.yaml` never refuses the attach and never stops the hub.** It
degrades to the third row. This is stated because D15's startup validation is otherwise fail-loud:
**the mirror identity read is explicitly excluded from that list** — one remote whose identity file
rots must not prevent every other KB from being read. A mirror whose *root* cannot be read at all is
a different failure and bands `unavailable` (D6).

Four collision rules:

| case | behaviour | message shape |
|---|---|---|
| same fingerprint already attached | **REFUSE** | `agora kb attach: this KB is already attached as 'hana-research'. Nothing was fetched.` |
| same `kb_id`, different transport | **WARN + force `--alias`** | `WARNING: kb_id 01J9B… is already attached as 'hana-research' over git+ssh://forge.example/hana/kb.git. A kb_id is a self-claim (ADR-0041 R3) — these may be the same KB, a fork, or an impersonation. Attaching anyway requires an explicit --alias.` |
| same alias, different fingerprint | **REFUSE**, print BOTH fingerprints, require `--alias` or `agora kb detach` | `agora kb attach: alias 'hana-research' is already attached to (01J9B…, git+ssh://…/hana/kb.git); you gave (01JQ2…, https://other.example/kb). Choose --alias, or 'agora kb detach hana-research' first.` |
| `kb_id` absent or not a canonical ULID | **ACCEPT**, badge per the table above; the `kb_id`-collision rule simply does not apply (there is nothing to collide) | — |

*(Message shapes are written in the **OD-9(A)** `agora kb <verb>` spelling, which D9's refusal
message and OD-9's recommendation also use. If OD-9(B) — top-level verbs — is chosen instead, every
message shape in this ADR must be re-spelled in the same pass.)*

The same-fingerprint case is a **refusal rather than an idempotent no-op**, because a silent
success trains an operator to run `attach` as the sync command it is not. The non-ULID case is
accepted rather than refused because `is_ulid` is deliberately strict
(`src/agora_kb/core/ids.py:148-155`) and refusing would turn a *display* concern into an
*availability* failure — the inverse of `KbIdentity`'s validator
(`src/agora_kb/config.py:600-607`), whose strictness is right because that file is the local repo's
own.

**Impersonation is structurally open and this ADR does not close it.** A `kb_id` is minted by
whoever ran `agora repo init` on the remote (`src/agora_kb/cli.py:719-723`), lives in a
git-**tracked** file that travels with the clone, and is copied verbatim into every note's `kb:`
field — so any party who can serve a tree can serve any `kb_id`. **The only mitigation adopted here
is presentational and MUST be uniform: every surface that renders a remote hit renders
`alias · kb_id · transport` together** — never a bare `kb_id`, never a bare alias, and never the
mirror's directory name. ADR-0041 R3 states this obligation; this ADR implements it.

The badge's carrier is the **`FederatedHit`/`KbBand` field set (D6)**, never `SearchHit`
(frozen + `extra="forbid"`, `src/agora_kb/core/wiki.py:735-747`). **`SearchHit.repo` is NOT the
alias**: it is `layout.root.name`, the mirror's *directory basename*
(`src/agora_kb/core/wiki.py:786`), it can collide across two attached KBs, and no surface may
present it as an identity. The badge must appear on: each read verb's human output **and** its
`--json` payload (`src/agora_kb/cli.py:1419-1426`), the MCP tool payloads, and the web `?kb=`
switcher.

### B. Profile and composition

#### D5 — `profile.yaml`, and ONE consumption contract for CLI, MCP and web

**Profile NAME grammar, decided so `--profile NAME` and `resolve_profile(profile=…)` are not a
surface without a spelling.** A profile is a FILE: `$AGORA_HOME/profiles/<name>.yaml`, with
`$AGORA_HOME/profile.yaml` retained as the alias for `<name> = "default"` (it is the shape
`docs/DESIGN.md:469` already names, and the single-profile operator should not have to make a
directory). `<name>` is validated by the **alias grammar of D2** — one `is_safe_component` decision
in this ADR, not two — so `../etc/passwd`, `a/b` and `.hidden` are refused before any path is
composed. `--profile PATH` (a value containing a `/` or ending in `.yaml`) is the explicit escape
hatch and is read verbatim.

```yaml
# $AGORA_HOME/profiles/default.yaml   (== $AGORA_HOME/profile.yaml)  — LOCAL · never git-tracked
version: 1                        # int, REQUIRED, must == 1
read: [general, hana-research]    # list[alias], REQUIRED, non-empty, no duplicates.
                                  #   DECLARATION ORDER IS BAND ORDER.
                                  #   Every alias must exist in registry.yaml or load REFUSES.
write: general                    # alias, REQUIRED for any write verb.
                                  #   MUST resolve to a role: owner entry — refused at LOAD time.
merge:                            # OPTIONAL; absent == the defaults below
  mode: bands                     # bands (default) | interleave | concat.  NO rrf, ever.
  per_kb_limit: 5                 # int >= 1 — the `limit=` handed to each KB's query
  max_kbs: 8                      # int >= 1 — hard cap on bands actually queried
```

Posture: unknown **top-level** keys are tolerated (D2's reasoning); unknown keys inside `merge:` and
any **bad value** fail loud with `ConfigError`, per the uniform operator-config rule *"a typo must
surface, never silently take a default"* (`src/agora_kb/config.py:978-984`). The single most
dangerous typo — `write:` naming a `role: reader` KB — is refused **at load**, not at first write.

**Registry present, profile ABSENT — decided, not left to fall out of two independent loaders.**
D1 enters HUB MODE from the registry alone, so this case is reachable the moment an operator runs
`agora kb add` and nothing else. **A missing profile is SYNTHESISED, never a refusal:** `read:` = the
registry's entries in **file order** (the same declaration-order rule, just declared in the other
file), and `write:` = the single `role: owner` entry when there is exactly one. **With two or more
owner entries and no profile, every WRITE verb refuses** with a message naming the ambiguity and the
two fixes (`--kb <alias>`, or write a profile); reads still band normally. A synthesised profile is
labelled `profile: "(synthesised from registry)"` in `FederatedQueryResult.profile` so no surface
claims a file that does not exist.

**ONE consumption contract**, two functions, in the new `src/agora_kb/hub/` package:

```python
def resolve_profile(home: Path, *, profile: str = "default") -> ResolvedProfile
# ResolvedProfile = (bands: tuple[ResolvedKb, ...], merge: MergePolicy, write: ResolvedKb | None)
# ResolvedKb      = (alias, kb_id, role, layout: RepoLayout, provenance: BandProvenance)

def federated_query(profile: ResolvedProfile, question: str) -> FederatedQueryResult
```

CLI, MCP and web **must all** obtain their bands from `resolve_profile` and their hits from
`federated_query`; **no face may build a band list itself**. This is enforceable, not aspirational —
but the enforceable property is **payload-object equality, not byte equality**, because the three
faces serialise through three different encoders and always will: the CLI prints
`json.dumps(payload, indent=2, ensure_ascii=False)` (`_print_json`, `src/agora_kb/cli.py:1420-1426`),
the web face returns a plain dict that Starlette renders with `separators=(",", ":")` and no
indentation (`src/agora_kb/faces/web/app.py:538-543`), and an MCP tool hands its dict to FastMCP's
own transport encoding (`src/agora_kb/faces/mcp_server.py:1508-1513`). A golden 3-KB fixture
therefore asserts `json.loads(cli_stdout) == api_json == tool_payload` for
`agora query --kb all --json`, `kb_query(question, kb="all")` and `GET /api/search?q=…&kb=all`
(Test plan T1). Byte-level goldens are kept where one serializer is on both sides: the CLI's own
`--json` rendering, and the pre/post-ADR single-repo comparison (T11).

The read companions live on the same object, and the `note → raw → not_found` algorithm — today
spelled **twice**, at `src/agora_kb/faces/mcp_server.py:1547-1556` and
`src/agora_kb/cli.py:1485-1502` — is collapsed to **one** copy under it rather than gaining a third.
That collapse is load-bearing for D10: it is the single place the reader-KB `raw/` refusal has to be
propagated instead of being swallowed by two independent `if raw["status"] == "ok"` branches.

**Module placement, normatively.** Registry, profile, attach and federation live in a **new
`src/agora_kb/hub/` package that DEPENDS on `core` and is never imported by it.** ADR-0041 R6
already refused to put a config read on the query hot path (`src/agora_kb/core/wiki.py:709`;
`docs/adr/0041-stratum-kind-first-layout.md:1058-1071`); a
registry read inside `core/wiki.py` is the same mistake one level up. Two greps are the tests:
`src/agora_kb/core/**` contains no `hub` import, and `src/agora_kb/hub/**` contains **zero**
occurrences of `query_lexical` — the second grep **is** the #144 pin restated for federation, and
it is cheaper than prose.

#### D6 — `FederatedHit` is fixed; no field is added to `SearchHit` or `QueryResult`

`SearchHit` and `QueryResult` are both frozen pydantic models with `extra="forbid"`
(`src/agora_kb/core/wiki.py:738`, `:753`), and ADR-0012 §0 says *"Output is **exactly** the
DATA-MODEL §9 shape, nothing added"*. The wrapper is therefore **forced by the code**, not merely
preferred. ADR-0012 stays untouched — no addendum is required.

```python
# src/agora_kb/hub/federation.py
@dataclass(frozen=True, slots=True)
class FederatedHit:
    kb_alias: str                            # registry key = LOCAL alias. THE join key.
    kb_id: str | None                        # display/join only (D3); None for an identity-less KB
    kb_role: Literal["owner", "reader"]
    band: int                                # 0-based index into profile.read (declaration order)
    rank_in_band: int                        # 1-based rank inside this KB's own QueryResult
    hit: SearchHit                           # the ADR-0012 object; see `redacted` below
    redacted: bool                           # True iff `hit.excerpt` is NOT the corpus bytes

@dataclass(frozen=True, slots=True)
class BandProvenance:
    transport: str                           # "local:<resolved path>" | "git+ssh://…" (D4's transport_id)
    commit: str | None                       # the pinned mirror commit (None for a local KB)
    fetched_at: str | None                   # ISO-8601 Z, mirror only
    schema_version: int | None               # the KB's own KB-schema version
    writable: bool                           # False for every role: reader KB, structurally

@dataclass(frozen=True, slots=True)
class KbBand:
    kb_alias: str
    kb_id: str | None
    kb_role: Literal["owner", "reader"]
    band: int
    status: Literal["ok", "not_found", "unavailable"]
    provenance: BandProvenance
    hits: tuple[FederatedHit, ...]
    redacted_hits: int                       # how many of `hits` carry redacted == True

@dataclass(frozen=True, slots=True)
class FederatedQueryResult:
    query: str
    status: Literal["ok", "not_found", "degraded"]
    profile: str
    bands: tuple[KbBand, ...]                # DECLARATION ORDER
    unavailable_bands: tuple[str, ...]       # aliases whose band status is "unavailable"
```

**The top-level `status` has THREE values, not two, and the third is the whole point of the second
one.** `ok` iff any band is `ok`; **`degraded`** iff no band is `ok` **and at least one band is
`unavailable`**; `not_found` only when every band answered honestly and empty. A two-valued wrapper
would re-create at the profile level exactly the ambiguity `KbBand.status: "unavailable"` was added
to remove — a profile whose only attached KB is a mis-resolved mirror would answer `not_found`,
indistinguishable from an honest empty answer. `unavailable_bands` carries the aliases so a caller
does not have to walk the bands to render the warning.

**Exit codes, stated because T12 otherwise pins only the single-KB case.** Every read verb keeps the
pre-ADR rule — *an empty search is a RESULT, not a failure* — so `not_found` and `degraded` both
**exit 0**; the human rendering of `degraded` prints the unavailable aliases and their reasons to
**stderr**, the way the D6 read-only line already does (`src/agora_kb/cli.py:1331-1335`).

**How redaction and "the frozen ADR-0012 object" coexist — decided, because D10 makes them
collide.** D10 runs `core.redact.sanitize()` on every excerpt emitted from a `role: reader` KB, and
`SearchHit` is `frozen=True` + `extra="forbid"` (`src/agora_kb/core/wiki.py:735-747`), so "verbatim,
unmodified" and "redacted at band time" cannot both be literally true. **The decision is (a):
`FederatedHit.hit` is `search_hit.model_copy(update={"excerpt": sanitized})` for a reader KB and the
untouched object for an owner KB, with `redacted: bool` on the hit and `redacted_hits: int` on the
band saying which happened.** The model's *shape* is still frozen and still ADR-0012's — no field is
added, no field is dropped, `extra="forbid"` is honoured — and the caller is told, rather than left
to assume the excerpt is the corpus bytes. The alternative (b), redacting only at render time and
shipping unredacted remote text in the JSON payload, is **rejected**: T1's one-contract rule would
then propagate unredacted bytes to all three faces, which is the opposite of D10's purpose.
`hit.line`/`anchor` remain pre-redaction offsets — the caveat D10 already records — and `redacted`
is what makes that caveat legible at the call site.

Three deliberate deviations from the H1 sketch
(`docs/notes/agora-kh-design-judgement.md:186-189`), recorded so the sketch can be amended in the
same unit rather than left as a competing field list:

- **`kb_local_score` is DROPPED.** It is `hit.score` re-exported under a name whose whole purpose is
  cross-KB comparison, which D7 forbids; the value is already inside `hit`.
- **`transport`, `commit`, `fetched_at` MOVE off the hit onto `BandProvenance`.** They are constant
  per KB and are provenance of the *mirror*, not of the hit; per-hit copies make provenance bytes
  `O(#hits)` instead of `O(#KBs)` and invite per-hit divergence.
- **`rank_in_kb` → `rank_in_band`**, because band index and rank must be read together.

**`status: "unavailable"` is new and load-bearing.** `QueryResult.status` is
`Literal["ok", "not_found"]` (`src/agora_kb/core/wiki.py:750-757`) and a missing, unreadable or
mis-resolved mirror is neither. This is not hypothetical: a nonexistent `path:`,
`guard_repo_schema_version` passing silently on a directory that is not an Agora repo
(`src/agora_kb/config.py:522-534`), and `query_lexical` returning a clean `not_found` on an empty
corpus (`src/agora_kb/core/wiki.py:829-831`) together mean a mistyped registry path would otherwise
band as a healthy empty KB. **Without `unavailable`, a broken attach is indistinguishable from an
empty one and the operator silently trusts a short answer.** (An earlier draft also justified
`unavailable` with *"`RepoLayout` never expands `~`"* — `src/agora_kb/core/layout.py:293-296` is
`.absolute()` only, which is true. But D2's loader rule already `expanduser`s `path:`/`mirror:`
before requiring them absolute, precisely to close that hazard, so a `~/…` entry resolves normally
and is **not** an `unavailable` case. The claim is dropped here and from T12.)

**Surface grammar.** The federated address is a **separate `kb` argument, never an in-band path
prefix**, and it is spelled **`all`** on every face — one token for the whole contract:

- MCP: `kb_query(question, kb="")`, `kb_read(path, kb="")`, `kb_neighbors(path, depth=1, kb="")`.
  `kb=""` → today's payload byte-identical; `kb="<alias>"` → the same single-KB payload shape
  against that KB; **`kb="all"`** → the banded payload. **Still seven tools.**
- CLI: **`--kb ALIAS|all`** on **every `--repo`-bearing verb** (D1 — the alias tier of the
  precedence ladder is meaningless otherwise), argparse-mutually-exclusive with `--repo`. The two
  **read-only spellings** — the value `all`, and `--profile NAME|PATH` — are accepted on
  `query` / `read` / `neighbors` only; a write verb given `--kb all` refuses, because "write to
  every KB" is not a thing this ADR admits.
- Web: `?kb=<alias>` and `?kb=all` on read routes.

**Why `all` and not `*`.** An earlier draft spelled the MCP value `*` while CLI and web said `all`,
which mints two permanent client-facing tokens for one value and makes T1's cross-face comparison
compare two different requests. `all` wins: it is already the CLI and URL spelling, it needs no
shell quoting, and it is not a glob the reader will expect to match `hana-*`. The MCP default stays
the **empty string** rather than an omitted/`None` parameter for one checkable reason: FastMCP
derives the tool schema from the signature, and a `str` parameter with a `""` default keeps the
parameter's declared type a plain `string` instead of a nullable union — the smaller schema change
for already-connected clients, which is the load-bearing premise of keeping seven tools (and is
itself listed as UNVERIFIED at the end of this ADR).

Four checkable reasons for the kwarg over `alias:path`: (1) `rawstore.resolve`'s gate 1 requires
`posixpath.normpath(rel) == rel` and `rel.startswith("raw/")`
(`src/agora_kb/core/rawstore.py:26-45`), so a prefix spelling puts a parse step in front of the one
gate whose value is that it is spelled once; (2) `Wiki.get_note` matches `rel_path` for **equality**
against enumerated notes (`src/agora_kb/core/wiki.py:918-930`), needing a second strip site that can
disagree with the first; (3) a `sources:` string is stored and passed **verbatim**
(`src/agora_kb/core/rawstore.py:102-105`) and must remain a valid `kb_read` argument after
federation — an in-band prefix makes a stored citation ambiguous between "a note in KB *x*" and "a
note whose path starts with `x/`", in a namespace that already has a real top-level directory per
kind; (4) the alias is **local** (it is the registry key), so a path embedding it is not portable
between two operators' registries.

**Do not name the parameter `scope`/`scopes`.** That name is already reserved on `kb_context` for
ADR-0030 (`src/agora_kb/faces/mcp_server.py:1588-1590`;
`docs/adr/0027-gold-context-packs.md:151-157`), and a near-name on the query tools is precisely the
surface on which README:48's *"An ADR-0037 that starts composing packs has taken 0030's job"*
becomes arguable.

**Extending three tools rather than adding an eighth** follows the repo's own written precedent:
*"Extending `kb_read` rather than adding an eighth tool keeps the client-facing tool count at
seven, which is the expensive thing to reverse"* (`src/agora_kb/faces/mcp_server.py:1545-1546`).
`tests/faces/test_mcp_server.py:488-505` locks the tool **names** only, so the same unit must extend
that test to assert the **parameter sets** of the three read tools.

#### D7 — Declaration-order bands; cross-corpus raw-score comparison forbidden; RRF rejected

Three rules:

1. **`mode: bands`** — band *i* is entirely above band *i+1*; declaration order in `profile.read`
   **is** band order.
2. **Inside a band, ADR-0012 §7's order is preserved verbatim and untouched.**
3. **There is no global order across bands, and no field from which one could be derived.**

**Forbidden, by name:** comparing `hit.score` across bands; any fused or normalised score field;
re-sorting the union by score; z-scoring or min-maxing per KB "to make them comparable".

The reason is arithmetic, not stylistic. IDF is `log(1 + (n − dft + 0.5)/(dft + 0.5))` over **that
repo's own** `n` (`src/agora_kb/core/wiki.py:1519-1536`); `avgdl` is per-field **and** per-repo
(`:1531`); `_structural` divides by that repo's `max_indeg` (`:1573-1576`); and `_lexical` squashes
through `raw / (raw + PIVOT)` (`:1539-1571`). So `0.42` in a 40-note KB and `0.42` in a 4 000-note
KB are not the same quantity, and the squash makes the discrepancy **non-monotone** rather than a
scale factor a normaliser could remove.

**RRF is REJECTED, with the reason.** RRF scores a document as
`score(d) = Σ_L 1/(k + rank_L(d))` over the lists it appears in. Federated bands are **pairwise
disjoint in document identity by construction**: a hit is identified by `(kb_alias, path)`, custody
boundaries are repo boundaries (invariant 5), and a forked copy in another KB is a *different*
document under a different `kb_id` — and since `kb_id` is a self-claim that may not arbitrate
canonicality (D3), the system is **not permitted** to identify the two. Over disjoint lists each `d`
appears in exactly one `L`, so the sum collapses to `score(d) = 1/(k + rank_band(d))`, a strictly
decreasing function of `rank_in_band` alone. Ordering by it yields all rank-1 hits, then all rank-2
hits, … — **round-robin interleave** — with ties among equal ranks that RRF itself cannot break, so
the tie-break must come from outside, and the only order available is declaration order. **RRF ≡
interleave with declaration-order tie-breaks**, which `mode: interleave` already provides under an
honest name.

RRF therefore buys zero information while costing three things: it launders per-KB rank into a
pseudo-score that *looks* comparable (the exact property the IDF argument forbids); adopting it
would require reopening ADR-0012 §11's *"no RRF"* verdict
(`docs/adr/0012-deterministic-query-ranking.md:499`); and it is **empirically worse on this
corpus** — STRATEGY §13 measured RRF fusion collapsing negative/abstention accuracy from 1.000 to
0.100 while `llm_then_bm25` won at r@1 0.583 (`docs/STRATEGY-2026-08.md:439-446`). RRF is
non-trivial only when lists **overlap**, which federation across custody boundaries excludes.

The two non-default modes, defined so they are not re-litigated: **`interleave`** = round-robin over
bands in declaration order (a band that runs out is skipped) — stated here to be the RRF-equivalent,
named honestly; **`concat`** = bands flattened in declaration order, for a consumer that cannot
render grouping. **No cross-KB dedup in any mode**: the same basename in two KBs is two documents
under two custodies, and choosing whose copy is canonical is an authorisation-flavoured judgement
`kb_id` must not make. "Dedupe the fork" is the obvious first feature request and must arrive as an
ADR, not a patch.

**ADR-0012 §11 is hereby resolved, without amending ADR-0012.** §11 defers cross-repo ordering
*"until multi-tenancy (when a documented cross-repo normalization can be decided)"* while imagining
a merge *"under the §7 global order"* (`docs/adr/0012-deterministic-query-ranking.md:499-506`).
**This ADR is that decision, and it decides AGAINST a cross-repo normalisation: there is no global
§7 order across KBs, there are bands.** Because no §0 field, semantic, weight, floor or ordering
changes, §11's deferral is discharged in prose here rather than by an addendum there. Saying nothing
is the failure mode: a later reader would find §11's "global order" clause and read it as
authorisation.

**The `[[basename]]` identity space is per-repo and must never cross a band.** `by_basename` is
built from one Wiki's own notes (`src/agora_kb/core/wiki.py:845-847`), `_compute_indegrees` consumes
it (`:848`), and the graph's identity map is per-handler
(`src/agora_kb/faces/mcp_server.py:961`). **A remote note's `[[basename]]` or `[Title](x.md)` link
may never resolve to a local note of the same basename, and no composer may hand a merged note list
to any of those functions.** This is invariant 8 at the link layer.

#### D8 — An LLM rerank tier sits ABOVE the bands and never touches the write-path oracle

Three sentences fix the seam:

1. A rerank tier **consumes an assembled `FederatedQueryResult`** and emits a re-ordering of
   `FederatedHit`s and/or an `ok`/`not_found` verdict — STRATEGY §13's accepted shape, in which the
   LLM owns `ok`/`not_found` and the order while BM25F backfills below the model's pick
   (`docs/STRATEGY-2026-08.md:443-444`).
2. It **never runs inside a band, never inside `Wiki`, and never reaches `query_lexical`.** For the
   single-KB case the only legal attachment point remains `Wiki.query`'s own body — *"Structurally a
   pure delegation to `query_lexical`: a ranking tier added here can only be written INSIDE this
   body"* (`src/agora_kb/core/wiki.py:789-805`). For the multi-KB case it is the composer above the
   bands.
3. **This ADR does not enable a rerank tier.** It fixes the seam so a later ADR can attach one
   without touching the oracle; that later ADR is still required.

The `#144` pin is the constraint being honoured: *"this is `query_lexical`, the model-free lexical
oracle, NOT `query` … Do not 'unify' this back onto `query()`"*
(`src/agora_kb/curator/bundle.py:198-206`). The mechanical guard is D5's grep: **zero occurrences
of `query_lexical` under `src/agora_kb/hub/`**. A second reason the tier belongs above the composer:
`agora eval`'s ranking goldens run through `Wiki.query`
(`src/agora_kb/core/rank_snapshot.py:207`), so a tier inside that body would move the goldens.

### C. Structurally read-only S1

#### D9 — `role: reader` is a structural refusal, with an enumerated surface

An attached KB is **read-only by construction**, not by policy. The construction has three layers,
and **only the third is a boundary**:

1. **Attach shape.** `git clone --no-tags --single-branch --branch <branch> -- <url> <mirror>`, then
   `git -C <mirror> -c advice.detachedHead=false checkout <pinned_commit>`, **keeping the local
   branch ref at the pin**. Do **not** achieve read-only by deleting the branch: with the branch
   deleted, reads still work off the working tree but `agora index build` fails
   (`git rev-parse --verify refs/heads/main` → rc 128), i.e. the ADR-0012 §2 accelerator becomes
   unbuildable for zero safety gain.
2. **Git-level fence (convenience only).** `git -C <mirror> remote set-url --push origin DISABLED`
   plus `git -C <mirror> config --local agora.mirror true` as a machine-readable second witness.
   **This is not the boundary**: `set-url --push` blocks push *by remote name* and is bypassed by
   pushing to an explicit URL, and `agora sync` accepts *"a git remote name or URL"*
   (`src/agora_kb/cli.py:2181-2183`).
3. **The boundary: a marker + one predicate.**

**MIRROR marker: `<mirror>/.agora-mirror.yaml`**, a **closed key set**
`{kb_id, alias, source, pinned_commit, attached_at, role}` and nothing else — the closed posture of
`_meta/kb.yaml`, deliberately the opposite of the registry's tolerant one: *a registry is a
convenience file, a marker is a fence*. It is registered in the mirror's **untracked**
`$GIT_DIR/info/exclude` at attach time via `git rev-parse --git-path info/exclude` — the idiom the
curator already uses (`src/agora_kb/curator/worker.py:1915-1921`) — so it never shows as untracked
and can never be staged.

Why not the other two places: `_meta/` is git-**tracked**, and its own layout docstring already
rules that policy must not live there because *"a git-tracked enforcing `kind` would let an upstream
author unlock a downstream operator's personal-scope connectors"*
(`src/agora_kb/core/layout.py:393-408`) — inverted, the same argument forbids an upstream that could
**clear** a downstream fence. `_kb/` is git-ignored and documented as a rebuildable, expendable
spool (`src/agora_kb/core/layout.py:465-473`) with a shipped `agora index clear`, so a fence there
**fails open** on a cache wipe.

**The predicate keys on EXISTENCE, not on parse.** `.agora-mirror.yaml` present ⇒ refuse, whether or
not it parses. Parse only to enrich the message. **An unparseable marker refuses LOUDER, never
fails open.**

**`assert_not_mirror(layout: RepoLayout) -> None` lives in `src/agora_kb/core/inbox.py`,
immediately beside `assert_writable_repo_schema`** (`src/agora_kb/core/inbox.py:164`) and is called
from `Inbox.write` on the line after it (`:279`). Same rationale, verbatim: it is the **write
boundary's** rule, and *"a second copy of a three-line predicate is exactly how two call sites end
up disagreeing"*.

It is **additionally** wired at the **five** git mutation primitives, which ADR-0041 D6 does not
have and which is what closes the `agora sync` hole structurally: `Repo.commit_worktree`,
`Repo.compare_and_swap_branch`, `Repo.commit_all`, `Repo.push_backup` and **`Repo.sync_to_branch`**
(`src/agora_kb/core/repo.py:704`, `:728`, `:540`, `:556`, `:498`). The fifth is the one an earlier
draft's "four primitives" list missed, and it is the one that would move a mirror **off its pin**:
`sync_to_branch` runs `git merge --ff-only` or `git read-tree -m -u`, rewriting the working tree and
advancing HEAD (`src/agora_kb/core/repo.py:498-538`). It is latent today — its only caller is inside
the curator publish (`src/agora_kb/curator/worker.py:1824`, reached from `run`/`recover` at `:749`,
`:1361`, `:1511`), which D9 already refuses upstream — but "structurally read-only" that depends on
a caller's refusal is policy wearing structure's clothes, and the enumeration must be complete on
its own terms. The assertion must fire on the `RepoLayout` **before** the curator's temporary
detached worktree exists (`src/agora_kb/core/repo.py:668`, `:696`), because a root marker is
invisible inside that worktree and `commit_worktree` stages `git add -A` (`:22-24`).

**Attach and refresh are the ONE local writer of a mirror directory, and invariant 2 is not
violated.** A mirror's `wiki/` tree is a git-materialised **replica** of an upstream curator's
output, so invariant 2 (*"Only the curator writes `wiki/`, indexes, and `log.md`"*, `AGENTS.md:24-29`)
binds that **upstream** curator, not this machine. The only local writers of a mirror directory are
`agora kb attach` / `refresh` / `detach` — whole-tree materialisation and removal via `git clone`,
`git fetch --no-tags` + re-pin-by-checkout, and `rm -rf`, never a note edit — plus the root
`.agora-mirror.yaml` marker, which is outside `wiki/` and outside git. **Nothing else in this
process may write inside a mirror**, which is precisely what `assert_not_mirror` enforces at
`Inbox.write` and the five primitives above. Stated because a reviewer would otherwise have to
guess whether `agora kb refresh` is an invariant-2 violation or outside its subject; the same
sentence is echoed in the ADR-0006 amendment.

**Enumerated refusal surface.** Each row: the entry point, the wiring site, and the verdict.

| entry point | wiring | verdict |
|---|---|---|
| `agora curate` | `cli.py:1613`, beside `assert_writable_repo_schema` | **REFUSE** |
| `agora watch` | `cli.py:2318` (pre-flight, whole message) **and** `cli.py:2485` (per tick) | **REFUSE** |
| `agora requeue` | `cli.py:1833` — an `os.replace` that never touches `Inbox.write` | **REFUSE** |
| `agora harvest` | `cli.py:2097`, before the scan | **REFUSE** — a mirror can accept no candidates, so reading another agent's memory has no possible benefit |
| `agora capture` | inherits via `Inbox.write` (`cli.py:1250`) | **REFUSE**, no second gate |
| `agora sync` | `cli.py:2178` — **NEW, no gate exists today**; a pre-flight beside `is_initialized()`, **plus** the `Repo.push_backup` primitive | **REFUSE** |
| `agora repo init <mirror>` | `cli.py:728`, **before any emit** | **REFUSE** |
| `agora index build` | `cli.py:2599` (the operator verb), **not** inside `build_cache` | **REFUSE** (D11 — this materialises the cache #166 forbids) |
| `agora gold build` | `cli.py:2682` | **REFUSE** — a mirror composing an outbound pack is 0030's job and D10 forbids remote notes in gold |
| `agora import <vault> <mirror>` | already refused by `_assert_importable_destination` (`ingest/vault_import.py:1313`, `:1458`); enrich the message via the `is_mirror(layout)` predicate | **REFUSE**; the mirror sentence so the message names the real reason |
| `agora import --from-kb <mirror> <new>` | `ingest/kb_convert.py`, beside `_assert_convertible` (`:863`, refusal at `:900-905`) | **REFUSE in H1** — see below (**OD-12**) |
| `agora index status` / `index clear` | — | **ALLOW** (no cache is ever built for a reader KB, so both are no-ops) |
| web `POST /api/upload`, `POST /api/upload-batch`, `POST /upload` | inherit via the single write seam (`faces/web/app.py:1396`), plus a **named** `except MirrorRepoError` arm beside `:1422` returning **403** | **REFUSE** |
| MCP `kb_remember` | inherits via `Inbox.write` (`faces/mcp_server.py:145`) | **REFUSE** |
| MCP `kb_curate` | `faces/mcp_server.py:1193`, explicit, matching D6 | **REFUSE** |
| `Inbox.write` itself | `core/inbox.py:279` | **REFUSE** — the one call covering every future writer |
| `agora query` / `read` / `neighbors`, `status`, `doctor`, browse, graph | — | **ALLOW** — never routed through any writability predicate (`cli.py:309-314`) |

**`index build` is gated at the CLI verb, not inside `build_cache`.** `build_cache` has exactly two
callers (`src/agora_kb/cli.py:2612` and `src/agora_kb/curator/worker.py:1867`), and the second runs
inside a publish D9 already refuses, so gating the verb is sufficient **and** keeps
`assert_not_mirror` out of `core/wiki.py` — which matters because T5 pins the mention-set of that
symbol module by module.

**`agora import --from-kb <mirror> <new>` is a CUSTODY TRANSFER, and H1 refuses it.** An earlier
draft allowed it as *"the one crossing D6 authorises"*. That reading is too generous: D6 authorises
a conversion of a repo **this machine owns** from schema 1 to schema 2, not the wholesale copying of
someone else's KB out from under every reader-KB control. The converter reads a **schema-1** source
by construction (`CONVERTER_SOURCE_SCHEMA_VERSION = 1`, `src/agora_kb/ingest/kb_convert.py:87`,
enforced at `:900-905`) — exactly the class of mirror D12 deliberately allows attaching — copies
`raw/` **byte-identically** (D6 rule 5, `:29-30`), and enumerates **every** note through a direct
`parse_all_notes(src_layout, schema_version=1)` (`:965`) that is outside D11's `Wiki(population=)`
seam and has no people filter (`grep -n people src/agora_kb/ingest/kb_convert.py` → one comment at
`:1115`). After conversion those notes are ordinary local schema-2 notes under a **kind** directory,
so gold's exclusion — `is_ungraded_people_note`, i.e. `schema_version >= 2 and is_people_path`
(`src/agora_kb/core/gold.py:531`, `src/agora_kb/schema/notes.py:118-130`) — no longer matches them
and they enter the local pack. That is the same laundering shape as the #165 `file:`-connector
bypass this ADR escalates to a blocker in OD-5, reached by **one deliberate command** instead of a
config line; refusing the weaker path while allowing the stronger one would be incoherent. The
refusal is one line beside `_assert_convertible`, keyed on the same `is_mirror` predicate as
everything else in D9. **OD-12** records the alternative (allow it behind an explicit confirmation,
with D10/D11 restated as controls on *reads* of a mirror rather than on a conversion the operator
commands).

**Web upload refusals return 403, not 422 — and the reason is not the one an earlier draft gave.**
That draft said *"422 is the D6 arm, and it means this file is bad"*, which misreads the code it
cites: the arm at `src/agora_kb/faces/web/app.py:1422-1443` is the **repo-level**
`ReadOnlySchemaVersionError` refusal, and its own comment says the opposite — *"the operator's file
is fine; their REPO is the old half"* — while giving a client-compatibility reason for keeping 422:
*"the same status the route already uses for every other per-file capture failure, so a client
branching on the code needs no new case"* (`:1440-1442`). The genuinely file-level 422s are the
other two arms (`AttachmentError` `:1416-1421`, `ValueError` `:1444-1449`). The real argument for
403 is therefore narrower and is stated as such: a mirror refusal is **not per-file at all and is
constant for the whole repo**, so a client that retries the same upload against the same repo will
always fail, and a status that says *forbidden* rather than *unprocessable* is the one that stops
the retry loop.

**One consequence of that choice must be handled, or the status is invisible.** `_do_upload_batch`
wraps each per-file capture in `except HTTPException` and renders it as that file's own
`FileReceipt(error=…)` (`src/agora_kb/faces/web/app.py:1287-1288`), so a 403 raised at the shared
write seam would still surface as a per-file receipt error — the exact per-file framing 403 was
chosen to avoid. **The batch routes therefore check `is_mirror` ONCE, before the per-file loop, and
return a single batch-level 403**; the per-seam arm remains as the belt for the single-file route
and for any future writer.

**Exception shape.** `class MirrorRepoError(ConfigError)` in `agora_kb/config.py`, constructed like
`ReadOnlySchemaVersionError` — calling `ConfigError.__init__` **directly**, not `super()`, because
the parent's sentence and remedy would be wrong (`src/agora_kb/config.py:380-383`) — carrying
`{repo, alias, kb_id, source, pinned_commit}` as attributes so callers re-render rather than
re-parse. `ConfigError` is a `ValueError` (`src/agora_kb/config.py:132`), which is what makes the
ordering below load-bearing rather than tidy.

Because it is **not** an `UnsupportedSchemaVersionError`, define one module-level tuple
`_REPO_REFUSALS = (UnsupportedSchemaVersionError, MirrorRepoError)` and widen the **first**
except-arm at `cli.py:1614`, `:1834`, `:2098` and `:2319` to it. This is stated once so the trap is
not re-derived per site: every one of those sites orders `except UnsupportedSchemaVersionError`
**before** `except ConfigError` precisely because falling into the `ConfigError` arm would render
the verdict as `invalid config:`, *"mislabelling a schema verdict as a malformed file and sending
the operator to edit repo.yaml"*.

**Two sites are NOT in that list and each needs its own wiring, named here so neither is derived
from the pattern:**

- **`cli.py:2486` is not an except-arm.** `assert_writable_repo_schema(layout)` at
  `src/agora_kb/cli.py:2485` is followed at `:2486` by `now = datetime.now(UTC)` with no `try:`; the
  raise lands in the watch loop's blanket `except Exception` (`:2340`) → `_print_tick_failure`
  (`:2347`), which truncates to `_TICK_DETAIL_CHARS = 200` and backs off. For a schema verdict that
  is deliberate (#97). For a **mirror** verdict it is wrong for the reason the pre-flight comment at
  `:2311-2316` already gives — it would *"TRUNCATE the message … and then back off politely forever
  over a state no amount of waiting fixes"*. **Decision: the `agora watch` pre-flight at `:2318`
  owns the whole message, and the per-tick assertion raises a short, truncation-safe form
  (`<repo>: attached mirror — writes refuse (ADR-0037 D9)`) that the loop treats as terminal:
  `MirrorRepoError` is re-raised out of the loop with exit 1 rather than counted as a tick
  failure.** A mirror marker cannot appear and then resolve itself; backing off over it is a crash
  loop with extra steps.
- **`agora sync` has no arm to widen.** `_cmd_sync`'s only `ConfigError` handler wraps
  `load_backup_policy` and renders `sync: invalid config — …` (`src/agora_kb/cli.py:2202-2206`),
  and `repo.push_backup(policy.remote)` at `:2217` is guarded only by
  `except (GitError, ValueError)` at `:2218` — and `MirrorRepoError` **is** a `ValueError`, so
  without an explicit arm the mirror verdict would be printed as `sync: push failed — …` and
  recorded by `_record_backup_result(..., ok=False, ...)` as a failed backup push. **Decision: a
  pre-flight `assert_not_mirror(layout)` immediately after the `is_initialized()` check
  (`cli.py:2196`) inside a dedicated `except MirrorRepoError` arm, and a `except MirrorRepoError`
  arm ordered BEFORE `except (GitError, ValueError)` at `:2218` that re-renders the refusal and
  does NOT call `_record_backup_result`.** A repo this machine refuses to write has no backup
  outcome to record; writing one would put a false failure in `_kb/backup.json` and in
  `agora doctor`'s backup line.

**Refusal message**, modelled line-for-line on `src/agora_kb/config.py:383-392`:

```
<repo>: this KB is an ATTACHED MIRROR (alias 'hana-research', kb_id 01J9B…, pinned at 9f1c2d0abcde,
from git+ssh://git@forge.example/hana/kb.git) and is READ-ONLY BY CONSTRUCTION: agora writes only
KBs this machine owns. Reads (query, read, neighbors, status, browse, doctor, the MCP read tools,
the web read routes) keep working; writes (curate, watch, requeue, harvest, sync, repo init, index
build, gold build and every inbox capture) refuse rather than fork a KB whose upstream this machine
does not own. To change this content, write to its SOURCE; to refresh this copy,
'agora kb refresh hana-research'; to stop mirroring, 'agora kb detach hana-research' (ADR-0037 D9).
```

Unparseable-marker variant: the same sentence with `(marker unreadable: <exc>)` in place of the
parenthetical, and the same refusal.

**Why structure and not policy.** The registry cannot be the gate: every write entry point accepts a
bare path (`--repo <mirror>`, an MCP server pointed at the mirror directory, a web app built over
it) and consults no registry. The marker travels with the *directory*, which is what the operator
actually types.

**The same argument applies to the READ fences, and D10/D11 are keyed on the same marker.** A
registry-keyed read fence would be defeated by exactly the bypass this paragraph names for writes:
`agora query --repo $AGORA_HOME/remotes/hana` builds handlers from a bare path
(`_handlers(repo_path)`, `src/agora_kb/cli.py:1401`), `AgoraHandlers.__init__` constructs its own
`Wiki(repo.layout)` with no role input (`src/agora_kb/faces/mcp_server.py:118-123`), and
`build_server`/`build_app` take only a `repo_path` (`:1461`; `src/agora_kb/faces/web/app.py:470`).
D1 compounds it by resolving an alias to a **path** before dispatch, so no role survives into the
handler chain. **Decision: `is_mirror(layout)` — the same `.agora-mirror.yaml` existence test
`assert_not_mirror` uses — is the input to BOTH read controls.** A mirror is population-filtered
(D11) and serves no `raw/` (D10) whether it was reached through the registry, through
`--repo <path>`, or through a face pointed straight at the directory. The registry's `role: reader`
remains the operator's *declaration*; the marker is the *fence*, and there is exactly one of each.

#### D10 — Remote-ingest posture, and an attached reader KB's `raw/`

**Isolated clone.** The mirror lives under `$AGORA_HOME/remotes/<alias>` and is Agora-managed local
state; it is not a place for the operator's own files, so `agora kb detach` can remove it whole.

**Caps and refusals reuse the harvester's shipped numbers and shapes** rather than inventing new
ones: per-KB ceilings modelled on `src/agora_kb/harvester/connectors.py:236-241` and
`src/agora_kb/harvester/session_connector.py:122-125` (1 MiB per file is the `FileConnector` number
and is the right one for a `.md` corpus). **Non-`.md` skipped; oversize skipped; symlink rejected on
IDENTITY, not resolution** — the ADR-0018 precedent, where *a symlink is refused for what it is,
never graded on where it points* (`src/agora_kb/harvester/connectors.py:450-480`) — **every match
required to resolve WITHIN the mirror root** (`:560-562`), and **every refusal emitting a distinct,
target-naming note** so `agora doctor` can show *why* a KB banded thin.

**`strip_sentinel_spans` + the redactor on every remote excerpt.** The composed entry point is
`core.redact.sanitize()` = `strip_agora_sentinels` + `redact`
(`src/agora_kb/core/redact.py:284-293`), whose own docstring names it *"the entry point the future
`session:` connector / networked callers use"*. **ADR-0037 will be `sanitize()`'s first production
caller** — it has zero callers in `src/` today — so the composed path must be tested against real
corpus text (CJK, code fences, frontmatter, long tables), not only synthetic secrets.

For H1 the redaction runs **per emitted excerpt at band time**, which is cheap (the redactor
measured ≈ 19 MB/s single-core, ≈ 24 µs for a 438-byte excerpt) and fails **closed** if any cache is
later added in front of it. Two honest caveats are recorded rather than hidden: (a) an excerpt is
redacted *after* `line`/`anchor` were computed on unredacted text, so a hit's line number may not
align with the redacted rendering; (b) `AGORA_SPAN_RE` is DOTALL and first-closer-wins
(`src/agora_kb/core/sentinel.py:65-81`), so a remote note that legitimately contains an
`agora:pack` opener in prose and a much later `:end` will have everything between them dropped from
the banded text with no signal — the producer-side neutralization that defangs this runs on **our**
emissions, not on a remote's stored notes. Corpus-level redaction into a derived, commit-keyed cache
is the correct long-term order (it keeps line numbers self-consistent and stops a secret being a
searchable token) and is deferred to **OD-4**.

**Remote notes NEVER enter gold.** `PackAssembler` is constructed over exactly one `Repo` and holds
exactly one `RepoLayout`, and `assemble()` calls `parse_all_notes(self._layout)` over one worktree
(`src/agora_kb/core/gold.py:508-531`). **The invariant to preserve is structural: `PackAssembler`
keeps taking exactly one `Repo`, and no band composer, `ResolvedProfile` or `FederatedHit` may ever
be an input to it.** The H1 exit test *"no `role: reader` KB byte is in any gold pack"* is therefore
written against that single-layout construction — as a test that **no code path hands it a second
layout** (Test plan T3).

**`raw/` of an attached reader KB: REFUSED, and the refusal is written, not assumed.** A
commit-pinned clone **does** contain the remote's `raw/`, blobs included — only `_kb/` is ignored
(`src/agora_kb/core/repo.py:52-54`) and `.gitattributes` preserves `raw/_blob/**` bytes (`:64-79`) —
so absence cannot be relied on. `AgoraHandlers.raw()` is the **single shared seam** for `kb_read`,
`GET /raw`, `GET /api/raw` and `agora read`, *"so provenance cannot mean one thing on one face and
something else on another"* (`src/agora_kb/faces/mcp_server.py:271-277`); the refusal is therefore
**one DECISION site** — a mirror's handler serves no `raw/` on any face — but it is **not one edit**,
and the difference is what T8 asserts.

Two independent reasons, both recorded: (i) `raw/` is the one namespace whose content passed
**neither** the curator's PLAN/APPLY grading, **nor** the ADR-0007 candidate gate, **nor** ADR-0023
redaction — the handler's own docstring says so, and names the undesigned people/egress control
(R1/#166) as what would gate it; (ii) serving another operator's ungraded bytes through a band is
the widest possible egress for the smallest benefit.

The refusal is a **FOURTH status, `refused`**, distinct from `AgoraHandlers.raw()`'s existing
`ok` / `not_found` / `invalid_path`. Naming it leaks nothing: unlike `rawstore`'s deliberate
None-for-everything (`src/agora_kb/core/rawstore.py:41-45`), which exists so a caller cannot walk
the filesystem one status at a time, this is a **policy answer about a whole KB**, constant for
every path, and therefore not a probing oracle. Its note reads:

```
attached KBs are read-only mirrors of the curated wiki; raw/ captures are not served (ADR-0037 D10).
```

so the agent stops following that KB's `sources:` strings instead of retrying.

**The fourth status must be PROPAGATED at three call sites or it is swallowed.** Every consumer of
`AgoraHandlers.raw()` today narrows on `== "ok"` and collapses everything else into the not-found
shape, so adding `refused` at the handler alone would render as `not_found`/404 on all three faces:

| consumer | today | required change |
|---|---|---|
| `kb_read` | `raw = handlers.raw(path); if raw["status"] == "ok": return raw` → `_kb_read_not_found(path, raw_note=…)` (`src/agora_kb/faces/mcp_server.py:1547-1556`) | pass `refused` through as its own payload |
| `agora read` | the same algorithm spelled a second time in `_read_payload` (`src/agora_kb/cli.py:1485-1502`) | **not a second edit** — D5 collapses these two copies into one, and the refusal rides that collapse |
| `GET /api/raw` (and `GET /raw`) | `if payload["status"] != "ok": raise HTTPException(404, detail=payload["note"])` (`src/agora_kb/faces/web/app.py:588-589`) | a `refused` status becomes **403** with the same note, matching D9's web-upload reasoning: constant for the whole repo, so a retry is pointless |

**A remote note's `sources:` rows need ONE new argument at TWO call sites; they do not degrade
correctly on their own.** The web face decides linkability server-side with the same predicate
`/raw` enforces — `rawstore.resolve` returns a ref and it is not a sidecar — and *"with
`raw_enabled` off every row is plain text, because a link to a disabled route is worse than no
link"* (`src/agora_kb/faces/web/app.py:1739-1780`). But that predicate is
`raw_enabled and rawstore.resolve(layout, entry) is not None and ref.kind != "sidecar"` (`:1770-1776`),
and **neither half knows about a mirror**: `raw_enabled` is a single app-wide operator switch
defaulting to `True` (`src/agora_kb/config.py:1242`, loader default at `:1442-1443`), a mirror has no
`_kb/repo.yaml` to override it (only `_kb/` is git-ignored, `src/agora_kb/core/repo.py:52-54`), and
this ADR has already established that the clone carries `raw/` in full — so `resolve` **succeeds**.
Left alone, a reader KB's note page would offer live `/raw` links into the route D10 refuses.
**Decision: the reader-KB refusal is an input to the linkability predicate.** `render_note_body`
and `_source_rows` already take a `raw_enabled` keyword (`src/agora_kb/faces/web/app.py:1739-1741`,
`:1781-1783`), and the `/note` route already threads it at both call sites (`:827`, `:839-843`,
`:846-850`); a reader binding passes `raw_enabled=False`, so the existing "plain text" branch is
reused rather than a second link rule being written. This is a **known cost**, not a costless
trade: a remote claim becomes uncheckable against its evidence, and the named future direction is a
remote `agora read` proxied through the owning operator's own face — not a wider mirror.

#### D11 — The #166 clause

> **A `role: reader` KB's `wiki/people/**` notes enter no band result, no graph, and no cache.**

It is a **POPULATION filter, not an eligibility filter** — the notes are **never candidates**, not
candidates that get dropped late. `core/gold.py` states the reason and this ADR cites rather than
restates it: an eligibility-only exclusion *"would still let `wiki/people/**` move the pack's
contents through the graph"* (`src/agora_kb/core/gold.py:519-531`), and gold keeps a **second
layer** in `_eligible()` (`:640`). Both layers are required to claim parity.

**Where it is applied.** There are **two** enumerators reachable through `Wiki` — and the clause
needs both, or it is enforced for `kb_query` and open for `kb_read` / `kb_neighbors` / web browse,
which #166's own table names as a pull surface — **plus a third direct `parse_all_notes` call in
`agora doctor`**, which D9's table ALLOWs on a mirror:

| seam | file:line | what it feeds |
|---|---|---|
| `Wiki._iter_note_files()` | `src/agora_kb/core/wiki.py:964-986` | `_load_notes_uncached` (`:987-995`), `_load_notes` (`:996-1030`) → the ranker (`:828`), and `build_cache` (`:1697`) |
| `schema.notes.parse_all_notes(layout)` **via `Wiki.list_notes()`** | `src/agora_kb/schema/notes.py:429`; `src/agora_kb/core/wiki.py:898-916` | `get_note`, `AgoraHandlers.browse`, `health()`, `graph()`, and `gold` |
| `_doctor_parse_notes(layout)` — a **direct** call, outside `Wiki` | `src/agora_kb/cli.py:3390-3401` | `_doctor_notes` and the `--agents` SOURCE-SEEN column (`cli.py:2934`, `:2943`, `:2948`; `_agent_seen_cell` at `:3375-3387`) |

The other direct `parse_all_notes` callers are covered elsewhere and are listed so the enumeration
is closed: `core/gold.py:531` (D9 REFUSEs `gold build`, and D10 bars remote notes from gold),
`schema/lint.py:1092`/`:1111` (a mirror is never linted, D12), `curator/worker.py:415` (curate is
REFUSEd), and `ingest/kb_convert.py:965` (`import --from-kb`, now REFUSEd on a mirror — D9).

**Doctor's third enumerator is IN SCOPE and is filtered, but the reason is worth stating:** it emits
**counts, not content** (`notes:` totals and a per-agent "N notes" cell), so the leak is small — but
`agora doctor --registry` is precisely the surface that will be pointed at a mirror, a count is
still an inference about a colleague's private tree, and leaving one enumerator unfiltered would
make the clause "no cache, no graph, no band — except one table" rather than a population rule.
`_doctor_parse_notes` therefore applies the same predicate when `is_mirror(layout)`.

**One predicate, three seams.** `Wiki.__init__` gains an **additive keyword**
`population: PopulationFilter | None = None` (default `None` ⇒ today's behaviour byte-identical),
applied inside `_iter_note_files` and to `list_notes`'s `parse_all_notes` result. Because
`AgoraHandlers.__init__` constructs its own `Wiki(repo.layout)` (`src/agora_kb/faces/mcp_server.py:118-123`),
an additive `Wiki` keyword alone would **not** reach `browse`/`health`/`graph`/`note`: **`AgoraHandlers`
gains the matching `population: PopulationFilter | None = None` keyword and relays it**, which is the
wiring site T7's `browse`/`graph` assertions need. The hub constructs a reader binding's `Wiki` and
`AgoraHandlers` with that filter; an owner binding passes `None`. Both derive the filter from
`is_mirror(layout)` (D9), not from the registry, so a bare-path read of a mirror is fenced
identically to a bare-path write.

**The predicate for an ATTACHED KB is the unconditional `schema.notes.is_people_path`, not the
version-gated `is_ungraded_people_note`.** The version gate exists so a caller *agrees with
`lint()`* (`src/agora_kb/schema/notes.py:118-130`), and **an attached KB is never linted** (D12), so
the gate buys nothing there — while a schema-1 remote that merely owns a literal `people` domain
would otherwise be admitted on a version technicality. The **local** KB keeps
`is_ungraded_people_note` unchanged. This asymmetry is deliberate; see **OD-3**.

**A band-time drop is NOT an acceptable implementation.** `Wiki.query` is a pure delegation to
`query_lexical` and the pipeline is closed (`src/agora_kb/core/wiki.py:799-805`, `:828-855`), so
anything that removes hits *after* `query_lexical` returns is an eligibility filter by construction:
IDF, in-degree and `d_moc` were already computed with those notes in the corpus. Injecting at the
enumerators is strictly **upstream** of that closed pipeline and therefore touches nothing #144 pins.

**Accept the intended side effect explicitly**: excluding the notes from the population changes that
mirror's `n`, `df` and `max_indeg`, so a reader KB's scores are computed over the people-free
corpus. That is correct — those notes are not eligible evidence in that KB at all.

**The LOCAL half is UNCHANGED and out of scope.** The local KB's own `wiki/people/**` stays open on
`kb_query` / `kb_read` / `kb_neighbors` / web browse, per ADR-0041 D3.3 and residual risk R1
(`docs/adr/0041-stratum-kind-first-layout.md:522-535`, `:1028-1034`). #166's *"Open beyond that
clause"* paragraph remains the owner of the rest. **A change to the local posture is an ADR-0041
amendment, not an ADR-0037 clause.** Stated in one sentence so a reader cannot infer that the
reader-KB clause silently applied to their own tree — and stated a second time in the band UI,
because two KBs in one band now have different people postures, and an operator who concludes
"people notes are private" will be wrong about their own KB
(`docs/LIMITATIONS.md:553-556` already warns: *"Treat `wiki/people/**` as human-owned and
searchable, not as private"*).

**"No cache" is net-new, not free.** A fresh mirror has no `_kb/` (git-ignored,
`src/agora_kb/core/repo.py:52-54`) and the read path never writes one
(`src/agora_kb/core/wiki.py:996-1005`) — but `agora index build` would create one from an
**unfiltered** enumeration (`src/agora_kb/core/wiki.py:1694-1708`), and `build_cache`'s own
docstring already scopes it to *"the owner tree"* (`:1665-1672`). That is why D9 refuses
`agora index build` on a reader KB. The cost is accepted: every federated query full-parses each
attached mirror, capped by `merge.max_kbs` and `merge.per_kb_limit`. **If performance later forces a
cache for reader KBs it must live under `$AGORA_HOME`, never inside the mirror worktree** (writing
`_kb/` into a mirror dirties the pin and writes into another operator's tree), and it must inherit
this exclusion, or the clause is reopened through the cache.

**Blocking rule (#166 "Done when").** Either this clause is implemented alongside the first attach
code path, or **`agora kb attach` refuses to admit a KB until it is**. Attach must not ship with the
clause as documentation only.

**The `file:` connector is an ungated bypass and must be closed in the same unit.**
`_require_source_path` *requires* an absolute or `~`-rooted path
(`src/agora_kb/harvester/connectors.py:198-210`), so `$AGORA_HOME/remotes/<alias>/wiki/**/*.md` is a
legal connector glob, and the only content exclusion in the scan is `_is_within_gold` (`:810-818`).
`docs/LIMITATIONS.md:544-551` states the fence has no code and that such a glob *"carries
human-owned content into the inbox and thence into `raw/`"*. Consequence: an operator could harvest
an attached KB's people notes into the **local** inbox, where the curator may publish them into the
**local** wiki and thence into the **local** gold pack — defeating both this clause and "remote
notes never enter gold" through configuration alone, with only the ADR-0007 candidate gate as a
brake. **Either #165's fence lands in the same unit, or `agora kb attach` refuses while any configured
connector glob resolves inside `$AGORA_HOME`.** See **OD-5**.

#### D12 — An attached KB on schema 1: reads work, writes refuse, and the registry says so

`SUPPORTED_KB_SCHEMA_VERSIONS` is `frozenset({1, 2})` and writability is the strictly narrower
question of equality with `MAX_SUPPORTED_KB_SCHEMA_VERSION`
(`src/agora_kb/config.py:295`, `:300`, `:398-441`). A schema-1 attached KB therefore **reads
normally** and **cannot be written** — which is moot, because a `role: reader` KB cannot be written
anyway.

**Attach is ALLOWED for a schema-1 KB**, and the registry surfaces it. Refusing would strand exactly
the KBs `SUPPORTED = {1, 2}` was widened for.

**Two verdicts, two scopes — normatively.** `agora doctor` today prints
`write: READ-ONLY — this build reads KB schema 1 and refuses to write it…` and **fails the verdict**
with exit 1 (`src/agora_kb/cli.py:3002-3030`, reached from `_doctor_schema` at `:2996`). That is
correct for a repo you own and **wrong for a mirror you deliberately cannot write**. So:

- the **host** repo's verdict stays keyed on the host repo;
- attached KBs are reported in their **own read-only section**, `agora doctor --registry`, which is
  **observation-only and never affects `ok`** — copying the contract comment verbatim from
  `src/agora_kb/cli.py:2937`/`:2941` and wrapping the table in the same `try/except` `_doctor_agents`
  uses (`:3199-3202`).

Row shape, one per binding. Note the `kb_id` column on the schema-1 row: `_meta/kb.yaml` **does not
exist on schema 1** (D4), so the cell is `-`, never a value carried over from the registry line:

```
  registry: 3 attached
    general         owner   local:/Users/me/knowledge          schema=2  pinned=-         kb_id 01J8Z…
    hana-research   reader  git+ssh://forge.example/hana/kb    schema=2  pinned=9f1c2d0  kb_id 01J9B…
    legacy-team     reader  git+ssh://forge.example/old/kb     schema=1  pinned=3ab77e1  kb_id -        read-only:schema-1
```

Without this rule the registry silently harbours a repo `doctor` calls unhealthy — and, worse,
training operators to ignore `status: unhealthy` is the exact opposite of what #96 bought.

**An attached KB is never linted.** `lint()` is producer grading and a mirror has no local producer;
`lint(` has exactly **one** CLI call site, inside `_cmd_repo_init`
(`src/agora_kb/cli.py:906`), so `agora doctor` and `agora status` do not lint at all and this is
true by construction for the CLI. The **one** surface that would lint a mirror is the web dashboard
via `AgoraHandlers.health()` (`src/agora_kb/faces/mcp_server.py:549`;
`src/agora_kb/faces/web/app.py:726`, `:1021`, `:1031`), so the `?kb=` switcher must not offer
`/dashboard` for a `role: reader` KB. (A mirror without a local producer would otherwise report a
wall of findings for a repo the operator cannot fix.)

`agora status` gains a mirror line in the existing `key: <machine-readable value>` grammar, with the
remedy on **stderr**, exactly as the D6 read-only line does (`src/agora_kb/cli.py:1331-1335`), and
one shared sentence function beside `_read_only_schema_note` (`:685-698`) so status, doctor and the
refusal all say the same thing:

```
mirror: hana-research (READ-ONLY — writes refuse)
```

### D. Boundaries

#### D13 — The 0030 / 0037 split

The split is quoted, not paraphrased. `docs/adr/README.md:41`:

> `0030 reserved: federation / team-audience pack composition — Phase-4-coupled, not yet authored.`
> `SPLIT AGAINST 0037 (see below): 0030 owns OUTBOUND PACK COMPOSITION — the ADR-0027 §7 role rule`
> `(federation is the sole COMPOSER; bridges/agents are pure consumers), the additive `scopes`
> parameter, and §9's obligation to CITE ADR-0027 §8 rather than restate it. It does NOT own the
> local registry.`

`docs/adr/README.md:48`:

> `0037 owns the LOCAL READ-ONLY REGISTRY — alias resolution, `kb_id` as join/display identity`
> `(never an authorisation input, ADR-0041 D1.5/R3), attach, and result banding. It owns NO outbound`
> `pack composition; that stays 0030, which ADR-0027 §7/§9 names normatively. An ADR-0037 that`
> `starts composing packs has taken 0030's job.`

Three normative sentences follow:

1. **This ADR produces no pack and defines no `scopes` parameter.** `kb_context(pack, scopes?)`
   remains ADR-0027 §7's tool with **ADR-0030** as its COMPOSER
   (`docs/adr/0027-gold-context-packs.md:151-157`).
2. **This ADR's bands are a read-time ordering of per-KB `QueryResult`s — composition of *results*,
   not of *packs*.** The distinguishing test, stated so it can be applied mechanically: **a band
   result is never written to `_kb/gold/` and never re-enters a pack.**
3. **If the bands ever need to emit a single merged artefact to an agent, that artefact is a pack
   and the work stops until 0030 is authored.**

**One same-unit obligation on ADR-0027.** §8 declares itself *"the single normative spec for every
Agora→agent emission path"* and names ADR-0023's session distiller, reserved ADR-0026, and reserved
**ADR-0030 (federation)** as the ADRs that must cite it
(`docs/adr/0027-gold-context-packs.md:159-162`). That list predates the 0030/0037 split and does not
name 0037. A band that emits a remote KB's excerpts to an agent **is** an Agora→agent emission path.
ADR-0027's second banner already declares itself *"the single index of scope changes"* (`:5`), so
the mechanism exists: a **third append-only banner line** on ADR-0027 records the band path as a
further emission path on the same PULL surface. The exact text is in `0006-amendment.md`.

**The citation is scoped, because ADR-0041's own Accepted banner on `0027:4` forbids the wider
reading:** *"the PULL surface (agent-initiated MCP reads) is hereby named as an emission path whose
control is DISTINCT and STILL UNDESIGNED, owned by ADR-0041 residual risk R1. Neither the push
exclusion nor §8 may be cited as covering it."* So the sentence is **not** "ADR-0037 cites §8 and is
therefore compliant". It is: **ADR-0037 inherits §8 unchanged for anything that IS a pack — and a
band is not one** (§8's operative rules are pack-shaped: *"Every emitted pack is wrapped
`<!-- agora:pack … -->`"*, `docs/adr/0027-gold-context-packs.md:163-168`, with assembly-time
neutralization a `PackAssembler` duty, and §9's cite list at `:159-162` enumerating pack
PRODUCERS/COMPOSERS — ADR-0023's distiller, reserved 0026, reserved 0030 — which D13 sentence 2
says the bands are not).

For the band path's own R1 status, this ADR follows `0027:5`'s treatment of the `raw/` bridge —
**named, not folded**:

- the **reader-KB half is CLOSED**, by two named controls this ADR supplies rather than inherits:
  D11's `wiki/people/**` population filter and D10's `raw/` refusal, plus `sanitize()` on every
  remote excerpt;
- the **local half is UNCHANGED and remains R1's undesigned control** (D11's "the LOCAL half is out
  of scope"), and §8 may not be cited as covering it here either.

**This ADR cites §8; it does not restate it, and it does not claim §8 covers the band path.**

#### D14 — Invariants 7 and 8

The ADR is the normative source; `AGENTS.md:21-34` is the mirror. The **only** enumerated invariant
list in the repo is that one (`CLAUDE.md` is a symlink to `AGENTS.md`); `docs/DESIGN.md` references
invariants **by number only** and contains no list, so no third copy is created. The exact sentences:

> **7. Different audience or custody means a different repo.** The only two cross-KB movements are
> **read composition** and a **provenance-carrying inbox event**; nothing else crosses a repo
> boundary — no merge, no shared wiki, no write into a KB this process does not own (ADR-0006 as
> amended by ADR-0037).

> **8. Federation never erases identity.** Every **federated result** carries the alias, `kb_id` and
> revision of the KB it came from, and everything derived from a KB must be removable **per repo**
> when that KB is revoked, detached, or deleted. `kb_id` is display/join identity only — never an
> authorisation input (ADR-0041 D1.5/R3).

**Invariant 8's first clause is deliberately narrowed to what this ADR delivers and tests.** An
earlier draft wrote *"every band result, graph node, gold pack and cache entry carries the `kb_id` +
revision"*, which is **false for three of its four artefacts in the frozen tree and is made
permanently unachievable by this ADR's own T9**: `grep -rn kb_id src/agora_kb/faces
src/agora_kb/core/gold.py` is **empty**, a gold pack's sentinel is
`<!-- agora:pack repo={layout.root.name} pack=… commit={curated_sha} -->` — directory basename plus
commit, no `kb_id` (`src/agora_kb/core/gold.py:672-674`) — a graph node carries
`{id, title, subjects, status, kind, orphan}` and no identity stamp at all, and `CachePayload` is
exactly `{cache_schema_version, curated_commit, notes}` (`src/agora_kb/core/index_cache.py:96-98`).
Writing the wide clause into `AGENTS.md` as a **non-negotiable invariant** in the same commit that
forbids ever satisfying it is the failure mode to avoid. (The *revision* half does already hold for
gold's `commit=` and the cache's `curated_commit`; only the `kb_id` half fails.)

**Stamping `kb_id` onto graph nodes, gold packs and cache entries is therefore a NAMED DEFERRAL, not
part of invariant 8.** It is not needed for H1 — a reader KB has no cache (D11) and never enters
gold (D10), and the graph is per-handler — and it would be the right shape for a later ADR that
makes any of those three artefacts cross a KB boundary. Recorded here so a future reader does not
find the narrowed invariant and conclude the wide property was overlooked.

Both invariants are wired to concrete mechanisms so they are testable, not aspirational. Invariant 8
is satisfied by: the `FederatedHit`/`KbBand` wrapper carrying alias + `kb_id` + pinned commit (D6);
every derived artefact for a reader KB being keyed under `$AGORA_HOME/remotes/<alias>` so **revoke =
delete one directory**; gold never seeing a remote note at all (D10); and D7's rule that the
`[[basename]]` identity space never crosses a band.

Adding invariants is itself an ADR-bearing act (`AGENTS.md:21` — *"do not violate without an ADR"*),
so the `AGENTS.md` text is **gated on this ADR reaching Accepted** and lands in that commit, not in
the authoring commit — see `0006-amendment.md`'s landing checklist, which separates the two.
**Nothing in the repo tests the invariant list today**, so it is review-enforced only unless OD-8 is
taken.

#### D15 — Process model: per-KB instances, no module-global mutable state

Three structural clauses:

1. **Per-KB `Repo` / `RepoLayout` / `Wiki` / `AgoraHandlers` instances.** Already the shape:
   `RepoLayout` is a frozen dataclass over one root (`src/agora_kb/core/layout.py:285-296`),
   `Repo.resolve` makes one with no git calls (`src/agora_kb/core/repo.py:289-292`), `Wiki` stores
   only a layout (`src/agora_kb/core/wiki.py:784-786`), and `AgoraHandlers` builds its own
   Inbox/Wiki/StateStore over one repo (`src/agora_kb/faces/mcp_server.py:118-123`). **Every
   constructor in that chain is pure — no I/O, no git — so building N bindings eagerly at process
   start is free**, and the `Federation` object is therefore built **once** and **immutable after
   start**: an ordered `tuple[KbBinding, ...]`, never a dict mutated per request.
2. **No module-global mutable state.** The audit is clean: zero `functools.lru_cache`/`@cache`, zero
   `global` statements, zero import-time `os.environ` reads, zero `os.chdir`, zero
   `logging.basicConfig`, zero `sys.path` mutation in `src/`. The web face resolves policy per-repo
   with the rationale in-code — *"never a module-global the browser could flip across repos"*
   (`src/agora_kb/faces/web/app.py:496-498`) — and the Prometheus exporter builds a **fresh**
   `CollectorRegistry` per render, never the global default
   (`src/agora_kb/faces/web/metrics.py:302-318`).
3. **Every write path names its KB explicitly**, with the per-repo `_kb/curator.lock`
   (`src/agora_kb/curator/claim.py:58-77`) as the write-custody gate. A second fd on the same lock in
   the **same process** is refused (`flock(LOCK_EX|LOCK_NB)` → `BlockingIOError`), so a multi-KB
   process structurally cannot double-curate one repo. (Verified on darwin; the portable replacement
   is reserved **ADR-0038**.)

**The one exception, named and fenced.** `agora_kb.core.wiki.FM_ENABLED`
(`src/agora_kb/core/wiki.py:100`, read at `:1618`) is rebound process-wide by
`rank_snapshot._fm_mode` (`src/agora_kb/core/rank_snapshot.py:282-300`), whose own docstring says it
*"is not safe to run concurrently with another query in the same process"*. It is the **only** object
in the tree that violates clause 2. **Decision: the registry/band path MUST NOT call `_fm_mode`, and
`agora eval` stays single-KB and is never run inside a hub process.** `agora eval` writes nothing
unless `--out` is given (`src/agora_kb/cli.py:2761-2776`), so it is otherwise safe on a mirror. The
alternative — moving `fm_enabled` onto the `Wiki` instance — is **OD-7**. Writing "no module-global
mutable state" without naming this would make the invariant false on day one.

**The web face's one genuine process-wide constraint.** Four `WebConfig` keys are bound at **app
construction** and cannot vary per `?kb=`: `web.features.graph_enabled` and
`web.features.raw_enabled` are Jinja globals (`src/agora_kb/faces/web/app.py:504`, `:509` — a *soft*
constraint, liftable by moving them into the per-route context), while
`web.security.require_origin` and `web.security.allowed_hosts` are baked into the ASGI middleware
stack (`:522-525`) and are genuinely process-wide. **Profile mode therefore requires ONE
`$AGORA_HOME`-level `web:` policy for those two keys and REFUSES TO START on a per-KB mismatch** —
never a silent union, which would be a silent widening of a security control. Everything else
(graph caps, upload limits, extensions, identity) is read per-request from the closure and is
re-resolved per binding.

**Web `?kb=` is minimal in H1**, deliberately, so the existing web test suite is an *extension*
rather than a *migration*: (1) `?kb=<alias>` on read routes only; (2) a per-request
`_handlers_for(kb)` returning the app's default handlers when `kb` is absent, so every existing URL
and test keeps its exact behaviour; (3) one Jinja global `kb_qs` appended to internal links, because
all 26 routes and every template link are absolute paths today and a switcher without threading
drops the reader back to the default KB on the first click; (4) a plain no-JS `<select>`-shaped list
of `?kb=` links in `base.html`; (5) **write routes do not accept `?kb=`** in H1, and `/dashboard` is
not offered for a `role: reader` KB (D12). A banded global search **UI** is explicitly outside H1.

**Startup validation, all fail-loud, refuse to start:** every alias pathsafe and unique
(case-folded); every `read:`/`write:` alias resolves in the registry; `write:` is exactly one alias
whose role is not `reader`; that write KB passes `assert_writable_repo_schema` (say it at boot, not
at first capture); no two bindings resolve (`Path.resolve()`) to the same root; a duplicate `kb_id`
across bindings **warns** and forces distinct aliases; the web security-key pair identical across
bindings (or one `$AGORA_HOME`-level policy); and every `role: reader` mirror root carrying a
`.agora-mirror.yaml` whose fingerprint matches its registry entry.

**Three things are deliberately NOT in that list**, because one remote's rot must not stop the whole
hub: **(a)** the mirror's `_meta/kb.yaml` identity read, which is tolerant by D4 and degrades to
`kb_id: none`; **(b)** a mirror root that is missing, unreadable or not an Agora repo, which bands
`unavailable` (D6) rather than refusing to start; **(c)** a mirror on schema 1, which reads normally
(D12). The fail-loud list is about **operator-authored grammar** — registry, profile, security keys
— which the operator can fix locally in seconds. A remote's state is not.

**Resolve mirror roots with `Path.resolve()`, not `Path.absolute()`.** `RepoLayout` normalises with
`.absolute()` and explicitly does not resolve symlinks
(`src/agora_kb/core/layout.py:293-296`), so a root-equality check written against `layout.root` is
defeated by `ln -s`. A structural control that a symlink defeats is a policy control wearing
structure's clothes.

---

## What this ADR does NOT own

Quoted from `docs/adr/README.md:41`, verbatim:

> **0030 owns OUTBOUND PACK COMPOSITION** — the ADR-0027 §7 role rule (federation is the sole
> COMPOSER; bridges/agents are pure consumers), the additive `scopes` parameter, and §9's obligation
> to CITE ADR-0027 §8 rather than restate it. It does NOT own the local registry.

And from `docs/adr/README.md:48`, verbatim:

> **It owns NO outbound pack composition; that stays 0030, which ADR-0027 §7/§9 names normatively.
> An ADR-0037 that starts composing packs has taken 0030's job.**

Also **not** owned here:

- **Authorisation of any kind.** No `Principal`, no token, no permission check. ADR-0036 remains the
  first component that will *enforce* invariant 5 rather than inherit it
  (`docs/adr/0036-authn-authz.md:51-52`). The registry's `role:` is a **local structural
  capability declaration** about a local path, asserted by the local operator, and is **never** the
  ADR-0036 `reader`/`writer`/`curator-admin` lattice despite the shared words — reading it as a grant
  would re-create the domain-ACL false promise ADR-0036 demoted (`docs/adr/0036-authn-authz.md:143-150`).
- **S2 contributor** (append into a remote's `_kb/inbox/<principal>/`), which is H2 and auth-coupled.
- **The local KB's own `wiki/people/**` pull posture**, which stays ADR-0041 D3.3 / R1 / #166.
- **Bidirectional sync, pull, or reconciliation.** The only new git verb this ADR admits is `fetch`,
  and only inside `agora kb refresh` (`git fetch --no-tags -- origin <ref>` then re-pin by checkout,
  never `pull`). `src/agora_kb/core/repo.py:26-29` currently states normatively that no fetch code
  exists; that sentence is amended by the attach implementation, not by silence.

---

## Consequences

**+** One process, one agent session, one browser tab reaches N KBs, with no merge and no shared
wiki. Read composition is the *only* new cross-KB movement, which is exactly invariant 7.

**+** ADR-0012 is untouched: no field is added, no weight or ordering changes, and §11's deferral is
discharged by decision rather than by an addendum. The `#144` write-path oracle gains a **mechanical**
guard (zero `query_lexical` under `hub/`) that is stronger than the prose pin it had.

**+** The `curate`-on-a-clone hole — proven live, and the `agora sync` variant of it pushes into
someone else's repo — is closed by one predicate at one boundary plus five git primitives, matching
ADR-0041 D6's shape, which is already test-pinned in both directions.

**+** `raw/`, the emission path Wave A opened, gets its first real access rule instead of an assumed
absence.

**+** Everything derived from a reader KB lives under `$AGORA_HOME/remotes/<alias>`, so **revoke =
`rm -rf` one directory** — invariant 8's removability clause becomes a filesystem fact.

**−** **Performance.** A reader KB has no `_kb/` and D9 refuses `agora index build` on it, so every
federated query full-parses every attached mirror. With `max_kbs: 8` a single `--kb all` is up to
eight full markdown scans with no cache in front of any of them. Capped, not solved.

**−** **Mirror DISK, and it is sharpened by two of this ADR's own decisions.** OD-6(A) mandates a
**full clone**, and only `_kb/` is git-ignored (`src/agora_kb/core/repo.py:52-54`) while
`.gitattributes` preserves `raw/_blob/**` bytes verbatim (`:73-79`) — so every attached KB costs its
upstream's entire `raw/` tree, binaries included, on local disk, for content **D10 then refuses to
serve on every face**. The operator pays full storage for bytes the design will never surface. This
is the strongest argument for OD-6(B) — and it is a *disk and bandwidth* argument, not an access
one, which is exactly how OD-6 says B must be described.

**−** **Resident memory of N unfiltered corpora per federated query.** The performance cost above is
not only wall time: with no `_kb/` cache, `max_kbs: 8` means up to eight full note corpora parsed
into memory during one `--kb all`, not eight sequential scans that each free before the next.

**−** **Provenance drill-down is lost for remote hits.** Refusing a mirror's `raw/` removes exactly
what #169 Wave A was built to add. A remote claim becomes uncheckable against its evidence.

**−** **Two people postures in one band.** The operator's own people notes are searchable and
returned; a teammate's are not. Deliberate (D3.3 vs #166), and a real footgun if the band UI does not
badge every hit.

**−** **A new durable local surface.** `$AGORA_HOME` is the first user-level config in the codebase
(`grep -rn AGORA_HOME src/` is empty today; `Path.home()` appears nowhere in `src/`). Two new YAML
loaders, a marker format, and a mirror directory tree are all net-new state to back up, migrate and
document.

**−** **Impersonation stays open.** A remote asserts its own `kb_id`, and homograph confusables
survive the alias grammar (ADR-0041 R4). The mitigation is a badge, which is presentation.

**−** **Alias grammar is one-way.** Loosening later is safe; tightening later invalidates registries
operators have already written and badges already rendered.

**−** **`sanitize()` runs in production for the first time.** Its composed behaviour on real remote
corpus text is unexercised (zero `src/` callers today).

---

## Open sub-decisions (Proposed; recommendations carried but not yet ratified)

1. **OD-1 — One file or two?** *A)* `registry.yaml` (what exists) + `profile.yaml` (what this caller
   reads/writes), per the H1 sketch. *B)* one merged `$AGORA_HOME/agora.yaml`.
   **Recommendation: A.** The two answer different questions and change at different rates — a KB is
   attached once, a profile is edited per task — and `docs/DESIGN.md:469` and
   `docs/adr/README.md:48` already name both shapes. Cost: two loaders, two failure modes.

2. **OD-2 — `agora index build` on a `role: reader` KB.** *A)* refuse. *B)* allow (the cache is
   derived, git-ignored and rebuildable, and the bands would be much faster with it).
   **Recommendation: A (refuse), as written in D9.** `build_cache` writes from an **unfiltered**
   enumeration (`src/agora_kb/core/wiki.py:1694-1708`) and its own docstring scopes it to *"the owner
   tree"* (`:1665-1672`), so allowing it makes #166's "no cache" clause violable by a supported CLI
   verb. If B is chosen, the cache **must** be built from the D11-filtered population and **must**
   live under `$AGORA_HOME`, never inside the mirror.

3. **OD-3 — People predicate for an attached KB.** *A)* unconditional `is_people_path`. *B)*
   version-gated `is_ungraded_people_note`, matching every existing caller.
   **Recommendation: A**, as written in D11 — the version gate exists to agree with `lint()`, and a
   mirror is never linted, while B would admit a schema-1 remote's literal `people` domain on a
   technicality. B's merit is uniformity: one predicate everywhere is one fewer rule to remember.

4. **OD-4 — Redaction timing for remote content.** *A)* `sanitize()` per emitted excerpt at band time
   (H1). *B)* redact the mirror corpus once into a derived cache keyed on
   `(kb_id, pinned_commit, rel_path, source_digest)` under `$AGORA_HOME`, invalidated the way the
   ADR-0012 §2 cache is, with a band-time `sanitize()` retained as a belt.
   **Recommendation: A now, B when a cache is added.** B is the correct *order* (redaction shifts
   byte offsets — the PEM rule collapses a whole block into one placeholder — so line numbers stay
   self-consistent only if redaction precedes parse; and a secret then is not a searchable token),
   but it is a new durable artefact holding remote content, and `CACHE_SCHEMA_VERSION` discipline
   would have to treat the **redaction policy** as part of the derivation, or a tightened
   `DEFAULT_ON_CLASSES` leaves stale under-redacted text in place. **Never *(C)* redact into the
   mirror worktree**: that breaks the pin's meaning, leaves `git status` permanently dirty, and turns
   the next fetch into a conflict.

5. **OD-5 — Is the #165 `file:`-connector fence a blocker for H1?** *A)* yes, land it in the same
   unit. *B)* yes, but satisfy it by having `agora kb attach` refuse while any configured connector glob
   resolves inside `$AGORA_HOME`. *C)* no, accept the bypass and document it.
   **Recommendation: A, with B as the acceptable interim.** C ships a documented path from an
   attached KB's people notes into the local gold pack, which is the thing #166 exists to prevent.

6. **OD-6 — Sparse-checkout of the mirror.** *A)* full clone; the in-process population filter (D11)
   and the `raw/` refusal (D10) are the boundary. *B)* additionally fetch with a cone-mode sparse
   allowlist of kind directories that omits `wiki/people/` and `raw/`.
   **Recommendation: A for correctness, B as an optional bandwidth optimisation only, and never
   described as the control.** Sparse-checkout is a construction-time property: `git sparse-checkout
   disable`, or an ordinary `git checkout` during troubleshooting, re-materialises everything with no
   signal to Agora. `--filter=blob:none` is likewise a bandwidth control, not an access control (the
   remote stays a promisor). **B's git mechanics were not verified in this drafting pass and must be
   run once against a throwaway clone before any recipe enters the implementation.**

7. **OD-7 — `wiki.FM_ENABLED`.** *A)* fence it: the band path never calls `_fm_mode`, and `agora
   eval` stays single-KB and outside hub processes. *B)* move `fm_enabled` onto the `Wiki` instance
   and delete the global.
   **Recommendation: A for H1, B as follow-up.** B is the honest fix for invariant 5's new structural
   clause but touches the ranking goldens' harness, and H1 should not move the goldens.

8. **OD-8 — Test the invariant list?** *A)* add a test pinning `AGENTS.md`'s enumerated invariants
   (count + first sentence of each). *B)* leave them review-enforced.
   **Recommendation: A.** Nothing tests them today (verified), the list is cited by number all over
   the codebase, and this ADR is the change that takes it from six to eight.

9. **OD-9 — Where `agora kb add` / `attach` / `detach` / `refresh` / `list` live in the verb tree.**
   *A)* a new `agora kb` subcommand group. *B)* top-level verbs.
   **Recommendation: A**, matching `agora repo` / `index` / `gold`, and leaving `agora kb list` as
   the natural human counterpart to `doctor --registry`. **Every message shape in this ADR is
   written in the A spelling**; choosing B is a re-spell of D4's four rows and D9's refusal message,
   in the same pass.

10. **OD-10 — Which issue number this ADR is filed under.** The README row below uses **#166**, the
    only open issue that names ADR-0037 as its owner. If a dedicated H1 federation issue exists or is
    opened, the status cell should name it and keep `#166` as the blocking-clause reference.

11. **OD-11 — What a bare command targets in HUB MODE.** With `--kb` and `--repo` both absent, D1's
    ladder falls through to `profile.yaml write:` (write verbs) / `read[0]` (read verbs). *A)* the
    ladder as written — the profile always wins, so a bare `agora curate` typed inside a KB
    directory writes to the profile's KB, not the CWD. *B)* **CWD-first**: `.` when the CWD is an
    initialized Agora repo, the profile otherwise. *C)* REFUSE for write verbs when neither flag is
    given in hub mode, per D15 clause 3's *"every write path names its KB explicitly"*.
    **Recommendation: B**, with C as the safe conservative. A is the *shortest* rule and the one
    that surprises hardest: `cd ~/knowledge && agora curate` silently curating a different KB is a
    single-keystroke path to publishing into the wrong tree, and it is not undoable by re-running
    the command. B keeps the muscle memory every existing operator already has and reserves the
    profile for the case where the CWD says nothing. This is called out as an OD rather than settled
    by a `>` sign because it is irreversible UX, not a precedence detail.

12. **OD-12 — `agora import --from-kb` with a MIRROR as the source.** *A)* **REFUSE in H1**, one line
    beside `_assert_convertible` keyed on `is_mirror`, as written in D9. *B)* allow it behind an
    explicit `--i-am-taking-custody`-shaped confirmation, and state in D10/D11 that those two
    controls are scoped to READS of a mirror, not to a conversion the operator commands.
    **Recommendation: A for H1.** The conversion voids every reader-KB control at once (D11's
    population filter, D10's `raw/` refusal, and "remote notes never enter gold" — see D9 for the
    mechanism), and OD-5 already escalates the strictly *weaker* #165 connector bypass to a blocker;
    allowing the stronger one in the same ADR would be incoherent. B's merit is that a genuine
    custody handover (a colleague leaves, hands you the KB) is a real workflow — but it is a
    workflow with a correct shape today: clone it yourself, `agora kb detach`, then convert a repo
    you own.

13. **OD-13 — The word "registry".** *A)* keep `registry.yaml` / `doctor --registry` and rename
    doctor's local `registry` variable to `backend_registry`, as written in D2. *B)* rename the new
    concept: `$AGORA_HOME/kbs.yaml`, `agora doctor --kbs`, `agora kb list`, leaving "registry" to
    mean the adapters `BackendRegistry` (`src/agora_kb/curator/backends.py:107`) it already means.
    **Recommendation: A**, because "registry" is what the H1 sketch, `docs/DESIGN.md` and
    `docs/adr/README.md:48` all already call it and renaming the *documented* concept to protect an
    *internal* class name is the wrong direction. B's merit is real though: `agora doctor` would
    otherwise print two different tables both called "registry", which is why A carries the variable
    rename as a hard requirement rather than a nicety.

14. **OD-14 — `$AGORA_HOME`'s default location, per platform.** D1 fixes it at `Path.home() /
    ".agora"` on the strength of `docs/DESIGN.md:469`. That is a POSIX answer, and the repo has an
    active native-Windows track (`docs/ROADMAP.md:332` Track B / #85; `:153` "Windows **conditional**
    on the CI gate"), where `%LOCALAPPDATA%` is the convention and a dotted home folder plus deep
    `remotes/<alias>/…` clone paths is exactly where the platform bites. *A)* `~/.agora` everywhere.
    *B)* per platform: POSIX `~/.agora`, Windows `%LOCALAPPDATA%\agora`, `$AGORA_HOME` overriding
    both. *C)* declare Windows out of H1 scope and leave the Windows path to the Track-B ADR.
    **Recommendation: B**, with C acceptable **only if written down** — the D1 argument as drafted
    ("no XDG precedent in the codebase") proves nothing either way, since `grep -rn AGORA_HOME src/`
    and `grep -rn 'Path.home()' src/` are both empty and there is no precedent for **any** user-level
    path. Whichever is chosen, the first user-level directory in the project should not acquire its
    location by omission.

---

## Residual risks

- **RR-1 — Impersonation is not closed.** A remote asserts its own `kb_id` (ADR-0041 R3). The badge
  is presentation; a determined party can serve any identity. Accepted, per R3's own wording.
- **RR-2 — Homograph aliases.** `is_safe_component` neither folds case nor defends confusables
  (`src/agora_kb/core/pathsafe.py:35-39`, and the case note at `:172`; ADR-0041 R4). The case-insensitive collision
  rule closes half; a Cyrillic-`а` alias that is visually identical to a Latin one remains possible,
  **on precisely the surface (a provenance badge) where it is weaponisable**.
- **RR-3 — The marker is one `rm` away.** `.agora-mirror.yaml` defends against **accident**, not an
  adversary with local shell. `git config --local agora.mirror true` raises the bar to two deliberate
  acts; nothing short of filesystem permissions closes it.
- **RR-4 — `sanitize()` on foreign corpora is unexercised**, and `AGORA_SPAN_RE`'s DOTALL
  first-closer-wins behaviour can silently delete a large span of a remote note that merely mentions
  the sentinel grammar in prose.
- **RR-5 — Redaction changes what the ranker scores** if OD-4(B) is ever taken, so a redaction-policy
  change silently changes band **content**, not just presentation. Recorded so a future debugging
  session does not chase a phantom ranking regression.
- **RR-6 — The local/reader people asymmetry** will mislead an operator about their own tree unless
  every hit is badged.
- **RR-7 — Curate inside a *sparse* mirror** (if OD-6(B) is taken) would compute a final diff against
  a partial worktree — a distinct and probably worse failure than the full-worktree case. D9 refuses
  it either way; recorded because the two decisions interact.
- **RR-8 — `SearchHit.repo` collisions.** Two mirrors whose directories share a basename yield hits
  that are indistinguishable in the frozen ADR-0012 model, and the model is `extra="forbid"` so
  nothing can be added late. Any UI that groups or badges on `hit.repo` instead of `kb_alias` is
  silently wrong. *(This is a hit-label collision only. It is **not** a cache collision today: the
  cache path is `<that repo's own>/_kb/index/<stem>.notes.json`
  (`src/agora_kb/core/layout.py:571-585`), so two mirrors in different directories cannot collide —
  it would become one if a central cache under `$AGORA_HOME` were ever built.)*

---

## Test plan

A **3-KB fixture** built with `tests/support/kb_builder.py::build_kb`, with the helper in
`tests/support/` (there is no `conftest.py` anywhere in the repo — verified — so fixtures are built
per module today).

- **A** — `role: owner`, schema 2, **git-inited** (the `tests/faces/test_mcp_server_graph.py:20-33`
  pattern) so gold can assemble; `build_kb` deliberately does not `git init`.
- **B** — `role: reader`, schema 2 mirror carrying a `wiki/people/` note (`_V2Layout.person_path`),
  one `raw/` capture and one `raw/_blob` entry (`blobs=`), plus a `.agora-mirror.yaml`.
- **C** — `role: reader`, **schema 1** (which forces `kb_id`/`kb_name` to defaults in the builder).

| # | assertion |
|---|---|
| **T1** | **ONE CONTRACT.** `json.loads(agora query --kb all --json)` == the `GET /api/search?q=…&kb=all` JSON == `kb_query(question, kb="all")`'s payload — the **same decoded object**, not the same bytes (the three faces serialise through three encoders: `cli.py:1420-1426` indents, Starlette compacts, FastMCP encodes its own; D5). A separate byte-level golden covers the CLI's own `--json` rendering. |
| **T2** | **BAND ORDER.** `federated_query` over `read: [A, B, C]` returns bands in declaration order; reversing the profile reverses the bands with **byte-identical per-band hit lists**. No `hit.score` is compared across bands anywhere in the composer (grep). |
| **T3** | **NO READER BYTE IN ANY GOLD PACK.** `agora gold build` on A, then assert no B/C `rel_path`, title or body line appears in `_kb/gold/default.md`; **and** that no code path constructs `PackAssembler` with anything but a single `Repo` (the H1 exit criterion, written structurally). |
| **T4** | **FROZEN MODEL.** `SearchHit.model_config` is `frozen` + `extra == "forbid"`, and every federated field lives on the wrapper (guards ADR-0012). |
| **T4b** | **REDACTION IS VISIBLE, NOT SILENT.** An owner-KB hit is the identical `SearchHit` object `Wiki.query` returned and `FederatedHit.redacted is False`; a reader-KB hit whose excerpt `sanitize()` changed is a `model_copy` with `redacted is True` and its band's `redacted_hits` incremented; no `SearchHit` field is added or dropped in either case (D6/D10). |
| **T5** | **MIRROR REFUSAL AT EVERY ENUMERATED ENTRY POINT.** Parametrized over `curate`, `watch`, `requeue`, `harvest`, `sync`, `index build`, `gold build`, `capture --file`, `repo init`, `import --from-kb`, web `POST /api/upload` + `/api/upload-batch`, MCP `kb_curate` and `kb_remember` against **B** — each aimed with `--repo <mirror path>`, the bare-path spelling D9 says the marker must catch: non-zero exit / refusal payload **and** B's tree byte-unchanged (hash the tree before and after). Also: `agora sync` on **B** prints the mirror refusal, **not** `sync: push failed`, and writes **no** `_kb/backup.json` entry (D9's sync arm); and `agora watch --once` on **B** exits 1 with the whole message rather than a 200-char truncated tick failure. |
| **T5b** | **SITE-SET PINS, three of them**, mirroring `tests/test_schema_version_guard.py:819-855`'s predicate/wrapper split rather than conflating them (the helper is a whole-file substring scan, so every mention counts): `modules_mentioning("assert_not_mirror") == ["cli.py", "core/inbox.py", "core/repo.py", "faces/mcp_server.py"]` — which is only true if `index build` is gated at `cli.py:2599` and **not** inside `core/wiki.py`'s `build_cache` (D9); `modules_mentioning("MirrorRepoError") == ["cli.py", "config.py", "core/inbox.py", "faces/web/app.py"]`; and `modules_mentioning("is_mirror")` additionally covers `ingest/vault_import.py`, `ingest/kb_convert.py` and `faces/mcp_server.py` (the D10/D11 read fences and the two import refusals). |
| **T6** | **SYMLINK BYPASS.** A symlink pointing at B's root also refuses (pins the `Path.resolve()` requirement against `src/agora_kb/core/layout.py:293-296`). |
| **T7** | **#166 CLAUSE, ALL THREE ENUMERATORS.** B's `wiki/people/**` yields no `kb_query` hit; `kb_read` refuses; `kb_neighbors`/`graph` list it as neither node nor centre; web browse omits it; `agora doctor --registry` / `--agents` counts exclude it (the `_doctor_parse_notes` seam, `cli.py:3390-3401`); it is absent from any cache payload; **and** removing it changes B's in-degree/`d_moc` (proving a population filter, not a late drop). Run once through the registry and once through a bare `--repo <mirror>` — both must be fenced (D9's marker-keyed rule). |
| **T8** | **READER `raw/` REFUSED, AND THE STATUS SURVIVES THE TRIP.** `handlers.raw()` on B returns the fourth status `refused`; **`kb_read` returns it verbatim rather than the `_kb_read_not_found` shape** (the `mcp_server.py:1547-1556` collapse, and `agora read` via the D5-collapsed single copy); `GET /api/raw` and `GET /raw` return **403** with the teaching note rather than 404 (the `app.py:588-589` mapping). Both B's `raw/` and `raw/_blob` paths. And, with `raw_enabled` at its shipped default `True` (`config.py:1242`), a remote note's `sources:` rows render as **plain text** — asserted against a reader binding, which is what proves the new `raw_enabled=False`/reader argument is actually threaded (D10). |
| **T9** | **`kb_id` IS NOT AN AUTHORISATION INPUT.** `grep -rn kb_id src/agora_kb/faces src/agora_kb/core/gold.py` stays **empty** (the shipped baseline). `src/agora_kb/hub` is pinned **positively** instead — the only `kb_id` reads there are the D4 fingerprint comparison and the badge renderer, each named — because a bare emptiness grep over `hub/` would fail on this ADR's own mandated D4 code (regression test for D3). |
| **T10** | **SCHEMA-1 ATTACHED KB.** C's notes band normally; `agora read --kb C <path>` succeeds; every write entry refuses; C's badge reads `kb_id: none (schema 1)` and its `doctor --registry` `kb_id` cell is `-`, never a registry-typed value (D4); `doctor --registry` prints C's `read-only:schema-1` row **while the host verdict stays `status: healthy`** (pins the `cli.py:3029` hazard). |
| **T11** | **THE FOUR PROBE OUTCOMES (D1).** (a) no `$AGORA_HOME` ⇒ a golden set of `agora query/read/neighbors/status`, `kb_query` and `GET /api/search` payloads match the **pre-ADR bytes exactly**; (b) a registry that parses with an empty `kbs:` ⇒ the same bytes, **not** a zero-KB hub; (c) a **malformed** registry and (d) an **unreadable (chmod 000)** one ⇒ every command refuses — including one that passes `--repo` explicitly — with a message naming the registry path and the `AGORA_HOME=` escape hatch. |
| **T12** | **`unavailable` AND `degraded` ARE REACHABLE.** A registry entry whose `path:` does not exist bands as `unavailable`, **not** `not_found`, visible in both the human and the JSON rendering, with the alias in `unavailable_bands`; a profile whose only band is that entry answers top-level **`degraded`**, not `not_found`; exit code stays 0 and the reason goes to stderr. (A `path:` spelled `~/…` is **not** in this test: D2's loader `expanduser`s it, so it resolves normally — T13 asserts that instead.) |
| **T13** | **LOADER REFUSALS AND ONE LOADER SUCCESS.** Refusals: non-mapping `kbs:`; duplicate alias key; boolean alias key (`no:`); relative `path:`; unknown `role:`; `path` + `transport` both present; missing `version:`; a profile NAME containing `/` or `..`. Success: **`path: "~/knowledge"` is `expanduser`ed at load** and resolves normally (the rule that closes `RepoLayout`'s `.absolute()`-only hazard, `core/layout.py:293-296`). Plus the shape-keyed `role:` defaults: a `path:` entry with no `role:` loads as `owner`, a `transport:` entry as `reader`. |
| **T13b** | **REGISTRY WITHOUT PROFILE.** A registry with two KBs and no `profile.yaml`: reads band in registry file order and `FederatedQueryResult.profile == "(synthesised from registry)"`; with exactly one `role: owner` entry a write verb targets it; with two owner entries every write verb refuses naming both aliases, while reads still band. |
| **T14** | **PROCESS MODEL.** Two `Federation`s over disjoint registries in one process do not cross-contaminate; each binding's `AgoraHandlers` is a distinct object over a distinct `RepoLayout`; and `grep` finds no `lru_cache`/`global`/import-time `os.environ` under `src/agora_kb/hub/`. |
| **T15** | **NO `query_lexical` UNDER `hub/`**, and no `hub` import under `src/agora_kb/core/**` (the #144 pin, mechanised). |

---

## Evidence appendix

All paths relative to the repository root; line numbers as of the frozen read-only worktree at
`feat/drilldown-169-wave-a` @ `03b7df2`. Facts marked **†** were re-verified by command during this
drafting pass; the rest are carried from the reader pass with their citations intact.

**Frozen models / ADR-0012 (†)**
`src/agora_kb/core/wiki.py:735-747` `SearchHit` frozen + `extra="forbid"`, 7 fields ·
`:750-757` `QueryResult` frozen, `status: Literal["ok","not_found"]` ·
`:784-786` `self.repo = layout.root.name` ·
`:789-805` `query` is a pure delegation to `query_lexical` ·
`:1519-1536` per-repo IDF/`avgdl`/`max_indeg` · `:1539-1571` the `raw/(raw+PIVOT)` squash ·
`docs/adr/0012-deterministic-query-ranking.md:499-506` §11's deferral and its "global order" clause ·
`src/agora_kb/curator/bundle.py:198-206` the #144 permanent pin.

**The two enumerators (†)**
`src/agora_kb/core/wiki.py:964-986` `_iter_note_files` (no filter) → `:987-995`, `:996-1030`,
`:1697` ·
`src/agora_kb/core/wiki.py:898-916` `list_notes` → `schema/notes.py:429` `parse_all_notes` →
`get_note` (`:918-930`), browse, `health()`, `graph()`, gold.

**People (†)**
`src/agora_kb/schema/notes.py:105-115` `is_people_path` · `:118-130` `is_ungraded_people_note` and
its "must agree with `lint()`" rationale ·
`src/agora_kb/core/gold.py:519-531` the population filter and its own reasoning; `:640` the second
layer · `src/agora_kb/curator/constants.py:33-40` `ALLOWLIST_DENY_PREFIXES` (a **write** gate) ·
`src/agora_kb/core/wiki.py:689-716` `_is_people_path` (identity space only) ·
`docs/LIMITATIONS.md:538-556` the shipped asymmetry, the unimplemented `file:` fence, and
"human-owned and searchable, not private".

**`lint()` call sites (†)** — exactly one in the CLI: `src/agora_kb/cli.py:906` (`_cmd_repo_init`).
Everything else is `ingest/kb_convert.py:1145`, `ingest/vault_import.py:1719`,
`curator/worker.py:1195`, and `faces/mcp_server.py:549` (`health()`, the web dashboard).
`agora doctor` and `agora status` do **not** lint.

**Clone / mirror mechanics (†)**
`src/agora_kb/core/repo.py:52-54` `_GITIGNORE` is `_kb/` + `.DS_Store` **only** — so a clone carries
`raw/` in full · `:64-79` `.gitattributes` preserves `raw/_blob/**` bytes ·
`:289-292` `Repo.resolve` validates nothing · `:26-29` "no pull/fetch/bidirectional code" ·
`src/agora_kb/config.py:192-202` a freshly-**cloned** repo is a blessed, fully-operational state.

**Write refusal precedent (†)**
`src/agora_kb/core/inbox.py:164` `assert_writable_repo_schema` and its "one spelling" rationale ·
call sites `core/inbox.py:279`, `cli.py:1613`, `:1833`, `:2097`, `:2318`, `:2485`,
`faces/mcp_server.py:1193` · the both-directions pin at
`tests/test_schema_version_guard.py:819-855` (the `modules_mentioning` helper at `:839-844`, the
predicate/wrapper split at `:846-854`) · `src/agora_kb/config.py:132` `ConfigError(ValueError)` ·
`:380-383` `ReadOnlySchemaVersionError`'s direct `ConfigError.__init__` comment, `:389-395` its
message shape · `:600-607` `KbIdentity`'s ULID validator and `:622-658` `load_kb_identity`'s
raise-on-anything-present posture (why D4 needs a tolerant reader) ·
`tests/support/kb_builder.py:655-660` refusing `kb_id` on a schema-1 build.

**Doctor / status (†)**
`src/agora_kb/cli.py:3002-3030` `_doctor_writable_schema` **fails the verdict** ·
`:2937`, `:2941`, `:2951`, `:2955` the observation-only contract comments ·
`:3199-3202` `_doctor_agents`' try/except · `:1331-1335` the `status` value-line grammar ·
`:685-698` `_read_only_schema_note`, the shared sentence ·
`:2907`, `:2918`, `:2943`, `:3171` the pre-existing `registry`/`registry_error` locals that are the
**backend** registry (`src/agora_kb/curator/backends.py:107`), the D2/OD-13 name collision ·
`:3390-3401` `_doctor_parse_notes`, the third note enumerator, feeding `:2934`/`:2943`/`:2948` and
`_agent_seen_cell` (`:3375-3387`).

**CLI structure (†)**
`src/agora_kb/cli.py:602-654` `_schema_version_guard`, the one structural hook (the quoted rule at
`:607-610`; `getattr(args, "repo", None)` at `:637`) ·
`:309-314` the read verbs deliberately outside the write gate ·
`:1401` `_handlers(repo_path)` — a bare path in, `AgoraHandlers` out, no role ·
`:2178-2225` `agora sync` (no gate; the `ConfigError` arm at `:2202-2206`, `push_backup` at `:2217`
under `except (GitError, ValueError)` at `:2218`) ·
`:2485-2486` the per-tick `assert_writable_repo_schema` with **no** enclosing `try:`, whose raise
lands in the loop guard at `:2340` → `_print_tick_failure` (`:2347`) ·
`:2599-2606` `agora index build` (`build_cache`'s only two callers: `:2612` and
`curator/worker.py:1867`) ·
`:2682-2700` `agora gold build` · `:2761-2776` `agora eval` writes nothing without `--out` ·
19 × `add_argument("--repo", default=".")` at `:283`, `:306`, `:315`, `:333`, `:344`, `:363`,
`:383`, `:426`, `:448`, `:453`, `:458`, `:466`, `:477`, `:489`, `:513`, `:520`, `:534`, `:540`,
`:556`.

**Web read path (†)**
`src/agora_kb/faces/web/app.py:470` `build_app`; 26 routes in its closure
(`grep -c '@app\.\(get\|post\)'`) · `:588-589` `/api/raw` maps every non-`ok` status to 404 ·
`:827`, `:839-843`, `:846-850` the `/note` route threading `raw_enabled` into `render_note_body` and
`_source_rows` · `:1739-1741`, `:1770-1776`, `:1781-1783` the linkability predicate and the two
`raw_enabled` keywords · `:1416-1449` the three upload except-arms (the D6 repo-level arm at
`:1422-1443`, whose own comment gives the 422 rationale at `:1440-1442`) · `:1287-1288`
`_do_upload_batch` turning an `HTTPException` into a per-file `FileReceipt(error=…)`.

**Converter (†)**
`src/agora_kb/ingest/kb_convert.py:87` `CONVERTER_SOURCE_SCHEMA_VERSION = 1`, enforced at
`:900-905` · `:29-30` rule 5, `raw/` copied byte-identically · `:965` the direct
`parse_all_notes(src_layout, schema_version=1)` with no people filter · `:1032`, `:1112`, `:1139`
the NEW `kb_id` minted and stamped (rule 6).

**Process model (†)**
`src/agora_kb/core/wiki.py:100` + `src/agora_kb/core/rank_snapshot.py:283-300` — `FM_ENABLED`, the
single module-global mutable · `src/agora_kb/faces/web/app.py:470-509` per-repo `build_app` with the
invariant-5 comment at `:496-498` · `src/agora_kb/faces/mcp_server.py:1461-1485` per-repo
`build_server` · `src/agora_kb/faces/web/metrics.py:302-318` fresh `CollectorRegistry` per render.

**`raw/` (†)**
`src/agora_kb/core/rawstore.py:1-58` the three gates, "creates nothing, ever", and the explicit
disclaimer that egress policy belongs to the faces (R1/#166) ·
`src/agora_kb/faces/mcp_server.py:271-330` `AgoraHandlers.raw()`, the shared seam, three statuses,
and the ADR-0027 §8 note naming the fourth emission path ·
`docs/adr/0027-gold-context-packs.md:5` the second banner, "the single index of scope changes".

**Boundaries (†)**
`docs/adr/README.md:41`, `:48` the 0030/0037 split ·
`docs/adr/0027-gold-context-packs.md:151-157` (§7 ROLE RULE + `scopes` → 0030), `:159-162` (§8's
scope sentence and its cite list) ·
`docs/adr/0041-stratum-kind-first-layout.md:270-286` (D1.5 + the two-reservations box), `:1047-1050`
(R3), `:1051-1054` (R4) ·
`docs/adr/0036-authn-authz.md:48-52` (*"Tenancy today is process topology"* + *"the first component
that will enforce invariant #5"*), `:118-127`, `:143-150`, `:286-292` ·
`docs/adr/0006-repo-as-tenant-boundary.md:4` (the existing append-only banner), `:17`
(*"queries fan out only over those"*), `:21-22` (*"Isolation is structural"*),
`:28-30` · `AGENTS.md:21-34` (the only enumerated invariant list; `CLAUDE.md` is a symlink;
invariant 2 at `:24-29`, invariant 5 on line 33) ·
`docs/DESIGN.md:465-471` (the `~/.agora/profile.yaml` reservation, currently attributed to 0030) ·
`docs/notes/agora-kh-design-judgement.md:186-189` (FederatedHit sketch + RRF rejection),
`:196-230` (registry/profile/S1 sketch), `:320-326` (the minimal `?kb=` switcher),
`:358-372` (invariants 7/8 + the invariant-5 revision), `:382`/`:390` (0006 and 0037 both in H1),
`:401` (the H1 exit criterion).

**Alias grammar (†)**
`src/agora_kb/core/pathsafe.py:204-211` `is_safe_component` — **the predicate the alias grammar
reuses** · `:58` `DEFAULT_MAX_BYTES = 180` (`:68` is `_SEP`, not the cap) · `:214-248`
`is_safe_filename_stem`, the **union** predicate the alias grammar deliberately does NOT reuse,
whose docstring at `:226-230` states that it exists to admit `con`/`foo-`/`foo.` ·
`:35-39` the module's "does not decide case / does not defend homographs" disclaimers, and `:172`
`safe_slug_component`'s own "case is not folded" note · `src/agora_kb/core/layout.py:47-48`
`_WRITER_RE`/`_WRITER_MAX`, the separate legacy writer charset.

**Redaction / sentinel (†)**
`src/agora_kb/core/redact.py:284-293` `sanitize` and its "future networked callers" docstring ·
`:13-17` one-way, no reverse map · `src/agora_kb/core/sentinel.py:65-81` `AGORA_SPAN_RE`, DOTALL,
first-closer-wins.

**Carried from the reader pass, NOT re-verified here (live-run evidence; re-run before quoting as
fact).** A full curator publish on a plain clone (status `published`, the clone's branch advanced and
diverged from origin); `agora sync` from a mirror pushing into the SOURCE repo
(`sync: pushed main @ … -> origin`, origin's `main` moved); `agora gold build --repo <mirror>`
succeeding; `agora repo init <mirror>` writing `adapters.yaml` **before** its lint verdict; the
`remote set-url --push DISABLED` bypass via an explicit URL; and the branchless-mirror
`agora index build` failure. Their **static preconditions** are all verified above; the four
end-to-end outcomes are not.

**UNVERIFIED and load-bearing.** The git mechanics behind OD-6(B) — `sparse-checkout` shaping only
the working tree, cone mode being include-only, and `--filter=blob:none` degrading silently when the
server lacks `uploadpack.allowFilter` — are training-memory claims with no local run (this pass was
forbidden git write operations). The redactor throughput figure (≈ 19 MB/s) is a single-host
micro-benchmark, and the "6 MB mirror ⇒ ~320 ms" figure derived from it is an illustration, not a
measurement of any real corpus. `.omc/plans/JUDGEMENT-BRIEF-2026-09-05.md` §5 is **not present in the
frozen worktree** and was taken from the task brief. Whether the pinned FastMCP version treats an
added optional tool parameter as non-breaking for already-connected clients was not checked, and it
is the load-bearing premise of D6's keep-seven-tools recommendation.
