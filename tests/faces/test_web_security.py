"""Web-face e2e for the browser-mediated attack defenses (issue #94, ADR-0025 appendix).

The ``127.0.0.1`` bind is a NETWORK boundary, and a browser walks straight through it. Three
guards:

- **DNS rebinding** (whole-KB read): a TTL-0 attacker domain rebound to loopback would give an
  attacker page same-origin reads of every read route. The rebound request still carries the
  attacker's DNS name in ``Host``, so the ``web.security.allowed_hosts`` gate answers 400.
- **CSRF** (inbox injection): ``POST /api/upload`` is multipart = a CORS "simple request", so a
  cross-site auto-submitting form reaches it with no preflight and the WRITE lands in the
  append-only, undeletable inbox. ``_OriginGuardMiddleware`` answers 403 unless the
  ``Origin``/``Referer`` authority IS the request's own ``Host`` — and, critically, the inbox depth
  must be UNCHANGED (the rejection happens in ASGI middleware, before the body is parsed and
  before any append). The baseline is deliberately NOT the allowlist: that list carries entries
  (hub-local loopback for health checks, subdomain wildcards) that must never become trusted write
  origins.
- **Clickjacking** (framed UI): every response denies framing, since a click inside an iframe
  submits with the face's own origin and the CSRF guard cannot tell it apart.

The absent-``Origin`` policy is deliberately two-sided and both sides are locked here: allowed by
default (the documented upload ``curl`` procedures in ``deploy/README.md`` /
``docs/DEPLOY-TEAM.md`` send no ``Origin``), refused under ``web.security.require_origin: true``.

Every ``TestClient`` in the suite pins ``base_url="http://127.0.0.1"``: the default allowlist
deliberately does NOT carry starlette's ``testserver`` — a production default must never ship a
bypass host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agora_kb.core import Inbox, Repo  # noqa: E402
from agora_kb.core.gold import build_gold  # noqa: E402

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

GEN_AT = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)

# The read routes an attacker page would exfiltrate through after a successful rebind.
_READ_ROUTES = (
    "/api/notes",
    "/api/notes/wiki/ai-tech/themes/curator-concurrency.md",
    "/api/search?q=curator",
    "/api/gold/default",
    "/metrics",
    "/dashboard",
)

THEME_MD = (
    "---\ntitle: Curator Concurrency\ntype: theme\naliases: []\ntags: []\n"
    "created: '2026-06-01'\nupdated: '2026-07-01'\nstatus: active\n"
    "summary: single-writer CAS keeps the wiki consistent\nsources: [raw/a.md]\n"
    "related: []\nconfidence: high\n---\n\n# Curator Concurrency\n\nbody\n"
)


# --- fixtures (mirror test_web_hardening.py / test_gold_consumption.py) --------------------------
def _git(root: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_DATE": "2026-07-26T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-07-26T00:00:00+00:00",
    }
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=env)


def _init_repo(tmp_path: Path) -> Repo:
    repo = Repo.resolve(tmp_path)
    repo.init()
    return repo


def _full_repo(tmp_path: Path) -> Repo:
    """A repo with one COMMITTED theme and the default gold pack BUILT.

    Both are needed so ``/api/notes/{path}`` and ``/api/gold/{pack}`` can answer 200 — the point of
    the Host tests is that a loopback Host reaches the real payload while an attacker Host does
    not, which a 404-vs-400 comparison would state only weakly.
    """
    repo = _init_repo(tmp_path)
    themes = tmp_path / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "curator-concurrency.md").write_text(THEME_MD, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "theme")
    build_gold(repo, generated_at=GEN_AT)
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


# --- (a) Host allowlist — the DNS-rebinding defense ----------------------------------------------
@requires_git
def test_rebound_host_is_refused_on_every_read_route(tmp_path: Path) -> None:
    """``Host: evil.com`` → 400 on every read surface a rebind would exfiltrate."""
    pytest.importorskip("prometheus_client")
    _full_repo(tmp_path)
    client = _client(tmp_path)

    for route in _READ_ROUTES:
        resp = client.get(route, headers={"Host": "evil.com"})
        assert resp.status_code == 400, route
        # The refusal must be diagnosable: upstream's bare "Invalid host header" names neither the
        # product nor the knob, so an operator who just put a proxy in front cannot tell whether
        # the 400 came from the proxy or the app. Never echo the attacker-supplied Host, though.
        assert "web.security.allowed_hosts" in resp.text, route
        assert "issue #94" in resp.text, route
        assert "evil.com" not in resp.text, route


@requires_git
@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "127.0.0.1", "localhost"])
def test_loopback_hosts_reach_every_read_route(tmp_path: Path, host: str) -> None:
    """The default allowlist accepts both loopback spellings, with or without a port.

    The port is stripped by ``TrustedHostMiddleware`` before matching (``host.split(":")[0]``), so
    a portless allowlist entry covers every port the operator binds.
    """
    pytest.importorskip("prometheus_client")
    _full_repo(tmp_path)
    client = _client(tmp_path)

    for route in _READ_ROUTES:
        resp = client.get(route, headers={"Host": host})
        assert resp.status_code == 200, f"{host} {route}"


def test_operator_can_allow_a_public_host_and_a_subdomain_wildcard(tmp_path: Path) -> None:
    """A proxied deployment adds its PUBLIC host (the ``docs/DEPLOY-TEAM.md`` §2 standard)."""
    _init_repo(tmp_path)
    _write_repo_yaml(
        tmp_path,
        "web:\n  security:\n    allowed_hosts: [kb.example.com, '*.team.example.com']\n",
    )
    client = _client(tmp_path)

    assert client.get("/api/status", headers={"Host": "kb.example.com"}).status_code == 200
    assert client.get("/api/status", headers={"Host": "a.team.example.com"}).status_code == 200
    # The loopback default is REPLACED, not extended — an explicit list is exactly what it says.
    assert client.get("/api/status", headers={"Host": "127.0.0.1"}).status_code == 400
    assert client.get("/api/status", headers={"Host": "evil.com"}).status_code == 400


def test_ipv6_literal_host_is_refused_and_unconfigurable(tmp_path: Path) -> None:
    """IPv6-literal binds are an EXPLICITLY unsupported posture, consistent in code and docs.

    ``TrustedHostMiddleware`` matches on ``Host.split(":")[0]``, which mangles ``[::1]:8000`` into
    ``"["`` — no pattern can ever match it. Rather than reimplement Host matching or plant a
    synthetic bypass entry, the posture is declared: the request is refused, and an operator who
    reaches for the obvious fix (adding ``::1`` to the allowlist) gets a ConfigError that names the
    limitation and the workaround instead of a silent no-op.
    """
    from agora_kb.config import ConfigError, load_web_config

    _init_repo(tmp_path)
    client = _client(tmp_path)
    assert client.get("/api/status", headers={"Host": "[::1]:8000"}).status_code == 400

    _write_repo_yaml(tmp_path, "web:\n  security:\n    allowed_hosts: ['::1']\n")
    with pytest.raises(ConfigError, match="IPv6 literals"):
        load_web_config(Repo.resolve(tmp_path).layout)


# --- (b) Origin/Referer — the CSRF defense -------------------------------------------------------
def test_cross_site_origin_is_refused_on_all_three_write_routes(tmp_path: Path) -> None:
    """403 on every state-changing route, and the inbox is byte-for-byte untouched."""
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = _depth(tmp_path)
    evil = {"Origin": "http://evil.com"}

    single = client.post("/api/upload", data={"text": "payload"}, headers=evil)
    batch = client.post(
        "/api/upload-batch",
        files=[("files", ("a.md", b"# A\n", "text/markdown"))],
        headers=evil,
    )
    htmx = client.post("/upload", data={"text": "payload"}, headers=evil)

    for resp in (single, batch, htmx):
        assert resp.status_code == 403
        assert "cross-origin write refused" in resp.text
    # The whole point: nothing was appended. The inbox is append-only and the curator has no
    # delete op, so a single accepted injection is unrecoverable through product features.
    assert _depth(tmp_path) == before


def test_null_origin_is_refused(tmp_path: Path) -> None:
    """A sandboxed iframe / ``data:`` document sends ``Origin: null`` — treated as a mismatch."""
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = _depth(tmp_path)

    resp = client.post("/api/upload", data={"text": "x"}, headers={"Origin": "null"})
    assert resp.status_code == 403
    assert _depth(tmp_path) == before


def test_same_origin_upload_is_unchanged(tmp_path: Path) -> None:
    """The HTMX form's own same-origin POST keeps its exact pre-#94 receipt and queues one item."""
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = _depth(tmp_path)

    # A browser states the SAME authority in Origin and Host, whatever port it reached.
    resp = client.post(
        "/api/upload",
        data={"text": "single-writer curator", "domain": "ai-tech"},
        headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"},
    )
    assert resp.status_code == 200
    receipt = resp.json()
    assert receipt["queued"] is True
    assert receipt["identity_source"] == "process"
    assert _depth(tmp_path) == before + 1

    # The HTMX fragment path is the same guard, the same pass.
    frag = client.post(
        "/upload",
        data={"text": "another capture"},
        headers={"Origin": "http://127.0.0.1"},
    )
    assert frag.status_code == 200
    assert "queued" in frag.text.lower()
    assert _depth(tmp_path) == before + 2


