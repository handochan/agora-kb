"""Web-face e2e for per-user identity threading behind a trusted reverse proxy (issue #67).

Mirrors ``tests/faces/test_web.py``'s fixtures over a :class:`fastapi.testclient.TestClient` on a
small temp repo. Locks the trust-boundary contract of ``web.identity.trusted_header``
(ADR-0025 appendix):

- default (``trusted_header`` unset) → every request header is IGNORED; the process ``--user`` is
  stamped byte-identically (the opt-in lock: a client-forgeable header must never steer provenance);
- configured + header present → the header value is the stamped ``web:<user>`` on the REAL inbox
  event, and the receipt carries ``identity_source: "header"``;
- configured + header absent → process-``--user`` fallback (``identity_source: "process"``);
- configured + present-but-INVALID value (empty / spaces / traversal-ish / overlong / newline /
  non-ASCII) → 400 and NOTHING is written (forgery attempt or proxy misconfig — silent fallback
  would poison provenance);
- the batch and HTMX write paths behave identically (one request = one resolved identity);
- ``strip_domain`` truncates an email-form value at the first ``@`` before validation.

The injection edge cases (newline, whitespace-only, UTF-8 mojibake) are exercised at the
:func:`_resolve_upload_user` helper level over a hand-built ASGI scope — the HTTP client stack
(httpx/h11) would otherwise reject or normalize them before the app ever saw the value, which is
exactly why the helper must not rely on that happening.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agora_kb.core import Inbox, Repo  # noqa: E402


# --- fixtures (mirror test_web.py / test_web_hardening.py) --------------------------------------
def _init_repo(tmp_path: Path) -> Repo:
    repo = Repo.resolve(tmp_path)
    repo.init()
    return repo


def _write_repo_yaml(tmp_path: Path, text: str) -> None:
    layout = Repo.resolve(tmp_path).layout
    layout.kb_dir.mkdir(parents=True, exist_ok=True)
    (layout.kb_dir / "repo.yaml").write_text(text, encoding="utf-8")


_IDENTITY_YAML = "web:\n  identity:\n    trusted_header: X-Remote-User\n"


def _client(tmp_path: Path, *, user: str = "alice") -> TestClient:
    from agora_kb.faces.web import build_app

    app = build_app(repo_path=tmp_path, writer="web", user=user)
    return TestClient(app)


def _depth(tmp_path: Path) -> int:
    return Inbox(Repo.resolve(tmp_path).layout).depth()


def _inbox_items(tmp_path: Path) -> list[dict[str, object]]:
    """Every inbox event's parsed frontmatter ∪ ``{'body': …}``, sorted by path (read-only)."""
    from agora_kb.core import frontmatter

    layout = Repo.resolve(tmp_path).layout
    items: list[dict[str, object]] = []
    for path in sorted(layout.inbox_dir.glob("*/*.md")):
        fm, body = frontmatter.parse(path.read_text(encoding="utf-8"))
        item = dict(fm)
        item["body"] = body
        items.append(item)
    return items


def _scope_request(header_value: bytes | None) -> fastapi.Request:
    """A bare starlette Request over a hand-built ASGI scope (bypasses client-side normalizing)."""
    from fastapi import Request

    headers = [] if header_value is None else [(b"x-remote-user", header_value)]
    return Request({"type": "http", "method": "POST", "path": "/api/upload", "headers": headers})


# --- (a) default config: the header is fully ignored (the opt-in lock) --------------------------
def test_default_config_ignores_identity_header(tmp_path: Path) -> None:
    """Without web.identity.trusted_header a spoofed header changes NOTHING — web:<--user> stamps.

    The byte-identical lock: the inbox event written under a spoofed header carries the process
    user, identity_source stays "process", and the forged name appears nowhere in the event.
    """
    _init_repo(tmp_path)
    client = _client(tmp_path, user="alice")

    resp = client.post(
        "/api/upload",
        data={"text": "A finding worth remembering."},
        headers={"X-Remote-User": "mallory"},
    )
    assert resp.status_code == 200
    receipt = resp.json()
    assert receipt["queued"] is True
    assert receipt["identity_source"] == "process"

    (item,) = _inbox_items(tmp_path)
    assert item["source"] == "web:alice"
    # The forged identity leaked nowhere into the persisted event (frontmatter or body).
    layout = Repo.resolve(tmp_path).layout
    (event_path,) = sorted(layout.inbox_dir.glob("*/*.md"))
    assert "mallory" not in event_path.read_text(encoding="utf-8")


