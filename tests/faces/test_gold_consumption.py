"""Tests for the ADR-0027 Phase-C gold consumption channels (issue #40).

One shared, transport-free read — :meth:`AgoraHandlers.gold_pack` — behind four thin wrappers: the
``kb_context`` MCP tool, the ``agora://gold/{pack}`` resource, the ``gold_context`` prompt, and the
web ``GET /api/gold/{pack}``. The suite locks the issue's completion criteria:

- the handler serves the BUILT ``_kb/gold/<pack>.md`` artifact **byte-identically** (never
  reassembles — ADR-0027 decision 3), degrades to a clear ``not_built`` note (with build guidance
  and the packs that DO exist), and rejects traversal-unsafe pack names without reading anything;
- the MCP channels are driven over a REAL ``fastmcp.Client`` (tool + resource + prompt);
- MCP and web return the SAME handler payload (the ADR-0019/0021 two-face lock).

The repo fixture mirrors ``tests/test_cli.py::_gold_repo`` (hermetic git env, one eligible theme).
Web tests skip cleanly when fastapi is absent (``importorskip`` inside the web client helper).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastmcp import Client

from agora_kb.core import Repo
from agora_kb.core.gold import build_gold
from agora_kb.faces.mcp_server import AgoraHandlers, build_server

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

GEN_AT = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)

THEME_MD = (
    "---\ntitle: Curator Concurrency\ntype: theme\naliases: []\ntags: []\n"
    "created: '2026-06-01'\nupdated: '2026-07-01'\nstatus: active\n"
    "summary: single-writer CAS keeps the wiki consistent\nsources: [raw/a.md]\n"
    "related: []\nconfidence: high\n---\n\n# Curator Concurrency\n\nbody\n"
)


# --- fixtures -----------------------------------------------------------------------------------
def _git(root: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_DATE": "2026-07-05T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-07-05T00:00:00+00:00",
    }
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=env)


def _repo(tmp_path: Path) -> Repo:
    """Init a repo with one eligible theme COMMITTED (the assembler needs a curated tip)."""
    root = tmp_path / "kb"
    repo = Repo.resolve(root)
    repo.init()
    themes = root / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "curator-concurrency.md").write_text(THEME_MD, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "theme")
    return repo


def _built_repo(tmp_path: Path) -> Repo:
    """The same repo with the default gold pack BUILT (the Phase-A producer, reused verbatim)."""
    repo = _repo(tmp_path)
    build_gold(repo, generated_at=GEN_AT)
    return repo


def _call(server: object, tool: str, args: dict[str, object]) -> dict[str, object]:
    async def _run() -> dict[str, object]:
        async with Client(server) as client:
            return (await client.call_tool(tool, args)).data

    return asyncio.run(_run())


def _web_client(repo: Repo):  # noqa: ANN202 - TestClient; fastapi is optional (importorskip)
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from agora_kb.faces.web import build_app

    return TestClient(build_app(repo_path=repo.layout.root, writer="web", user="alice"))


# --- (a) handler unit tests ---------------------------------------------------------------------
@requires_git
def test_handler_serves_built_pack_byte_identical(tmp_path: Path) -> None:
    """The served ``text`` is the built artifact's EXACT bytes + the meta sidecar fields."""
    repo = _built_repo(tmp_path)
    payload = AgoraHandlers(repo).gold_pack()

    assert payload["status"] == "ok"
    assert payload["pack"] == "default"
    # (d) The byte-identical serving contract: text == the on-disk artifact, bit for bit.
    assert payload["text"].encode("utf-8") == repo.layout.gold_pack_path("default").read_bytes()
    assert payload["text"].startswith("<!-- agora:pack ")  # the §8 sentinel wrap is intact
    assert "Curator Concurrency" in payload["text"]
    meta = payload["meta"]
    assert meta is not None
    assert meta["curated_sha"] == repo.branch_commit()
    assert meta["note_count"] == 1
    assert meta["budget_tokens"] == 2000
    assert meta["estimator"] == "cjk-v1"
    assert meta["harvest_derived_share"] == 0.0


