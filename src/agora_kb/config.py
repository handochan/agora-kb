"""Repo + adapter configuration loaders (DATA-MODEL §3 ``_kb/repo.yaml`` / §8 ``adapters.yaml``).

This is the Phase-2 *wiring* seam between the on-disk operator config and the typed inputs the
curator run-loop already consumes. Two config surfaces are read here, both plain YAML:

* ``_kb/repo.yaml`` (DATA-MODEL §3) — per-repo identity + curator policy. :func:`load_repo_config`
  maps it onto a :class:`RepoConfig` that bundles the pieces the worker/CLI need: the repo
  ``name``/``kind`` (forward-looking identity fields, round-tripped but not yet consumed), the FIXED
  :class:`~agora_kb.schema.emit.Taxonomy` (``domains`` /
  ``allowed_tags`` / ``taxonomy_policy`` / ``schema_version`` — the §4.1/§4.4 gate input), the
  :class:`~agora_kb.curator.triggers.TriggerConfig` (cron/threshold/idle), the ``default_backend``
  name, and the ``max_attempts`` retry budget (ADR-0011 §5.1). A repo with no ``repo.yaml`` yet
  loads sensible defaults so ``agora status`` / ``curate`` work on a freshly-cloned tree.
* ``adapters.yaml`` (DATA-MODEL §8) — the WRITE-adapter registry. :func:`load_backend_registry` is a
  thin wrapper over :meth:`agora_kb.curator.BackendRegistry.from_file`; an absent file returns
  ``None`` (an explicit "no brain configured" signal the CLI/MCP surface as a clear note, not a
  crash — a :class:`BackendRegistry` requires ≥1 backend by construction, so a 0-backend registry
  cannot exist). The registry's own typing/validation owns the backend specs.

Nothing here invokes a model, touches git, or writes the wiki — it is pure config I/O that produces
the typed inputs the deterministic run-loop already takes (so the integrity boundary is unchanged).

The taxonomy on disk is the AUTHORITATIVE input (``_meta/taxonomy.yaml`` is what the bundle copies +
the lint reads, ADR-0010 D6); ``repo.yaml`` is the operator-facing summary. :func:`load_repo_config`
PREFERS the emitted ``_meta/taxonomy.yaml`` for ``allowed_tags`` (the closed tag set the gate
enforces) and only falls back to ``repo.yaml domains`` when the taxonomy file is absent, so the
config never widens the closed vocabulary the curator validates against.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .core.layout import RepoLayout
from .core.redact import DEFAULT_ON_CLASSES, KNOWN_CLASSES, RedactionPolicy
from .curator import BackendRegistry, TriggerConfig
from .curator.constants import DEFAULT_BODY_BYTE_BOUND, DEFAULT_MAX_ATTEMPTS, DEFAULT_RELATED_K
from .schema import Taxonomy

__all__ = [
    "ConfigError",
    "RepoConfig",
    "load_repo_config",
    "write_default_repo_config",
    "write_default_adapters_yaml",
    "load_backend_registry",
    "HarvestPolicy",
    "load_harvest_policy",
    "RedactSettings",
    "load_redact_policy",
    "IndexPolicy",
    "load_index_policy",
    "ConnectorSpec",
    "load_connector_specs",
    "WebConfig",
    "WebGraphConfig",
    "WebUploadConfig",
    "WebExtensionsConfig",
    "WebFeaturesConfig",
    "load_web_config",
]

# Harvest scope values (ADR-0007 mechanism 3). Kept as plain strings HERE (not the harvester's
# Scope enum) so this config seam never imports the harvester package — the harvester imports
# config, not the reverse, so the dependency stays acyclic. The harvester maps these to its enum.
_SCOPE_VALUES = ("personal", "team")

# DATA-MODEL §3: the per-repo config lives in the git-ignored operational spool at _kb/repo.yaml.
_REPO_CONFIG_NAME = "repo.yaml"
# DATA-MODEL §3 defaults mirror the documented example (kept in sync with TriggerConfig defaults).
_DEFAULT_NAME = "personal"
_DEFAULT_KIND = "personal"
# Reuse the single source of truth for the §5.1 retry budget (curator/constants.py) so RepoConfig's
# default can never silently drift from the worker's actual DEFAULT_MAX_ATTEMPTS.
_DEFAULT_MAX_ATTEMPTS = DEFAULT_MAX_ATTEMPTS
# Same SSOT reuse for the §1.3 repo-global tuning thresholds (ADR-0022 step 2).
_DEFAULT_BODY_BYTE_BOUND = DEFAULT_BODY_BYTE_BOUND
_DEFAULT_RELATED_K = DEFAULT_RELATED_K
# The OSS default brain (ADR-0005 / INGEST-CONTRACT §8: local Qwen via Ollama, zero API cost). Only
# a NAME here; the executable/argv binding lives in adapters.yaml (DATA-MODEL §8).
_DEFAULT_BACKEND = "qwen"


class ConfigError(ValueError):
    """A config file on disk is malformed (unparseable YAML, or a typed-mismatch operator value).

    Raised so the CLI/MCP can present a clean, actionable message instead of letting a raw
    ``yaml.YAMLError`` / ``ValueError`` escape as a stacktrace out of ``agora status`` / ``curate``
    / MCP ``kb_curate``. ``repo.yaml`` is operator-local policy-with-defaults, so a typo there fails
    CLEARLY rather than silently changing the operator's stated policy.
    """


class RepoConfig(BaseModel):
    """Typed view of ``_kb/repo.yaml`` (DATA-MODEL §3) — the curator/CLI policy inputs.

    Bundles the pieces the run-loop + CLI take: the FIXED :class:`Taxonomy` (the §4.1/§4.4 closed
    vocabulary), the :class:`TriggerConfig` (cron/threshold/idle, DATA-MODEL §3
    ``curator.triggers``, consumed by ``agora curate``), ``default_backend`` (the brain name read by
    the CLI/MCP), and ``max_attempts`` (the §5.1 retry budget threaded into ``worker.run``).
    ``name``/``kind`` are first-class §3 IDENTITY fields that round-trip but are not yet consumed by
    any surface (forward-looking until status output / MCP repo identity wires them). Defaults match
    the DATA-MODEL §3 example so a repo with no ``repo.yaml`` still loads a usable policy.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = _DEFAULT_NAME
    kind: str = _DEFAULT_KIND
    taxonomy: Taxonomy = Field(default_factory=Taxonomy)
    triggers: TriggerConfig = Field(default_factory=TriggerConfig)
    default_backend: str = _DEFAULT_BACKEND
    max_attempts: int = Field(default=_DEFAULT_MAX_ATTEMPTS, ge=1)
    # ADR-0013 fail-closed opt-in: when False (default) a ``sandbox: strict`` backend with no usable
    # kernel sandbox raises SandboxUnavailable rather than running unconfined; True opts into the
    # restricted fallback (network egress + out-of-worktree writes are NOT prevented, forced
    # review-mode). Read from ``curator.allow_reduced_isolation`` (DATA-MODEL §3 curator policy).
    allow_reduced_isolation: bool = False
    # §1.3 repo-global curator tuning surfaces (ADR-0022 step 2 / DATA-MODEL §3.1).
    # ``body_byte_bound`` is the ``{n_bytes}`` PASS-2 prompt hint; ``related_k`` is the bundle's
    # ``wiki.query`` breadth; ``max_orphans`` (``None`` ⇒ orphan check SKIPPED, byte-identical to
    # today) gates a WARNING-only ``L2-1`` lint finding when the whole-tree orphan-theme count
    # exceeds it. Read from ``curator.limits.body_byte_bound`` / ``curator.limits.related_k`` /
    # ``curator.lint.max_orphans`` — the DATA-MODEL §3 repo-global nesting.
    body_byte_bound: int = Field(default=_DEFAULT_BODY_BYTE_BOUND, ge=1)
    related_k: int = Field(default=_DEFAULT_RELATED_K, ge=1)
    max_orphans: int | None = Field(default=None, ge=0)


