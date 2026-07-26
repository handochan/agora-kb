"""Tests for the Phase-3 web face (FastAPI + HTMX; ADR-0019 / ADR-0020).

Exercised over a :class:`fastapi.testclient.TestClient` on a small temp repo. Covers the JSON API
(status/search/notes/note), the HTML home (has a search box), the multi-modal upload pipeline
(text + a tiny pdf + a monkeypatched url — each asserting an inbox event was written with
``source=web:<user>``), the markdown→HTML XSS guard (a raw ``<script>`` in a note body is escaped),
and intra-wiki ``.md`` link rewriting to ``/note/``. All web tests skip cleanly when fastapi is
absent (``importorskip``), and a separate test guards that ``import agora_kb`` is NOT regressed by
the optional web stack (``faces.web`` is lazily imported).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agora_kb.core import Inbox, Repo  # noqa: E402


# --- fixtures -----------------------------------------------------------------------------------
def _init_repo(tmp_path: Path) -> Repo:
    repo = Repo.resolve(tmp_path)
    repo.init()
    return repo


def _write_wiki_notes(tmp_path: Path) -> None:
    """A small navigable corpus (index + MOC + two themes) under ``wiki/`` (mirrors browse test)."""
    (tmp_path / "index.md").write_text(
        "---\ntype: index\nstatus: active\n---\n"
        "# personal\n\n- [AI Tech MOC](wiki/ai-tech/ai-tech-moc.md)\n",
        encoding="utf-8",
    )
    domain = tmp_path / "wiki" / "ai-tech"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "ai-tech-moc.md").write_text(
        "---\nstatus: active\ntype: moc\n---\n# AI Tech\n\n"
        "- [Curator concurrency](themes/curator-concurrency.md) — single-writer curator\n",
        encoding="utf-8",
    )
    themes = domain / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "curator-concurrency.md").write_text(
        "---\nstatus: active\ntype: theme\ntags: [single-writer, concurrency]\n"
        "title: Curator Concurrency Model\n---\n"
        "# Curator Concurrency\n\n"
        "The curator acquires a per-repo flock. See [Inbox design](inbox-design.md).\n",
        encoding="utf-8",
    )
    (themes / "inbox-design.md").write_text(
        "---\nstatus: active\ntype: theme\ntags: [inbox]\n---\n"
        "# Inbox Design\n\nThe inbox is append-only and per-writer namespaced.\n",
        encoding="utf-8",
    )


def _client(tmp_path: Path, *, user: str = "alice") -> TestClient:
    from agora_kb.faces.web import build_app

    app = build_app(repo_path=tmp_path, writer="web", user=user)
    # Host must match the default web.security.allowed_hosts (loopback only, issue #94):
    # starlette's TestClient default `testserver` is deliberately NOT allow-listed.
    return TestClient(app, base_url="http://127.0.0.1")


# --- HTML home ----------------------------------------------------------------------------------
def test_home_renders_with_search_box(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert 'type="search"' in body
    assert 'hx-get="/search"' in body
    # The vendored htmx script (no CDN) and stylesheet are referenced.
    assert "/static/htmx.min.js" in body
    # The domain/notes list shows the corpus.
    assert "Curator Concurrency Model" in body


# --- JSON API -----------------------------------------------------------------------------------
def test_api_status(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) >= {"inbox_depth", "processed_today", "failed", "counters"}
    assert data["inbox_depth"] == 0


def test_api_search_ok_and_not_found(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    client = _client(tmp_path)

    ok = client.get("/api/search", params={"q": "curator concurrency"})
    assert ok.status_code == 200
    payload = ok.json()
    assert payload["status"] == "ok"
    assert payload["hits"]
    assert any("curator-concurrency" in h["path"] for h in payload["hits"])

    nf = client.get("/api/search", params={"q": "zzzznonexistentterm"})
    assert nf.status_code == 200
    assert nf.json()["status"] == "not_found"


def test_api_notes_list_and_one(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    client = _client(tmp_path)

    listing = client.get("/api/notes")
    assert listing.status_code == 200
    data = listing.json()
    assert data["domains"] == ["ai-tech"]
    rel_paths = [n["rel_path"] for n in data["notes"]]
    assert "index.md" in rel_paths
    assert "wiki/ai-tech/themes/curator-concurrency.md" in rel_paths

    one = client.get("/api/notes/wiki/ai-tech/themes/curator-concurrency.md")
    assert one.status_code == 200
    note = one.json()
    assert note["basename"] == "curator-concurrency"
    assert note["body"].startswith("# Curator Concurrency")
    assert note["links"] == ["inbox-design"]


def test_api_note_404(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    client = _client(tmp_path)

    miss = client.get("/api/notes/wiki/ai-tech/themes/nope.md")
    assert miss.status_code == 404
    # Traversal-safe: an escape path resolves to no note → 404, not a file read.
    traversal = client.get("/api/notes/../../etc/passwd")
    assert traversal.status_code in (404, 400)


# --- upload pipeline ----------------------------------------------------------------------------
def test_api_upload_text_writes_inbox_with_web_source(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    client = _client(tmp_path, user="alice")
    before = Inbox(Repo.resolve(tmp_path).layout).depth()

    resp = client.post(
        "/api/upload",
        data={"text": "A finding worth remembering.", "tags": "single-writer, inbox"},
    )
    assert resp.status_code == 200
    receipt = resp.json()
    assert receipt["queued"] is True
    assert receipt["inbox_depth"] == before + 1

    item = _read_only_inbox_item(tmp_path)
    assert item["source"] == "web:alice"
    assert "A finding worth remembering." in item["body"]
    # The deterministic provenance header rode into the capture body (ADR-0020).
    assert "captured-by: web" in item["body"]


def test_api_upload_url_monkeypatched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path)
    from agora_kb.faces.web import app as web_app
    from agora_kb.ingest.extractors import ExtractedDoc

    def _fake_extract(**kwargs: object) -> ExtractedDoc:
        assert kwargs.get("url") == "https://example.com/post"
        return ExtractedDoc(
            markdown="# Fetched\n\nExtracted body.",
            title="Fetched Article",
            source_url="https://example.com/post",
            content_sha256="0" * 64,
            mime="text/html",
            extractor="url",
        )

    monkeypatch.setattr(web_app, "extract", _fake_extract)
    client = _client(tmp_path, user="bob")

    resp = client.post("/api/upload", data={"url": "https://example.com/post"})
    assert resp.status_code == 200
    assert resp.json()["queued"] is True

    item = _read_only_inbox_item(tmp_path)
    assert item["source"] == "web:bob"
    assert "Extracted body." in item["body"]
    assert "source-url:" in item["body"]
    assert "https://example.com/post" in item["body"]


def test_api_upload_tiny_pdf(tmp_path: Path) -> None:
    pytest.importorskip("pdfminer")
    _init_repo(tmp_path)
    client = _client(tmp_path, user="carol")
    pdf_bytes = _tiny_pdf()

    resp = client.post(
        "/api/upload",
        files={"file": ("note.pdf", pdf_bytes, "application/pdf")},
    )
    # Extraction of a minimal valid PDF succeeds (200); a malformed one would be 422 — either way
    # the face must not 500. We assert the success path on a real tiny PDF.
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued"] is True
    item = _read_only_inbox_item(tmp_path)
    assert item["source"] == "web:carol"
    assert "extractor:" in item["body"]


def test_api_upload_no_input_is_400(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    client = _client(tmp_path)
    resp = client.post("/api/upload", data={})
    assert resp.status_code == 400


def test_api_upload_non_kebab_tag_is_422_not_500(tmp_path: Path) -> None:
    """An untrusted non-kebab tag from the form must surface as a clean 422, never a raw 500.

    Regression for the upload-tags crash: ``_parse_tags`` now validates kebab-case at the face
    boundary so the pydantic ``InboxItem`` ValueError never escapes uncaught. The inbox must stay
    empty (no partial write) on the rejected request.
    """
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = Inbox(Repo.resolve(tmp_path).layout).depth()

    for bad in ("Foo Bar, NotKebab!", "Not_Kebab_Tag", "Bad Tag!"):
        resp = client.post("/api/upload", data={"text": "a real finding", "tags": bad})
        assert resp.status_code == 422, (bad, resp.text)

    # No partial/corrupt write happened on any rejected request.
    assert Inbox(Repo.resolve(tmp_path).layout).depth() == before


def test_htmx_upload_non_kebab_tag_renders_error_fragment_not_500(tmp_path: Path) -> None:
    """The HTMX form path must render its friendly receipt-error fragment (not a raw 500) for a
    non-kebab tag — the HTTPException is caught by ``upload_submit`` and shown in ``_receipt.html``.
    """
    _init_repo(tmp_path)
    client = _client(tmp_path)
    resp = client.post("/upload", data={"text": "a real finding", "tags": "Bad_Tag!"})
    assert resp.status_code == 422
    assert "kebab-case" in resp.text  # the error detail is surfaced in the fragment


# --- browse tolerance: a fenceless wiki note must not 500 the browse face ------------------------
def test_browse_tolerates_fenceless_note(tmp_path: Path) -> None:
    """A single foreign / un-normalized note lacking a frontmatter fence must NOT 500 the browse UI.

    Regression for the strict-vs-tolerant asymmetry: ``parse_all_notes`` now degrades a fenceless
    note (empty frontmatter + full body) on the read path (ADR-0014 D1 tolerant consumer), matching
    ``Wiki.query``. GET /, /api/notes, /upload, /note/... must all stay 200 (search already did).
    """
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    fenceless = tmp_path / "wiki" / "general"
    fenceless.mkdir(parents=True, exist_ok=True)
    (fenceless / "nofm.md").write_text("# No frontmatter here\n\nbody.\n", encoding="utf-8")
    client = _client(tmp_path)

    assert client.get("/").status_code == 200
    assert client.get("/api/notes").status_code == 200
    assert client.get("/upload").status_code == 200
    assert client.get("/api/search", params={"q": "body"}).status_code == 200

    # The degraded note is listed (basename title, since it has no frontmatter title) and readable.
    listing = client.get("/api/notes").json()
    rel_paths = [n["rel_path"] for n in listing["notes"]]
    assert "wiki/general/nofm.md" in rel_paths
    note = client.get("/api/notes/wiki/general/nofm.md")
    assert note.status_code == 200
    assert note.json()["body"].startswith("# No frontmatter here")
    assert client.get("/note/wiki/general/nofm.md").status_code == 200


def test_api_upload_extractor_error_is_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed/garbage extractor input maps to 422 (ADR-0020 §5), not 500."""
    _init_repo(tmp_path)
    from agora_kb.faces.web import app as web_app
    from agora_kb.ingest.extractors import ExtractorError

    def _boom(**kwargs: object):
        raise ExtractorError("no extractable text")

    monkeypatch.setattr(web_app, "extract", _boom)
    client = _client(tmp_path)
    resp = client.post("/api/upload", data={"url": "https://example.com/garbage"})
    assert resp.status_code == 422, resp.text