@requires_git
def test_handler_byte_identical_even_with_cr_bytes(tmp_path: Path) -> None:
    """CR bytes survive the serve path — the contract is unconditional, not builder-LF-only.

    The producer can never emit ``\\r`` (``_collapse_ws`` + ``atomic_write_text(newline="\\n")``),
    so a CR-bearing pack only arises out-of-band (manual placement, rsync/backup restore). The
    read must still be byte-identical: ``Path.read_text`` would silently translate ``\\r\\n`` /
    lone ``\\r`` to ``\\n`` (universal newlines; 3.12 has no ``newline=`` param), which is why the
    handler reads via ``read_bytes().decode()``.
    """
    repo = _repo(tmp_path)
    raw = b"<!-- agora:pack crlfpack -->\r\nline one\r\nlone-cr\rend\n<!-- /agora:pack -->\n"
    pack_path = repo.layout.gold_pack_path("crlfpack")
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    pack_path.write_bytes(raw)

    payload = AgoraHandlers(repo).gold_pack("crlfpack")

    assert payload["status"] == "ok"
    assert payload["text"].encode("utf-8") == raw  # \r\n and lone \r preserved bit for bit
    assert payload["meta"] is None  # no sidecar out-of-band — advisory meta degrades to None


@requires_git
def test_handler_not_built_gives_actionable_note(tmp_path: Path) -> None:
    """No built pack → a clear ``not_built`` payload with build guidance, never an exception."""
    repo = _repo(tmp_path)
    payload = AgoraHandlers(repo).gold_pack()

    assert payload["status"] == "not_built"
    assert payload["pack"] == "default"
    assert payload["packs"] == []
    assert "agora gold build" in str(payload["note"])
    assert "text" not in payload


@requires_git
def test_handler_not_built_lists_existing_packs(tmp_path: Path) -> None:
    """Asking for an unbuilt name still lists the packs that DO exist (discoverability)."""
    repo = _built_repo(tmp_path)
    payload = AgoraHandlers(repo).gold_pack("other")

    assert payload["status"] == "not_built"
    assert payload["packs"] == ["default"]


@requires_git
def test_handler_rejects_traversal_names(tmp_path: Path) -> None:
    """An unsafe pack name is refused BEFORE any path is built — nothing escapes ``_kb/gold/``."""
    repo = _built_repo(tmp_path)
    handlers = AgoraHandlers(repo)

    for name in ("../evil", "..", "a/b", ".hidden", "", "a\\b"):
        payload = handlers.gold_pack(name)
        assert payload["status"] == "invalid_name", name
        assert "text" not in payload
        assert payload["packs"] == ["default"]  # discoverability survives the rejection


@requires_git
def test_handler_tolerates_corrupt_meta_sidecar(tmp_path: Path) -> None:
    """A corrupt sidecar degrades to ``meta: None`` — the pack BYTES stay the contract."""
    repo = _built_repo(tmp_path)
    repo.layout.gold_meta_path("default").write_text("not json", encoding="utf-8")
    payload = AgoraHandlers(repo).gold_pack()

    assert payload["status"] == "ok"
    assert payload["meta"] is None
    assert payload["text"].encode("utf-8") == repo.layout.gold_pack_path("default").read_bytes()


# --- (b) MCP channels over a real Client --------------------------------------------------------
@requires_git
def test_kb_context_serves_pack_over_real_client(tmp_path: Path) -> None:
    repo = _built_repo(tmp_path)
    server = build_server(repo_path=repo.layout.root, writer="local")
    data = _call(server, "kb_context", {})

    assert data["status"] == "ok"
    assert data["text"].encode("utf-8") == repo.layout.gold_pack_path("default").read_bytes()
    assert data["meta"]["note_count"] == 1


