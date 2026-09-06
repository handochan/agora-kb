"""Web-face e2e for the ``raw/`` provenance drill-down: ``/raw``, ``/api/raw``, linked ``sources:``.

Before this wave a citation was a dead string on every face — accurate provenance nobody could
follow (DRILLDOWN-169 §0). These tests drive the two new routes over a real ``TestClient``, and
they are deliberately weighted towards what serving a capture tier COSTS rather than towards the
happy path: ``raw/`` holds bytes that passed none of the curator's PLAN/APPLY grading, that only
the ``session:`` harvest connector ever redacts, and whose ``media_type``/``<ext>``/``filename``
are all uploader-chosen values (§4 T1/T4/T5).

So the pins here are: an ``.html`` blob is never served as HTML (stored XSS, T1), a text capture is
escaped into a ``<pre>`` and never markdown-rendered (D7), every adversarial spelling of a path is
refused on BOTH routes with no target bytes in the response (T2/T3), a ``.meta.yaml`` sidecar is
not itself citable (D9 / lint L1-8b), an oversize capture is truncated rather than loaded (T9), the
``Content-Disposition`` survives a Korean and a traversal filename (T4), and the two faces agree
byte-for-byte about the same blob — because "the faces disagree about provenance" is the exact
failure #169 exists to prevent.

``build_kb`` (not hand-written YAML) materialises the blob tier: only the builder writes
``raw/_blob/<ab>/<sha256>.<ext>`` and its sidecar in APPLY's own shape, key order included.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agora_kb.core import Repo  # noqa: E402
from agora_kb.core.layout import SIDECAR_SUFFIX, blob_ref  # noqa: E402
from agora_kb.core.rawstore import MAX_RAW_TEXT_BYTES  # noqa: E402
from agora_kb.faces.mcp_server import AgoraHandlers  # noqa: E402
from tests.support.kb_builder import NoteSpec, build_kb  # noqa: E402

#: Real symlinks require an elevated privilege on Windows (the ``tests/core/test_rawstore.py``
#: posture).
posix_symlinks = pytest.mark.skipif(
    os.name != "posix", reason="real symlinks require privilege on Windows"
)

# --- the raw/ corpus ----------------------------------------------------------------------------
TEXT_SOURCE = "raw/ai-tech/e1.md"
TEXT_BODY = "# Extracted\n\nA curator advances one repo at a time.\n"

#: A capture carrying BOTH hazards a rendered page would have: an executable tag, and a pre-flip
#: schema-1 relative link of the kind the owner's own captures are full of (§8 E-1).
XSS_SOURCE = "raw/ai-tech/e2.md"
XSS_BODY = (
    "<script>alert(1)</script>\n\n"
    "See [curator concurrency](../../wiki/concepts/curator-concurrency.md).\n"
)

#: NOT text under any codec: a NUL, a lone ``0xff`` and a CRLF, so a text-mode read or a decode
#: attempt anywhere on the path corrupts it into a visible failure instead of passing by luck.
BLOB_BYTES = b"%PDF-1.7\r\n\x00\xff\xfe binary payload \x00\r\nnot text\n"
BLOB_SHA = hashlib.sha256(BLOB_BYTES).hexdigest()
BLOB_REF = blob_ref(BLOB_SHA, "pdf")
SIDECAR_REF = f"{BLOB_REF}{SIDECAR_SUFFIX}"

#: The capture facts APPLY records beside the bytes, minus the three it derives from the blob
#: itself (``sha256`` / ``ext`` / ``bytes``, which the builder writes).
SIDECAR_FACTS: dict[str, object] = {
    "media_type": "application/pdf",
    "filename": "2026-q3-report.pdf",
    "captured_at": "2026-06-13T02:40:10Z",
    "writer": "dochan",
    "source": "web:dochan",
    "event_id": "01J8ZQ3M4N5P6Q7R8S9T0V1W2X",
}

#: The stored-XSS fixture: an uploader controls the bytes, the ``media_type`` part header AND the
#: extension, so all three say "HTML" and the response must still say octet-stream (§4 T1).
HTML_BYTES = b"<html><body><script>alert('stored xss')</script></body></html>\n"
HTML_SHA = hashlib.sha256(HTML_BYTES).hexdigest()
HTML_REF = blob_ref(HTML_SHA, "html")
HTML_FACTS: dict[str, object] = {"media_type": "text/html", "filename": "note.html"}

#: A hand-written note whose ``sources:`` are what an ADVERSARY (or a hand edit, or a vault import)
#: can put there: lint L1-8 is a bare ``exists()``, and on a journal ``sources:`` is graded by
#: nothing at all, so none of these is hypothetical (§7 R-10).
HOSTILE_NOTE = "wiki/concepts/citations.md"
HOSTILE_SOURCES = [
    "javascript:alert(1)",
    "/etc/hosts",
    "../../../../etc/hosts",
    "harvest:claude-code",
    SIDECAR_REF,
    TEXT_SOURCE,
    BLOB_REF,
]


# --- fixtures -----------------------------------------------------------------------------------
def _repo(
    tmp_path: Path,
    *,
    schema_version: int = 2,
    blobs: list[tuple[str, str, bytes, dict[str, object]]] | None = None,
) -> Path:
    """A KB with the whole ``raw/`` capture tier on disk plus one note that cites into it.

    ``raw/`` is byte-identical on schema 1 and schema 2 (ADR-0041 D1.4), which is what lets
    ``schema_version`` be a parameter here rather than a second fixture.
    """
    build_kb(
        tmp_path,
        [
            NoteSpec(
                kind="theme",
                domain="ai-tech",
                title="Curator Concurrency",
                body="The curator advances one repo at a time under a per-repo lock.",
                slug="curator-concurrency",
                sources=[TEXT_SOURCE],
            )
        ],
        schema_version=schema_version,
        domains=["ai-tech", "general"],
        blobs=blobs if blobs is not None else [(BLOB_SHA, "pdf", BLOB_BYTES, SIDECAR_FACTS)],
    )
    # The builder only materializes a cited source that does not exist yet, so these writes ARE the
    # artifacts' content from every reader's point of view.
    (tmp_path / TEXT_SOURCE).write_text(TEXT_BODY, encoding="utf-8", newline="\n")
    (tmp_path / XSS_SOURCE).write_text(XSS_BODY, encoding="utf-8", newline="\n")
    return tmp_path


def _write_hostile_note(tmp_path: Path, sources: list[str] | None = None) -> None:
    """Add a note whose ``sources:`` mixes servable citations with hostile strings."""
    entries = "\n".join(f"  - {s}" for s in (sources if sources is not None else HOSTILE_SOURCES))
    (tmp_path / HOSTILE_NOTE).write_text(
        "---\nstatus: active\nkind: concept\nsubjects: [ai-tech]\n"
        "title: Citations\nsources:\n" + entries + "\n---\n\n# Citations\n\nBody.\n",
        encoding="utf-8",
    )


def _client(tmp_path: Path) -> TestClient:
    from agora_kb.faces.web import build_app

    app = build_app(repo_path=tmp_path, writer="web", user="alice")
    # Host must match the default web.security.allowed_hosts (loopback only, issue #94).
    return TestClient(app, base_url="http://127.0.0.1")


def _disable_raw(tmp_path: Path) -> None:
    layout = Repo.resolve(tmp_path).layout
    layout.kb_dir.mkdir(parents=True, exist_ok=True)
    (layout.kb_dir / "repo.yaml").write_text(
        "web:\n  features:\n    raw_enabled: false\n", encoding="utf-8"
    )


# --- (1) text: the JSON contract ----------------------------------------------------------------
def test_api_raw_serves_a_text_artifact(tmp_path: Path) -> None:
    """``GET /api/raw/<tail>`` returns the handler payload for the citation ``raw/<tail>``."""
    client = _client(_repo(tmp_path))

    resp = client.get("/api/raw/ai-tech/e1.md")

    assert resp.status_code == 200
    data = resp.json()
    # The stored identity is echoed WHOLE (`raw/…`), even though the URL dropped the prefix (D6):
    # a face that hands back a trimmed path teaches an agent a citation spelling nothing accepts.
    assert data["path"] == TEXT_SOURCE
    assert data["resource"] == "raw"
    assert data["raw_kind"] == "text"
    assert data["text"] == TEXT_BODY
    assert data["bytes"] == len(TEXT_BODY.encode())
    assert data["truncated"] is False


# --- (2) text: the HTML page --------------------------------------------------------------------
def test_raw_page_renders_text_xss_safe(tmp_path: Path) -> None:
    """A capture is ESCAPED into a ``<pre>`` and never markdown-rendered (D7).

    Two properties in one page: a ``<script>`` in an ungraded capture is inert text, and the
    capture's own schema-1 relative link is NOT rewritten into a ``/note/`` href — rendering it
    would enrol uncurated content in the site's link graph, which is the reversibility argument D7
    turns on.
    """
    client = _client(_repo(tmp_path))

    resp = client.get("/raw/ai-tech/e2.md")

    assert resp.status_code == 200
    html = resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)" not in html
    assert "<pre" in html
    # Not markdown: the link is still literal source text, and no /note/ href was synthesized.
    assert "[curator concurrency](../../wiki/concepts/curator-concurrency.md)" in html
    assert "/note/wiki/concepts/curator-concurrency.md" not in html


# --- (3) blob: metadata only, never bytes (D5) --------------------------------------------------
def test_api_raw_blob_returns_meta_and_never_bytes(tmp_path: Path) -> None:
    """The JSON route reports the capture facts and points at the download; it ships no bytes.

    D5 is normative on the MCP side and this route inherits it: there is no base64 channel in this
    codebase, and adding one here would make the undesigned egress control (R1/#166) a
    content-type matrix instead of one surface.
    """
    client = _client(_repo(tmp_path))

    resp = client.get(f"/api/raw/{BLOB_REF[len('raw/') :]}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["raw_kind"] == "blob"
    assert data["path"] == BLOB_REF
    assert data["bytes"] == len(BLOB_BYTES)
    assert data["meta"]["sha256"] == BLOB_SHA
    assert data["meta"]["media_type"] == "application/pdf"
    assert data["download_url"] == f"/raw/_blob/{BLOB_SHA[:2]}/{BLOB_SHA}.pdf"
    # No bytes, in any spelling: not the payload keys a text artifact uses, and nothing in the
    # response body decodes to the blob.
    assert "text" not in data
    assert "body" not in data
    assert "content" not in data
    assert "base64" not in resp.text
    assert BLOB_BYTES[:8].decode("latin-1") not in resp.text


# --- (4) blob: the fixed hardened header set (D8) -----------------------------------------------
def test_raw_blob_downloads_with_hardened_headers(tmp_path: Path) -> None:
    """The bytes come back verbatim under the FIXED header set — none of it typed from metadata."""
    client = _client(_repo(tmp_path))

    resp = client.get(f"/raw/_blob/{BLOB_SHA[:2]}/{BLOB_SHA}.pdf")

    assert resp.status_code == 200
    assert resp.content == BLOB_BYTES
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"].startswith("attachment")
    assert resp.headers["content-length"] == str(len(BLOB_BYTES))
    assert resp.headers["etag"] == f'"{BLOB_SHA}"'
    # `private`, not `public`: the face carries no auth of its own (§4 T7), so a shared/proxy cache
    # must never be authorised to store an ungraded capture; the browser cache is the point.
    assert resp.headers["cache-control"] == "private, max-age=31536000, immutable"
    # The route sets its OWN CSP, and `_SecurityHeadersMiddleware` uses setdefault — so the
    # route's policy would SHADOW the global framing denial if it did not repeat frame-ancestors.
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_an_html_blob_is_never_served_as_html(tmp_path: Path) -> None:
    """Stored-XSS regression pin (§4 T1): media_type AND extension say HTML, the response does not.

    A face-origin HTML document would hold same-origin reads of every ``/api/*`` route and a
    same-origin ``POST /api/upload`` into the append-only, DELETE-less inbox. ``media_type`` is the
    uploader's own multipart part header (``normalize_media_type`` only checks the SHAPE) and
    ``<ext>`` is the last component of the uploader's filename — both are attacker-chosen, so
    neither may type a response.
    """
    client = _client(_repo(tmp_path, blobs=[(HTML_SHA, "html", HTML_BYTES, HTML_FACTS)]))

    resp = client.get(f"/raw/_blob/{HTML_SHA[:2]}/{HTML_SHA}.html")

    assert resp.status_code == 200
    assert resp.content == HTML_BYTES
    assert resp.headers["content-type"] == "application/octet-stream"
    assert "html" not in resp.headers["content-type"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"].startswith("attachment")
    # And the JSON face reports the declared type as DATA without ever acting on it.
    api = client.get(f"/api/raw/_blob/{HTML_SHA[:2]}/{HTML_SHA}.html")
    assert api.json()["meta"]["media_type"] == "text/html"


@pytest.mark.parametrize("prefix", ["_blob", "_BLOB", "_Blob"])
def test_a_blob_is_never_served_as_text_because_of_its_spelling(
    tmp_path: Path, prefix: str
) -> None:
    """The blob classification survives a case-insensitive filesystem (APFS/NTFS, §4 T1 + D5).

    A case-flipped ``_blob`` segment resolves to the real file on APFS and NTFS. Classified
    ``"text"``, that file leaves through the TEXT branch: its bytes land in the JSON payload's
    ``text`` (bypassing D5, which is normative) and the page renders it in ``<pre>`` with none of
    D8's hardening — no ``octet-stream``, no ``attachment``, no route CSP. Either the path 404s
    (case-sensitive filesystem) or it is served as the BLOB it is; nothing in between.
    """
    client = _client(_repo(tmp_path, blobs=[(HTML_SHA, "html", HTML_BYTES, HTML_FACTS)]))
    tail = f"{prefix}/{HTML_SHA[:2]}/{HTML_SHA}.html"

    page = client.get(f"/raw/{tail}")
    api = client.get(f"/api/raw/{tail}")

    assert page.status_code in (200, 404)
    if page.status_code == 200:
        assert page.headers["content-type"] == "application/octet-stream"
        assert page.headers["content-disposition"].startswith("attachment")
    assert api.status_code in (200, 404)
    if api.status_code == 200:
        body = api.json()
        assert body["raw_kind"] == "blob"
        assert "text" not in body
        assert "<script" not in api.text


@pytest.mark.parametrize(
    ("filename", "expect_star"),
    [
        ("2026-q3-report.pdf", True),
        ("보고서 2026.pdf", True),
        # A traversal name has nothing representable left after sanitization → no filename* at all,
        # and certainly no path in the header.
        ("../../etc/passwd", False),
        (None, False),
    ],
)
def test_blob_filename_is_rfc6266_safe(
    tmp_path: Path, filename: str | None, expect_star: bool
) -> None:
    """Content-Disposition always carries the ASCII fallback; the display name is percent-encoded.

    Injection is structurally impossible upstream (``sanitize_attachment_filename`` runs the repo's
    closed Unicode-category allowlist, so CR/LF/``"``/``;``/space cannot survive) — but a Korean
    name DOES survive it, and a bare ``filename=`` with one in it is a latin-1 encode error on the
    wire, which is why both forms are always emitted (§4 T4).
    """
    facts: dict[str, object] = {"media_type": "application/pdf"}
    if filename is not None:
        facts["filename"] = filename
    client = _client(_repo(tmp_path, blobs=[(BLOB_SHA, "pdf", BLOB_BYTES, facts)]))

    resp = client.get(f"/raw/_blob/{BLOB_SHA[:2]}/{BLOB_SHA}.pdf")

    disposition = resp.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="{BLOB_SHA[:12]}.pdf"')
    assert ("filename*=UTF-8''" in disposition) is expect_star
    assert "passwd" not in disposition
    assert "\r" not in disposition and "\n" not in disposition
    disposition.encode("latin-1")  # what starlette does on the wire; must not raise


# --- (5) hostile paths: refused on BOTH routes, with nothing leaked ------------------------------
#: One vector per structural escape. Each is the URL TAIL, i.e. what follows ``/raw/`` — so the
#: citation the handler sees is ``raw/<tail>``, exactly what a hand-edited ``sources:`` can hold.
_HOSTILE_TAILS = (
    "..%2f..%2fetc%2fhosts",  # out of the repo entirely
    "..%2fwiki%2fconcepts%2fcurator-concurrency.md",  # in-tree, but out of raw/ (gate 1)
    "..%2fwiki%2fpeople%2fhando%2fsecret.md",  # the human-owned namespace (ADR-0041 D3.3)
    "_blob%2f..%2f..%2fetc%2fhosts",  # escape from under the blob prefix
    ".%2fai-tech%2f..%2f..%2findex.md",  # non-normalized spelling of an in-tree note
    "%2fetc%2fhosts",  # absolute
    "ai-tech",  # a directory, not a file
    "ai-tech/nope.md",  # simply absent
    # An embedded NUL: starlette decodes %00 into the path param, and CPython's realpath RAISES
    # on it. Without a textual rejection in rawstore's gate 1 this is a 500, not a 404 — the one
    # vector in this table that crashes the route instead of being refused by it.
    "ai-tech%2fe1.md%00.png",
    "a%00b.md",
)


@pytest.mark.parametrize("tail", _HOSTILE_TAILS)
@pytest.mark.parametrize("prefix", ["/raw/", "/api/raw/"])
def test_traversal_and_absolute_and_non_raw_paths_are_refused(
    tmp_path: Path, prefix: str, tail: str
) -> None:
    """Every escape spelling 404s on both routes, and no target content appears in the body."""
    root = _repo(tmp_path)
    person = root / "wiki" / "people" / "hando"
    person.mkdir(parents=True, exist_ok=True)
    (person / "secret.md").write_text("HUMAN OWNED SECRET\n", encoding="utf-8")
    (root.parent / "outside-secret.md").write_text("TOP SECRET OUTSIDE\n", encoding="utf-8")
    client = _client(root)

    resp = client.get(f"{prefix}{tail}")

    assert resp.status_code == 404, tail
    assert "HUMAN OWNED SECRET" not in resp.text
    assert "TOP SECRET OUTSIDE" not in resp.text
    assert "root:" not in resp.text  # /etc/hosts / /etc/passwd content shapes
    assert "per-repo lock" not in resp.text  # the wiki note's body, reached via ../


def test_a_nul_bearing_citation_does_not_500_the_note_page(tmp_path: Path) -> None:
    """The NUL vector is reachable from STORED DATA, not just from a URL — and must not crash.

    ``_source_rows`` resolves every ``sources:`` entry on the pre-existing ``/note`` page, and a
    YAML double-quoted ``"raw/a\\0b.md"`` yields a real NUL. A note whose page rendered fine before
    this wave must keep rendering: an ungraded ``sources:`` (``_is_sourced_kind`` never checks a
    journal's at all) is exactly the population D12's predicate has to survive.
    """
    root = _repo(tmp_path)
    (root / HOSTILE_NOTE).write_text(
        "---\nstatus: active\nkind: concept\nsubjects: [ai-tech]\n"
        'title: Citations\nsources:\n  - "raw/a\\0b.md"\n  - ' + TEXT_SOURCE + "\n"
        "---\n\n# Citations\n\nBody.\n",
        encoding="utf-8",
    )
    client = _client(root)

    resp = client.get(f"/note/{HOSTILE_NOTE}")

    assert resp.status_code == 200
    # The servable neighbour still links; the NUL row stays plain text rather than taking the page.
    assert f'href="/raw/{TEXT_SOURCE[len("raw/") :]}"' in resp.text


@posix_symlinks
def test_the_raw_directory_being_a_symlink_is_refused_on_both_routes(tmp_path: Path) -> None:
    """§4 T3, the variant gate 2 cannot see: ``raw/`` ITSELF replaced by a link to another tree.

    Containment compares ``candidate.resolve()`` against ``raw_dir.resolve()``, and both follow the
    same link, so every path under the target "contains". Git stores symlinks, so a contributor to
    a hub repo an operator later serves can ship this shape.
    """
    root = _repo(tmp_path)
    outside = root.parent / "outside-raw-target"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "passwd.md").write_text("SECRET OUTSIDE THE REPO\n", encoding="utf-8")

    real_raw = root / "raw"
    for path in sorted(real_raw.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    real_raw.rmdir()
    real_raw.symlink_to(outside, target_is_directory=True)
    client = _client(root)

    for url in ("/raw/passwd.md", "/api/raw/passwd.md"):
        resp = client.get(url)
        assert resp.status_code == 404, url
        assert "SECRET OUTSIDE THE REPO" not in resp.text, url


@posix_symlinks
def test_symlink_out_of_the_repo_is_refused(tmp_path: Path) -> None:
    """A link INSIDE ``raw/`` pointing outside it is refused by identity, not graded by target."""
    root = _repo(tmp_path)
    secret = root.parent / "outside-secret.md"
    secret.write_text("TOP SECRET OUTSIDE THE REPO\n", encoding="utf-8")
    (root / "raw" / "ai-tech" / "link.md").symlink_to(secret)
    client = _client(root)

    for url in ("/raw/ai-tech/link.md", "/api/raw/ai-tech/link.md"):
        resp = client.get(url)
        assert resp.status_code == 404, url
        assert "TOP SECRET" not in resp.text, url


# --- (6) the sidecar is not an artifact (D9 / lint L1-8b) ---------------------------------------
@pytest.mark.parametrize("suffix", [SIDECAR_SUFFIX, ".META.YAML", ".Meta.Yaml"])
def test_sidecar_path_is_not_directly_servable(tmp_path: Path, suffix: str) -> None:
    """A ``*.meta.yaml`` 404s with the rule it teaches — while its fields ride on the blob.

    Parametrized over SPELLING because the filesystem may not care: on APFS/NTFS a case-flipped
    suffix resolves to the very same sidecar, and a case-sensitive classifier would call it a blob
    and stream it as a download — D9 undone by capitalisation. On a case-sensitive filesystem the
    flipped spellings simply do not exist, which is also a 404, so the assertion holds either way.
    """
    client = _client(_repo(tmp_path))
    tail = f"{BLOB_REF}{suffix}"[len("raw/") :]

    page = client.get(f"/raw/{tail}")
    api = client.get(f"/api/raw/{tail}")

    assert page.status_code == 404
    assert api.status_code == 404
    assert "sha256:" not in page.text  # never the sidecar's YAML, under any spelling
    if suffix != SIDECAR_SUFFIX:
        return
    # The dead end teaches the rule and names the artifact instead of merely refusing.
    assert "L1-8b" in api.json()["detail"]
    assert BLOB_REF in api.json()["detail"]
    # …and every field the sidecar holds IS reachable, through the blob it describes.
    blob = client.get(f"/api/raw/{BLOB_REF[len('raw/') :]}").json()
    assert blob["meta"]["event_id"] == SIDECAR_FACTS["event_id"]


def test_a_missing_raw_path_is_404_not_500(tmp_path: Path) -> None:
    """An absent capture renders the not-found PAGE (the note_page posture), never a traceback."""
    client = _client(_repo(tmp_path))

    resp = client.get("/raw/ai-tech/gone.md")

    assert resp.status_code == 404
    assert "Capture not found" in resp.text
    assert "raw/ai-tech/gone.md" in resp.text


def test_the_not_found_page_escapes_the_requested_path(tmp_path: Path) -> None:
    """The 404 page ECHOES the path, so the echo must be autoescaped, not reflected as markup."""
    client = _client(_repo(tmp_path))

    resp = client.get("/raw/ai-tech/%3Cscript%3Ealert(1)%3C/script%3E.md")

    assert resp.status_code == 404
    assert "<script>alert(1)" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_a_non_ascii_citation_round_trips_through_the_url(tmp_path: Path) -> None:
    """``sources:`` → href → route → the SAME citation: the D6 conversion is lossless.

    Korean filenames are ordinary here (#56/#57), and the href escapes both the space and the
    non-ASCII bytes — so this pins that the escaping is reversed by the router rather than
    reaching ``rawstore`` percent-encoded, which would resolve to nothing.
    """
    root = _repo(tmp_path)
    citation = "raw/general/보고서 2026.md"
    (root / citation).parent.mkdir(parents=True, exist_ok=True)
    (root / citation).write_text("# 보고서\n\n본문.\n", encoding="utf-8")
    _write_hostile_note(root, sources=[citation])
    client = _client(root)

    note = client.get(f"/note/{HOSTILE_NOTE}").text
    href = "/raw/general/%EB%B3%B4%EA%B3%A0%EC%84%9C%202026.md"
    assert f'href="{href}"' in note

    resp = client.get(href)
    assert resp.status_code == 200
    assert "본문." in resp.text


# --- (7) resource bounds (§4 T9) ----------------------------------------------------------------
def test_oversize_text_is_not_rendered_into_memory(tmp_path: Path) -> None:
    """A capture past ``MAX_RAW_TEXT_BYTES`` comes back truncated AND says so.

    The 25 MiB cap is a WRITE-side control; before this the read side had none. Truncation is
    honest rather than silent so a reader is never shown a clipped document as a whole one.
    """
    root = _repo(tmp_path)
    big = "x" * (MAX_RAW_TEXT_BYTES + 4096)
    (root / "raw" / "ai-tech" / "big.md").write_text(big, encoding="utf-8", newline="\n")
    client = _client(root)

    data = client.get("/api/raw/ai-tech/big.md").json()

    assert data["truncated"] is True
    assert len(data["text"].encode("utf-8")) <= MAX_RAW_TEXT_BYTES
    assert data["bytes"] == len(big)  # the TRUE size, not the served length
    # The PAGE is smaller than the artifact it shows: the whole capture never reached the template.
    page = client.get("/raw/ai-tech/big.md")
    assert page.status_code == 200
    assert len(page.content) < len(big)
    assert f"Showing the beginning of this {len(big)}-byte capture" in page.text


# --- (8) the operator kill switch (D11) ---------------------------------------------------------
def test_raw_feature_flag_off_404s_both_routes_and_unlinks_sources(tmp_path: Path) -> None:
    """``raw_enabled: false`` closes the routes AND stops the note page offering the links."""
    root = _repo(tmp_path)
    _write_hostile_note(root)
    _disable_raw(root)
    client = _client(root)

    assert client.get("/raw/ai-tech/e1.md").status_code == 404
    assert client.get("/api/raw/ai-tech/e1.md").status_code == 404
    note = client.get(f"/note/{HOSTILE_NOTE}")
    assert note.status_code == 200
    # The citation is still SHOWN — provenance is not the thing being switched off — but nothing
    # on the page points at a route that would 404.
    assert TEXT_SOURCE in note.text
    assert 'href="/raw/' not in note.text


def test_raw_feature_flag_off_leaves_a_body_citation_verbatim(tmp_path: Path) -> None:
    """With the feature off a body link into ``raw/`` is NOT rewritten — not even to /note/."""
    root = _repo(tmp_path)
    (root / "wiki" / "concepts" / "cited.md").write_text(
        "---\nstatus: active\nkind: concept\nsubjects: [ai-tech]\ntitle: Cited\n---\n\n"
        "# Cited\n\nSee [the capture](../../raw/ai-tech/e1.md).\n",
        encoding="utf-8",
    )
    _disable_raw(root)
    client = _client(root)

    html = client.get("/note/wiki/concepts/cited.md").text

    assert 'href="/raw/ai-tech/e1.md"' not in html
    assert "../../raw/ai-tech/e1.md" in html


# --- (9) sources: linkification is server-computed (D12) ----------------------------------------
def test_note_page_links_only_servable_sources(tmp_path: Path) -> None:
    """A hostile ``sources:`` entry renders as plain text; a real citation renders as a link.

    The predicate is the route's own (``rawstore.resolve`` and not a sidecar), so a link that is
    OFFERED always opens — and ``javascript:``/absolute/traversal/``harvest:`` strings keep
    rendering exactly as the escaped ``<li>`` text they are today (§7 R-9/R-10).
    """
    root = _repo(tmp_path)
    _write_hostile_note(root)
    client = _client(root)

    html = client.get(f"/note/{HOSTILE_NOTE}").text

    assert 'href="/raw/ai-tech/e1.md"' in html
    assert f'href="/raw/_blob/{BLOB_SHA[:2]}/{BLOB_SHA}.pdf" download' in html
    # Never linked: no href is synthesized for any of these, in any spelling.
    assert "javascript:alert(1)" in html  # shown as text…
    assert 'href="javascript' not in html  # …never as a target
    assert 'href="/etc/hosts"' not in html
    assert 'href="/raw/../' not in html
    assert "harvest:claude-code" in html  # shown…
    assert 'href="/raw/harvest' not in html  # …never linked
    # The sidecar is excluded by the same predicate the route enforces (L1-8b).
    assert SIDECAR_REF in html
    assert f'href="/raw/_blob/{BLOB_SHA[:2]}/{BLOB_SHA}.pdf.meta.yaml"' not in html


def test_note_page_escapes_a_hostile_source_string(tmp_path: Path) -> None:
    """An HTML-bearing ``sources:`` entry stays autoescaped text — the row is never marked safe."""
    root = _repo(tmp_path)
    _write_hostile_note(root, sources=["'><script>alert(1)</script>", TEXT_SOURCE])
    client = _client(root)

    html = client.get(f"/note/{HOSTILE_NOTE}").text

    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_a_scalar_sources_value_is_shown_unlinked_not_dropped(tmp_path: Path) -> None:
    """A hand-written ``sources: raw/…`` (a scalar, not a list) still appears on the page.

    The Metadata disclosure and the Sources term now key on the SAME server-computed rows, so an
    entry that produces no link must still produce a row — otherwise the disclosure opens onto
    nothing and the read face is quieter about the note than the file it is showing. Hand-edited
    and imported notes are exactly the ungraded population D12 is written for.
    """
    root = _repo(tmp_path)
    (root / HOSTILE_NOTE).write_text(
        "---\nstatus: active\nkind: concept\nsubjects: [ai-tech]\n"
        f"title: Citations\nsources: {TEXT_SOURCE}\n---\n\n# Citations\n\nBody.\n",
        encoding="utf-8",
    )
    client = _client(root)

    html = client.get(f"/note/{HOSTILE_NOTE}").text

    assert "<dt>Sources</dt>" in html
    assert TEXT_SOURCE in html
    assert f'href="/raw/{TEXT_SOURCE[len("raw/") :]}"' not in html  # a scalar is never linkified


def test_a_note_with_no_metadata_renders_no_empty_disclosure(tmp_path: Path) -> None:
    """The converse pin: the gate keys on the computed rows, so it never opens onto nothing."""
    root = _repo(tmp_path)
    (root / HOSTILE_NOTE).write_text(
        "---\nstatus: active\nkind: concept\nsubjects: [ai-tech]\n"
        "title: Citations\n---\n\n# Citations\n\nBody.\n",
        encoding="utf-8",
    )
    client = _client(root)

    html = client.get(f"/note/{HOSTILE_NOTE}").text

    assert '<details class="fm-meta">' not in html


# --- (10) schema independence + cross-face parity ------------------------------------------------
def test_raw_routes_serve_a_schema_1_repo(tmp_path: Path) -> None:
    """``raw/`` never moved (ADR-0041 D1.4), so a read-only schema-1 repo drills down too."""
    client = _client(_repo(tmp_path, schema_version=1))

    text = client.get("/raw/ai-tech/e1.md")
    blob = client.get(f"/raw/_blob/{BLOB_SHA[:2]}/{BLOB_SHA}.pdf")

    assert text.status_code == 200
    assert TEXT_BODY.strip().splitlines()[0].lstrip("# ") in text.text
    assert blob.status_code == 200
    assert blob.content == BLOB_BYTES


def test_cross_face_parity_for_one_blob(tmp_path: Path) -> None:
    """Both faces describe the SAME blob identically — the failure #169 exists to prevent.

    ``AgoraHandlers.raw`` is the seam ``kb_read`` wraps, so this compares what an agent is told
    with what a browser is handed: the recorded size against ``Content-Length``, and the recorded
    digest against the ``ETag`` (which is taken from the content-addressed basename, never from
    the sidecar).
    """
    root = _repo(tmp_path)
    payload = AgoraHandlers(Repo.resolve(root)).raw(BLOB_REF)
    client = _client(root)

    resp = client.get(f"/raw/_blob/{BLOB_SHA[:2]}/{BLOB_SHA}.pdf")

    assert payload["status"] == "ok"
    meta = payload["meta"]
    assert isinstance(meta, dict)
    assert str(meta["bytes"]) == resp.headers["content-length"]
    assert meta["sha256"] == resp.headers["etag"].strip('"')
    assert payload["bytes"] == len(resp.content)


def test_the_blob_note_url_is_the_one_composer_not_a_second_spelling(tmp_path: Path) -> None:
    """D6 pin: the URL in the shared seam's ``note`` comes from ``_raw_href``, not from ``f"/{p}"``.

    The seam is shared — ``/api/raw`` and ``agora read`` return this same sentence — so a
    hand-rolled second spelling of "drop one segment, percent-encode the rest" would agree only by
    luck and diverge the first time a captured path needs escaping. Asserted against the web face's
    own composer so the two sites cannot drift apart silently.
    """
    from agora_kb.faces.web.app import _raw_href

    root = _repo(tmp_path)
    payload = AgoraHandlers(Repo.resolve(root)).raw(BLOB_REF)
    note = str(payload["note"])

    href = _raw_href(str(payload["path"]))
    assert href in note
    # …and the JSON route's download_url is that same string, so agent and browser agree.
    api = _client(root).get(f"/api/raw/{BLOB_REF[len('raw/') :]}").json()
    assert api["download_url"] == href
    assert api["note"] == note