def test_api_upload_extractor_unavailable_is_503_with_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ``ingest`` dependency maps to 503 and surfaces the install remedy (ADR-0020 §5)."""
    _init_repo(tmp_path)
    from agora_kb.faces.web import app as web_app
    from agora_kb.ingest.extractors import ExtractorUnavailable

    def _missing(**kwargs: object):
        raise ExtractorUnavailable("pdfminer not installed")

    monkeypatch.setattr(web_app, "extract", _missing)
    client = _client(tmp_path)
    resp = client.post("/api/upload", data={"url": "https://example.com/post"})
    assert resp.status_code == 503, resp.text
    assert "install the ingest extra" in resp.json()["detail"]


def test_api_upload_oversize_file_is_413(tmp_path: Path) -> None:
    """An over-``MAX_UPLOAD_BYTES`` file upload hits the face's own 413 guard before any write.

    The file path streams via ``UploadFile`` (spooled), so it reaches the app's explicit
    ``MAX_UPLOAD_BYTES`` check at app.py rather than Starlette's per-form-field cap (which guards
    the plain-``text`` field at a lower limit and is its own 4xx). No write happens on rejection.
    """
    _init_repo(tmp_path)
    from agora_kb.faces.web.app import MAX_UPLOAD_BYTES

    client = _client(tmp_path)
    before = Inbox(Repo.resolve(tmp_path).layout).depth()
    big = b"x" * (MAX_UPLOAD_BYTES + 1)
    resp = client.post("/api/upload", files={"file": ("big.txt", big, "text/plain")})
    assert resp.status_code == 413, resp.text
    assert Inbox(Repo.resolve(tmp_path).layout).depth() == before


# --- HTMX upload fragment -----------------------------------------------------------------------
def test_htmx_upload_returns_receipt_fragment(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    client = _client(tmp_path, user="dave")
    resp = client.post("/upload", data={"text": "captured via the form"})
    assert resp.status_code == 200
    # The eventual-consistency message is shown (DESIGN §2.2).
    assert "after the next curator run" in resp.text
    assert "web:dave" not in resp.text  # the source is internal, not leaked into the receipt UI
    item = _read_only_inbox_item(tmp_path)
    assert item["source"] == "web:dave"


# --- markdown render: XSS guard + link rewriting ------------------------------------------------
def test_note_render_escapes_script_xss(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    domain = tmp_path / "wiki" / "general"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "evil.md").write_text(
        "---\ntype: theme\nstatus: active\n---\n"
        "# Evil\n\nA harvested candidate: <script>alert('xss')</script> and a [link](other.md).\n",
        encoding="utf-8",
    )
    client = _client(tmp_path)

    resp = client.get("/note/wiki/general/evil.md")
    assert resp.status_code == 200
    html = resp.text
    # Raw <script> must NOT survive into the rendered body — markdown-it html=False escapes it.
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_note_render_strips_body_sentinels_and_breaks_lines(tmp_path: Path) -> None:
    """The curator's `<!-- agora:body:start/end -->` region markers must NOT appear in the rendered
    note — they are an internal AUTHOR-pass mechanism, and html=False would otherwise escape them
    into VISIBLE text. A single newline between prose lines renders as a break, not a run-on."""
    from agora_kb.faces.web.app import render_note_body

    body = (
        "<!-- agora:body:start id=2026-06-21T15-44-44.960Z--44ad09--c1 -->\n"
        "Core Thesis & Definition\n"
        "AI agents are systems, not isolated models.\n"
        "<!-- agora:body:end id=2026-06-21T15-44-44.960Z--44ad09--c1 -->\n"
    )
    html = render_note_body(body)
    assert "agora:body" not in html  # internal sentinels stripped
    assert "<!--" not in html
    assert "Core Thesis" in html  # prose preserved
    assert "<br" in html  # single newline → line break (breaks=True), not a collapsed run-on

    # End-to-end over the route: a stored note whose body carries sentinels renders clean.
    _init_repo(tmp_path)
    domain = tmp_path / "wiki" / "general"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "sentinel.md").write_text(
        "---\ntype: theme\nstatus: active\n---\n\n" + body, encoding="utf-8"
    )
    resp = _client(tmp_path).get("/note/wiki/general/sentinel.md")
    assert resp.status_code == 200
    assert "agora:body" not in resp.text


def test_note_render_rewrites_intra_wiki_links(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/note/wiki/ai-tech/themes/curator-concurrency.md")
    assert resp.status_code == 200
    html = resp.text
    # The body link [Inbox design](inbox-design.md) is rewritten to the resolved web route.
    assert 'href="/note/wiki/ai-tech/themes/inbox-design.md"' in html
    # The bare relative .md target is not left in place.
    assert 'href="inbox-design.md"' not in html


def test_note_render_leaves_external_links(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    domain = tmp_path / "wiki" / "general"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "ext.md").write_text(
        "---\ntype: theme\nstatus: active\n---\n"
        "# Ext\n\nSee [GCP](https://cloud.google.com) and [self](other.md).\n",
        encoding="utf-8",
    )
    client = _client(tmp_path)
    resp = client.get("/note/wiki/general/ext.md")
    assert resp.status_code == 200
    assert 'href="https://cloud.google.com"' in resp.text


def test_note_page_404(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    client = _client(tmp_path)
    resp = client.get("/note/wiki/general/nope.md")
    assert resp.status_code == 404
    assert "not found" in resp.text.lower()


# --- note header: collapsible frontmatter metadata block (pure-template, autoescaped) -----------
def test_note_header_renders_rich_frontmatter_metadata(tmp_path: Path) -> None:
    """A note with rich frontmatter surfaces summary/sources/created/updated/aliases/related/
    confidence in the collapsible <details>Metadata</details> header block (ADR-0019 read face)."""
    _init_repo(tmp_path)
    domain = tmp_path / "wiki" / "general"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "rich.md").write_text(
        "---\n"
        "type: theme\n"
        "status: active\n"
        "title: Rich Note\n"
        "summary: A concise note summary line.\n"
        "sources:\n"
        "  - raw/2026/06/web-alice/a1b2.md\n"
        "  - raw/2026/06/web-alice/c3d4.md\n"
        "created: 2026-06-01\n"
        "updated: 2026-06-20\n"
        "aliases:\n"
        "  - Wealthy Note\n"
        "related:\n"
        "  - '[[inbox-design]]'\n"
        "confidence: high\n"
        "---\n"
        "# Rich Note\n\nBody text.\n",
        encoding="utf-8",
    )
    client = _client(tmp_path)

    resp = client.get("/note/wiki/general/rich.md")
    assert resp.status_code == 200
    html = resp.text
    # The collapsible block exists.
    assert "<details" in html
    assert "Metadata" in html
    # Each present field's value is rendered into the header.
    assert "A concise note summary line." in html
    assert "raw/2026/06/web-alice/a1b2.md" in html
    assert "2026-06-01" in html  # created
    assert "2026-06-20" in html  # updated
    assert "Wealthy Note" in html  # alias
    assert "high" in html  # confidence


def test_note_header_sparse_frontmatter_omits_metadata_block(tmp_path: Path) -> None:
    """A sparse note (only type/status/title) renders 200 with NO metadata <details> block and no
    broken/empty rows — guarding the `note.frontmatter.get(...)` missing-key path."""
    _init_repo(tmp_path)
    domain = tmp_path / "wiki" / "general"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "sparse.md").write_text(
        "---\ntype: theme\nstatus: active\ntitle: Sparse Note\n---\n# Sparse Note\n\nBody.\n",
        encoding="utf-8",
    )
    client = _client(tmp_path)

    resp = client.get("/note/wiki/general/sparse.md")
    assert resp.status_code == 200
    html = resp.text
    # No metadata block is emitted when none of the optional fields are present.
    assert "<details" not in html
    assert ">Metadata<" not in html
    # No literal None leaked from a missing-key render.
    assert "None" not in html


def test_note_header_metadata_is_html_escaped(tmp_path: Path) -> None:
    """Untrusted frontmatter values (a `<script>` summary, a `[[x]]` related entry) MUST be
    HTML-escaped by Jinja autoescape in the header — never rendered as raw markup (no |safe)."""
    _init_repo(tmp_path)
    domain = tmp_path / "wiki" / "general"
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "xss.md").write_text(
        "---\n"
        "type: theme\n"
        "status: active\n"
        "title: XSS Note\n"
        'summary: "<script>alert(1)</script>"\n'
        "related:\n"
        "  - '[[evil]]'\n"
        "---\n"
        "# XSS Note\n\nBody.\n",
        encoding="utf-8",
    )
    client = _client(tmp_path)

    resp = client.get("/note/wiki/general/xss.md")
    assert resp.status_code == 200
    html = resp.text
    # The raw <script> from the summary frontmatter never survives unescaped into the header.
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # The [[evil]] related entry is rendered as escaped text (brackets are literal, not markup).
    assert "[[evil]]" in html


# --- the lazy-import invariant (import agora_kb without fastapi must not regress) ----------------
def test_import_agora_kb_does_not_require_fastapi() -> None:
    """`import agora_kb` (and `import agora_kb.faces.web`) must not import fastapi at module load.

    Run in a SUBPROCESS so the already-imported fastapi in this process can't mask a regression.
    The subprocess imports the package and the web subpackage, then asserts ``fastapi`` is NOT in
    ``sys.modules`` — proving ``faces.web`` defers the fastapi import to ``build_app`` access.
    """
    code = (
        "import sys\n"
        "import agora_kb\n"
        "import agora_kb.faces.web\n"
        "assert 'fastapi' not in sys.modules, 'faces.web eagerly imported fastapi'\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# --- helpers ------------------------------------------------------------------------------------
def _read_only_inbox_item(tmp_path: Path) -> dict[str, object]:
    """Return the single inbox event's parsed frontmatter ∪ ``{'body': …}`` (read-only)."""
    from agora_kb.core import frontmatter

    layout = Repo.resolve(tmp_path).layout
    paths = sorted(layout.inbox_dir.glob("*/*.md"))
    assert len(paths) == 1, f"expected exactly one inbox event, got {paths}"
    fm, body = frontmatter.parse(paths[0].read_text(encoding="utf-8"))
    fm = dict(fm)
    fm["body"] = body
    return fm


def _tiny_pdf(text: str = "Hello Agora PDF") -> bytes:
    """A minimal single-page PDF whose only content is ``text`` (pdfminer-parseable).

    Mirrors the proven construction in ``tests/ingest/test_extractors.py`` — the stream ``/Length``
    is computed from the real content bytes and the text is positioned in-bounds, so pdfminer
    reliably extracts it (a hand-fudged length yields "no extractable text").
    """
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    ]
    stream = b"BT /F1 24 Tf 72 700 Td (" + text.encode("latin-1") + b") Tj ET"
    objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1,
        xref_pos,
    )
    return out