# --- (b) opt-in + header present → per-request web:<user> stamp ---------------------------------
def test_trusted_header_stamps_per_request_user(tmp_path: Path) -> None:
    """With trusted_header configured, the header value becomes the REAL inbox source stamp."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, _IDENTITY_YAML)
    client = _client(tmp_path, user="local")

    resp = client.post(
        "/api/upload",
        data={"text": "Bob's team finding."},
        headers={"X-Remote-User": "bob"},
    )
    assert resp.status_code == 200
    assert resp.json()["identity_source"] == "header"

    (item,) = _inbox_items(tmp_path)
    assert item["source"] == "web:bob"


def test_trusted_header_lookup_is_case_insensitive(tmp_path: Path) -> None:
    """HTTP header names are case-insensitive — a lowercase-sent header still resolves."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, _IDENTITY_YAML)
    client = _client(tmp_path, user="local")

    resp = client.post(
        "/api/upload",
        data={"text": "case test"},
        headers={"x-remote-user": "carol"},
    )
    assert resp.status_code == 200
    (item,) = _inbox_items(tmp_path)
    assert item["source"] == "web:carol"


# --- (c) opt-in + header absent → process-user fallback -----------------------------------------
def test_trusted_header_absent_falls_back_to_process_user(tmp_path: Path) -> None:
    """A proxied deployment where the header is missing keeps the --user fallback stamp."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, _IDENTITY_YAML)
    client = _client(tmp_path, user="alice")

    resp = client.post("/api/upload", data={"text": "no header on this one"})
    assert resp.status_code == 200
    assert resp.json()["identity_source"] == "process"

    (item,) = _inbox_items(tmp_path)
    assert item["source"] == "web:alice"


# --- (d) present-but-invalid header value → 400, nothing written --------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "",  # present-but-empty: the proxy authenticated NOBODY — refuse, don't guess
        "a b",  # inner whitespace never appears in a proxy-auth username
        "../etc",  # traversal-ish: leading char must be alphanumeric
        "-flag",  # leading '-' (mirrors core.models._TEAM_RE's leading-alnum rule)
        "a" * 129,  # overlong (> _REMOTE_USER_MAX_LEN)
    ],
)
def test_invalid_header_value_is_400_and_writes_nothing(tmp_path: Path, bad: str) -> None:
    """An invalid identity value is REFUSED (400) — never a silent fallback, never a write."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, _IDENTITY_YAML)
    client = _client(tmp_path, user="alice")
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload",
        data={"text": "should never be queued"},
        headers={"X-Remote-User": bad},
    )
    assert resp.status_code == 400, bad
    assert "identity header" in resp.json()["detail"]
    assert _depth(tmp_path) == before  # the inbox saw nothing


def test_duplicate_identity_header_is_400_and_writes_nothing(tmp_path: Path) -> None:
    """TWO occurrences of the identity header → 400, zero writes (append-mode proxy defense).

    An append-mode proxy (Apache ``RequestHeader append``, HAProxy ``add-header``) leaves the
    client's forged copy FIRST — a first-wins ``headers.get`` would stamp the forgery over the
    proxy-authenticated value. Duplicates are refused outright: they are exactly the
    proxy-misconfiguration signal the 400 policy exists to surface loudly.
    """
    import httpx

    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, _IDENTITY_YAML)
    client = _client(tmp_path, user="local")
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload",
        data={"text": "smuggling attempt"},
        headers=httpx.Headers([("X-Remote-User", "mallory"), ("X-Remote-User", "alice")]),
    )
    assert resp.status_code == 400
    assert "duplicate identity header" in resp.json()["detail"]
    assert _depth(tmp_path) == before  # neither the forged nor the real identity wrote anything


def test_duplicate_identity_header_rejected_at_the_helper() -> None:
    """Helper-level lock of the duplicate rule over a raw ASGI scope (mixed-case occurrences)."""
    from fastapi import HTTPException, Request

    from agora_kb.config import WebIdentityConfig
    from agora_kb.faces.web.app import _resolve_upload_user

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/upload",
            # HTTP header names are case-insensitive: both spellings are the SAME header.
            "headers": [(b"x-remote-user", b"mallory"), (b"x-remote-user", b"alice")],
        }
    )
    identity = WebIdentityConfig(trusted_header="X-Remote-User")
    with pytest.raises(HTTPException) as exc_info:
        _resolve_upload_user(request, identity=identity, process_user="local")
    assert exc_info.value.status_code == 400


def test_injection_shaped_values_rejected_at_the_helper() -> None:
    """Newline/CR injection, whitespace-only, and mojibake bytes all raise 400 at the seam.

    Exercised over a raw ASGI scope: client stacks may normalize/refuse these before the app sees
    them, but the helper must reject them on its own (defense in depth for exotic proxies).
    """
    from fastapi import HTTPException

    from agora_kb.config import WebIdentityConfig
    from agora_kb.faces.web.app import _resolve_upload_user

    identity = WebIdentityConfig(trusted_header="X-Remote-User")
    # b"u\xffser" = latin-1 non-ASCII (httpx refuses to SEND non-ASCII str values, but a raw
    # socket/exotic proxy could); the UTF-8 한글 bytes decode to latin-1 mojibake server-side.
    for raw in (b"a\nb", b"a\rb", b"   ", b"\t", b"u\xffser", "한글".encode()):
        with pytest.raises(HTTPException) as exc_info:
            _resolve_upload_user(_scope_request(raw), identity=identity, process_user="alice")
        assert exc_info.value.status_code == 400, raw


