"""Tests for the MCP face (faces.mcp_server, DESIGN §5.1).

The cognitive logic lives in transport-free :class:`AgoraHandlers`, so these tests drive it directly
over a real on-disk repo (``tmp_path``) — no live MCP transport, no server I/O. A single smoke test
exercises :func:`build_server` to confirm the 7 tools are registered (the #58 read-side companions
``kb_read`` / ``kb_neighbors`` are exercised end-to-end in ``test_mcp_read_tools.py``; the #40 gold
channels — ``kb_context`` + resource + prompt — in ``test_gold_consumption.py``).
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastmcp import Client

from agora_kb.config import KbIdentity, ReadOnlySchemaVersionError, write_kb_identity
from agora_kb.core import Inbox, Repo, failed_event_count
from agora_kb.core.wiki import Wiki
from agora_kb.faces.mcp_server import AgoraHandlers, build_server
from agora_kb.schema import Taxonomy, emit_schema

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

# A model-free stub curator brain shelled by SubprocessBackend: PASS 1 (cwd = bundle dir) emits a
# CREATE_THEME plan to stdout; PASS 2 (cwd = worktree) fills the agora:body sentinels with prose.
_STUB_BRAIN = """\
import json, re, sys
from pathlib import Path

cwd = Path.cwd()
candidates = cwd / "candidates.json"
START = re.compile(r"<!-- agora:body:start id=(.+?) -->")

if candidates.is_file():
    doc = json.loads(candidates.read_text())
    c0 = doc["candidates"][0]
    disp = {
        "candidate_id": c0["candidate_id"],
        "event_ids": [p["event_id"] for p in c0["provenance"]],
        "op": "CREATE_THEME",
        "domain": c0.get("domain") or "ai-tech",
        "basename": "curator-concurrency",
        "title": "Curator concurrency model",
        "summary": "One curator advances the curated branch under a per-repo lock.",
        "status": "active",
        "tags": ["curator", "concurrency"],
        "aliases": [],
        "links": [],
        "needs_prose": True,
        "reason": "New concept.",
    }
    print(json.dumps({"schema_version": 1, "run_id": doc["run_id"], "finished": True,
                      "dispositions": [disp]}))
    sys.exit(0)

for note in (cwd / "wiki").rglob("*.md"):
    text = note.read_text()
    out, in_region = [], False
    for line in text.split("\\n"):
        if START.search(line):
            out.append(line)
            out.append("The single curator holds a per-repo flock.")
            in_region = True
            continue
        if "agora:body:end" in line:
            in_region = False
            out.append(line)
            continue
        if in_region:
            continue
        out.append(line)
    note.write_text("\\n".join(out))
