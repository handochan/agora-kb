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


def _write_schema2_taxonomy(tmp_path: Path, domains: str = "[ai-tech]") -> None:
    """Declare KB wiki schema 2 (ADR-0041) — what ``resolve_schema_version`` reads.

    Without it the same bytes are read under the v1 derivation, where ``kind`` comes from ``type:``
    and the subject from the path; every ``kind`` in a kind-first tree would come back ``None``.
    """
    (tmp_path / "_meta").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_meta" / "taxonomy.yaml").write_text(
        f"schema_version: 2\ndomains: {domains}\nallowed_tags: []\n", encoding="utf-8"
    )


def _write_corpus(tmp_path: Path) -> None:
    """A schema-2 corpus mixing statuses, a journal, a people note, and overlapping tags."""
    _write_schema2_taxonomy(tmp_path)
    (tmp_path / "index.md").write_text(
        "---\nkind: index\nstatus: active\n---\n# personal\n", encoding="utf-8"
    )
    concepts = tmp_path / "wiki" / "concepts"
    concepts.mkdir(parents=True, exist_ok=True)
    (concepts / "curator-concurrency.md").write_text(
        "---\nkind: concept\nsubjects: [ai-tech]\nstatus: active\n"
        "tags: [single-writer, concurrency]\n"
        "title: Curator Concurrency\n---\n# Curator Concurrency\n\nbody\n",
        encoding="utf-8",
    )
    (concepts / "inbox-design.md").write_text(
        "---\nkind: concept\nsubjects: [ai-tech]\nstatus: stub\ntags: [single-writer]\n"
        "---\n# Inbox\n\nbody\n",
        encoding="utf-8",
    )
    (concepts / "contested-thing.md").write_text(
        "---\nkind: concept\nsubjects: [ai-tech]\nstatus: contested\ntags: [concurrency]\n"
        "---\n# C\n\nbody\n",
        encoding="utf-8",
    )
    journal = tmp_path / "wiki" / "notes" / "2026" / "06"
    journal.mkdir(parents=True, exist_ok=True)
    (journal / "2026-06-21.md").write_text(
        "---\nkind: note\nstatus: active\ntags: []\n---\n# Journal\n\nbody\n",
        encoding="utf-8",
    )
    people = tmp_path / "wiki" / "people" / "hando"
    people.mkdir(parents=True, exist_ok=True)
    (people / "desk.md").write_text(
        "---\nstatus: active\ntags: []\n---\n# Desk\n\nA human's own file.\n",
        encoding="utf-8",
    )


# --- handler aggregations (transport-free) ------------------------------------------------------
def test_health_kind_census_counts_off_layout_notes_as_unknown(tmp_path: Path) -> None:
    """The one anomaly the closed directory vocabulary exists to catch must be VISIBLE.

    A schema-2 note under an unknown ``wiki/<dir>/`` or directly under ``wiki/`` derives no kind
    (the L1-22 population), and a census seeded only from the closed vocabulary silently dropped
    it — so ``sum(by_kind.values()) < note_total`` with nothing on the panel saying so, and the
    off-layout note was invisible next to ``unmanaged_notes``, its sibling anomaly signal.
    """
    repo = _init_repo(tmp_path)
    _write_schema2_taxonomy(tmp_path)
    (tmp_path / "index.md").write_text(
        "---\nkind: index\nstatus: active\n---\n# personal\n", encoding="utf-8"
    )
    concepts = tmp_path / "wiki" / "concepts"
    concepts.mkdir(parents=True, exist_ok=True)
    (concepts / "a.md").write_text(
        "---\nkind: concept\nsubjects: [ai-tech]\nstatus: active\n---\n# A\n\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "wiki" / "general").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "general" / "off.md").write_text(
        "# No frontmatter here\n\nbody.\n", encoding="utf-8"
    )
    (tmp_path / "wiki" / "loose.md").write_text(
        "---\nstatus: active\n---\n# Loose\n\nbody.\n", encoding="utf-8"
    )
    health = AgoraHandlers(repo).health()

    assert health["note_total"] == 4
    assert health["by_kind"]["unknown"] == 2
    assert sum(health["by_kind"].values()) == health["note_total"]


