"""harvester — read adapters that pull from other agents' memory + working-context sources into
inbox candidates, with provenance/gate/scope safety (ADR-0007/0023).

Public surface:

* :class:`~agora_kb.harvester.connectors.Connector` (Protocol) + :class:`FileConnector` (diff a
  markdown ``MEMORY.md``, ADR-0017) + :class:`SessionConnector` (distill agent session transcripts,
  ADR-0023) — the read-adapter seam and its two implementations; the :class:`SessionReader` seam
  (:class:`ClaudeCodeJsonlReader` / :class:`TurnRecord`) parses one transcript format;
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
from .session_connector import SessionConnector
from .session_sources import ClaudeCodeJsonlReader, SessionReader, TurnRecord

__all__ = [
    "Connector",
    "ConnectorError",
    "ConnectorScan",
    "FileConnector",
    "SessionConnector",
    "SessionReader",
    "ClaudeCodeJsonlReader",
    "TurnRecord",
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