def test_helper_decision_table_without_header() -> None:
    """Helper-level lock of the two fallback rows: feature off, and configured-but-absent."""
    from agora_kb.config import WebIdentityConfig
    from agora_kb.faces.web.app import _resolve_upload_user

    off = WebIdentityConfig()
    assert _resolve_upload_user(_scope_request(b"mallory"), identity=off, process_user="alice") == (
        "alice",
        "process",
    )

    on = WebIdentityConfig(trusted_header="X-Remote-User")
    assert _resolve_upload_user(_scope_request(None), identity=on, process_user="alice") == (
        "alice",
        "process",
    )


# --- (e) the batch + HTMX write paths behave identically ----------------------------------------
def test_batch_stamps_every_event_with_header_user(tmp_path: Path) -> None:
    """/api/upload-batch: one request = one identity — every file's event carries web:<header>."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, _IDENTITY_YAML)
    client = _client(tmp_path, user="local")

    resp = client.post(
        "/api/upload-batch",
        files=[
            ("files", ("a.md", b"# Note A\n", "text/markdown")),
            ("files", ("b.txt", b"finding b\n", "text/plain")),
        ],
        headers={"X-Remote-User": "dana"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["identity_source"] == "header"
    assert all(r["queued"] for r in payload["results"])

    items = _inbox_items(tmp_path)
    assert len(items) == 2
    assert {item["source"] for item in items} == {"web:dana"}


def test_batch_invalid_header_is_400_before_any_write(tmp_path: Path) -> None:
    """An invalid identity on the batch path refuses the WHOLE batch up-front — zero writes."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, _IDENTITY_YAML)
    client = _client(tmp_path, user="local")
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload-batch",
        files=[("files", ("a.md", b"# Note A\n", "text/markdown"))],
        headers={"X-Remote-User": "no good"},
    )
    assert resp.status_code == 400
    assert _depth(tmp_path) == before


def test_htmx_upload_path_threads_header_identity(tmp_path: Path) -> None:
    """The HTMX POST /upload form path stamps the same per-request identity as the JSON API."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, _IDENTITY_YAML)
    client = _client(tmp_path, user="local")

    resp = client.post(
        "/upload",
        data={"text": "form capture"},
        headers={"X-Remote-User": "erin"},
    )
    assert resp.status_code == 200
    (item,) = _inbox_items(tmp_path)
    assert item["source"] == "web:erin"
    assert "web:erin" not in resp.text  # the source stays internal, not leaked into the UI


# --- (f) strip_domain -----------------------------------------------------------------------------
def test_strip_domain_truncates_email_form(tmp_path: Path) -> None:
    """strip_domain: true → alice@example.com stamps web:alice (validated AFTER the strip)."""
    _init_repo(tmp_path)
    _write_repo_yaml(
        tmp_path,
        "web:\n  identity:\n    trusted_header: X-Remote-User\n    strip_domain: true\n",
    )
    client = _client(tmp_path, user="local")

    resp = client.post(
        "/api/upload",
        data={"text": "email-form identity"},
        headers={"X-Remote-User": "alice@example.com"},
    )
    assert resp.status_code == 200
    (item,) = _inbox_items(tmp_path)
    assert item["source"] == "web:alice"


def test_no_strip_domain_keeps_email_form(tmp_path: Path) -> None:
    """strip_domain default (false) keeps the full email-form value ('@' is in the charset)."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, _IDENTITY_YAML)
    client = _client(tmp_path, user="local")

    resp = client.post(
        "/api/upload",
        data={"text": "full email identity"},
        headers={"X-Remote-User": "alice@example.com"},
    )
    assert resp.status_code == 200
    (item,) = _inbox_items(tmp_path)
    assert item["source"] == "web:alice@example.com"


def test_strip_domain_bare_domain_is_400(tmp_path: Path) -> None:
    """'@example.com' strips to an EMPTY local part → 400 (never an empty web: stamp)."""
    _init_repo(tmp_path)
    _write_repo_yaml(
        tmp_path,
        "web:\n  identity:\n    trusted_header: X-Remote-User\n    strip_domain: true\n",
    )
    client = _client(tmp_path, user="local")
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload",
        data={"text": "nope"},
        headers={"X-Remote-User": "@example.com"},
    )
    assert resp.status_code == 400
    assert _depth(tmp_path) == before