def test_health_counts_status_split_and_tags(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_corpus(tmp_path)
    health = AgoraHandlers(repo).health()

    # index + 3 concepts + 1 journal + 1 people note = 6 notes; the split is by KIND now, derived
    # from the directory (ADR-0041 D2.1) rather than from a `type:` the model could mistype.
    assert health["note_total"] == 6
    assert health["concepts"] == 3
    assert health["journals"] == 1
    # The census covers the whole closed kind vocabulary, so the tiers that ship EMPTY (summary /
    # entity, OD-7/OD-8) are visibly zero rather than missing.
    assert health["by_kind"] == {
        "concept": 3,
        "entity": 0,
        "index": 1,
        "map": 0,
        "note": 1,
        "person": 1,
        "summary": 0,
        "unknown": 0,
    }
    # And the census is TOTAL: every note lands in exactly one bucket, so the panel can never
    # under-report next to the "Notes" figure printed beside it.
    assert sum(health["by_kind"].values()) == health["note_total"]
    # `wiki/people/**` is read and counted as a note, but it is NOT an unmanaged-note anomaly: it
    # is human-owned BY DESIGN (D3.3). The three concepts + the journal + index have no curator
    # stamp in this hand-written fixture, so they are the four... five that DO count.
    assert health["unmanaged_notes"] == 5
    # status split over the frozen vocabulary; contested mirrors by_status (index + concurrency +
    # journal + the people note are active; inbox-design is stub; contested-thing is contested).
    assert health["by_status"] == {"active": 4, "stub": 1, "contested": 1, "deprecated": 0}
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
    """A body link to a missing note is a ``broken_links`` signal (L1-2 via lint() verbatim).

    Deliberately left on the SCHEMA-1 layout: ADR-0041 D6 keeps schema-1 repos READABLE by this
    build (only writes refuse), so at least one health assertion has to run against a v1 tree.
    """
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
    """``orphans`` counts claim-bearing notes nothing links TO (read-time L2-1), not broken links.

    A concept is referenced either by another note's BODY markdown link or by a frontmatter
    ``related:``/``children:`` ``[[ ]]`` — both inbound directions must keep it off the orphan
    list; only a note referenced by neither is an orphan (journals / maps / index roots, entities
    and people are exempt). ``wiki/people/**`` links are UNGRADED (ADR-0041 D3.3), so a human's
    link does NOT rescue a concept from the count — that is the one clause the dashboard shares
    with ``schema.lint``'s L2-1, and it is asserted here rather than assumed.
    """
    _init_repo(tmp_path)
    _write_schema2_taxonomy(tmp_path)
    # index body-links 'linked-body'; 'linked-body' frontmatter-relates to 'linked-fm'; 'lonely' is
    # referenced by nobody; 'human-only' is referenced ONLY from a people note. No dangling links
    # anywhere → broken_links must stay 0 (a genuinely distinct signal).
    (tmp_path / "index.md").write_text(
        "---\nkind: index\nstatus: active\n---\n# personal\n\n"
        "- [Linked](wiki/concepts/linked-body.md)\n",
        encoding="utf-8",
    )
    concepts = tmp_path / "wiki" / "concepts"
    concepts.mkdir(parents=True, exist_ok=True)
    (concepts / "linked-body.md").write_text(
        "---\nkind: concept\nstatus: active\ntags: []\nrelated: ['[[linked-fm]]']\n---\n"
        "# LB\n\nbody\n",
        encoding="utf-8",
    )
    (concepts / "linked-fm.md").write_text(
        "---\nkind: concept\nstatus: active\ntags: []\n---\n# LF\n\nbody\n", encoding="utf-8"
    )
    (concepts / "lonely.md").write_text(
        "---\nkind: concept\nstatus: active\ntags: []\n---\n# Lonely\n\nbody\n",
        encoding="utf-8",
    )
    (concepts / "human-only.md").write_text(
        "---\nkind: concept\nstatus: active\ntags: []\n---\n# HO\n\nbody\n", encoding="utf-8"
    )
    people = tmp_path / "wiki" / "people" / "hando"
    people.mkdir(parents=True, exist_ok=True)
    (people / "desk.md").write_text(
        "---\nstatus: active\n---\n# Desk\n\nSee [HO](../../concepts/human-only.md).\n",
        encoding="utf-8",
    )
    health = AgoraHandlers(Repo.resolve(tmp_path)).health()
    # 'lonely' (referenced by nobody) + 'human-only' (referenced only from an ungraded people
    # note); linked-body (body link) and linked-fm (frontmatter) are both referenced.
    assert health["orphans"] == 2
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


def test_harvester_status_reports_the_session_connector_format(tmp_path: Path) -> None:
    """The dashboard/MCP status face answers "which grammar is reading my transcripts" too (#147).

    `agora doctor` grew that line; the two OTHER human-facing surfaces read this dict, so without
    the key they could not answer it. Each kind-specific field is `None` on the other kind rather
    than a default that cannot take effect — the same rule `load_connector_specs` enforces when it
    rejects `format:` on a `file:` connector.
    """
    repo = _init_repo(tmp_path)
    repo.layout.kb_dir.mkdir(parents=True, exist_ok=True)
    (repo.layout.kb_dir / "repo.yaml").write_text(
        "name: personal\nkind: personal\nharvest:\n  enabled: true\n", encoding="utf-8"
    )
    (tmp_path / "adapters.yaml").write_text(
        "backends:\n  qwen:\n    argv: [agora-ollama-brain]\n"
        "default_backend: qwen\n"
        "connectors:\n"
        '  file:claude-code: { path: "~/.claude/MEMORY.md", scope: personal }\n'
        '  session:default: { path: "~/.claude/**/*.jsonl", scope: personal }\n'
        '  session:pinned: { path: "~/x/**/*.jsonl", scope: personal,'
        " format: claude-code-jsonl }\n",
        encoding="utf-8",
    )

    by_name = {c["name"]: c for c in AgoraHandlers(repo).harvester_status()["connectors"]}

    assert by_name["file:claude-code"]["format"] is None
    assert by_name["file:claude-code"]["follow_links"] is False
    # An undeclared format reports the EFFECTIVE default, not a blank.
    assert by_name["session:default"]["format"] == "claude-code-jsonl"
    assert by_name["session:pinned"]["format"] == "claude-code-jsonl"
    assert by_name["session:default"]["follow_links"] is None


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

    return TestClient(
        build_app(repo_path=tmp_path, writer="web", user="alice"), base_url="http://127.0.0.1"
    )


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
    assert hjson["note_total"] == 6
    assert set(hjson) >= {
        "by_kind",
        "by_status",
        "tag_distribution",
        "lint_ok",
        "contested",
        "orphans",
        "unmanaged_notes",
    }

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
    # The kind census and the #152 unmanaged-note count are RENDERED, not merely in the JSON —
    # an operator reads the panel, not /api/dashboard/health.
    assert "By kind" in health.text
    assert "Unmanaged" in health.text

    curator = client.get("/dashboard/curator")
    assert curator.status_code == 200
    assert "<!DOCTYPE html>" not in curator.text
    assert "Work log" in curator.text

    harvester = client.get("/dashboard/harvester")
    assert harvester.status_code == 200
    assert "<!DOCTYPE html>" not in harvester.text
    assert "Harvesting is" in harvester.text


# --- gold panel (ADR-0027, issue #37) -----------------------------------------------------------
def _build_gold(repo: Repo) -> None:
    from datetime import UTC, datetime

    from agora_kb.core.gold import build_gold

    build_gold(repo, generated_at=datetime.now(UTC))


def test_gold_status_absent_before_build(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    panel = AgoraHandlers(repo).gold_status()
    assert panel["gold"] == {"present": False, "pack": "default"}
    assert "bronze" in panel
    # kb_status carries the cheap meta-only gold row (present=False, no git).
    assert AgoraHandlers(repo).status()["gold"] == {"pack": "default", "present": False}


def test_gold_status_present_and_fresh_after_build(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_corpus(tmp_path)
    _build_gold(repo)
    panel = AgoraHandlers(repo).gold_status()
    gold = panel["gold"]
    assert gold["present"] is True
    assert gold["fresh"] is True  # meta.curated_sha == the (init) curated tip
    assert gold["note_count"] >= 1
    assert gold["budget_tokens"] == 2000
    assert gold["harvest_derived_share"] == 0.0
    # kb_status gold row is present + meta-only (no freshness key, since it does no git).
    row = AgoraHandlers(repo).status()["gold"]
    assert row["present"] is True and "fresh" not in row


def test_api_dashboard_gold_route(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_corpus(tmp_path)
    _build_gold(repo)
    resp = _client(tmp_path).get("/api/dashboard/gold")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gold"]["present"] is True


def test_dashboard_page_includes_gold_panel(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_corpus(tmp_path)
    _build_gold(repo)
    body = _client(tmp_path).get("/dashboard").text
    assert "Gold context pack" in body
    assert 'hx-get="/dashboard/gold"' in body
    # the fragment route renders the pack detail
    frag = _client(tmp_path).get("/dashboard/gold").text
    assert "fresh" in frag or "stale" in frag or "No gold pack" in frag
