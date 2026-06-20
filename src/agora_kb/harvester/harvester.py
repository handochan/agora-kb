"""The harvester orchestrator — scan connectors → gate → write gated candidates (ADR-0007).

This is the read-side mirror of :mod:`agora_kb.curator.worker`. It drives the configured
:class:`~agora_kb.harvester.connectors.Connector` instances, applies ADR-0007's three mandatory
safety mechanisms, and appends what survives to the inbox as **gated candidates** for the curator's
keep/merge/drop review:

1. **Candidate gate (mechanism 2 — the PRIMARY loop/pollution control).** Every harvested fact is
   written with ``kind=candidate`` + ``confidence=low``, so the curator's existing gate
   (``plan.py``: ``GATE_ALLOWED_OPS = {MERGE_INTO_THEME, MARK_CONTESTED, DROP}``) forbids it from
   *originating* a theme — it must re-pass review every cycle. This, not the marker skip below, is
   the ADR-0007 §1 guarantee in this phase.
2. **Provenance + loop marking (mechanism 1).** Items carry ``source=harvest:<agent>``; the curator
   already stamps the resulting note region ``origin: harvest:<agent>`` (``apply.py``). A
   *secondary, best-effort, verbatim-only* origin-marker skip lives in the connector. The realistic
   reworded KB→memory→KB loop is **not** eliminated — it is a stated residual risk bounded by the
   gate (ADR-0017).
3. **Scope lock (mechanism 3).** :func:`check_scope` is a HARD pre-write gate: a personal source may
   feed only a personal repo, fail-closed on an unknown repo kind. In Phase 2 this is a
   single-process harvester gate, NOT the deferred multi-tenant core write boundary (ADR-0017).

The per-connector position is the DATA-MODEL §6 cursor ``_kb/harvest/<connector>.json``. The
harvester owns ``proposed`` (and ``last_scan`` / ``last_content_sha256`` / ``source_path``); the
``accepted`` / ``rejected`` counters are contracted to the curator at finalize (ADR-0011) and are
DEFERRED — they round-trip untouched here so a later curator change can populate them (ADR-0017).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from agora_kb.config import ConnectorSpec, HarvestPolicy
from agora_kb.core.atomicio import atomic_write_text
from agora_kb.core.inbox import Inbox
from agora_kb.core.layout import RepoLayout, validate_writer
from agora_kb.core.models import Confidence, Kind

from .connectors import Connector, ConnectorError, FileConnector, HarvestedFact, Scope

__all__ = [
    "ScopeViolation",
    "check_scope",
    "HarvestCursor",
    "CursorStore",
    "ConnectorReport",
    "HarvestReport",
    "Harvester",
    "build_connectors",
]


class ScopeViolation(Exception):
    """A connector's scope is not permitted to feed the target repo (ADR-0007 mechanism 3).

    Raised by :func:`check_scope` as a HARD pre-write gate — privacy enforcement fails closed, so a
    personal memory source can never bleed into a team repo.
    """


def check_scope(connector_scope: Scope, policy: HarvestPolicy) -> None:
    """Raise :class:`ScopeViolation` unless ``connector_scope`` may feed this repo (ADR-0007 §3).

    Two layers, both fail-closed:

    * **Config policy (DATA-MODEL §3).** The repo accepts only connectors whose scope matches its
      declared ``harvest.scope_lock``.
    * **Identity backstop (privacy).** A connector may feed only a repo of the SAME ``kind``; the
      repo kind is the EXPLICIT ``repo.yaml`` ``kind`` and an absent/unknown kind is treated as
      ``team`` — so a personal source feeding a repo with no declared (personal) identity is
      REFUSED, never silently allowed. Together these require
      ``connector_scope == scope_lock == repo_kind``,
      which encodes "a personal source may feed only a personal repo" with no silent widening.
    """
    if connector_scope.value != policy.scope_lock:
        raise ScopeViolation(
            f"connector scope {connector_scope.value!r} is not permitted by this repo's "
            f"harvest.scope_lock={policy.scope_lock!r}"
        )
    # Fail closed: an absent/unknown repo kind is treated as 'team', so a personal source can never
    # feed a repo whose personal identity is not explicitly declared.
    effective_kind = (
        policy.repo_kind if policy.repo_kind in (Scope.personal, Scope.team) else "team"
    )
    if connector_scope.value != effective_kind:
        raise ScopeViolation(
            f"a {connector_scope.value!r} source may feed only a {connector_scope.value!r} repo; "
            f"this repo's kind is {policy.repo_kind!r} (treated as {effective_kind!r})"
        )


class HarvestCursor(BaseModel):
    """Per-connector scan position — ``_kb/harvest/<connector>.json`` (DATA-MODEL §6).

    Held to EXACTLY the documented §6 fields (no unbounded extension — ADR-0017). The harvester
    writes ``connector`` / ``source_path`` / ``last_scan`` / ``last_content_sha256`` / ``proposed``;
    ``accepted`` / ``rejected`` are the curator's at finalize (ADR-0011) and are DEFERRED — they are
    preserved across saves so a future curator change can populate them without the harvester
    clobbering them.
    """

    model_config = ConfigDict(extra="forbid")

    connector: str
    source_path: str | None = None
    last_scan: datetime | None = None
    last_content_sha256: str | None = None
    proposed: int = 0
    accepted: int = 0
    rejected: int = 0

    @field_validator("last_scan")
    @classmethod
    def _check_last_scan(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("last_scan must be timezone-aware (UTC)")
        return v if v is None else v.astimezone(UTC)

    @field_serializer("last_scan")
    def _ser_last_scan(self, v: datetime | None) -> str | None:
        # DATA-MODEL §6 form: 2026-06-13T02:00:00Z (second precision, explicit Z).
        return None if v is None else v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def to_json(self) -> str:
        return self.model_dump_json(indent=2) + "\n"


class CursorStore:
    """Atomic, tolerant load/save of one repo's ``_kb/harvest/<connector>.json`` cursors.

    The cursor is a derived, rebuildable, git-ignored performance optimization (DATA-MODEL §6/§8) —
    NEVER an integrity control. A missing OR corrupt/hand-edited cursor therefore loads as a FRESH
    cursor (the next scan simply re-reads from scratch; the candidate gate absorbs any re-flood)
    rather than wedging the connector.
    """

    def __init__(self, layout: RepoLayout) -> None:
        self._layout = layout

    def load(self, connector: str) -> HarvestCursor:
        """Load a connector's cursor; return a fresh one when absent or unreadable/corrupt."""
        path = self._layout.harvest_cursor_path(connector)
        if not path.exists():
            return HarvestCursor(connector=connector)
        try:
            cursor = HarvestCursor.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Tolerant: a corrupt/partial/hand-edited cursor must not wedge harvesting — re-scan.
            return HarvestCursor(connector=connector)
        # The on-disk connector field is advisory; trust the requested identity (the cursor path is
        # already namespaced to it), so a renamed/copied file can't mislabel the connector.
        if cursor.connector != connector:
            cursor = cursor.model_copy(update={"connector": connector})
        return cursor

    def save(self, cursor: HarvestCursor) -> None:
        """Atomically (durably) rewrite a connector's cursor JSON (DATA-MODEL §6)."""
        path = self._layout.harvest_cursor_path(cursor.connector)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, cursor.to_json(), exclusive=False)