sys.exit(0)
"""

# A brain that cannot run at all — the shape of an absent/dead Ollama daemon (#96). A non-zero
# PASS 1 makes SubprocessBackend.plan raise BackendUnavailableError, so worker._fail discards the
# run with a `PLAN-BACKEND:` reason and a durable `_kb/failed/**/error.json` record.
_DEAD_BRAIN = 'import sys\nsys.stderr.write("ollama: connection refused\\n")\nsys.exit(1)\n'

#: The fixture `_meta/kb.yaml` identity (ADR-0041 D1.5) — APPLY refuses to write without one.
_KB_ID = "01J8ZQ3M4N5P6Q7R8S9T0V1W2X"

_TAXONOMY = Taxonomy(
    schema_version=2,
    taxonomy_policy="open",
    allowed_tags=("curator", "concurrency"),
    domains=("ai-tech",),
)


# --- fixture helpers ----------------------------------------------------------------------------
def _init_repo(tmp_path: Path) -> Repo:
    """Create + initialize a knowledge repo and return it."""
    repo = Repo.resolve(tmp_path)
    repo.init()
    return repo


def _init_curatable_repo(tmp_path: Path) -> Repo:
    """Init a repo with the schema/taxonomy committed + a stub adapters.yaml, ready to curate.

    Mirrors `agora repo init` (schema + taxonomy committed at the curated tip so the bundle/lint
    inputs exist) plus a stub-brain adapters.yaml the SubprocessBackend shells. No real model.
    """
    repo = Repo.resolve(tmp_path)
    write_kb_identity(repo.layout, KbIdentity(kb_id=_KB_ID, name="agora-fixture"))
    for name in ("concepts", "summaries", "notes", "maps", "entities", "people"):
        (repo.layout.wiki_dir / name).mkdir(parents=True, exist_ok=True)
        (repo.layout.wiki_dir / name / ".gitkeep").write_text("", encoding="utf-8")
    repo.init(when=datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC), schema_version=2, kb_id=_KB_ID)
    emit_schema(repo.layout, taxonomy=_TAXONOMY, schema_version=2)
    repo.commit_all("chore: emit schema", when=datetime(2026, 6, 12, 1, 0, 0, tzinfo=UTC))

    brain = tmp_path / "stub_brain.py"
    brain.write_text(_STUB_BRAIN, encoding="utf-8")
    (tmp_path / "adapters.yaml").write_text(
        "backends:\n"
        f"  stub: {{ argv: [{sys.executable!r}, {str(brain)!r}], "
        'cwd: "{worktree}", prompt: stdin }\n'
        "default_backend: stub\n",
        encoding="utf-8",
    )
    # repo.yaml carries the matching taxonomy domain + the stub as the default brain.
    (repo.layout.kb_dir).mkdir(parents=True, exist_ok=True)
    (repo.layout.kb_dir / "repo.yaml").write_text(
        "name: personal\nkind: personal\nschema_version: 2\ndomains: [ai-tech]\n"
        "curator:\n  backend: stub\n",
        encoding="utf-8",
    )
    return repo


def _write_wiki_notes(tmp_path: Path) -> None:
    """Place a small navigable corpus on disk (index + MOC + two themes) under ``wiki/``."""
    # ADR-0014 D3: the produced MOC/index BODY child bullets are standard markdown links.
    (tmp_path / "index.md").write_text(
        "# personal\n\n- [AI Tech MOC](wiki/ai-tech/ai-tech-moc.md)\n", encoding="utf-8"
    )
    domain = tmp_path / "wiki" / "ai-tech"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "ai-tech-moc.md").write_text(
        "---\nstatus: active\n---\n# AI Tech\n\n"
        "- [Curator concurrency](themes/curator-concurrency.md) — single-writer curator\n"
        "- [Inbox design](themes/inbox-design.md) — append-only inbox\n",
        encoding="utf-8",
    )
    (domain / "curator-concurrency.md").write_text(
        "---\nstatus: active\ntags: [single-writer, concurrency]\n---\n"
        "# Curator Concurrency\n\n"
        "The curator acquires a per-repo flock so exactly one writer advances the branch.\n",
        encoding="utf-8",
    )
    (domain / "inbox-design.md").write_text(
        "---\nstatus: active\ntags: [inbox, append-only]\n---\n"
        "# Inbox Design\n\nThe inbox is append-only and per-writer namespaced.\n",
        encoding="utf-8",
    )


# --- remember (write path) ----------------------------------------------------------------------
def test_remember_writes_inbox_event_and_returns_receipt(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.remember("compare-and-swap publishes the curated branch")

    assert isinstance(result, dict)
    assert result["queued"] is True
    assert isinstance(result["id"], str) and result["id"]
    assert result["inbox_depth"] == 1

    # The event landed in this writer's inbox namespace on disk.
    writer_dir = repo.layout.inbox_writer_dir("local")
    events = list(writer_dir.glob("*.md"))
    assert len(events) == 1
    assert events[0].stem == result["id"]


def test_remember_uses_server_writer_and_passes_metadata(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="agent-7")

    handlers.remember(
        "kebab tags are validated by the core model",
        target="personal",
        domain="ai-tech",
        tags=["single-writer", "concurrency"],
        source="claude-code",
    )

    # The capture lands in the *server-configured* writer namespace, not a caller-chosen one.
    assert list(repo.layout.inbox_writer_dir("agent-7").glob("*.md"))
    assert not repo.layout.inbox_writer_dir("local").exists()


# --- query (read path) --------------------------------------------------------------------------
def test_query_returns_hits_for_known_topic(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.query("curator concurrency compare and swap")

    assert result["status"] == "ok"
    hits = result["hits"]
    assert isinstance(hits, list) and hits
    first = hits[0]
    # Every documented citation field is present and JSON-serializable.
    assert set(first) == {
        "repo",
        "path",
        "anchor",
        "line",
        "excerpt",
        "match_reason",
        "score",
    }
    assert any("curator-concurrency" in h["path"] for h in hits)
    assert first["match_reason"] in {"linked-theme", "heading", "lexical"}


def test_query_not_found_for_nonsense(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.query("zzzqqq nonexistent gibberish token")

    assert result["status"] == "not_found"
    assert result["hits"] == []
    assert result["query"] == "zzzqqq nonexistent gibberish token"


# --- status (meta) ------------------------------------------------------------------------------
def test_status_fresh_repo_reports_depth_and_nulls(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    result = handlers.status()

    assert result["inbox_depth"] == 0
    assert result["last_consolidation"] is None
    assert result["processed_today"] == 0
    assert result["last_commit"] is None
    assert result["counters"] == {"ingested": 0, "merged": 0, "dropped": 0, "failed": 0}
    assert result["failed"] == 0


def test_status_reflects_pending_and_failed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")
    handlers.remember("one pending capture")
    handlers.remember("another pending capture")

    # Simulate a terminal failure laid out the way worker._fail actually writes it:
    # failed/<date>/<run-id>/<event>.md (the terminal event) + error.json (the §5.1 retry record).
    # The count must track this NESTED layout, not a fabricated top-level file (the regression the
    # buggy `glob("*.md")` masked).
    run_dir = repo.layout.failed_dir / "2026-06-13" / "20260613T000000Z-deadbeef"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "20260613T024010Z-a1b2c3.md").write_text("oops\n", encoding="utf-8")
    (run_dir / "error.json").write_text('{"failed_checks": []}\n', encoding="utf-8")

    result = handlers.status()
    assert result["inbox_depth"] == 2
    # One terminal event under the nested run dir; error.json is NOT counted (it is not *.md).
    assert result["failed"] == 1


def test_kb_status_failed_delegates_to_the_shared_helper(tmp_path: Path) -> None:
    """`kb_status.failed` IS ``core.failed_event_count`` — #96 crit 8, half 2 (the SAME helper).

    The CLI prints the same number as ``agora status``'s ``failed_events``. The criterion is that
    the two agree BY CONSTRUCTION — one function — not because two hand-written recursive globs
    happen to match today and drift tomorrow.
    """
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")
    run_dir = repo.layout.failed_dir / "2026-06-13" / "20260613T000000Z-deadbeef"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "20260613T024010Z-a1b2c3.md").write_text("oops\n", encoding="utf-8")
    (run_dir / "20260613T024011Z-d4e5f6.md").write_text("oops\n", encoding="utf-8")
    (run_dir / "error.json").write_text('{"failed_checks": []}\n', encoding="utf-8")

    assert handlers.status()["failed"] == failed_event_count(repo.layout) == 2


def test_kb_status_failed_is_not_a_second_copy_of_the_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of "same helper": value equality alone would survive a re-inlined copy.

    Pinning the shared function to an impossible number proves the face DELEGATES rather than
    re-implementing the recursive glob — which is what #96 crit 8 actually asks for.
    """
    import agora_kb.faces.mcp_server as mcp_mod

    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")
    monkeypatch.setattr(mcp_mod, "failed_event_count", lambda layout: 99)

    assert handlers.status()["failed"] == 99


