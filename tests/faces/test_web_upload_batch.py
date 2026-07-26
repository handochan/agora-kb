"""Tests for the web-face enhancements (ADR-0025): drag-drop multi-upload, the WebConfig-threaded
graph caps + upload limits + allowed-extension gate, and the graph-feature flag.

Mirrors ``tests/faces/test_web.py``'s fixtures over a :class:`fastapi.testclient.TestClient` on a
small temp repo. Covers the multi-file batch (all-success, partial-success with a bad file, the
per-batch ``max_files`` / ``total_bytes`` 413 rejections), the allowed-extension 415 gate, the
graph caps coming from ``repo.yaml`` ``web:`` (truncation honoured), and the graph-feature flag
(``graph_enabled: false`` → /graph + /api/graph 404, the nav link hidden). The single-file
``/api/upload`` regression is covered in ``test_web.py``; here we add a single-file batch-path
co-existence check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agora_kb.core import Inbox, Repo  # noqa: E402


# --- fixtures (mirror test_web.py) --------------------------------------------------------------
def _init_repo(tmp_path: Path) -> Repo:
    repo = Repo.resolve(tmp_path)
    repo.init()
    return repo


def _write_repo_yaml(tmp_path: Path, text: str) -> None:
    layout = Repo.resolve(tmp_path).layout
    layout.kb_dir.mkdir(parents=True, exist_ok=True)
    (layout.kb_dir / "repo.yaml").write_text(text, encoding="utf-8")


def _client(tmp_path: Path, *, user: str = "alice") -> TestClient:
    from agora_kb.faces.web import build_app

    app = build_app(repo_path=tmp_path, writer="web", user=user)
    # Host must match the default web.security.allowed_hosts (loopback only, issue #94):
    # starlette's TestClient default `testserver` is deliberately NOT allow-listed.
    return TestClient(app, base_url="http://127.0.0.1")


def _depth(tmp_path: Path) -> int:
    return Inbox(Repo.resolve(tmp_path).layout).depth()


def _inbox_items(tmp_path: Path) -> list[dict[str, object]]:
    """Every inbox event's parsed frontmatter ∪ ``{'body': …}`` (read-only)."""
    from agora_kb.core import frontmatter

    layout = Repo.resolve(tmp_path).layout
    items: list[dict[str, object]] = []
    for path in sorted(layout.inbox_dir.glob("*/*.md")):
        fm, body = frontmatter.parse(path.read_text(encoding="utf-8"))
        fm = dict(fm)
        fm["body"] = body
        items.append(fm)
    return items


# --- multi-upload batch -------------------------------------------------------------------------
def test_batch_all_success(tmp_path: Path) -> None:
    """A batch of N text files yields N queued receipts + N independent inbox appends."""
    _init_repo(tmp_path)
    client = _client(tmp_path, user="alice")
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload-batch",
        files=[
            ("files", ("a.md", b"# Note A\n", "text/markdown")),
            ("files", ("b.txt", b"finding b\n", "text/plain")),
            ("files", ("c.md", b"# Note C\n", "text/markdown")),
        ],
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 3
    assert all(r["queued"] for r in results)
    assert {r["filename"] for r in results} == {"a.md", "b.txt", "c.md"}
    # Three independent inbox events (append-only per-event).
    assert _depth(tmp_path) == before + 3


def test_batch_bad_shared_tag_rejects_whole_batch_up_front(tmp_path: Path) -> None:
    """A non-kebab shared ``tags`` rejects the WHOLE batch with 422 BEFORE any file is read/queued.

    Locks the deliberate up-front validation (IMPL-SPEC Unit 4 / ``_parse_tags(tags)`` before the
    read loop in ``_do_upload_batch``): an invalid shared tag would fail every file identically, so
    it must surface as ONE clean batch error — never N duplicate per-file errors or a partial queue.
    """
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload-batch",
        files=[
            ("files", ("a.md", b"# A\n", "text/markdown")),
            ("files", ("b.md", b"# B\n", "text/markdown")),
        ],
        data={"tags": "NotKebab"},
    )
    assert resp.status_code == 422, resp.text
    assert "kebab-case" in resp.json()["detail"]
    # Up-front rejection: nothing was queued (the check precedes the read loop / any inbox write).
    assert _depth(tmp_path) == before