@dataclass(frozen=True)
class ConnectorReport:
    """Per-connector outcome of one harvest run (the unit the CLI/doctor digests)."""

    name: str
    agent: str
    scope: str
    status: Literal["ok", "unchanged", "scope-refused", "error"]
    facts_found: int = 0
    written: int = 0
    deduped: int = 0
    message: str | None = None
    notes: tuple[str, ...] = ()
    dry_run: bool = False
    preview: tuple[HarvestedFact, ...] = ()


@dataclass(frozen=True)
class HarvestReport:
    """The full outcome of an ``agora harvest`` run."""

    enabled: bool
    connectors: tuple[ConnectorReport, ...] = ()
    note: str | None = None

    @property
    def total_written(self) -> int:
        return sum(c.written for c in self.connectors)


def build_connectors(specs: list[ConnectorSpec]) -> list[Connector]:
    """Turn parsed :class:`ConnectorSpec`\\ s into live connectors (ADR-0004 read-adapter seam).

    Only ``file:`` connectors are implemented in this phase; an unknown connector type (a future
    ``letta:`` / ``mem0:`` key, DATA-MODEL §8) raises :class:`ConnectorError` (FAIL LOUD — never a
    silent skip). Each connector validates its own identity/path at construction.
    """
    connectors: list[Connector] = []
    for spec in specs:
        if spec.name.startswith("file:"):
            connectors.append(
                FileConnector(name=spec.name, path=spec.path or "", scope=Scope(spec.scope))
            )
        else:
            raise ConnectorError(
                f"unsupported connector type for {spec.name!r}: only 'file:' connectors are "
                "implemented in this phase (Letta/mem0 API connectors are deferred, DATA-MODEL §8)"
            )
    return connectors