def test_status_processed_today_counts_today_partition(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")

    # Two items finalized today + one in a stale (yesterday-shaped) partition that must be ignored.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    today_dir = repo.layout.processed_dir / today
    today_dir.mkdir(parents=True, exist_ok=True)
    (today_dir / "a.md").write_text("done\n", encoding="utf-8")
    (today_dir / "b.md").write_text("done\n", encoding="utf-8")
    stale_dir = repo.layout.processed_dir / "2000-01-01"
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / "old.md").write_text("done\n", encoding="utf-8")

    result = handlers.status()
    assert result["processed_today"] == 2


# --- curate (real consolidation run) ------------------------------------------------------------
def test_curate_no_backend_returns_clear_note(tmp_path: Path) -> None:
    """A repo with NO adapters.yaml reports status='no_backend' (a clear note), never crashes."""
    repo = _init_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="local")
    handlers.remember("a single capture")

    result = handlers.curate()

    assert result["status"] == "no_backend"
    assert result["published_commit"] is None
    assert "no backend configured" in result["note"]


def test_curate_with_invalid_routing_returns_no_backend(tmp_path: Path) -> None:
    """ADR-0015: a malformed ``routing:`` block makes the SILENT MCP face report ``no_backend``
    rather than raising the config ``ValueError`` to the client."""
    repo = _init_repo(tmp_path)
    (repo.layout.root / "adapters.yaml").write_text(
        "backends:\n  qwen: { argv: [agora-ollama-brain], network: loopback }\n"
        "default_backend: qwen\nrouting:\n  plan: ghost\n",
        encoding="utf-8",
    )
    handlers = AgoraHandlers(repo, writer="local")
    handlers.remember("a single capture")

    assert handlers.curate()["status"] == "no_backend"


