"""Prometheus ``/metrics`` exporter — the operational half of the observability split.

DESIGN §5.3 / ADR-0019 §5 split observability in two: **operational time-series** (queue depth,
throughput, cursors) go to Prometheus + Grafana (the AGPL graphing UI is an external
operator-supplied sidecar, never bundled — ADR-0019 §5); **content/health** views (note counts,
lint signals, tag distribution) stay in the in-app dashboard. This module is the Prometheus side.

It is deliberately **cheap on every scrape**. Prometheus scrapes ``/metrics`` frequently, so the
collector reads ONLY the already-materialized metadata the meta face already serves — the inbox dir
count, ``_kb/state.json`` counters/last-run, and the per-connector harvest cursors — via the same
transport-free :class:`~agora_kb.faces.mcp_server.AgoraHandlers` seams the dashboard's *cheap*
panels use (:meth:`AgoraHandlers.status` / :meth:`~AgoraHandlers.curator_status` /
:meth:`~AgoraHandlers.harvester_status`). It NEVER runs :meth:`AgoraHandlers.health` — that is the
heavy ``lint()`` + whole-tree note scan path, which is the content/health view and stays in the
dashboard. No ``lint()``, no whole-tree note scan, no write path: this is a pure read derived from
existing metadata (invariants 1/2 untouched).

``prometheus_client`` is the OPTIONAL ``metrics`` extra. It is imported **lazily** (inside the
collector / :func:`render_latest`), so ``import agora_kb`` and the web app keep working without the
extra installed; a missing dependency surfaces as a clear HTTP 503 with an install remedy at the
``/metrics`` route, never an import-time crash.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agora_kb.core import Repo
from agora_kb.faces.mcp_server import AgoraHandlers

if TYPE_CHECKING:
    from collections.abc import Iterator

    from prometheus_client.core import Metric

__all__ = ["AgoraCollector", "render_latest", "MetricsUnavailable", "INSTALL_METRICS"]

# The remedy printed when the optional `metrics` extra (prometheus-client) is missing.
INSTALL_METRICS = (
    "install the metrics extra: pip install 'agora-kb[metrics]' (or uv sync --extra metrics)"
)


class MetricsUnavailable(RuntimeError):
    """Raised by :func:`render_latest` when ``prometheus-client`` (the ``metrics`` extra) is absent.

    Carries the install remedy in its message so the ``/metrics`` route can return a clear 503
    rather than letting a raw :class:`ImportError` escape (the route still starts without it).
    """


def _iso_to_epoch(value: str | None) -> float | None:
    """Parse an ``AgoraHandlers`` ISO-Z timestamp (``2026-06-13T03:00:12Z``) to a unix epoch second.

    The meta seams render UTC instants as ``...Z`` strings (``mcp_server._iso_z``); Prometheus wants
    unix seconds for a ``_timestamp_seconds`` gauge. ``None`` (never run / never scanned) propagates
    as ``None`` so the caller can omit the sample. Tolerant: an unparseable value yields ``None``
    rather than raising on a scrape.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class AgoraCollector:
    """A custom ``prometheus_client`` collector that reads CURRENT state on each scrape.

    Implements the collector protocol: :meth:`collect` is a generator that, when Prometheus scrapes,
    reads the live metadata via the cheap :class:`AgoraHandlers` meta seams and yields one metric
    family per signal. Constructed over a repo path; it re-resolves nothing heavy per scrape — each
    ``collect()`` is a fresh ``status`` / ``curator_status`` / ``harvester_status``
    read (inbox dir count + ``state.json`` + cursors), never :meth:`AgoraHandlers.health` (the heavy
    ``lint()`` / whole-tree scan path). Register it in a DEDICATED registry, not the global default,
    so repeated app construction (tests, multiple instances) never double-registers.
    """

    def __init__(self, repo: Repo) -> None:
        self._repo = repo

    def collect(self) -> Iterator[Metric]:
        """Yield the current Agora operational metric families (called by Prometheus per scrape).

        Reads the three CHEAP meta aggregations — never ``health()`` — and maps them onto Prometheus
        families with conventional names/units (``snake_case``, base units, ``_total`` for monotonic
        counters, ``_timestamp_seconds`` for unix instants, ``agora_`` prefix). Robust to None:
        a never-run curator omits the last-consolidation sample; a null backend omits the info
        metric; an empty connector list yields the (empty) harvester families without error.
        """
        # Lazy import: keeps `import agora_kb` and the web app working without the `metrics` extra.
        from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

        handlers = AgoraHandlers(self._repo)
        status = handlers.status()
        curator = handlers.curator_status()
        harvester = handlers.harvester_status()

        # --- inbox / backlog gauges (cheap dir count + state.json) -------------------------------
        yield GaugeMetricFamily(
            "agora_inbox_depth",
            "Pending items in the inbox awaiting consolidation.",
            value=float(status["inbox_depth"]),  # type: ignore[arg-type]
        )
        yield GaugeMetricFamily(
            "agora_processed_today",
            "Items consolidated today (UTC) into the processed spool.",
            value=float(status["processed_today"]),  # type: ignore[arg-type]
        )
        yield GaugeMetricFamily(
            "agora_failed",
            "Terminal-failure items currently under failed/.",
            value=float(status["failed"]),  # type: ignore[arg-type]
        )

        # --- cumulative curator dispositions (monotonic state.json counters → counter) -----------
        counters = status["counters"]
        dispositions = CounterMetricFamily(
            "agora_curator_dispositions",
            "Cumulative curator dispositions by operation (state.json counters).",
            labels=["op"],
        )
        for op in ("ingested", "merged", "dropped", "failed"):
            dispositions.add_metric([op], float(counters[op]))  # type: ignore[index]
        yield dispositions

        # --- last consolidation (unix timestamp gauge; omitted when never run) -------------------
        last_run = _iso_to_epoch(status["last_consolidation"])  # type: ignore[arg-type]
        if last_run is not None:
            yield GaugeMetricFamily(
                "agora_last_consolidation_timestamp_seconds",
                "Unix time of the last consolidation run (omitted when the curator has never run).",
                value=last_run,
            )

        # --- active backend (info-metric: value 1 + a backend label; omitted when null) ----------
        backend = curator["active_backend"]
        if backend is not None:
            backend_info = GaugeMetricFamily(
                "agora_active_backend_info",
                "Active curator AUTHOR backend (value 1; the brain name is the 'backend' label).",
                labels=["backend"],
            )
            backend_info.add_metric([str(backend)], 1.0)
            yield backend_info

        # --- per-connector harvester families (counters monotonic; last_scan a ts gauge) ---------
        connectors = harvester["connectors"]
        proposed = CounterMetricFamily(
            "agora_harvester_proposed",
            "Candidates proposed by a harvester connector (cumulative).",
            labels=["connector"],
        )
        accepted = CounterMetricFamily(
            "agora_harvester_accepted",
            "Candidates accepted from a connector (curator-owned; deferred ADR-0017 §7 → 0).",
            labels=["connector"],
        )
        rejected = CounterMetricFamily(
            "agora_harvester_rejected",
            "Candidates rejected from a connector (curator-owned; deferred ADR-0017 §7 → 0).",
            labels=["connector"],
        )
        last_scan = GaugeMetricFamily(
            "agora_harvester_last_scan_timestamp_seconds",
            "Unix time of a connector's last scan (omitted per-connector when never scanned).",
            labels=["connector"],
        )
        for conn in connectors:  # type: ignore[union-attr]
            name = str(conn["name"])
            proposed.add_metric([name], float(conn["proposed"]))
            # accepted/rejected are curator-owned and CURRENTLY 0 (deferred) — export the real 0.
            accepted.add_metric([name], float(conn["accepted"]))
            rejected.add_metric([name], float(conn["rejected"]))
            scanned = _iso_to_epoch(conn["last_scan"])
            if scanned is not None:
                last_scan.add_metric([name], scanned)
        yield proposed
        yield accepted
        yield rejected
        yield last_scan

        # --- gold pack gauges (ADR-0027 / #37) ---------------------------------------------------
        # Cheap: the `gold` row is read from the pack's meta sidecar (no assemble, no git — the
        # status() row is meta-only). Age is derived from the recorded build instant at scrape time
        # (freshness = curation cadence, ADR-0027 decision 6); everything is omitted when no pack
        # has been built yet.
        gold = status["gold"]
        if isinstance(gold, dict) and gold.get("present"):
            pack = str(gold.get("pack", "default"))
            note_count = GaugeMetricFamily(
                "agora_gold_pack_note_count",
                "Notes assembled into a gold context pack.",
                labels=["pack"],
            )
            note_count.add_metric([pack], float(gold["note_count"]))  # type: ignore[arg-type]
            yield note_count
            est_tokens = GaugeMetricFamily(
                "agora_gold_pack_est_tokens",
                "Estimated token size of a gold context pack (script-aware estimator).",
                labels=["pack"],
            )
            est_tokens.add_metric([pack], float(gold["est_tokens"]))  # type: ignore[arg-type]
            yield est_tokens
            harvest_share = GaugeMetricFamily(
                "agora_gold_pack_harvest_derived_share",
                "Fraction of a gold pack that is harvest-derived (ADR-0027 §8 cap telemetry).",
                labels=["pack"],
            )
            harvest_share.add_metric(
                [pack],
                float(gold["harvest_derived_share"]),  # type: ignore[arg-type]
            )
            yield harvest_share
            generated = _iso_to_epoch(gold.get("generated_at"))  # type: ignore[arg-type]
            if generated is not None:
                gen_ts = GaugeMetricFamily(
                    "agora_gold_pack_generated_timestamp_seconds",
                    "Unix time a gold pack was last built.",
                    labels=["pack"],
                )
                gen_ts.add_metric([pack], generated)
                yield gen_ts
                age = GaugeMetricFamily(
                    "agora_gold_pack_age_seconds",
                    "Seconds since a gold pack was last built (freshness = curation cadence).",
                    labels=["pack"],
                )
                age.add_metric([pack], max(0.0, datetime.now(UTC).timestamp() - generated))
                yield age


def render_latest(repo: Repo) -> tuple[bytes, str]:
    """Render the Prometheus exposition for ``repo`` → ``(body, content_type)``.

    Registers a fresh :class:`AgoraCollector` in a DEDICATED :class:`CollectorRegistry` (NOT the
    global default ``REGISTRY``) so repeated calls / multiple app instances / tests never collide on
    a duplicate registration, then returns ``generate_latest(registry)`` and ``CONTENT_TYPE_LATEST``
    (the Prometheus text exposition media type). Imports ``prometheus_client`` lazily; raises
    :class:`MetricsUnavailable` (with the install remedy) when the ``metrics`` extra is absent so
    the caller can map it to a 503 instead of crashing.
    """
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
    except ImportError as exc:  # the optional `metrics` extra is not installed
        raise MetricsUnavailable(f"prometheus-client is not installed — {INSTALL_METRICS}") from exc

    registry = CollectorRegistry()
    registry.register(AgoraCollector(repo))
    return generate_latest(registry), CONTENT_TYPE_LATEST
