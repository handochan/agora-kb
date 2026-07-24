"""Tests for the Prometheus ``/metrics`` exporter (DESIGN §5.3 / ADR-0019 §5).

The operational half of the two-way observability split: a custom ``prometheus_client`` collector
(:class:`agora_kb.faces.web.metrics.AgoraCollector`) reads the CHEAP meta seams on each scrape
(inbox depth, cumulative curator counters, harvester cursors, last-run timestamps, active backend) —
never the heavy ``lint()`` / whole-tree ``health()`` path, which stays in the dashboard. Two
surfaces: the collector over a planted temp repo, and the web ``/metrics`` route. The
``prometheus_client`` dep is the optional ``metrics`` extra (imported lazily) — the prometheus tests
``importorskip`` it; the 503-when-missing test does NOT (it monkeypatches the lazy import to raise
and must pass with the extra installed).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agora_kb.core import Repo
from agora_kb.core.state import StateStore


# --- fixtures -----------------------------------------------------------------------------------
def _init_repo(tmp_path: Path) -> Repo:
    repo = Repo.resolve(tmp_path)
    repo.init()
    return repo


def _plant_state(repo: Repo) -> None:
    """Plant state.json counters + last_run so the cumulative-counter / timestamp families fire."""
    from agora_kb.core.state import LastBatch

    store = StateStore(repo.layout)
    state = store.load()
    state.counters.ingested = 5
    state.counters.merged = 3
    state.counters.dropped = 2
    state.counters.failed = 1
    state.mark_run(datetime(2026, 6, 21, 3, 0, 12, tzinfo=UTC), commit="deadbeef")
    # #60: the last-run batch shape (claim cap observability) the batch gauges read.
    state.last_batch = LastBatch(claimed=12, candidates=8, cap=8, inbox_remaining=4)
    store.save(state)


def _plant_connector(repo: Repo) -> None:
    """Enable harvest + declare one connector with a planted cursor (proposed>0, the rest 0)."""
    from agora_kb.harvester import CursorStore, HarvestCursor

    repo.layout.kb_dir.mkdir(parents=True, exist_ok=True)
    (repo.layout.kb_dir / "repo.yaml").write_text(
        "name: personal\nkind: personal\nharvest:\n  enabled: true\n  scope_lock: personal\n",
        encoding="utf-8",
    )
    (repo.layout.root / "adapters.yaml").write_text(
        "backends:\n  qwen:\n    argv: [agora-ollama-brain]\n"
        "default_backend: qwen\n"
        "connectors:\n"
        "  file:claude-code:\n"
        '    path: "~/.claude/MEMORY.md"\n'
        "    scope: personal\n",
        encoding="utf-8",
    )
    CursorStore(repo.layout).save(
        HarvestCursor(
            connector="file:claude-code",
            source_path="/home/x/.claude/MEMORY.md",
            last_scan=datetime(2026, 6, 21, 2, 0, 0, tzinfo=UTC),
            proposed=7,
        )
    )


# --- (e) lazy-import invariant: the metrics module must stay import-light -------------------------
def test_metrics_module_import_is_light() -> None:
    """HARD invariant (ADR-0005): importing ``agora_kb.faces.web.metrics`` must NOT pull in
    ``prometheus_client`` — it is the optional ``metrics`` extra, imported lazily only on a scrape.

    A fresh subprocess checks ``sys.modules`` (this test process already imported prometheus_client
    via the importorskip below, so it cannot assert in-process). Placed BEFORE that importorskip so
    it runs even in a base install without the metrics extra — exactly where the invariant matters.
    """
    import subprocess
    import sys

    code = (
        "import sys, agora_kb.faces.web.metrics; "
        "assert 'prometheus_client' not in sys.modules, 'metrics module eagerly imported the extra'"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# --- (a) collector over a planted temp repo -----------------------------------------------------
prometheus_client = pytest.importorskip("prometheus_client")


def test_collector_renders_expected_families(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _plant_state(repo)
    _plant_connector(repo)
    # Two pending inbox items so the depth gauge is non-zero.
    from agora_kb.faces.mcp_server import AgoraHandlers

    AgoraHandlers(repo, writer="local").remember("one pending")
    AgoraHandlers(repo, writer="local").remember("two pending")

    from agora_kb.faces.web.metrics import render_latest

    body, content_type = render_latest(repo)
    assert content_type.startswith("text/plain")
    text = body.decode("utf-8")

    # Inbox / backlog gauges.
    assert "agora_inbox_depth 2.0" in text
    assert "agora_processed_today 0.0" in text
    assert "agora_failed 0.0" in text

    # Cumulative curator dispositions counter (note the _total suffix the client appends).
    assert 'agora_curator_dispositions_total{op="ingested"} 5.0' in text
    assert 'agora_curator_dispositions_total{op="merged"} 3.0' in text
    assert 'agora_curator_dispositions_total{op="dropped"} 2.0' in text
    assert 'agora_curator_dispositions_total{op="failed"} 1.0' in text

    # Active backend info-metric (value 1, the brain name in the label).
    assert 'agora_active_backend_info{backend="qwen"} 1.0' in text

    # Last-run batch shape gauges (#60: batch size per run + cap + post-claim queue depth).
    assert "agora_last_run_claimed_events 12.0" in text
    assert "agora_last_run_candidates 8.0" in text
    assert "agora_max_candidates_per_run 8.0" in text
    assert "agora_last_run_inbox_remaining 4.0" in text

    # Per-connector harvester counters: proposed>0; accepted/rejected the REAL deferred 0 (no fake).
    assert 'agora_harvester_proposed_total{connector="file:claude-code"} 7.0' in text
    assert 'agora_harvester_accepted_total{connector="file:claude-code"} 0.0' in text
    assert 'agora_harvester_rejected_total{connector="file:claude-code"} 0.0' in text

    # Parse the exposition so timestamp samples are compared as floats (the text form may render
    # large unix seconds in scientific notation, so a literal substring match would be brittle).
    from prometheus_client.parser import text_string_to_metric_families

    samples = {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for fam in text_string_to_metric_families(text)
        for s in fam.samples
    }
    expected_ts = datetime(2026, 6, 21, 3, 0, 12, tzinfo=UTC).timestamp()
    assert samples[("agora_last_consolidation_timestamp_seconds", ())] == expected_ts
    expected_scan = datetime(2026, 6, 21, 2, 0, 0, tzinfo=UTC).timestamp()
    assert (
        samples[
            (
                "agora_harvester_last_scan_timestamp_seconds",
                (("connector", "file:claude-code"),),
            )
        ]
        == expected_scan
    )
    # Well-formed families are present (the parser keeps the _total suffix on counter sample names).
    assert ("agora_inbox_depth", ()) in samples
    assert ("agora_curator_dispositions_total", (("op", "ingested"),)) in samples
    assert ("agora_harvester_proposed_total", (("connector", "file:claude-code"),)) in samples


def test_collector_robust_to_empty_repo(tmp_path: Path) -> None:
    """A fresh repo (no last_run, no backend, no connectors) renders fine, omitting samples."""
    repo = _init_repo(tmp_path)
    from agora_kb.faces.web.metrics import render_latest

    text = render_latest(repo)[0].decode("utf-8")
    assert "agora_inbox_depth 0.0" in text
    assert 'agora_curator_dispositions_total{op="ingested"} 0.0' in text
    # Never run → the last-consolidation sample is OMITTED entirely (not faked as 0).
    assert "agora_last_consolidation_timestamp_seconds" not in text
    # Never run → no batch shape either: the #60 gauges are OMITTED, never faked as 0.
    assert "agora_last_run_claimed_events" not in text
    assert "agora_last_run_candidates" not in text
    assert "agora_max_candidates_per_run" not in text
    assert "agora_last_run_inbox_remaining" not in text
    # No adapters.yaml → null backend → the info metric is omitted.
    assert "agora_active_backend_info" not in text
    # No connectors → the harvester family HELP/TYPE may appear but carries no samples.
    assert "agora_harvester_proposed_total{" not in text


# --- (b) web route 200 + content-type -----------------------------------------------------------
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _client(tmp_path: Path) -> TestClient:
    from agora_kb.faces.web import build_app

    return TestClient(build_app(repo_path=tmp_path, writer="web", user="alice"))


def test_metrics_route_returns_prometheus_exposition(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _plant_state(repo)
    _plant_connector(repo)
    resp = _client(tmp_path).get("/metrics")
    assert resp.status_code == 200
    # The Prometheus exposition content-type (CONTENT_TYPE_LATEST), NOT application/json. We compare
    # against the library constant rather than hardcoding a version so a prometheus-client bump
    # (e.g. the 0.0.4 → 1.0.0 default) does not falsely fail this contract check.
    from prometheus_client import CONTENT_TYPE_LATEST

    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-type"] == CONTENT_TYPE_LATEST
    body = resp.text
    assert "agora_inbox_depth" in body
    assert 'agora_curator_dispositions_total{op="ingested"} 5.0' in body
    # Per-connector harvester metrics traverse the full HTTP route, not just render_latest().
    assert 'agora_harvester_proposed_total{connector="file:claude-code"} 7.0' in body


# --- (c) 503 when prometheus-client is missing (passes WITH the extra installed) ----------------
def test_metrics_503_when_prometheus_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch the lazy import to raise ImportError → /metrics returns 503 + the install remedy.

    Proves the route degrades cleanly (startup never hard-requires the optional `metrics` extra);
    must pass even though prometheus-client IS installed here (we force the failure path).
    """
    _init_repo(tmp_path)
    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "prometheus_client" or name.startswith("prometheus_client."):
            raise ImportError("forced: prometheus_client absent")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    resp = _client(tmp_path).get("/metrics")
    assert resp.status_code == 503
    assert "agora-kb[metrics]" in resp.text


