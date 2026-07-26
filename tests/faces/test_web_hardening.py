"""Web-face e2e for the upload-hardening guards (issues #66 + #53).

Mirrors ``tests/faces/test_web.py``'s fixtures over a :class:`fastapi.testclient.TestClient` on a
small temp repo. Covers the HTTP mapping of the two extractor-layer guards: an SSRF-blocked URL →
422 (``ExtractorError`` tolerance), the operator's ``web.upload.url_enabled: false`` switch → 403
BEFORE any resolve/connect, a zip decompression-bomb upload → 422 with the operator-tuned
``web.upload.max_uncompressed_bytes`` cap, and the .epub happy path → queued inbox capture. All
network-free: the SSRF cases use literal IPs the guard rejects before any socket exists.
"""

from __future__ import annotations

import io
import zipfile
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


def _zip_declaring(n_bytes: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("blob.bin", b"\0" * n_bytes)
    return buf.getvalue()


# --- SSRF guard → 422 (issue #66) ---------------------------------------------------------------
def test_upload_private_url_maps_to_422(tmp_path: Path) -> None:
    """A private-network URL is blocked by the extractor's SSRF guard and surfaces as 422."""
    pytest.importorskip("trafilatura")
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = _depth(tmp_path)

    for url in ("http://127.0.0.1:9/x", "http://10.0.0.1/", "http://169.254.169.254/latest"):
        resp = client.post("/api/upload", data={"url": url})
        assert resp.status_code == 422, url
        assert "SSRF guard" in resp.json()["detail"]
    assert _depth(tmp_path) == before  # nothing was queued


def test_upload_non_http_scheme_maps_to_422(tmp_path: Path) -> None:
    pytest.importorskip("trafilatura")
    _init_repo(tmp_path)
    client = _client(tmp_path)

    resp = client.post("/api/upload", data={"url": "file:///etc/passwd"})
    assert resp.status_code == 422
    assert "not allowed" in resp.json()["detail"]


# --- the operator's url_enabled switch → 403 (issue #66 / the #68 team guide) -------------------
def test_url_disabled_maps_to_403_before_any_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`web.upload.url_enabled: false` refuses url capture up-front — extract() is never called."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  upload:\n    url_enabled: false\n")

    from agora_kb.faces.web import app as web_app

    def no_extract(**kwargs: object) -> None:
        raise AssertionError("url capture disabled — extract() must never run")

    monkeypatch.setattr(web_app, "extract", no_extract)
    client = _client(tmp_path)

    resp = client.post("/api/upload", data={"url": "https://example.com/article"})
    assert resp.status_code == 403
    assert "url_enabled" in resp.json()["detail"]


def test_url_enabled_default_still_allows_url_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: with the default config a (mocked-extractor) url upload still queues."""
    _init_repo(tmp_path)

    from agora_kb.faces.web import app as web_app
    from agora_kb.ingest.extractors import ExtractedDoc

    def fake_extract(**kwargs: object) -> ExtractedDoc:
        assert kwargs.get("url") == "https://example.com/post"
        return ExtractedDoc(
            markdown="# Post\n\nbody\n",
            title="Post",
            source_url="https://example.com/post",
            content_sha256="0" * 64,
            mime="text/html",
            extractor="url",
        )

    monkeypatch.setattr(web_app, "extract", fake_extract)
    client = _client(tmp_path)

    resp = client.post("/api/upload", data={"url": "https://example.com/post"})
    assert resp.status_code == 200
    assert resp.json()["queued"] is True


# --- zip decompression-bomb → 422 (issue #53) ---------------------------------------------------
def test_upload_zip_bomb_maps_to_422(tmp_path: Path) -> None:
    """A high-ratio .docx is rejected by the extractor guard with the operator's cap → 422."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  upload:\n    max_uncompressed_bytes: 1000\n")
    client = _client(tmp_path)
    before = _depth(tmp_path)

    bomb = _zip_declaring(4000)  # tiny compressed, 4000 declared > the 1000 cap
    resp = client.post(
        "/api/upload",
        files={
            "file": (
                "bomb.docx",
                bomb,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert resp.status_code == 422
    assert "decompression-bomb" in resp.json()["detail"]
    assert _depth(tmp_path) == before


def test_upload_batch_zip_bomb_is_per_file_error(tmp_path: Path) -> None:
    """In a batch, the bomb fails as ITS OWN receipt while a good file still queues."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  upload:\n    max_uncompressed_bytes: 1000\n")
    client = _client(tmp_path)

    resp = client.post(
        "/api/upload-batch",
        files=[
            ("files", ("good.md", b"# Fine note\n", "text/markdown")),
            ("files", ("bomb.docx", _zip_declaring(4000), "application/octet-stream")),
        ],
    )
    assert resp.status_code == 200
    results = {r["filename"]: r for r in resp.json()["results"]}
    assert results["good.md"]["queued"] is True
    assert results["bomb.docx"]["queued"] is False
    assert "decompression-bomb" in results["bomb.docx"]["error"]


# --- .epub upload happy path (ADR-0025 rec D) ---------------------------------------------------
def test_upload_epub_queues(tmp_path: Path) -> None:
    pytest.importorskip("markitdown")
    from tests.ingest.test_upload_hardening import _make_epub

    _init_repo(tmp_path)
    client = _client(tmp_path)

    resp = client.post(
        "/api/upload",
        files={"file": ("book.epub", _make_epub(), "application/epub+zip")},
    )
    assert resp.status_code == 200
    assert resp.json()["queued"] is True