class Harvester:
    """Run the configured connectors, gate them, and append gated candidates to the inbox."""

    def __init__(self, layout: RepoLayout) -> None:
        self._layout = layout
        self._inbox = Inbox(layout)
        self._cursors = CursorStore(layout)

    def run(
        self,
        connectors: list[Connector],
        *,
        policy: HarvestPolicy,
        repo_name: str = "personal",
        now: datetime | None = None,
        dry_run: bool = False,
        only: str | None = None,
    ) -> HarvestReport:
        """Harvest each connector (or just ``only``) into gated candidates; return a report.

        Short-circuits to a no-op when ``policy.enabled`` is False (ADR-0007 opt-in). ``now`` is
        injectable (the CLI passes ``datetime.now(UTC)``) so a run is a deterministic function of
        its inputs for tests. ``dry_run`` runs segmentation + the scope gate and reports what WOULD
        be written WITHOUT touching the inbox or advancing any cursor (the noise-pollution preview).
        """
        if not policy.enabled:
            return HarvestReport(
                enabled=False,
                note="harvest is disabled (set harvest.enabled: true in _kb/repo.yaml — ADR-0007)",
            )
        when = now or datetime.now(UTC)
        reports: list[ConnectorReport] = []
        for connector in connectors:
            if only is not None and connector.name != only:
                continue
            reports.append(
                self._run_one(
                    connector, policy=policy, repo_name=repo_name, now=when, dry_run=dry_run
                )
            )
        return HarvestReport(enabled=True, connectors=tuple(reports))

    # --- internals ------------------------------------------------------------------------------
    def _run_one(
        self,
        connector: Connector,
        *,
        policy: HarvestPolicy,
        repo_name: str,
        now: datetime,
        dry_run: bool,
    ) -> ConnectorReport:
        """Gate, scan, and (unless dry-run) write one connector's facts + advance its cursor."""
        # Scope gate FIRST — before any source read or inbox write (ADR-0007 mechanism 3).
        try:
            check_scope(connector.scope, policy)
        except ScopeViolation as exc:
            return ConnectorReport(
                name=connector.name,
                agent=connector.agent,
                scope=connector.scope.value,
                status="scope-refused",
                message=str(exc),
                dry_run=dry_run,
            )

        cursor = self._cursors.load(connector.name)
        try:
            scan = connector.scan(last_content_sha256=cursor.last_content_sha256)
        except OSError as exc:  # a source read that failed despite the connector's own guards
            return ConnectorReport(
                name=connector.name,
                agent=connector.agent,
                scope=connector.scope.value,
                status="error",
                message=f"scan failed: {exc}",
                dry_run=dry_run,
            )

        if scan.unchanged:
            return ConnectorReport(
                name=connector.name,
                agent=connector.agent,
                scope=connector.scope.value,
                status="unchanged",
                notes=scan.notes,
                dry_run=dry_run,
            )

        if dry_run:
            return ConnectorReport(
                name=connector.name,
                agent=connector.agent,
                scope=connector.scope.value,
                status="ok",
                facts_found=len(scan.facts),
                notes=scan.notes,
                dry_run=True,
                preview=scan.facts,
            )

        writer = f"harvest-{connector.agent}"
        source = f"harvest:{connector.agent}"
        if connector.scope == Scope.personal:
            target = "personal"
        else:
            # A team target is 'team:<repo_name>'; its name part uses the same safe-component
            # charset as a writer (models._TEAM_RE == _WRITER_RE charset). Validate it up front so
            # an invalid repo name yields a clean error report, not an uncaught error mid-write.
            try:
                validate_writer(repo_name)
            except ValueError as exc:
                return ConnectorReport(
                    name=connector.name,
                    agent=connector.agent,
                    scope=connector.scope.value,
                    status="error",
                    message=f"repo name {repo_name!r} is not a valid team target ({exc})",
                )
            target = f"team:{repo_name}"
        written = 0
        deduped = 0
        for fact in scan.facts:
            receipt = self._inbox.write(
                text=fact.text,
                writer=writer,
                source=source,
                target=target,
                domain=fact.domain,
                tags=list(fact.tags),
                kind=Kind.candidate,
                confidence=Confidence.low,
                event_key=fact.fact_key,
                now=now,
            )
            if receipt.queued:
                written += 1
            else:
                deduped += 1

        # Advance the §6 cursor: harvester-owned fields only; accepted/rejected stay untouched.
        # PRESERVE the prior whole-source hash on a no-match scan (content_sha256 is None) so a
        # transient source disappearance does not clobber the fast no-op next cycle (ADR-0017).
        cursor.source_path = scan.source_path
        cursor.last_scan = now
        if scan.content_sha256 is not None:
            cursor.last_content_sha256 = scan.content_sha256
        cursor.proposed += written
        self._cursors.save(cursor)

        return ConnectorReport(
            name=connector.name,
            agent=connector.agent,
            scope=connector.scope.value,
            status="ok",
            facts_found=len(scan.facts),
            written=written,
            deduped=deduped,
            notes=scan.notes,
        )