# --- (d) the scrape never runs the heavy lint()/health() path -----------------------------------
def test_metrics_does_not_call_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make health() raise; /metrics must still 200 — proving lint() is off the scrape path.

    DESIGN §5.3 / ADR-0019 §5: /metrics is the cheap operational read; the heavy content/health view
    (lint() + a whole-tree note scan) stays in the dashboard. If the collector ever reached
    health(), this monkeypatched explosion would surface as a 500.
    """
    _init_repo(tmp_path)
    from agora_kb.faces.mcp_server import AgoraHandlers

    def _boom(self: AgoraHandlers) -> dict[str, object]:
        raise AssertionError("health()/lint() must NOT run on a /metrics scrape (cheap-read guard)")

    monkeypatch.setattr(AgoraHandlers, "health", _boom)
    resp = _client(tmp_path).get("/metrics")
    assert resp.status_code == 200
    assert "agora_inbox_depth" in resp.text


def test_gold_gauges_after_build(tmp_path: Path) -> None:
    """ADR-0027 / #37: the gold pack gauges appear once a pack is built (and are absent before)."""
    from datetime import UTC, datetime

    from agora_kb.core.gold import build_gold
    from agora_kb.faces.web.metrics import render_latest

    repo = _init_repo(tmp_path)
    # Before any build: no gold gauges (present=False → omitted).
    assert "agora_gold_pack_note_count" not in render_latest(repo)[0].decode("utf-8")
    build_gold(repo, generated_at=datetime.now(UTC))
    text = render_latest(repo)[0].decode("utf-8")
    assert 'agora_gold_pack_note_count{pack="default"}' in text
    assert 'agora_gold_pack_est_tokens{pack="default"}' in text
    assert 'agora_gold_pack_harvest_derived_share{pack="default"}' in text
    assert 'agora_gold_pack_age_seconds{pack="default"}' in text
    assert 'agora_gold_pack_generated_timestamp_seconds{pack="default"}' in text