@requires_git
def test_kb_context_not_built(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    server = build_server(repo_path=repo.layout.root, writer="local")
    data = _call(server, "kb_context", {})

    assert data["status"] == "not_built"
    assert "agora gold build" in data["note"]


@requires_git
def test_gold_resource_reads_pack_byte_identical(tmp_path: Path) -> None:
    """``agora://gold/{pack}`` returns the artifact text verbatim (the resource IS the pack)."""
    repo = _built_repo(tmp_path)
    server = build_server(repo_path=repo.layout.root)

    async def _run() -> list[object]:
        async with Client(server) as client:
            return await client.read_resource("agora://gold/default")

    contents = asyncio.run(_run())
    assert contents[0].text.encode("utf-8") == repo.layout.gold_pack_path("default").read_bytes()


@requires_git
def test_gold_resource_not_built_raises_actionable_error(tmp_path: Path) -> None:
    """A not-built resource read fails with the build guidance — never placeholder bytes."""
    repo = _repo(tmp_path)
    server = build_server(repo_path=repo.layout.root)

    async def _run() -> None:
        async with Client(server) as client:
            await client.read_resource("agora://gold/default")

    with pytest.raises(Exception, match="agora gold build"):
        asyncio.run(_run())


@requires_git
def test_gold_context_prompt_injects_pack_verbatim(tmp_path: Path) -> None:
    repo = _built_repo(tmp_path)
    server = build_server(repo_path=repo.layout.root)

    async def _run() -> object:
        async with Client(server) as client:
            return await client.get_prompt("gold_context", {})

    result = asyncio.run(_run())
    text = result.messages[0].content.text
    assert text.encode("utf-8") == repo.layout.gold_pack_path("default").read_bytes()


@requires_git
def test_gold_context_prompt_not_built_returns_guidance(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    server = build_server(repo_path=repo.layout.root)

    async def _run() -> object:
        async with Client(server) as client:
            return await client.get_prompt("gold_context", {})

    result = asyncio.run(_run())
    assert "agora gold build" in result.messages[0].content.text


@requires_git
def test_kb_context_description_teaches_pack_semantics(tmp_path: Path) -> None:
    """The tool description names the sentinel contract, the retrieval tools, and the opt-in
    posture — an agent learns what the pack is from the tool surface itself."""
    repo = _repo(tmp_path)
    server = build_server(repo_path=repo.layout.root)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    desc = tools["kb_context"].description or ""

    assert "kb_query" in desc and "kb_read" in desc  # relation to the retrieval tools
    assert "agora:pack" in desc  # the §8 sentinel contract is named
    assert "auto-injected" in desc  # pull-only, opt-in posture


# --- (c) web channel + the two-face lock --------------------------------------------------------
@requires_git
def test_api_gold_serves_pack_byte_identical(tmp_path: Path) -> None:
    repo = _built_repo(tmp_path)
    resp = _web_client(repo).get("/api/gold/default")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["text"].encode("utf-8") == repo.layout.gold_pack_path("default").read_bytes()
    assert body["meta"]["curated_sha"] == repo.branch_commit()


@requires_git
def test_api_gold_404_when_not_built(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    resp = _web_client(repo).get("/api/gold/default")

    assert resp.status_code == 404
    assert "agora gold build" in resp.json()["detail"]


@requires_git
def test_api_gold_404_on_invalid_name(tmp_path: Path) -> None:
    repo = _built_repo(tmp_path)
    resp = _web_client(repo).get("/api/gold/.hidden")

    assert resp.status_code == 404
    assert "invalid pack name" in resp.json()["detail"]


@requires_git
def test_mcp_and_web_return_the_same_handler_payload(tmp_path: Path) -> None:
    """The issue's completion criterion: MCP and web wrap ONE handler — identical payloads."""
    repo = _built_repo(tmp_path)
    server = build_server(repo_path=repo.layout.root, writer="local")

    mcp_data = _call(server, "kb_context", {})
    web_data = _web_client(repo).get("/api/gold/default").json()

    assert mcp_data == web_data
