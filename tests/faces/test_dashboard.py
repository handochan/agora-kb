"""Tests for the Phase-3c read-only dashboard (DESIGN §5.3 / ADR-0003 / ADR-0019).

Two surfaces over one shared meta seam: the transport-free :class:`AgoraHandlers` aggregations
(``health`` / ``curator_status`` / ``harvester_status``) and the web face's ``/dashboard`` page +
``/api/dashboard/*`` JSON + the polled HTMX fragment routes. The dashboard is READ-ONLY: it adds no
write path and reuses the deterministic ``lint()`` verbatim, so its KB-health signals are the same
code path the curator runs. All web tests skip cleanly when fastapi is absent (``importorskip``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.core import Repo
from agora_kb.faces.mcp_server import AgoraHandlers


# --- fixtures -----------------------------------------------------------------------------------
def _init_repo(tmp_path: Path) -> Repo:
    repo = Repo.resolve(tmp_path)
    repo.init()
    return repo


def _write_corpus(tmp_path: Path) -> None:
    """A small corpus mixing statuses, a daily, and tag overlap for distribution/count asserts."""
    (tmp_path / "index.md").write_text(
        "---\ntype: index\nstatus: active\n---\n# personal\n", encoding="utf-8"
    )
    themes = tmp_path / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "curator-concurrency.md").write_text(
        "---\ntype: theme\nstatus: active\ntags: [single-writer, concurrency]\n"
        "title: Curator Concurrency\n---\n# Curator Concurrency\n\nbody\n",
        encoding="utf-8",
    )
    (themes / "inbox-design.md").write_text(
        "---\ntype: theme\nstatus: stub\ntags: [single-writer]\n---\n# Inbox\n\nbody\n",
        encoding="utf-8",
    )
    (themes / "contested-thing.md").write_text(
        "---\ntype: theme\nstatus: contested\ntags: [concurrency]\n---\n# C\n\nbody\n",
        encoding="utf-8",
    )
    daily = tmp_path / "wiki" / "ai-tech" / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    (daily / "ai-tech-2026-06-21.md").write_text(
        "---\ntype: daily\nstatus: active\ntags: []\n---\n# Daily\n\nbody\n",
        encoding="utf-8",
    )


# --- handler aggregations (transport-free) ------------------------------------------------------
def test_health_counts_status_split_and_tags(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_corpus(tmp_path)
    health = AgoraHandlers(repo).health()

    # index + 3 themes + 1 daily = 5 notes; themes vs daily split is by the `type` field.
    assert health["note_total"] == 5
    assert health["themes"] == 3
    assert health["dailies"] == 1
    # status split over the frozen vocabulary; contested mirrors by_status (index + concurrency +
    # daily are active; inbox-design is stub; contested-thing is contested).
    assert health["by_status"] == {"active": 3, "stub": 1, "contested": 1, "deprecated": 0}
    assert health["contested"] == 1
    # tag distribution is a frequency map.
    assert health["tag_distribution"]["single-writer"] == 2
    assert health["tag_distribution"]["concurrency"] == 2
    # lint() is reused verbatim — its signals are surfaced (boolean + finding count + orphan count).
    assert isinstance(health["lint_ok"], bool)
    assert isinstance(health["lint_findings"], int)
    assert isinstance(health["orphans"], int)
    assert "last_consolidation" in health


def test_health_broken_links_from_lint(tmp_path: Path) -> None:
    """A body link to a missing note is a ``broken_links`` signal (L1-2 via lint() verbatim)."""
    _init_repo(tmp_path)
    (tmp_path / "index.md").write_text(
        "---\ntype: index\nstatus: active\n---\n# personal\n", encoding="utf-8"
    )
    themes = tmp_path / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "t.md").write_text(
        "---\ntype: theme\nstatus: active\ntags: []\n---\n"
        "# T\n\nSee [Ghost](nonexistent-note.md).\n",
        encoding="utf-8",
    )
    health = AgoraHandlers(Repo.resolve(tmp_path)).health()
    assert health["broken_links"] >= 1  # the dangling OUTBOUND link
    assert health["lint_ok"] is False


def test_health_orphans_derivation(tmp_path: Path) -> None:
    """``orphans`` counts themes nothing links TO (read-time L2-1), NOT broken outbound links.

    A theme is referenced either by another note's BODY markdown link or by a frontmatter
    ``related:``/``children:`` ``[[ ]]`` — both inbound directions must keep a theme off the orphan
    list; only a theme referenced by neither is an orphan (dailies / MOC / index roots are exempt).
    """
    _init_repo(tmp_path)
    # index body-links 'linked-body'; 'linked-body' frontmatter-relates to 'linked-fm'; 'lonely' is
    # referenced by nobody. No dangling links anywhere → broken_links must stay 0 (distinct signal).
    (tmp_path / "index.md").write_text(
        "---\ntype: index\nstatus: active\n---\n# personal\n\n"
        "- [Linked](wiki/ai-tech/themes/linked-body.md)\n",
        encoding="utf-8",
    )
    themes = tmp_path / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "linked-body.md").write_text(
        "---\ntype: theme\nstatus: active\ntags: []\nrelated: ['[[linked-fm]]']\n---\n"
        "# LB\n\nbody\n",
        encoding="utf-8",
    )
    (themes / "linked-fm.md").write_text(
        "---\ntype: theme\nstatus: active\ntags: []\n---\n# LF\n\nbody\n", encoding="utf-8"
    )
    (themes / "lonely.md").write_text(
        "---\ntype: theme\nstatus: active\ntags: []\n---\n# Lonely\n\nbody\n", encoding="utf-8"
    )
    health = AgoraHandlers(Repo.resolve(tmp_path)).health()
    assert (
        health["orphans"] == 1
    )  # only 'lonely' — linked-body (body) + linked-fm (frontmatter) ref'd
    assert health["broken_links"] == 0  # genuinely distinct from the orphan signal


def test_curator_status_active_backend_and_log(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    # Wire a default adapters.yaml (the OSS `qwen` brain) → it is the resolved active author.
    from agora_kb.config import write_default_adapters_yaml

    write_default_adapters_yaml(repo.layout)
    # Plant a log.md in the exact worker._append_log shape and assert the tail parses it.
    repo.layout.log_file.write_text(
        "# Curator log\n\n"
        "## run-2026-06-21T00:00:00Z--aaa\n"
        "- base: `deadbeef`\n"
        "- dispositions: CREATE_THEME=2, DROP=1\n"
        "- dropped: cand-1\n\n"
        "## run-2026-06-21T01:00:00Z--bbb\n"
        "- base: `cafef00d`\n"
        "- dispositions: no-op\n",
        encoding="utf-8",
    )
    status = AgoraHandlers(repo).curator_status()

    assert status["active_backend"] == "qwen"
    assert status["inbox_depth"] == 0
    assert set(status["counters"]) == {"ingested", "merged", "dropped", "failed"}

    log = status["recent_log"]
    assert isinstance(log, list) and len(log) == 2
    # Newest first.
    assert log[0]["run_id"] == "run-2026-06-21T01:00:00Z--bbb"
    assert log[0]["ops"] == {}  # no-op parses to empty ops
    assert log[1]["run_id"] == "run-2026-06-21T00:00:00Z--aaa"
    assert log[1]["base"] == "deadbeef"
    assert log[1]["ops"] == {"CREATE_THEME": 2, "DROP": 1}
    assert log[1]["dropped"] == ["cand-1"]


def test_curator_status_no_backend_when_adapters_absent(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)  # Repo.init() emits no adapters.yaml → no backend configured
    assert not (tmp_path / "adapters.yaml").exists()
    status = AgoraHandlers(repo).curator_status()
    assert status["active_backend"] is None
    assert status["recent_log"] == []  # no log.md yet


def test_curator_status_active_backend_routing_author_wins(tmp_path: Path) -> None:
    """ADR-0015 per-act routing: the AUTHOR act's brain is the active backend, over default_backend.

    The dashboard names the brain that actually materializes wiki prose (the AUTHOR act), so an
    ``adapters.yaml`` routing author away from the default must win — mirroring ``agora doctor``.
    """
    repo = _init_repo(tmp_path)
    (tmp_path / "adapters.yaml").write_text(
        "backends:\n"
        "  qwen:\n    argv: [agora-ollama-brain]\n"
        "  hermes:\n    argv: [hermes, chat]\n"
        "default_backend: qwen\n"
        "routing:\n  plan: qwen\n  author: hermes\n",
        encoding="utf-8",
    )
    status = AgoraHandlers(repo).curator_status()
    assert status["active_backend"] == "hermes"  # author-routed brain, NOT the qwen default


def test_recent_log_parses_contested_and_pending_body(tmp_path: Path) -> None:
    """The ``- contested:`` / ``- pending-body:`` log.md branches parse (worker._append_log emits
    both); covers the branches the happy-path log test leaves unexercised."""
    repo = _init_repo(tmp_path)
    repo.layout.log_file.write_text(
        "# Curator log\n\n"
        "## run-2026-06-21T03:00:00Z--ccc\n"
        "- base: `f00d`\n"
        "- dispositions: MARK_CONTESTED=1\n"
        "- contested: theme-a, theme-b\n"
        "- pending-body: cand-9\n",
        encoding="utf-8",
    )
    entry = AgoraHandlers(repo).curator_status()["recent_log"][0]
    assert entry["ops"] == {"MARK_CONTESTED": 1}
    assert entry["contested"] == ["theme-a", "theme-b"]
    assert entry["pending_body"] == ["cand-9"]


def test_harvester_status_from_planted_cursor(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    # Enable harvest + declare a connector in the two config surfaces it reads.
    from agora_kb.harvester import CursorStore, HarvestCursor

    repo.layout.kb_dir.mkdir(parents=True, exist_ok=True)
    (repo.layout.kb_dir / "repo.yaml").write_text(
        "name: personal\nkind: personal\nharvest:\n  enabled: true\n  scope_lock: personal\n",
        encoding="utf-8",
    )
    (tmp_path / "adapters.yaml").write_text(
        "backends:\n  qwen:\n    argv: [agora-ollama-brain]\n"
        "default_backend: qwen\n"
        "connectors:\n"
        "  file:claude-code:\n"
        '    path: "~/.claude/MEMORY.md"\n'
        "    scope: personal\n"
        "    follow_links: true\n",
        encoding="utf-8",
    )
    # Plant a cursor showing proposed>0; accepted/rejected stay 0 (curator-owned, deferred).
    from datetime import UTC, datetime

    CursorStore(repo.layout).save(
        HarvestCursor(
            connector="file:claude-code",
            source_path="/home/x/.claude/MEMORY.md",
            last_scan=datetime(2026, 6, 21, 2, 0, 0, tzinfo=UTC),
            proposed=7,
        )
    )

    h = AgoraHandlers(repo).harvester_status()
    assert h["enabled"] is True
    assert len(h["connectors"]) == 1
    conn = h["connectors"][0]
    assert conn["name"] == "file:claude-code"
    assert conn["scope"] == "personal"
    assert conn["follow_links"] is True
    assert conn["proposed"] == 7
    # Deferred curator-owned counters render as-is (0), never faked.
    assert conn["accepted"] == 0
    assert conn["rejected"] == 0
    assert conn["last_scan"] == "2026-06-21T02:00:00Z"


def test_harvester_status_disabled_default(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    h = AgoraHandlers(repo).harvester_status()
    assert h["enabled"] is False  # opt-in default
    assert h["connectors"] == []  # the default adapters.yaml emits connectors commented-out


# --- web routes over TestClient -----------------------------------------------------------------
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _client(tmp_path: Path) -> TestClient:
    from agora_kb.faces.web import build_app

    return TestClient(build_app(repo_path=tmp_path, writer="web", user="alice"))


def test_dashboard_page_renders_three_panels(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_corpus(tmp_path)
    resp = _client(tmp_path).get("/dashboard")
    assert resp.status_code == 200
    body = resp.text
    assert "KB health" in body
    assert "Curator status" in body
    assert "Harvester status" in body
    # The polled panels carry their hx-get + 5s trigger; the heavy health panel is load-only.
    assert 'hx-get="/dashboard/curator"' in body
    assert 'hx-get="/dashboard/harvester"' in body
    assert "every 5s" in body
    assert 'hx-get="/dashboard/health"' in body
    # Linked from the nav.
    assert 'href="/dashboard"' in body


def test_api_dashboard_endpoints(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_corpus(tmp_path)
    client = _client(tmp_path)

    health = client.get("/api/dashboard/health")
    assert health.status_code == 200
    hjson = health.json()
    assert hjson["note_total"] == 5
    assert set(hjson) >= {"by_status", "tag_distribution", "lint_ok", "contested", "orphans"}

    curator = client.get("/api/dashboard/curator")
    assert curator.status_code == 200
    assert set(curator.json()) >= {"inbox_depth", "counters", "active_backend", "recent_log"}

    harvester = client.get("/api/dashboard/harvester")
    assert harvester.status_code == 200
    assert set(harvester.json()) >= {"enabled", "connectors"}


def test_polled_fragment_routes_return_partials(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_corpus(tmp_path)
    client = _client(tmp_path)

    # Fragments are bare partials (no <html> wrapper), suitable for hx-swap into a #panel target.
    health = client.get("/dashboard/health")
    assert health.status_code == 200
    assert "<!DOCTYPE html>" not in health.text
    assert "Tag distribution" in health.text

    curator = client.get("/dashboard/curator")
    assert curator.status_code == 200
    assert "<!DOCTYPE html>" not in curator.text
    assert "Work log" in curator.text

    harvester = client.get("/dashboard/harvester")
    assert harvester.status_code == 200
    assert "<!DOCTYPE html>" not in harvester.text
    assert "Harvesting is" in harvester.text