def test_batch_valid_shared_tag_and_domain_propagate_per_file(tmp_path: Path) -> None:
    """A valid shared ``tags``/``domain`` is applied to EVERY file's inbox event (per-file)."""
    _init_repo(tmp_path)
    client = _client(tmp_path, user="alice")
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload-batch",
        files=[
            ("files", ("a.md", b"# Note A\n", "text/markdown")),
            ("files", ("b.txt", b"finding b\n", "text/plain")),
        ],
        data={"tags": "single-writer", "domain": "general"},
    )
    assert resp.status_code == 200, resp.text
    assert all(r["queued"] for r in resp.json()["results"])
    assert _depth(tmp_path) == before + 2

    items = _inbox_items(tmp_path)
    assert len(items) == 2
    # The shared domain/tags/source rode through to each per-file inbox event.
    for item in items:
        assert item["source"] == "web:alice"
        assert item["domain"] == "general"
        assert item["tags"] == ["single-writer"]


def test_batch_partial_success_one_bad_file(tmp_path: Path) -> None:
    """A bad file gets its own error receipt; the good files still queue (best-effort)."""
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload-batch",
        files=[
            ("files", ("good.md", b"# Good\n", "text/markdown")),
            ("files", ("bad.bin", b"\x00\x01 unroutable", "application/octet-stream")),
        ],
    )
    assert resp.status_code == 200, resp.text
    results = {r["filename"]: r for r in resp.json()["results"]}
    assert results["good.md"]["queued"] is True
    assert results["good.md"]["id"]
    assert results["bad.bin"]["queued"] is False
    assert results["bad.bin"]["error"]
    # Only the good file was appended (partial success is correct — the inbox is append-only).
    assert _depth(tmp_path) == before + 1


def test_batch_rejects_too_many_files(tmp_path: Path) -> None:
    """Exceeding web.upload.max_files rejects the WHOLE batch with 413 before any write."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  upload:\n    max_files: 2\n")
    client = _client(tmp_path)
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload-batch",
        files=[
            ("files", ("a.md", b"a\n", "text/markdown")),
            ("files", ("b.md", b"b\n", "text/markdown")),
            ("files", ("c.md", b"c\n", "text/markdown")),
        ],
    )
    assert resp.status_code == 413, resp.text
    assert "too many files" in resp.json()["detail"]
    assert _depth(tmp_path) == before  # nothing queued


def test_batch_rejects_total_bytes(tmp_path: Path) -> None:
    """Exceeding web.upload.total_bytes rejects the WHOLE batch with 413 before any write."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  upload:\n    total_bytes: 100\n")
    client = _client(tmp_path)
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload-batch",
        files=[
            ("files", ("a.md", b"x" * 60, "text/markdown")),
            ("files", ("b.md", b"y" * 60, "text/markdown")),  # running total now 120 > 100
        ],
    )
    assert resp.status_code == 413, resp.text
    assert "batch too large" in resp.json()["detail"]
    assert _depth(tmp_path) == before  # nothing queued