@requires_git
def test_curate_with_stub_backend_publishes(tmp_path: Path) -> None:
    """`kb_curate` runs curator.worker against a STUB backend and PUBLISHES a theme.

    End-to-end through the MCP handler: a capture is consolidated into a CREATE_THEME by the stub
    brain (PASS 1 plan + PASS 2 prose), the worker commits + CAS-publishes, and the handler returns
    {status: published, published_commit, counts}. The synced working copy then resolves the theme.
    """
    repo = _init_curatable_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="dochan")
    Inbox(repo.layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )

    result = handlers.curate()

    assert result["status"] == "published"
    assert isinstance(result["published_commit"], str) and result["published_commit"]
    assert result["counts"].get("CREATE_THEME") == 1
    # Read-after-publish: the theme is on disk and the deterministic query resolves it.
    theme = repo.layout.wiki_dir / "concepts" / "curator-concurrency.md"
    assert theme.is_file()
    assert Wiki(repo.layout).query("curator concurrency").status == "ok"
    # The BODY oracle (#115). Without it this test passes on a note whose body is still the
    # `_summary pending_` placeholder — which is exactly what the Linux sandbox produced while
    # every other assertion here stayed green. `published` must mean prose, not just structure.
    assert "The single curator holds a per-repo flock." in theme.read_text(encoding="utf-8")
    assert result["counts"].get("prose_pending") == 0
    assert result["warnings"] == []
    # #96: `failure` is set ONLY by worker._fail, so a published run must carry None — the key is
    # additive and must add no noise to the happy path an agent reads on every curate.
    assert result["failure"] is None


def test_curate_empty_inbox_with_backend_is_noop(tmp_path: Path) -> None:
    """With a backend configured but an empty inbox, curator.worker no-ops (nothing to claim)."""
    repo = _init_curatable_repo(tmp_path)
    handlers = AgoraHandlers(repo, writer="dochan")

    result = handlers.curate()

    assert result["status"] == "noop"
    assert result["published_commit"] is None


@requires_git
def test_kb_curate_failed_payload_carries_failure(tmp_path: Path) -> None:
    """A FAILED run's payload says WHY and WHERE, not just ``status: "failed"`` (#96).

    The fatal counterpart to ``warnings``: an agent that saw only the verdict sat in exactly the
    position the human operator was in before #96. ``record_path`` is repo-RELATIVE, so the agent
    resolves it against the repo root it already knows (and it never leaks the host layout,
    invariant 5); ``reasons`` ships in full because each entry is already bounded at construction
    (``worker._bounded_reasons``), so no unbounded brain traceback can reach a context window.
    """
    repo = _init_curatable_repo(tmp_path)
    # A dead brain: PASS 1 exits non-zero, so the run publishes nothing and _fail records it.
    (tmp_path / "stub_brain.py").write_text(_DEAD_BRAIN, encoding="utf-8")
    handlers = AgoraHandlers(repo, writer="dochan")
    Inbox(repo.layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )

    result = handlers.curate()

    assert result["status"] == "failed"
    # The fatal cause rides its OWN key: it must not leak into the non-fatal #115 channel.
    assert result["warnings"] == []
    failure = result["failure"]
    assert failure is not None
    assert isinstance(failure["run_id"], str) and failure["run_id"]
    assert failure["phase"] == "claimed"  # PLAN failed, so nothing was ever applied
    assert failure["cas_conflict"] is False
    assert failure["reasons"] and failure["reasons"][0].startswith("PLAN-BACKEND")
    assert "connection refused" in failure["reasons"][0]  # the brain's own stderr, bounded
    record = failure["record_path"]
    assert record.startswith("_kb/failed/") and record.endswith("/error.json")
    assert (repo.layout.root / record).is_file()  # relative to the repo root the agent knows


