"""harvester — read adapters that pull from other agents' memory systems into inbox candidates, with
provenance/gate/scope safety (ADR-0007).

Public surface:

* :class:`~agora_kb.harvester.connectors.Connector` (Protocol) + :class:`FileConnector` — the
  read-adapter seam and the only Phase-2 connector (diff a markdown ``MEMORY.md``);
* :class:`Harvester` — the orchestrator that gates connectors and appends gated candidates;
* :func:`build_connectors` — :class:`~agora_kb.config.ConnectorSpec` → live connectors;
* :class:`HarvestReport` / :class:`ConnectorReport`, the per-connector cursor
  (:class:`HarvestCursor` / :class:`CursorStore`), and the scope gate (:func:`check_scope` /
  :class:`ScopeViolation`).
"""

from .connectors import (
    Connector,
    ConnectorError,
    ConnectorScan,
    FileConnector,
    HarvestedFact,
    Scope,
)
from .harvester import (
    ConnectorReport,
    CursorStore,
    HarvestCursor,
    Harvester,
    HarvestReport,
    ScopeViolation,
    build_connectors,
    check_scope,
)

__all__ = [
    "Connector",
    "ConnectorError",
    "ConnectorScan",
    "FileConnector",
    "HarvestedFact",
    "Scope",
    "ConnectorReport",
    "CursorStore",
    "HarvestCursor",
    "HarvestReport",
    "Harvester",
    "ScopeViolation",
    "build_connectors",
    "check_scope",
]