def test_batch_per_file_oversize_is_per_file_error(tmp_path: Path) -> None:
    """A per-file max_bytes overflow is that file's own error, not a whole-batch rejection."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  upload:\n    max_bytes: 10\n    total_bytes: 100000\n")
    client = _client(tmp_path)
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload-batch",
        files=[
            ("files", ("small.md", b"ok\n", "text/markdown")),
            ("files", ("big.md", b"x" * 50, "text/markdown")),  # > per-file 10
        ],
    )
    assert resp.status_code == 200, resp.text
    results = {r["filename"]: r for r in resp.json()["results"]}
    assert results["small.md"]["queued"] is True
    assert results["big.md"]["queued"] is False
    assert "too large" in results["big.md"]["error"]
    assert _depth(tmp_path) == before + 1


def test_batch_empty_files_rejected_422(tmp_path: Path) -> None:
    """POST /api/upload-batch with no file parts fails FastAPI's list-required validation (422).

    The ``files: list[UploadFile]`` parameter is required, so an empty batch never reaches the
    handler's own ``provide at least one file`` 400 guard — FastAPI rejects the missing field with
    422 first. Lock whichever actually fires (here: 422) so the contract is documented.
    """
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = _depth(tmp_path)

    resp = client.post("/api/upload-batch", files=[])
    assert resp.status_code == 422, resp.text
    assert _depth(tmp_path) == before  # nothing queued


def test_htmx_upload_multiple_files_renders_batch_fragment(tmp_path: Path) -> None:
    """The HTMX /upload path with ≥2 files renders the batch fragment, not the single one."""
    _init_repo(tmp_path)
    client = _client(tmp_path)
    resp = client.post(
        "/upload",
        files=[
            ("file", ("a.md", b"# A\n", "text/markdown")),
            ("file", ("b.md", b"# B\n", "text/markdown")),
        ],
    )
    assert resp.status_code == 200, resp.text
    assert "Batch captured" in resp.text
    assert "2 queued" in resp.text


def test_htmx_upload_single_file_still_single_receipt(tmp_path: Path) -> None:
    """The HTMX /upload path with one file keeps the single-receipt fragment (regression)."""
    _init_repo(tmp_path)
    client = _client(tmp_path)
    resp = client.post("/upload", files={"file": ("a.md", b"# A\n", "text/markdown")})
    assert resp.status_code == 200, resp.text
    assert "after the next curator run" in resp.text  # the single-receipt eventual-consistency note
    assert "Batch captured" not in resp.text


def test_htmx_upload_empty_file_part_with_text_degrades_to_single_receipt(tmp_path: Path) -> None:
    """An empty file part (browser's filename="") + a text field → the SINGLE-receipt fragment.

    Locks the ``upload_submit`` degrade path: a browser whose ``multiple`` file input is left empty
    still POSTs an empty-filename file part alongside the url/text capture form. The handler filters
    ``f.filename`` falsy parts out (``real_files`` empty) and runs the single ``_do_upload`` over
    the ``text`` field — NOT the batch path. A raw multipart body is crafted because the test client
    can't emit a ``filename=""`` UploadFile part via its ``files=`` shorthand (it becomes a str
    field FastAPI's ``list[UploadFile]`` rejects); the real browser sends exactly this shape.
    """
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = _depth(tmp_path)

    boundary = "----agoraboundary"
    body = "\r\n".join(
        [
            f"--{boundary}",
            'Content-Disposition: form-data; name="file"; filename=""',
            "Content-Type: application/octet-stream",
            "",
            "",
            f"--{boundary}",
            'Content-Disposition: form-data; name="text"',
            "",
            "a plain text finding",
            f"--{boundary}--",
            "",
        ]
    ).encode()

    resp = client.post(
        "/upload",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp.status_code == 200, resp.text
    # The single-receipt (eventual-consistency note), NOT the batch fragment.
    assert "after the next curator run" in resp.text
    assert "Batch captured" not in resp.text
    # The text field produced exactly one inbox append (the degrade-to-text write path).
    assert _depth(tmp_path) == before + 1


# --- allowed-extension gate ---------------------------------------------------------------------
def test_allowed_extension_gate_blocks_415(tmp_path: Path) -> None:
    """With web.extensions.allowed set, a disallowed extension is 415 BEFORE any extract/write."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  extensions:\n    allowed: [.md, .txt]\n")
    client = _client(tmp_path)
    before = _depth(tmp_path)

    resp = client.post("/api/upload", files={"file": ("note.pdf", b"%PDF-1.4", "application/pdf")})
    assert resp.status_code == 415, resp.text
    assert "not allowed" in resp.json()["detail"]
    assert _depth(tmp_path) == before


def test_allowed_extension_gate_permits_listed(tmp_path: Path) -> None:
    """A listed extension passes the gate and is captured normally."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  extensions:\n    allowed: [.md, .txt]\n")
    client = _client(tmp_path)

    resp = client.post("/api/upload", files={"file": ("note.md", b"# allowed\n", "text/markdown")})
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued"] is True


def test_allowed_extension_none_means_no_gate(tmp_path: Path) -> None:
    """The default (allowed=None) imposes no face gate — the extractor's supported set governs."""
    _init_repo(tmp_path)
    client = _client(tmp_path)
    resp = client.post("/api/upload", files={"file": ("note.md", b"# ok\n", "text/markdown")})
    assert resp.status_code == 200, resp.text