# --- live MCP client round-trip (the Exit clause) -----------------------------------------------
@requires_git
def test_mcp_client_roundtrip_capture_curate_query(tmp_path: Path) -> None:
    """A REAL MCP client (``fastmcp.Client``) drives the server over the protocol: capture → curate
    → query returns it with a path citation. This is the ROADMAP Phase-1 Exit clause — "capture from
    any MCP client → curator atomically files it → deterministic query returns it" — proven end to
    end through tool dispatch + serialization, not by calling the handlers directly.
    """
    repo = _init_curatable_repo(tmp_path)
    server = build_server(repo_path=repo.layout.root, writer="agent-1")

    async def _run() -> dict[str, object]:
        async with Client(server) as client:
            remembered = await client.call_tool(
                "kb_remember",
                {"text": "One curator advances the branch under a lock.", "domain": "ai-tech"},
            )
            status_before = await client.call_tool("kb_status", {})
            curated = await client.call_tool("kb_curate", {})
            queried = await client.call_tool("kb_query", {"question": "curator concurrency"})
            return {
                "remembered": remembered.data,
                "status_before": status_before.data,
                "curated": curated.data,
                "queried": queried.data,
            }

    out = asyncio.run(_run())

    assert out["remembered"]["queued"] is True
    assert out["status_before"]["inbox_depth"] == 1
    # The curator atomically filed the capture and published a commit (CQRS write→curate→read).
    assert out["curated"]["status"] == "published"
    assert out["curated"]["published_commit"]
    # The deterministic query now resolves the freshly-curated theme with a path citation.
    assert out["queried"]["status"] == "ok"
    assert any("curator-concurrency" in hit["path"] for hit in out["queried"]["hits"])


# --- build_server wiring smoke test -------------------------------------------------------------
def test_build_server_registers_seven_tools(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    server = build_server(repo_path=tmp_path, writer="local")

    # The FastMCP instance constructs and carries the 7 agent tools — the original 4, the #58
    # read-side companions (kb_read / kb_neighbors), and the #40 gold consumption tool
    # (kb_context). The pre-existing 6 stay registered unchanged (regression lock).
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "kb_remember",
        "kb_query",
        "kb_read",
        "kb_neighbors",
        "kb_context",
        "kb_status",
        "kb_curate",
    }


# --- ADR-0041 D6: the write refusal on the MCP face ----------------------------------------------


def _make_schema_1(repo: Repo) -> None:
    """Rewrite the fixture's canonical declaration to schema 1 — the owner's existing KB shape."""
    taxonomy_path = repo.layout.meta_dir / "taxonomy.yaml"
    raw = taxonomy_path.read_text(encoding="utf-8")
    taxonomy_path.write_text(
        raw.replace("schema_version: 2", "schema_version: 1"), encoding="utf-8"
    )


def test_kb_curate_refuses_a_schema_1_repo(tmp_path: Path) -> None:
    """ADR-0041 D6: the ``kb_curate`` handler is one of the exhaustive write-path call sites.

    A RAISE, not a ``{"status": ...}`` dict: the ``no_backend`` shape reports a repo that is fine
    but unconfigured, whereas this is a refusal to touch the repo at all, and the FastMCP tool
    error carries the remedy to the calling agent verbatim. It fires BEFORE the backend is built,
    so a schema-1 repo refuses identically whether or not a brain is wired — the verdict is about
    the repo, never about ``adapters.yaml``.
    """
    repo = _init_curatable_repo(tmp_path)
    _make_schema_1(repo)
    handlers = AgoraHandlers(repo)

    with pytest.raises(ReadOnlySchemaVersionError) as exc:
        handlers.curate()

    assert "agora import --from-kb" in str(exc.value)


def test_kb_remember_refuses_a_schema_1_repo_while_reads_keep_working(tmp_path: Path) -> None:
    """The server-construction guard CANNOT express this, which is why the gate is per-write-path.

    ``guard_repo_schema_version`` passes for a schema-1 repo by design (``SUPPORTED_KB_SCHEMA_
    VERSIONS`` is ``{1, 2}`` so reads keep working), so one and the SAME server object must serve
    ``kb_query``/``kb_status`` and refuse ``kb_remember``. That asymmetry is only reachable from a
    gate inside ``Inbox.write``.
    """
    repo = _init_curatable_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    _make_schema_1(repo)
    handlers = AgoraHandlers(repo)

    with pytest.raises(ReadOnlySchemaVersionError):
        handlers.remember(text="a fact the curator could never drain")

    # Same object, same repo: the read tools are unaffected.
    assert handlers.status()["inbox_depth"] == 0
    assert handlers.query("curator")["status"] in ("ok", "not_found")