def repo_config_path(layout: RepoLayout) -> Path:
    """Path of the per-repo config ``_kb/repo.yaml`` (DATA-MODEL §3)."""
    return layout.kb_dir / _REPO_CONFIG_NAME


def load_repo_config(layout: RepoLayout) -> RepoConfig:
    """Load ``_kb/repo.yaml`` into a :class:`RepoConfig`; return defaults when it is absent.

    A missing ``repo.yaml`` is NOT an error (a freshly-cloned or pre-config repo): the documented
    DATA-MODEL §3 defaults load so ``agora status`` / ``curate`` keep working. The ``curator``
    sub-mapping is read for ``backend`` (→ ``default_backend``), ``max_attempts``, and
    ``triggers`` (cron/threshold/idle). The taxonomy is assembled by PREFERRING the emitted
    ``_meta/taxonomy.yaml`` (the authoritative closed vocabulary the gate enforces, ADR-0010 D6) and
    falling back to ``repo.yaml domains`` only when that file is absent — the config never widens
    the closed tag/domain set the curator validates against.
    """
    raw = _read_yaml_mapping(repo_config_path(layout))

    name = _opt_str(raw.get("name")) or _DEFAULT_NAME
    kind = _opt_str(raw.get("kind")) or _DEFAULT_KIND
    repo_domains = _str_list(raw.get("domains"))
    repo_schema_version = raw.get("schema_version")

    curator = _sub_mapping(raw.get("curator"))
    default_backend = _opt_str(curator.get("backend")) or _DEFAULT_BACKEND
    max_attempts = _opt_int(
        curator.get("max_attempts"), _DEFAULT_MAX_ATTEMPTS, key="curator.max_attempts"
    )
    allow_reduced_isolation = _opt_bool(
        curator.get("allow_reduced_isolation"), False, key="curator.allow_reduced_isolation"
    )
    triggers = _build_triggers(curator.get("triggers"))

    # §1.3 repo-global tuning thresholds (ADR-0022 step 2). Nesting matches the DATA-MODEL §3
    # example already in the docs/tests: the two size/breadth knobs under curator.limits, orphan
    # under curator.lint. max_orphans stays None (check skipped) unless set → lint byte-identical.
    limits = _sub_mapping(curator.get("limits"))
    body_byte_bound = _opt_int(
        limits.get("body_byte_bound"),
        _DEFAULT_BODY_BYTE_BOUND,
        key="curator.limits.body_byte_bound",
    )
    related_k = _opt_int(
        limits.get("related_k"), _DEFAULT_RELATED_K, key="curator.limits.related_k"
    )
    lint_cfg = _sub_mapping(curator.get("lint"))
    raw_orphans = lint_cfg.get("max_orphans")
    max_orphans = (
        _opt_int(raw_orphans, 0, key="curator.lint.max_orphans")
        if raw_orphans is not None
        else None
    )

    taxonomy = _load_taxonomy(
        layout,
        repo_domains=repo_domains,
        repo_schema_version=repo_schema_version,
    )

    return RepoConfig(
        name=name,
        kind=kind,
        taxonomy=taxonomy,
        triggers=triggers,
        default_backend=default_backend,
        max_attempts=max_attempts,
        allow_reduced_isolation=allow_reduced_isolation,
        body_byte_bound=body_byte_bound,
        related_k=related_k,
        max_orphans=max_orphans,
    )