def test_origin_scheme_is_not_compared(tmp_path: Path) -> None:
    """A TLS-terminating proxy sends ``https://`` while the app speaks http — that must pass."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  security:\n    allowed_hosts: [kb.example.com]\n")
    client = _client(tmp_path)
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload",
        data={"text": "proxied capture"},
        headers={"Host": "kb.example.com", "Origin": "https://kb.example.com"},
    )
    assert resp.status_code == 200
    assert _depth(tmp_path) == before + 1


def test_origin_port_is_compared(tmp_path: Path) -> None:
    """A different PORT of an allowed host is a different origin — and must not be trusted.

    The regression this locks: judging the Origin against ``allowed_hosts`` (rather than against
    the request's own Host) made every port of every allow-listed host a trusted write origin. On
    the documented team config (``[kb.example.com, 127.0.0.1]``, where the loopback entry exists
    only for hub-local health checks / Prometheus) that promoted *any page on any team member's
    laptop* into a CSRF-capable origin against the public hub — and ``require_origin: true``, the
    strongest documented hardening, did not close it either.
    """
    _init_repo(tmp_path)
    _write_repo_yaml(
        tmp_path,
        "web:\n  security:\n    allowed_hosts: [kb.example.com, 127.0.0.1]\n"
        "    require_origin: true\n",
    )
    client = _client(tmp_path)
    before = _depth(tmp_path)

    for origin in ("http://127.0.0.1:3000", "http://127.0.0.1", "http://kb.example.com:8443"):
        resp = client.post(
            "/api/upload",
            data={"text": "csrf"},
            headers={"Host": "kb.example.com", "Origin": origin},
        )
        assert resp.status_code == 403, origin
        assert _depth(tmp_path) == before, origin

    # The hub's own origin still writes — including on a non-default public port, where the
    # browser states the port in BOTH headers.
    ok = client.post(
        "/api/upload",
        data={"text": "legit"},
        headers={"Host": "kb.example.com:8443", "Origin": "https://kb.example.com:8443"},
    )
    assert ok.status_code == 200
    assert _depth(tmp_path) == before + 1


def test_wildcard_allowlist_does_not_trust_sibling_subdomains(tmp_path: Path) -> None:
    """``*.team.example.com`` is a HOST gate, not a write-origin grant.

    One XSS'd marketing subdomain (or a dangling-CNAME takeover) would otherwise be enough to
    inject into the append-only inbox of a hub whose browser credentials the victim already holds.
    """
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  security:\n    allowed_hosts: ['*.team.example.com']\n")
    client = _client(tmp_path)
    before = _depth(tmp_path)

    bad = client.post(
        "/api/upload",
        data={"text": "csrf"},
        headers={"Host": "kb.team.example.com", "Origin": "https://blog.team.example.com"},
    )
    assert bad.status_code == 403
    assert _depth(tmp_path) == before

    good = client.post(
        "/api/upload",
        data={"text": "legit"},
        headers={"Host": "kb.team.example.com", "Origin": "https://kb.team.example.com"},
    )
    assert good.status_code == 200
    assert _depth(tmp_path) == before + 1


def test_referer_is_the_fallback_when_origin_is_absent(tmp_path: Path) -> None:
    """No ``Origin`` but a ``Referer`` → judge on the Referer's host (both directions)."""
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = _depth(tmp_path)

    bad = client.post(
        "/api/upload", data={"text": "x"}, headers={"Referer": "http://evil.com/attack.html"}
    )
    assert bad.status_code == 403
    assert _depth(tmp_path) == before

    good = client.post(
        "/api/upload", data={"text": "y"}, headers={"Referer": "http://127.0.0.1/upload"}
    )
    assert good.status_code == 200
    assert _depth(tmp_path) == before + 1


def test_empty_origin_is_refused(tmp_path: Path) -> None:
    """An EMPTY ``Origin:`` is present-and-unusable → mismatch, not "absent".

    Regression: a truthiness test (``origin or referer``) fell through to the Referer on an empty
    Origin, so the policy depended on which other header happened to ride along — and with neither
    usable the write was allowed through the absent-Origin door the contract reserves for
    header-less scripted writers.
    """
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = _depth(tmp_path)

    resp = client.post("/api/upload", data={"text": "x"}, headers={"Origin": ""})
    assert resp.status_code == 403
    assert _depth(tmp_path) == before


def test_origin_wins_over_referer(tmp_path: Path) -> None:
    """``Origin`` is the authoritative signal; a benign ``Referer`` cannot launder a bad Origin."""
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = _depth(tmp_path)

    resp = client.post(
        "/api/upload",
        data={"text": "x"},
        headers={"Origin": "http://evil.com", "Referer": "http://127.0.0.1:8000/upload"},
    )
    assert resp.status_code == 403
    assert _depth(tmp_path) == before


def test_get_routes_are_never_origin_checked(tmp_path: Path) -> None:
    """Safe methods carry no state change, so a cross-site ``Origin`` on a GET is irrelevant.

    (The GET surface is defended by the Host allowlist instead — a cross-site GET cannot READ the
    response without a same-origin position, which is exactly what rebinding tries to forge.)
    """
    _init_repo(tmp_path)
    client = _client(tmp_path)

    for route in ("/api/status", "/api/notes", "/dashboard"):
        resp = client.get(route, headers={"Origin": "http://evil.com"})
        assert resp.status_code == 200, route


# --- (c) absent-Origin: the documented two-sided policy ------------------------------------------
def test_absent_origin_passes_by_default(tmp_path: Path) -> None:
    """The documented upload ``curl`` procedures send no Origin and must keep working."""
    _init_repo(tmp_path)
    client = _client(tmp_path)
    before = _depth(tmp_path)

    resp = client.post("/api/upload", data={"text": "scripted capture"})
    assert resp.status_code == 200
    assert _depth(tmp_path) == before + 1


def test_require_origin_refuses_absent_origin_on_every_write_route(tmp_path: Path) -> None:
    """``require_origin: true`` is the team-deployment hardening step (no scripted writers)."""
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  security:\n    require_origin: true\n")
    client = _client(tmp_path)
    before = _depth(tmp_path)

    single = client.post("/api/upload", data={"text": "x"})
    batch = client.post("/api/upload-batch", files=[("files", ("a.md", b"# A\n", "text/markdown"))])
    htmx = client.post("/upload", data={"text": "x"})
    for resp in (single, batch, htmx):
        assert resp.status_code == 403
        assert "require_origin" in resp.text
    assert _depth(tmp_path) == before

    # Reads are untouched by require_origin — it gates state change only.
    assert client.get("/api/status").status_code == 200
    # And a same-origin write still passes.
    ok = client.post("/api/upload", data={"text": "y"}, headers={"Origin": "http://127.0.0.1"})
    assert ok.status_code == 200
    assert _depth(tmp_path) == before + 1


# --- (d) the remaining browser surfaces ----------------------------------------------------------
def test_host_matching_is_case_insensitive(tmp_path: Path) -> None:
    """RFC 9110 §4.2.3: the host is case-insensitive, so ``LOCALHOST`` is ``localhost``.

    starlette compares ``host == pattern`` verbatim while the loader lowercases every pattern, so
    an unnormalized Host would 400 with no hint that CASE was the problem.
    """
    _init_repo(tmp_path)
    client = _client(tmp_path)

    for host in ("localhost", "LOCALHOST", "LocalHost:8000"):
        assert client.get("/api/status", headers={"Host": host}).status_code == 200, host


def test_www_prefixed_entry_never_redirects(tmp_path: Path) -> None:
    """``www_redirect`` is OFF: a Host outside the list is 400, one rule, no 307.

    A 307 preserves method AND body, so with the redirect on, a ``www.``-prefixed allowlist entry
    bounced even a state-changing request to another URL — with the scheme taken from the ASGI
    scope (an http downgrade behind TLS termination) — before the Origin guard ever ran.
    """
    _init_repo(tmp_path)
    _write_repo_yaml(tmp_path, "web:\n  security:\n    allowed_hosts: [www.kb.example.com]\n")
    client = _client(tmp_path)
    before = _depth(tmp_path)

    read = client.get("/api/status", headers={"Host": "kb.example.com"}, follow_redirects=False)
    assert read.status_code == 400
    write = client.post(
        "/api/upload",
        data={"text": "x"},
        headers={"Host": "kb.example.com", "Origin": "https://evil.com"},
        follow_redirects=False,
    )
    assert write.status_code == 400
    assert _depth(tmp_path) == before


def test_every_response_denies_framing(tmp_path: Path) -> None:
    """Clickjacking: a framed UI submits from the face's OWN origin, invisible to the CSRF guard.

    Refusals carry the headers too — a 400/403 page is as frameable as a 200.
    """
    _init_repo(tmp_path)
    client = _client(tmp_path)

    for resp in (
        client.get("/upload"),
        client.get("/"),
        client.get("/api/status"),
        client.get("/api/status", headers={"Host": "evil.com"}),
        client.post("/api/upload", data={"text": "x"}, headers={"Origin": "http://evil.com"}),
    ):
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["content-security-policy"] == "frame-ancestors 'none'"