# --- graph caps from WebConfig ------------------------------------------------------------------
def test_graph_caps_from_config_truncate(tmp_path: Path) -> None:
    """A small web.graph.max_nodes truncates the global graph honestly (node_total still true)."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  graph:\n    max_nodes: 1\n")
    # Seed two notes so the kept set (2) exceeds the cap (1).
    domain = tmp_path / "wiki" / "general"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "a.md").write_text("---\ntype: theme\nstatus: active\n---\n# A\n", encoding="utf-8")
    (domain / "b.md").write_text("---\ntype: theme\nstatus: active\n---\n# B\n", encoding="utf-8")
    client = _client(tmp_path)

    data = client.get("/api/graph").json()
    assert data["truncated"] is True
    assert len(data["nodes"]) == 1  # capped to the configured 1
    assert data["node_total"] >= 2  # honest pre-cap count


def test_graph_default_cap_does_not_truncate_small_kb(tmp_path: Path) -> None:
    """The LARGE default (10000) leaves a small corpus untruncated (raised-default regression)."""
    _init_repo(tmp_path)
    domain = tmp_path / "wiki" / "general"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "a.md").write_text("---\ntype: theme\nstatus: active\n---\n# A\n", encoding="utf-8")
    client = _client(tmp_path)

    data = client.get("/api/graph").json()
    assert data["truncated"] is False


# --- graph feature flag -------------------------------------------------------------------------
def _seed_note(tmp_path: Path) -> str:
    """Seed one wiki note and return its rel_path (so a /note page renders the local graph)."""
    domain = tmp_path / "wiki" / "general"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "a.md").write_text("---\ntype: theme\nstatus: active\n---\n# A\n", encoding="utf-8")
    return "wiki/general/a.md"


def test_graph_disabled_routes_404_and_nav_hidden(tmp_path: Path) -> None:
    """graph_enabled: false → /graph + /api/graph 404, nav link AND per-note local graph gone.

    The flag must guard the per-note Connections section too (note.html ``graph_enabled`` gate),
    not just the global /graph route — otherwise a disabled graph still ships graph.js + an empty
    canvas that 404s its data fetch on every note page.
    """
    _init_repo(tmp_path)
    rel_path = _seed_note(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  features:\n    graph_enabled: false\n")
    client = _client(tmp_path)

    assert client.get("/graph").status_code == 404
    assert client.get("/api/graph").status_code == 404
    # The nav link is gone from the home page.
    home = client.get("/")
    assert home.status_code == 200
    assert 'href="/graph"' not in home.text
    # The per-note Connections local-graph section + its graph.js are gone from the note page.
    note = client.get(f"/note/{rel_path}")
    assert note.status_code == 200
    assert "Connections" not in note.text
    assert "graph.js" not in note.text


def test_graph_enabled_default_serves_routes_and_nav(tmp_path: Path) -> None:
    """The default (graph_enabled: true) serves the graph routes, nav link, AND per-note graph."""
    _init_repo(tmp_path)
    rel_path = _seed_note(tmp_path)
    client = _client(tmp_path)
    assert client.get("/graph").status_code == 200
    assert client.get("/api/graph").status_code == 200
    assert 'href="/graph"' in client.get("/").text
    # The per-note Connections local-graph section + its graph.js ARE present (symmetric case).
    note = client.get(f"/note/{rel_path}")
    assert note.status_code == 200
    assert "Connections" in note.text
    assert "graph.js" in note.text


# --- upload form: drop-zone + multiple input ----------------------------------------------------
def test_upload_form_has_multiple_and_dropzone(tmp_path: Path) -> None:
    """The capture form exposes a multiple file input + the drop-zone wiring (static/upload.js)."""
    _init_repo(tmp_path)
    client = _client(tmp_path)
    html = client.get("/upload").text
    assert "multiple" in html
    assert 'id="dropzone"' in html
    assert "/static/upload.js" in html


def test_upload_js_served(tmp_path: Path) -> None:
    """The vendored drop-zone JS (no CDN/Node) is served from static and wires the input on drop."""
    _init_repo(tmp_path)
    client = _client(tmp_path)
    resp = client.get("/static/upload.js")
    assert resp.status_code == 200
    body = resp.text
    assert "dropzone" in body
    assert "dataTransfer" in body  # the drop handler fills the input from the drop event