def write_default_repo_config(
    layout: RepoLayout,
    *,
    name: str,
    domains: list[str] | tuple[str, ...],
    kind: str = _DEFAULT_KIND,
) -> Path:
    """Emit a starter ``_kb/repo.yaml`` (DATA-MODEL §3); return its path.

    Written at ``agora repo init`` alongside the schema emit. The shape mirrors the DATA-MODEL §3
    example (identity + ``curator`` policy with the default OSS backend, the §5.1 retry budget, and
    the cron/threshold/idle triggers). ``_kb/`` is git-ignored, so this is operator-local config —
    derived policy, not canonical knowledge — and an idempotent re-init simply overwrites it.
    ``kind`` defaults to ``"personal"`` (the Phase-1 MVP); a ``"team"`` repo passes ``kind="team"``
    so the §3 first-class identity field is settable rather than hardcoded.
    """
    triggers = TriggerConfig()
    doc: dict[str, object] = {
        "name": name,
        "kind": kind,
        "schema_version": 1,
        "domains": list(domains),
        "review_mode": "direct",
        "curator": {
            "backend": _DEFAULT_BACKEND,
            "max_attempts": _DEFAULT_MAX_ATTEMPTS,
            "allow_reduced_isolation": False,
            "triggers": {
                "cron": triggers.cron,
                "threshold": triggers.threshold,
                "idle_minutes": triggers.idle_minutes,
            },
        },
        # ADR-0007: the memory harvester is OPT-IN and disabled by default. Set enabled: true (and
        # configure connectors in adapters.yaml) to pull other agents' memory into gated candidates.
        # scope_lock guards privacy: a personal source may feed ONLY a personal repo (§3).
        "harvest": {
            "enabled": False,
            "scope_lock": "personal",
        },
    }
    path = repo_config_path(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def write_default_adapters_yaml(layout: RepoLayout, *, model: str | None = None) -> Path:
    """Emit a starter ``adapters.yaml`` (DATA-MODEL §8) wiring the OSS default brain; return path.

    Written at ``agora repo init`` so a freshly-initialized repo is IMMEDIATELY curate-able with the
    local-model brain (ADR-0005 / INGEST-CONTRACT §8: local Qwen via Ollama, zero API cost). The
    single ``qwen`` backend shells the ``agora-ollama-brain`` console script (its argv) — the
    generic :class:`SubprocessBackend` invokes it for BOTH curator passes over stdin. ``cwd`` is the
    ``{worktree}`` placeholder the worker resolves (PASS 2 writes there); ``network: loopback``
    documents that this backend reaches the localhost Ollama HTTP API (it is metadata for the
    operator, not enforcement). When ``model`` is given, ``--model <m>`` is appended to the argv so
    the brain targets a specific local model. The emitted name ``qwen`` MUST match
    :data:`_DEFAULT_BACKEND` and the ``curator.backend`` default in ``repo.yaml`` so the wiring is
    consistent out of the box.

    IDEMPOTENT + non-destructive: an EXISTING ``adapters.yaml`` is left untouched (its path is
    returned without a rewrite) so an operator's hand-tuned backend registry survives a re-init.
    Only the :class:`BackendSpec`-valid keys (``argv``/``cwd``/``prompt``/``sandbox``/``network``/
    ``timeout_s``) are emitted — ``BackendSpec`` is ``extra='forbid'`` — so the file round-trips
    through :func:`load_backend_registry` cleanly.
    """
    path = layout.root / "adapters.yaml"
    if path.is_file():
        # Non-destructive: never clobber an operator's hand-tuned registry on re-init.
        return path

    argv = ["agora-ollama-brain"]
    if model is not None:
        argv += ["--model", model]
    doc: dict[str, object] = {
        "backends": {
            _DEFAULT_BACKEND: {
                "argv": argv,
                "cwd": "{worktree}",
                "prompt": "stdin",
                "sandbox": "strict",
                "network": "loopback",
                "timeout_s": 600,
            },
        },
        "default_backend": _DEFAULT_BACKEND,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    # READ adapters — memory harvester connectors (ADR-0007). Emitted COMMENTED-OUT: harvesting is
    # opt-in (also gated by repo.yaml harvest.enabled), so a fresh repo wires no connector by
    # default. The BackendRegistry ignores this block; the harvester's loader (load_connector_specs)
    # reads it. Uncomment + point at a real memory file to start harvesting into gated candidates.
    text += (
        "\n# READ adapters — context harvester connectors (ADR-0007/0023; opt-in, also needs\n"
        "# repo.yaml harvest.enabled: true). scope guards privacy (personal feeds only a personal\n"
        "# repo). follow_links (ADR-0018, default off) follows a bullet's [Title](sibling.md)\n"
        "# and harvests the sibling's content instead of the thin one-line summary. A session:\n"
        "# connector (ADR-0023) distills agent SESSION transcripts (assistant reflections with a\n"
        "# durable-knowledge marker); it redacts secrets at its boundary per harvest.redact\n"
        "# (default on). Uncomment + point at a real source to enable harvesting.\n"
        "# connectors:\n"
        '#   file:claude-code: { path: "~/.claude/**/MEMORY.md", scope: personal }\n'
        '#   file:hermes: { path: "~/.hermes/MEMORY.md", scope: personal, follow_links: true }\n'
        '#   session:claude-code: { path: "~/.claude/projects/**/*.jsonl", scope: personal }\n'
    )
    path.write_text(text, encoding="utf-8")
    return path


def load_backend_registry(path: str | Path) -> BackendRegistry | None:
    """Load the WRITE-adapter registry from ``adapters.yaml`` (DATA-MODEL §8); ``None`` when absent.

    A thin wrapper over :meth:`agora_kb.curator.BackendRegistry.from_file`. An ABSENT
    ``adapters.yaml`` returns ``None`` so the caller (CLI/MCP) can surface a clear "no backend
    configured" note rather than crash — a :class:`BackendRegistry` requires ≥1 backend by
    construction, so there is no "empty registry" object to return. A PRESENT-but-malformed file
    still RAISES via the registry's own validation (a typo'd backend spec is a config error).
    """
    p = Path(path)
    if not p.is_file():
        return None
    return BackendRegistry.from_file(p)


# --- harvester config (ADR-0007: read adapters) -------------------------------------------------


class HarvestPolicy(BaseModel):
    """The repo's harvest policy, read from ``_kb/repo.yaml`` ``harvest:`` (DATA-MODEL §3).

    A SEPARATE model from :class:`RepoConfig` on purpose: ``RepoConfig`` is the integrity-neutral
    curator/CLI input and is ``extra='forbid'``; folding harvest policy in would couple the
    curator's config to an opt-in read-side feature. ``enabled`` defaults to ``False`` (ADR-0007:
    harvesting is opt-in and disabled by default). ``scope_lock`` is the source-scope the repo
    accepts (DATA-MODEL §3). ``repo_kind`` is the repo's EXPLICIT ``kind`` (``None`` when
    absent/omitted) — the fail-closed input to the scope gate: a harvester treats an unknown kind as
    ``team`` and refuses a personal feed (so a missing/edited ``repo.yaml`` can never silently let
    personal memory into a team repo, ADR-0007 mechanism 3 / ADR-0017).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    scope_lock: str = "personal"
    repo_kind: str | None = None


def load_harvest_policy(layout: RepoLayout) -> HarvestPolicy:
    """Load the ``harvest:`` policy from ``_kb/repo.yaml`` (DATA-MODEL §3); defaults when absent.

    Read via the same raw-mapping ``.get()`` path :func:`load_repo_config` uses (NOT by constructing
    ``RepoConfig`` from the raw dict, which is ``extra='forbid'`` and would reject a ``harvest:``
    key). A missing file / ``harvest:`` block yields ``enabled=False`` (opt-in default), so harvest
    fails SAFE — it simply does nothing until the operator explicitly enables it. An explicit but
    invalid ``harvest.scope_lock`` or repo ``kind`` raises :class:`ConfigError` (a typo in a
    privacy-relevant policy must surface, never silently take a default).
    """
    raw = _read_yaml_mapping(repo_config_path(layout))
    harvest = _sub_mapping(raw.get("harvest"))
    enabled = _opt_bool(harvest.get("enabled"), False, key="harvest.enabled")
    scope_lock = _opt_str(harvest.get("scope_lock")) or "personal"
    if scope_lock not in _SCOPE_VALUES:
        raise ConfigError(
            f"harvest.scope_lock must be one of {list(_SCOPE_VALUES)}, got {scope_lock!r}"
        )
    kind = _opt_str(raw.get("kind"))
    if kind is not None and kind not in _SCOPE_VALUES:
        raise ConfigError(f"repo kind must be one of {list(_SCOPE_VALUES)}, got {kind!r}")
    return HarvestPolicy(enabled=enabled, scope_lock=scope_lock, repo_kind=kind)


# --- connector-boundary redaction config (ADR-0023 decision 5 / addendum §5, issue #25) ---------


@dataclass(frozen=True)
class RedactSettings:
    """Resolved ``harvest.redact`` config: the kill-switch + the effective :class:`RedactionPolicy`.

    A frozen value (like :class:`ConnectorSpec`) so this config seam stays free of harvester state.
    ``enabled`` (default ``True``) is the kill-switch: when off, the ``session:`` connector performs
    NO redaction (the operator's explicit escape hatch, accepting the un-scrubbable-inbox risk).
    ``policy`` is the :class:`~agora_kb.core.redact.RedactionPolicy` the connector applies at its
    boundary before the immutable inbox write; the structural default-on tier
    (:data:`~agora_kb.core.redact.DEFAULT_ON_CLASSES`) is ALWAYS present when enabled — config only
    *widens* it (``pii`` extra classes, ``deny`` literals) or *narrowly suppresses* it (``allow``
    literals), never drops a structural class (ADR-0023 addendum §5).
    """

    enabled: bool
    policy: RedactionPolicy


def load_redact_policy(layout: RepoLayout) -> RedactSettings:
    """Load ``harvest.redact`` from ``_kb/repo.yaml`` (ADR-0023 addendum §5); defaults when absent.

    Fail-loud, mirroring :func:`load_harvest_policy`: read via the same raw-mapping ``.get()`` path
    (NOT by constructing an ``extra='forbid'`` model). A missing file / ``redact:`` block yields the
    secure default — redaction ENABLED with the Balanced-9 structural tier
    (:data:`~agora_kb.core.redact.DEFAULT_ON_CLASSES`) and empty allow/deny, so a ``session:``
    connector redacts out of the box. Keys:

    * ``enabled`` (bool, default ``True``) — the kill-switch.
    * ``pii`` (list of class names) — EXTRA classes to enable beyond the structural default-on set
      (e.g. the opt-in ``generic_assigned_secret``). Each MUST be in
      :data:`~agora_kb.core.redact.KNOWN_CLASSES` or it raises :class:`ConfigError` — a typo in a
      privacy-relevant policy must surface, never silently take a default.
    * ``allow`` (list of literals) — secrets NEVER redacted (a documented sample credential).
    * ``deny`` (list of literals) — extra literals ALWAYS redacted.

    The structural tier is never dropped: the effective class set is
    ``DEFAULT_ON_CLASSES | set(pii)`` — ``pii`` can only ADD.
    """
    raw = _read_yaml_mapping(repo_config_path(layout))
    harvest = _sub_mapping(raw.get("harvest"))
    redact = _sub_mapping(harvest.get("redact"))
    enabled = _opt_bool(redact.get("enabled"), True, key="harvest.redact.enabled")
    pii = _str_list(redact.get("pii"))
    unknown = [c for c in pii if c not in KNOWN_CLASSES]
    if unknown:
        raise ConfigError(
            f"harvest.redact.pii: unknown redaction class(es) {unknown}; "
            f"known classes are {sorted(KNOWN_CLASSES)}"
        )
    policy = RedactionPolicy(
        classes=DEFAULT_ON_CLASSES | frozenset(pii),
        allow=tuple(_str_list(redact.get("allow"))),
        deny=tuple(_str_list(redact.get("deny"))),
    )
    return RedactSettings(enabled=enabled, policy=policy)


# --- derived reader-cache config (ADR-0012 §2, issue #26) ---------------------------------------


class IndexPolicy(BaseModel):
    """The repo's reader-cache policy, from ``_kb/repo.yaml`` ``index:`` (DATA-MODEL §3).

    A SEPARATE model from :class:`RepoConfig` (which is ``extra='forbid'``), same posture as
    :class:`HarvestPolicy`. ``enabled`` (default ``True``) is the kill-switch: when off the
    read path always does today's full pure-Python scan. The reader's candidate prefilter is the
    exact in-memory inverted index built from the loaded ``field_tokens`` (free + correct in the
    current all-notes-loaded architecture); the optional FTS5/ripgrep candidate accelerators
    (ADR-0012 §9) are deferred to a future load-avoiding reader (issue #28), so there is no
    accelerator flag to configure here.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


def load_index_policy(layout: RepoLayout) -> IndexPolicy:
    """Load the ``index:`` policy from ``_kb/repo.yaml`` (DATA-MODEL §3); defaults when absent.

    Read via the same raw-mapping ``.get()`` path as :func:`load_harvest_policy` (NOT via
    ``RepoConfig``, ``extra='forbid'`` — it would reject ``index:``). A missing file /
    ``index:`` block yields the default (cache on). An explicit but non-boolean ``index.enabled``
    raises :class:`ConfigError` (a typo must surface, never silently take a default).
    """
    raw = _read_yaml_mapping(repo_config_path(layout))
    index = _sub_mapping(raw.get("index"))
    enabled = _opt_bool(index.get("enabled"), True, key="index.enabled")
    return IndexPolicy(enabled=enabled)


@dataclass(frozen=True)
class ConnectorSpec:
    """One READ-adapter entry parsed from ``adapters.yaml`` ``connectors:`` (DATA-MODEL §8).

    A lightweight, behavior-free value (the harvester turns it into a live connector) so this
    config seam stays free of any harvester import. ``name`` is the connector key (e.g.
    ``file:claude-code`` — the ``<type>:<agent>`` form). ``scope`` is the validated source scope
    (``personal`` | ``team``, a plain string here; the harvester maps it to its ``Scope`` enum).
    ``path`` is the source locator (a glob for a file connector; ``None`` for deferred API
    connectors).
    """

    name: str
    scope: str
    path: str | None
    follow_links: bool = False


def load_connector_specs(path: str | Path) -> list[ConnectorSpec] | None:
    """Load the READ-adapter ``connectors:`` block from ``adapters.yaml`` (DATA-MODEL §8).

    Returns ``None`` when the file or the ``connectors:`` block is absent (mirroring
    :func:`load_backend_registry`'s None-on-absent contract, so the caller surfaces a clear "no
    connectors configured" note rather than crashing). The existing :class:`BackendRegistry` IGNORES
    the ``connectors:`` block, so this is the connector family's OWN parser and it owns all
    validation: a non-mapping block, a non-mapping entry, or an unknown ``scope`` value raises
    :class:`ConfigError` (FAIL LOUD — operator config is a trust boundary, ADR-0007). An absent
    per-entry ``scope`` defaults to ``personal`` (the most restrictive scope; it may feed only a
    personal repo). Commented-out ``letta:`` / ``mem0:`` examples are simply not present after YAML
    parse and so are tolerated.
    """
    p = Path(path)
    if not p.is_file():
        return None
    raw = _read_yaml_mapping(p)
    conns = raw.get("connectors")
    if conns is None:
        return None
    if not isinstance(conns, dict):
        raise ConfigError("'connectors' must be a mapping of name → connector spec")
    specs: list[ConnectorSpec] = []
    for name, spec in conns.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"connector {name!r} must be a mapping of fields")
        scope = _opt_str(spec.get("scope")) or "personal"
        if scope not in _SCOPE_VALUES:
            raise ConfigError(
                f"connector {name!r}: scope must be one of {list(_SCOPE_VALUES)}, got {scope!r}"
            )
        # follow_links (ADR-0018) is opt-in; fail LOUD on a non-bool (e.g. the string "true") rather
        # than truthy-coercing it — a privacy/read-surface flag must never silently misread.
        follow_links = _opt_bool(
            spec.get("follow_links"), False, key=f"connector {name!r}: follow_links"
        )
        specs.append(
            ConnectorSpec(
                name=str(name),
                scope=scope,
                path=_opt_str(spec.get("path")),
                follow_links=follow_links,
            )
        )
    return specs


# --- web face config (ADR-0025: operator settings for the web face) -----------------------------
#
# The web face's operator-tunable policy: graph caps, per-file/per-batch upload limits, an optional
# allowed-extension restriction, and feature flags. Read from ``_kb/repo.yaml`` ``web:`` (DATA-MODEL
# §3) — NON-CANONICAL operator config (invariant 1): it lives in the git-ignored ``_kb/`` spool,
# never in ``wiki/`` and never round-tripped by the curator. Kept a SEPARATE model from
# :class:`RepoConfig` (web policy is a distinct concern; ``RepoConfig`` is the integrity-neutral
# curator input). :func:`load_web_config` resolves it PER-REPO inside the face's ``build_app`` so a
# browser can never flip one repo's policy onto another (invariant 5 / ADR-0006).

# Graph soft-cap default — LARGE so a sizable KB renders by default; honest truncation is kept
# (ADR-0021 §cap). Configurable down for small/slow clients.
_DEFAULT_GRAPH_MAX_NODES = 10_000
_DEFAULT_GRAPH_MAX_DEPTH = 3
# Upload limits — per-FILE (the today's MAX_UPLOAD_BYTES footgun bound) + per-BATCH count/total caps
# for the multi-upload path (ADR-0025). Localhost single-user guards, not auth controls.
_DEFAULT_UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # 25 MiB per file
_DEFAULT_UPLOAD_MAX_FILES = 50  # per-batch file count
_DEFAULT_UPLOAD_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MiB per batch


class WebGraphConfig(BaseModel):
    """Graph-viz caps (ADR-0021 caps → config, ADR-0025). ``extra='forbid'`` — a typo fails loud."""

    model_config = ConfigDict(extra="forbid")

    max_nodes: int = Field(default=_DEFAULT_GRAPH_MAX_NODES, ge=1)
    max_depth: int = Field(default=_DEFAULT_GRAPH_MAX_DEPTH, ge=1)


class WebUploadConfig(BaseModel):
    """Upload limits (ADR-0025): per-file max_bytes, per-batch max_files + total_bytes caps."""

    model_config = ConfigDict(extra="forbid")

    max_bytes: int = Field(default=_DEFAULT_UPLOAD_MAX_BYTES, ge=1)
    max_files: int = Field(default=_DEFAULT_UPLOAD_MAX_FILES, ge=1)
    total_bytes: int = Field(default=_DEFAULT_UPLOAD_TOTAL_BYTES, ge=1)


class WebExtensionsConfig(BaseModel):
    """Allowed-extension gate (ADR-0025).

    ``allowed=None`` (the default) → use the extractor's built-in supported set (no face-level
    gate); a LIST → restrict uploads to those extensions (face gates BEFORE calling ``extract``, so
    ``extract`` itself stays format-driven). Values are dotted, lowercased extensions (``.pdf``).
    """

    model_config = ConfigDict(extra="forbid")

    allowed: list[str] | None = None


class WebFeaturesConfig(BaseModel):
    """Web feature flags (ADR-0025). ``graph_enabled`` off → /graph routes 404 + nav link hides."""

    model_config = ConfigDict(extra="forbid")

    graph_enabled: bool = True


class WebConfig(BaseModel):
    """Operator settings for the web face (ADR-0025), read from ``_kb/repo.yaml`` ``web:``.

    The keystone the rest of the web-face enhancements consume: :class:`WebGraphConfig` caps, the
    :class:`WebUploadConfig` per-file/per-batch limits, the :class:`WebExtensionsConfig` allow-list,
    and :class:`WebFeaturesConfig` flags. NON-CANONICAL config (invariant 1) — never canonical
    knowledge, never touched by the curator. ``extra='forbid'`` on every sub-model so a typo'd wired
    key fails loud; the LOADER (:func:`load_web_config`) tolerates an absent ``web:`` block (→ all
    defaults) and unknown TOP-LEVEL ``repo.yaml`` keys.
    """

    model_config = ConfigDict(extra="forbid")

    graph: WebGraphConfig = Field(default_factory=WebGraphConfig)
    upload: WebUploadConfig = Field(default_factory=WebUploadConfig)
    extensions: WebExtensionsConfig = Field(default_factory=WebExtensionsConfig)
    features: WebFeaturesConfig = Field(default_factory=WebFeaturesConfig)


def load_web_config(layout: RepoLayout) -> WebConfig:
    """Load the ``web:`` block from ``_kb/repo.yaml`` (DATA-MODEL §3, ADR-0025); defaults if absent.

    Read via the same raw-mapping ``.get()`` path the other loaders use (NOT ``WebConfig(**raw)``):
    each field is narrowed with the tolerant ``_opt_int``/``_opt_bool``/``_str_list`` helpers, so an
    absent ``web:`` block (or any absent sub-key) yields the documented default. A present-but-
    wrong-typed value (e.g. ``graph.max_nodes: "lots"``) raises :class:`ConfigError` — operator
    policy must change CLEARLY, never silently take a default (mirrors the harvest/isolation
    loaders; see the module docstring near ``ConfigError``). Resolved PER-REPO by ``build_app`` so
    web policy can never leak across repos (invariant 5 / ADR-0006). Unknown sub-keys (e.g.
    ``web.graph.typo``) are IGNORED at the loader level, consistent with the other ``.get()``-based
    loaders (only WIRED keys are read), so a stray key never crashes the face.
    """
    raw = _read_yaml_mapping(repo_config_path(layout))
    web = _sub_mapping(raw.get("web"))

    graph = _sub_mapping(web.get("graph"))
    upload = _sub_mapping(web.get("upload"))
    extensions = _sub_mapping(web.get("extensions"))
    features = _sub_mapping(web.get("features"))

    allowed_raw = extensions.get("allowed")
    allowed: list[str] | None
    if allowed_raw is None:
        allowed = None
    else:
        # Normalize to lowercased, dot-prefixed extensions so the face's gate compares apples to the
        # extractor's `_ext()` output. A non-list `allowed:` is operator confusion → fail loud.
        if not isinstance(allowed_raw, list):
            raise ConfigError(f"web.extensions.allowed must be a list, got {allowed_raw!r}")
        # Validate every element is a string BEFORE normalizing: `_str_list` would SILENTLY drop a
        # non-string entry (e.g. `[.pdf, 123, .md]` → ['.pdf', '.md']), masking operator confusion.
        # Mirror the "must be a list" rule — change the stated policy CLEARLY, never silently.
        for e in allowed_raw:
            if not isinstance(e, str):
                raise ConfigError(f"web.extensions.allowed entries must be strings, got {e!r}")
        allowed = [_normalize_ext(e) for e in _str_list(allowed_raw)]

    return WebConfig(
        graph=WebGraphConfig(
            max_nodes=_opt_int(
                graph.get("max_nodes"), _DEFAULT_GRAPH_MAX_NODES, key="web.graph.max_nodes"
            ),
            max_depth=_opt_int(
                graph.get("max_depth"), _DEFAULT_GRAPH_MAX_DEPTH, key="web.graph.max_depth"
            ),
        ),
        upload=WebUploadConfig(
            max_bytes=_opt_int(
                upload.get("max_bytes"), _DEFAULT_UPLOAD_MAX_BYTES, key="web.upload.max_bytes"
            ),
            max_files=_opt_int(
                upload.get("max_files"), _DEFAULT_UPLOAD_MAX_FILES, key="web.upload.max_files"
            ),
            total_bytes=_opt_int(
                upload.get("total_bytes"),
                _DEFAULT_UPLOAD_TOTAL_BYTES,
                key="web.upload.total_bytes",
            ),
        ),
        extensions=WebExtensionsConfig(allowed=allowed),
        features=WebFeaturesConfig(
            graph_enabled=_opt_bool(
                features.get("graph_enabled"), True, key="web.features.graph_enabled"
            ),
        ),
    )


def _normalize_ext(ext: str) -> str:
    """Lowercase + dot-prefix an operator-supplied extension (``MD`` / ``.md`` → ``.md``)."""
    e = ext.strip().lower()
    if e and not e.startswith("."):
        e = f".{e}"
    return e


# --- internals ----------------------------------------------------------------------------------


def _load_taxonomy(
    layout: RepoLayout,
    *,
    repo_domains: list[str],
    repo_schema_version: object,
) -> Taxonomy:
    """Build the FIXED :class:`Taxonomy` from disk, PREFERRING ``_meta/taxonomy.yaml`` (D6).

    The emitted ``_meta/taxonomy.yaml`` is the authoritative closed vocabulary the bundle copies and
    the lint reads, so it wins for ``allowed_tags`` / ``domains`` / ``taxonomy_policy`` /
    ``schema_version``. Only when that file is absent (a pre-emit repo) do we fall back to
    ``repo.yaml domains`` so a freshly-cloned repo still has a usable domain hint. ``allowed_tags``
    is NEVER synthesized from ``repo.yaml`` (which lists no tags), so the config can never widen the
    closed tag set the curator validates against.
    """
    meta = _read_yaml_mapping(layout.root / "_meta" / "taxonomy.yaml")
    if meta:
        # _meta/taxonomy.yaml allowed_tags is a mapping {tag: {}} (emit.py); domains is a list.
        allowed_tags = meta.get("allowed_tags")
        if isinstance(allowed_tags, dict):
            tags = tuple(str(t) for t in allowed_tags)
        else:
            tags = tuple(_str_list(allowed_tags))
        return Taxonomy(
            schema_version=_opt_int(meta.get("schema_version"), 1, key="schema_version"),
            taxonomy_policy=_opt_str(meta.get("taxonomy_policy")) or "open",
            allowed_tags=tags,
            domains=tuple(_str_list(meta.get("domains"))),
        )
    return Taxonomy(
        schema_version=_opt_int(repo_schema_version, 1, key="schema_version"),
        taxonomy_policy="open",
        allowed_tags=(),
        domains=tuple(repo_domains),
    )


def _build_triggers(raw: object) -> TriggerConfig:
    """Build a :class:`TriggerConfig` from the ``curator.triggers`` mapping (defaults if absent).

    Only the keys :class:`TriggerConfig` knows (cron/threshold/idle_minutes) are forwarded, each
    narrowed to its expected type; a non-mapping or extra documented trigger context falls back to
    the field default rather than tripping ``extra='forbid'``.
    """
    triggers = _sub_mapping(raw)
    defaults = TriggerConfig()
    return TriggerConfig(
        cron=_opt_str(triggers.get("cron")) or defaults.cron,
        threshold=_opt_int(
            triggers.get("threshold"), defaults.threshold, key="curator.triggers.threshold"
        ),
        idle_minutes=_opt_int(
            triggers.get("idle_minutes"),
            defaults.idle_minutes,
            key="curator.triggers.idle_minutes",
        ),
    )


def _read_yaml_mapping(path: Path) -> dict[str, object]:
    """Parse ``path`` as a YAML mapping; ``{}`` for an absent/empty/non-mapping file.

    A syntactically-MALFORMED file (e.g. an unquoted ``cron: @daily``) is re-raised as a typed
    :class:`ConfigError` naming the path, so a typo in ``repo.yaml`` / ``_meta/taxonomy.yaml`` is
    surfaced cleanly by the CLI/MCP rather than escaping as a raw ``yaml.YAMLError`` stacktrace out
    of ``agora status`` / ``curate``. (Absent/empty/non-mapping still degrades to ``{}`` defaults.)
    """
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"malformed YAML in {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _sub_mapping(value: object) -> dict[str, object]:
    """Return ``value`` as a ``dict[str, object]`` iff it is a mapping, else ``{}`` (narrowing).

    Used so a missing/non-mapping ``curator``/``triggers`` sub-block reads as an empty mapping —
    every ``.get`` then returns ``None`` (the field default) — not a type error or attribute crash.
    """
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_int(value: object, default: int, *, key: str = "value") -> int:
    """Coerce an operator-supplied numeric to ``int``, or fall back to ``default`` when absent.

    An absent value (``None``) takes the ``default``. A genuine ``int`` (NOT bool — bool is an int
    subclass, so ``true``/``false`` would otherwise coerce to 1/0) is honored as-is. An INTEGRAL
    float (``5.0`` — a plausible operator typo) is accepted as its integer value rather than
    silently discarded. A supplied-but-wrong value (a non-integral float like ``5.5``, a string, …)
    raises a clear :class:`ConfigError` so the operator's stated policy is never silently replaced
    with a different default.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer, got boolean {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ConfigError(f"{key} must be an integer, got {value!r}")


def _opt_bool(value: object, default: bool, *, key: str = "value") -> bool:
    """Coerce an operator-supplied boolean, or fall back to ``default`` when absent.

    An absent value (``None``) takes ``default``. A genuine ``bool`` is honored. Any other type (a
    string like ``"true"``, an int, …) raises a clear :class:`ConfigError` so a fail-closed security
    flag (``curator.allow_reduced_isolation``) is never silently misread — an operator who typed a
    non-boolean must see it, not have it default to the safe value and mask a typo.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{key} must be a boolean, got {value!r}")


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, str)]
