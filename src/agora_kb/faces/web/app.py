"""The Phase-3 web face — FastAPI app over the core API (ADR-0019 / DESIGN §5.2).

A **thin face** (ADR-0003): every route delegates to the same transport-free
:class:`~agora_kb.faces.mcp_server.AgoraHandlers` the MCP face uses, plus the inbox/extractor
write path. The web face NEVER reads or mutates ``wiki/`` / git / ``raw/`` directly — reads go
through :meth:`AgoraHandlers.browse` / :meth:`AgoraHandlers.note` / :meth:`AgoraHandlers.query` /
:meth:`AgoraHandlers.status` (which call ``core.wiki``), and writes go through
:meth:`AgoraHandlers.remember` (the only write path → the inbox; the curator alone materializes
``raw/`` and the wiki, single-writer ADR-0002).

Two presentation layers over one contract (ADR-0019 §1/§6):

- the **first-class JSON API** under ``/api/*`` — the documented, durable contract the MCP face
  also consumes and a future SPA / mobile / third-party client would bind to; and
- the **server-rendered HTMX/Jinja2 UI** under ``/`` — partial ``hx-get``/``hx-post`` fragment
  swaps for search, the note reader, and upload receipts. No Node, no build step (HTMX is vendored
  into ``static/``).

This module imports ``fastapi`` / ``jinja2`` at top level, so it lives behind the optional ``web``
extra and is **lazy-imported** by ``agora web`` (and by ``faces.web.__init__``'s ``build_app``
re-export shim) — ``import agora_kb`` never requires fastapi (invariant 4 / ADR-0005).

**No auth (Phase 3 is localhost single-user; auth is deferred to ROADMAP Phase 4).** Identity is
still threaded so Phase 4 is not a retrofit: ``build_app`` takes ``writer``/``user`` and every
upload is stamped ``source = f"web:{user}"`` (the inbox ``web:<user>`` source form, DATA-MODEL §1).

**Per-user identity behind a trusted reverse proxy (issue #67, ADR-0025 appendix).** When the
operator sets ``web.identity.trusted_header`` (e.g. ``X-Remote-User``), each WRITE request's user
is taken from that header — the one an authenticating proxy (basic auth, SSO, …) injects — so a
team deployment stamps ``web:<alice>`` / ``web:<bob>`` instead of collapsing everyone into one
process-wide ``web:local``. Strictly OPT-IN: the default (``trusted_header: None``) ignores every
request header (a client-forgeable header must never influence provenance unless the operator
declared the proxy trust boundary), and an absent header falls back to the process ``--user``
(byte-identical personal-deployment behaviour). A PRESENT-but-invalid header value (empty, or
outside the conservative ``web:<user>`` token charset) is a forgery attempt or a proxy
misconfiguration → the write is refused with 400, never silently attributed. Reads are untouched —
identity only matters where provenance is stamped (the inbox write path).

**Browser-mediated attack defense (issue #94, ADR-0025 appendix).** The loopback bind is a network
boundary a browser walks straight through, so ``build_app`` registers three middlewares:
:class:`_HostAllowlistMiddleware` (starlette's ``TrustedHostMiddleware`` under an actionable 400)
rejects a ``Host`` outside ``web.security.allowed_hosts`` — loopback-only by default — which closes
DNS rebinding, which would otherwise give an attacker page same-origin reads of the whole KB;
:class:`_OriginGuardMiddleware` rejects a state-changing request whose ``Origin``/``Referer``
authority is not **this deployment's own** (closing cross-site form CSRF into the append-only
inbox); and :class:`_SecurityHeadersMiddleware` refuses framing (``X-Frame-Options`` / CSP
``frame-ancestors``), so a clickjacked UI cannot borrow the user's same-origin position. Still NOT
authentication — that is ADR-0036 / Phase 4; this is defense in depth on top of the unauthenticated
premise.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastapi import FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agora_kb.config import (
    WebConfig,
    WebIdentityConfig,
    guard_repo_schema_version,
    load_repo_config,
    load_web_config,
)
from agora_kb.core import Repo
from agora_kb.core.hashing import content_sha256
from agora_kb.faces.mcp_server import AgoraHandlers
from agora_kb.ingest.extractors import (
    ExtractorError,
    ExtractorUnavailable,
    extract,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.templating import _TemplateResponse
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["build_app", "MAX_UPLOAD_BYTES"]

# Reject uploads larger than this before extraction (a sane localhost guard; ADR-0020). The web
# face is single-user/localhost in Phase 3, so this is a footgun bound, not an auth control.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB

# The remedy printed when an `ingest` extra dependency is missing (mirrors ExtractorUnavailable).
_INSTALL_INGEST = (
    "install the ingest extra: pip install 'agora-kb[ingest]' (or uv sync --extra ingest)"
)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

# A markdown body link `[text](target)` (image links `![..](..)` excluded via the (?<!\!) guard).
_MDLINK_RE = re.compile(r"(?<!\!)\[(?P<text>[^\]\r\n]*)\]\((?P<target>[^)\r\n]+)\)")

# A proxy-supplied remote user (issue #67): a conservative single token — alnum start, then
# alnum/dot/underscore/hyphen (mirrors core.models._TEAM_RE's charset) plus '@' for email-form
# usernames. STRICTER than the inbox model's `web:.+` source rule (\Aweb:.+\Z), so every accepted
# value is guaranteed valid as `web:<user>` — the face pre-validates, the model never 500s.
_REMOTE_USER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._@-]*\Z")
# A sane bound for a proxy-injected username/email (not a wire limit — a forgery/misconfig guard).
_REMOTE_USER_MAX_LEN = 128


# --- JSON API response models (the documented ADR-0019 §1 contract) -----------------------------
class UploadReceipt(BaseModel):
    """The receipt returned by ``POST /api/upload`` — the inbox write outcome (DESIGN §2.2).

    Mirrors :meth:`AgoraHandlers.remember`'s ``{id, queued, inbox_depth}``: ``queued`` is True iff
    a new immutable inbox event was appended (eventual consistency — searchable after the next
    curator run, not now). ``identity_source`` records where the stamped user came from — the
    trusted proxy header (``"header"``) or the process ``--user`` fallback (``"process"``) — for
    audit (issue #67).
    """

    id: str
    queued: bool
    inbox_depth: int
    identity_source: str = "process"


class FileReceipt(BaseModel):
    """One file's outcome in a multi-upload batch (ADR-0025).

    Best-effort, per-file: a good file carries its inbox ``id`` + ``queued`` (the
    :meth:`AgoraHandlers.remember` outcome); a bad file carries ``error`` (the human-readable
    failure) with ``id=None`` / ``queued=False``. The batch is NOT atomic — a bad file never blocks
    a good one (the inbox is append-only per-event, so partial success is correct, ADR-0002/0020).
    """

    filename: str
    id: str | None = None
    queued: bool = False
    error: str | None = None


class BatchUploadReceipt(BaseModel):
    """The receipt for ``POST /api/upload-batch`` — one :class:`FileReceipt` per submitted file.

    ``identity_source`` is batch-level (one request = one resolved identity): ``"header"`` when the
    stamped user came from the trusted proxy header, ``"process"`` otherwise (issue #67).
    """

    results: list[FileReceipt]
    identity_source: str = "process"


# --- browser-mediated attack defense (issue #94, ADR-0025 appendix) -----------------------------
#
# The `127.0.0.1` bind is a NETWORK boundary and a browser walks straight through it: the victim
# only has to open a malicious page while `agora web` is running. Two paths, two guards:
#
# (A) CSRF → inbox injection. `POST /api/upload` is a multipart form = a CORS "simple request", so
#     an auto-submitting cross-site <form> reaches it with NO preflight. The attacker cannot read
#     the response, but the WRITE lands: appended to the append-only inbox (invariant 3), curated
#     into the wiki on the next run, and undeletable through product features (the plan op
#     vocabulary has no delete). :class:`_OriginGuardMiddleware` refuses a state-changing request
#     whose Origin/Referer authority is not the one the request itself was addressed to.
# (B) DNS rebinding → whole-KB read. A TTL-0 attacker domain rebound to 127.0.0.1 gives the
#     attacker page SAME-ORIGIN reads of `/api/notes`, `/api/gold/{pack}`, `/metrics`, … The
#     rebound request still carries the attacker's DNS NAME in `Host`, so
#     :class:`_HostAllowlistMiddleware` (starlette's ``TrustedHostMiddleware`` over
#     ``web.security.allowed_hosts``) rejects it with 400.
# (C) Framing → clickjacking. A page that IFRAMES the UI submits from an allowed origin, so (A)
#     cannot see it; :class:`_SecurityHeadersMiddleware` denies framing outright instead.
#
# None of the three is authentication — that stays ADR-0036 / Phase 4. This is defense in depth ON
# TOP of the unauthenticated premise, and it is honest about its reach: it closes BROWSER-mediated
# attacks only. Anything that can already run arbitrary local requests is out of scope.

#: Methods that never change state → never Origin-checked (RFC 9110 "safe"). Everything else is.
#: Checking by METHOD rather than by a route list is deliberate: a future write route inherits the
#: guard automatically instead of silently shipping unprotected.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _host_matches(host: str, patterns: Sequence[str]) -> bool:
    """Match ``host`` against the ``allowed_hosts`` patterns with starlette's exact semantics.

    Exact equality, or a leading ``*.`` subdomain wildcard (``*.example.com`` matches
    ``a.example.com``). Deliberately the SAME rule as the ``TrustedHostMiddleware`` this wraps, so
    the pre-check that produces the actionable 400 can never disagree with the enforcing gate
    underneath it (:class:`_HostAllowlistMiddleware`). A bare ``*`` never reaches here —
    :func:`agora_kb.config._host_pattern` refuses it.

    Only the HOST allowlist uses this. The Origin check does NOT: a wildcard entry would otherwise
    promote every sibling subdomain (one XSS'd marketing subdomain, one dangling CNAME) into a
    trusted WRITE origin, and a loopback entry kept for hub-local health checks would promote every
    port of every team member's laptop.
    """
    return any(
        host == pattern or (pattern.startswith("*.") and host.endswith(pattern[1:]))
        for pattern in patterns
    )


def _request_authority(headers: Headers) -> str:
    """This deployment's OWN authority (``host[:port]``), as the client addressed it in ``Host``.

    The comparison baseline for the Origin check. Safe to trust here because the request has
    already passed :class:`_HostAllowlistMiddleware` (registered OUTSIDE the Origin guard), so the
    value is one the operator allow-listed — never an arbitrary attacker string.
    """
    return headers.get("host", "").strip().lower()


def _stated_authority(value: str) -> str | None:
    """Normalize an ``Origin`` (or ``Referer``) header value to ``host[:port]``, else ``None``.

    ``None`` means "present but unusable" — the literal ``null`` origin a sandboxed iframe /
    ``data:`` document sends (a classic CSRF vector), a hostless value (``file://``), an empty
    value, or a malformed URL. Callers treat that as a MISMATCH, never as "absent".

    The SCHEME is dropped: a TLS-terminating proxy makes the browser send ``https://…`` while the
    app itself speaks plain http, so comparing schemes would 403 every proxied deployment. The
    PORT is KEPT — it is what distinguishes ``http://127.0.0.1:3000`` (some other page served on
    the victim's own loopback) from the face itself, and the whole ``origin`` concept is
    scheme+host+port. That is why the proxy contract is "pass the client's ``Host`` through
    VERBATIM" (nginx ``proxy_set_header Host $http_host``): a proxy that rewrites or truncates it
    breaks the equality the browser's own ``Origin`` states.
    """
    raw = value.strip()
    if not raw or raw.lower() == "null":
        return None
    try:
        parts = urllib.parse.urlsplit(raw)
        host, port = parts.hostname, parts.port
    except ValueError:  # malformed IPv6 bracket / non-numeric port / unparseable authority
        return None
    if not host:
        return None
    host = host.lower()
    if ":" in host:  # IPv6 literal — restore the brackets the Host header carries
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None else host


class _OriginGuardMiddleware:
    """Refuse CROSS-SITE state-changing requests (the CSRF half of issue #94).

    Policy (ADR-0025 appendix), applied to every non-safe method BEFORE the route — and therefore
    before FastAPI parses the multipart body and before any inbox append:

    - ``Origin`` (or, absent that, ``Referer``) PRESENT and its authority equals the request's own
      ``Host`` → pass. Same-origin browser use (the HTMX form) always lands here, so the UI is
      unaffected. The baseline is the request's own Host — NOT ``web.security.allowed_hosts``,
      which is the HOST gate's job: that list legitimately carries entries (hub-local ``127.0.0.1``
      for health checks and Prometheus, a ``*.team.example.com`` wildcard) that must never become
      trusted WRITE origins.
    - PRESENT and mismatched (incl. the ``null`` origin) → **403**, nothing written. This is the
      whole defense: every current browser attaches ``Origin`` to a cross-site form POST / fetch.
    - ABSENT → pass by default. Scripted writers (``curl``, CI, the upload procedures documented
      in ``deploy/README.md`` and ``docs/DEPLOY-TEAM.md``) send no ``Origin``, and refusing them
      by default would break documented operations for no browser-attack gain. The residual risk —
      a browser so old it omits ``Origin`` on cross-site writes — is closed by opting into
      ``web.security.require_origin: true``, at which point ABSENT → **403** too.

    A pure ASGI middleware (not ``BaseHTTPMiddleware``): no request/response buffering, no task
    group, and the rejection happens without ever pulling the request body.
    """

    def __init__(self, app: ASGIApp, *, require_origin: bool) -> None:
        self.app = app
        self.require_origin = require_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        # PRESENCE, not truthiness: an empty `Origin:` is PRESENT-and-unusable (→ mismatch), not
        # absent. Falling back to Referer on an empty Origin would make the policy depend on which
        # OTHER header happened to ride along.
        stated = headers.get("origin")
        if stated is None:
            stated = headers.get("referer")
        if stated is None:
            if self.require_origin:
                await self._refuse(
                    scope,
                    receive,
                    send,
                    "missing Origin/Referer on a state-changing request: "
                    "web.security.require_origin is true, so every write must state its origin.",
                )
                return
            await self.app(scope, receive, send)
            return
        if _stated_authority(stated) != _request_authority(headers):
            await self._refuse(
                scope,
                receive,
                send,
                "cross-origin write refused: the Origin/Referer of a state-changing request must "
                "match this deployment's own host and port (the Host header it was sent to). If a "
                "reverse proxy fronts the web face, it must pass the client's Host through "
                "VERBATIM (nginx: proxy_set_header Host $http_host; Caddy does this by default) "
                "and that hostname must be in web.security.allowed_hosts.",
            )
            return
        await self.app(scope, receive, send)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send, detail: str) -> None:
        """Send the 403 without echoing any attacker-controlled value back into the response."""
        response = PlainTextResponse(f"{detail} (agora web face, issue #94)", status_code=403)
        await response(scope, receive, send)


class _HostAllowlistMiddleware:
    """starlette's ``TrustedHostMiddleware`` with an actionable 400 and RFC-9110 Host casing.

    The enforcement is still upstream's — this wrapper adds only what the operator needs and the
    vendored middleware cannot express (plus the ``www_redirect`` opt-out below):

    - **A diagnosable refusal.** Upstream answers a bare ``Invalid host header``, which names
      neither the product nor the knob; an operator who puts a proxy in front and has not yet met
      ``web.security`` sees a 400 that does not even say it came from agora. The pre-check here
      uses :func:`_host_matches` — upstream's OWN rule — so the two layers can never disagree, and
      the body names the config key plus the operator-owned list (never the attacker-supplied
      Host).
    - **Case-insensitive Host matching.** RFC 9110 §4.2.3 makes the host case-insensitive, but
      upstream compares ``host == pattern`` verbatim while the loader lowercases every pattern, so
      ``Host: LOCALHOST`` would 400. Lowercasing the header in the scope (a lossless normalization)
      fixes the pre-check, the upstream check, and the Origin comparison in one place.

    ``www_redirect`` is turned OFF: a 307 preserves method AND body, so a ``www.``-prefixed entry
    would bounce a state-changing request to another URL (scheme taken from ``scope``, i.e. an
    http downgrade behind TLS termination) BEFORE the Origin guard ever sees it. This face has no
    reason to offer www canonicalization; "a Host outside the list is 400" stays a single rule.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: Sequence[str]) -> None:
        self.allowed_hosts = list(allowed_hosts)
        self.app = TrustedHostMiddleware(app, allowed_hosts=self.allowed_hosts, www_redirect=False)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope = _lowercase_host(scope)
        host = Headers(scope=scope).get("host", "").split(":")[0]
        if not _host_matches(host, self.allowed_hosts):
            response = PlainTextResponse(
                "unknown Host header: this request was addressed to a host the agora web face is "
                "not configured to answer on. Add it to web.security.allowed_hosts in repo.yaml "
                f"(currently: {', '.join(self.allowed_hosts)}) — and make sure any reverse proxy "
                "passes the client's Host through verbatim. (agora web face, issue #94)",
                status_code=400,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _lowercase_host(scope: Scope) -> Scope:
    """Return ``scope`` with a lowercased ``Host`` (RFC 9110 §4.2.3: the host is case-insensitive).

    Copies rather than mutates — the caller's scope dict is shared with whatever wrapped it. A
    no-op (same object) when the header is already lowercase, which is every browser request.
    """
    headers: list[tuple[bytes, bytes]] = scope.get("headers") or []
    if not any(k == b"host" and v != v.lower() for k, v in headers):
        return scope
    return {**scope, "headers": [(k, v.lower() if k == b"host" else v) for k, v in headers]}


class _SecurityHeadersMiddleware:
    """Deny framing on every response (the clickjacking half of issue #94).

    The Origin guard cannot see a clickjacking submit: a page that iframes ``/upload`` and lures a
    click submits with the FACE's own origin, which is exactly what the guard allows. Framing is
    never a legitimate use of this face (no embed story, no OAuth-style dialogs), so both the
    legacy header and its CSP successor are set to "never".

    ``setdefault``, not overwrite: a route that ever needs its own framing policy keeps it. The
    CSP carries ONLY ``frame-ancestors`` — a broader policy would have to be reconciled with the
    vendored htmx/force-graph assets and the markdown-rendered note bodies, which is a separate
    decision, not a side effect of the framing fix.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("x-frame-options", "DENY")
                headers.setdefault("content-security-policy", "frame-ancestors 'none'")
            await send(message)

        await self.app(scope, receive, _send)


def build_app(*, repo_path: Path, writer: str = "web", user: str = "local") -> FastAPI:
    """Construct the FastAPI web face over ``repo_path`` (mirrors ``mcp_server.build_server``).

    Resolves the repo, builds one :class:`AgoraHandlers` (the shared core seam), mounts the Jinja2
    templates + vendored static assets, and registers the JSON API + HTMX routes. ``writer`` is the
    inbox namespace for captures; ``user`` is the DEFAULT identity stamped into the ``web:<user>``
    source. When the operator opts in via ``web.identity.trusted_header`` (issue #67), each write
    request's user comes from that reverse-proxy-injected header instead (see
    :func:`_resolve_upload_user`); with the feature off (the default) ``user`` is process-fixed and
    request headers are ignored — the Phase-4 auth seam this param always reserved.

    Also registers the three issue-#94 browser-attack middlewares (Host allowlist over
    ``web.security.allowed_hosts``, Origin guard, framing denial) — per-repo, like every other
    :class:`WebConfig` knob.
    """
    repo = Repo.resolve(repo_path)
    # #98 / DESIGN §10 V9: same fail-loud gate `build_server` applies, for the same reason — `agora
    # web` stops at the CLI dispatch guard, but uvicorn factories, tests, and embedders call
    # build_app directly, and the web face has a WRITE path (upload → Inbox.write) that must not run
    # against a repo this build only THINKS it understands.
    guard_repo_schema_version(repo.layout)
    handlers = AgoraHandlers(repo, writer=writer)
    # ADR-0025: resolve the operator's web policy PER-REPO (invariant 5 / ADR-0006) — never a
    # module-global the browser could flip across repos. Threaded into the graph caps, the upload
    # limits, the allowed-extension gate, and the graph-feature flag below.
    web_config = load_web_config(repo.layout)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    # Expose the graph-feature flag to every template (base.html nav link, the per-note Connections
    # embed) without threading it through each route's context — set once on this app's own Jinja
    # environment, so it stays per-repo/tenant-safe (each build_app has its own Jinja2Templates).
    templates.env.globals["graph_enabled"] = web_config.features.graph_enabled

    app = FastAPI(
        title="Agora web face",
        summary="Browse, search, and capture knowledge over the Agora core API (ADR-0019).",
    )
    # Browser-mediated attack defense (issue #94) — see the middleware section above.
    # Registration order matters: starlette runs the MOST RECENTLY added middleware OUTERMOST, so
    # this reads bottom-up. The Host allowlist sits OUTSIDE the Origin guard for two reasons: a
    # rebound (or otherwise unknown-Host) request is rejected before anything else in the stack
    # looks at it, AND the Origin guard's comparison baseline — the request's own Host — is
    # therefore always an operator-allow-listed value by the time it is read. The framing headers
    # go outermost so they ride on refusals too.
    app.add_middleware(_OriginGuardMiddleware, require_origin=web_config.security.require_origin)
    app.add_middleware(
        _HostAllowlistMiddleware, allowed_hosts=list(web_config.security.allowed_hosts)
    )
    app.add_middleware(_SecurityHeadersMiddleware)
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ============================================================================================
    # JSON API — the first-class, documented, durable contract (ADR-0019 §1/§6).
    # ============================================================================================
    @app.get("/api/status", tags=["api"], summary="Curator/inbox status (the meta face).")
    def api_status() -> dict[str, object]:
        """Backlog depth, last consolidation, processed-today/failed counts, and counters."""
        return handlers.status()

    @app.get("/api/search", tags=["api"], summary="Deterministic evidence search.")
    def api_search(
        q: Annotated[str, Query(description="The search question.")],
    ) -> dict[str, object]:
        """Return ``{query, status, hits[...]}`` — ordered citations into ``wiki/`` (ADR-0012)."""
        return handlers.query(q)

    @app.get("/api/notes", tags=["api"], summary="List every wiki note + subjects.")
    def api_notes() -> dict[str, object]:
        """Return ``{notes: [...], subjects: [...]}`` — the browse listing (no body).

        Each row carries ``kind`` and ``subjects`` (ADR-0041 D2/D3.2); v1's ``type`` and scalar
        ``domain`` are gone, not mirrored — see :meth:`AgoraHandlers.browse` for why.
        """
        return handlers.browse()

    @app.get(
        "/api/notes/{rel_path:path}",
        tags=["api"],
        summary="One note's full read payload (404 if not tracked).",
    )
    def api_note(rel_path: str) -> dict[str, object]:
        """Return one note (summary + frontmatter + raw body + link basenames), or 404."""
        payload = handlers.note(rel_path)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"note not found: {rel_path}")
        return payload

    @app.get(
        "/api/graph",
        tags=["api"],
        summary="Knowledge-graph node/edge data for the /graph viz (read-only).",
    )
    def api_graph(
        center: Annotated[
            str | None, Query(description="A note rel_path to ego-center the graph on (local).")
        ] = None,
        depth: Annotated[int, Query(description="Local ego-graph BFS depth (clamped 1..3).")] = 1,
        subject: Annotated[
            str | None, Query(description="Restrict the global graph to one subject.")
        ] = None,
    ) -> dict[str, object]:
        """Return ``{nodes, edges, node_total, edge_total, truncated, center, depth}``.

        A THIN pass-through to :meth:`AgoraHandlers.graph` — clamping, empty-handling, the global
        cap, and the local ego-BFS all live in the handler (ADR-0019 §1/§6, no logic in the route).
        The graph caps come from the operator's :class:`WebConfig` (ADR-0025); a disabled graph
        feature 404s (the route is not served when graph is off).
        """
        if not web_config.features.graph_enabled:
            raise HTTPException(status_code=404, detail="graph feature is disabled")
        return handlers.graph(
            center=center,
            depth=depth,
            subject=subject,
            max_nodes=web_config.graph.max_nodes,
            max_depth=web_config.graph.max_depth,
        )

    @app.post(
        "/api/upload",
        tags=["api"],
        summary="Capture knowledge from a file | url | text (multipart/form).",
        response_model=UploadReceipt,
    )
    async def api_upload(
        request: Request,
        file: UploadFile | None = None,
        url: Annotated[str | None, Form()] = None,
        text: Annotated[str | None, Form()] = None,
        domain: Annotated[str | None, Form()] = None,
        tags: Annotated[str | None, Form()] = None,
    ) -> UploadReceipt:
        """Extract one of (file | url | text) → markdown, then append it to the inbox.

        Exactly one input is used (precedence file > url > text). The extracted markdown gets a
        deterministic provenance header (ADR-0020) and is written via :meth:`AgoraHandlers.remember`
        with ``source = web:<user>`` — the user is per-request when the operator configured
        ``web.identity.trusted_header`` (issue #67), else the process ``--user``. Returns the
        ``{id, queued, inbox_depth, identity_source}`` receipt — the item is searchable only after
        the next curator run (eventual consistency, DESIGN §2.2).

        Errors map to HTTP: unsupported/empty input or an invalid identity-header value → 400, url
        capture disabled by the operator (``web.upload.url_enabled: false``) → 403, oversize → 413,
        malformed/garbage input — incl. an SSRF-blocked URL (issue #66) or a decompression-bomb
        archive (issue #53) → 422 (:class:`ExtractorError`), a missing ``ingest`` dependency → 503
        (:class:`ExtractorUnavailable`, with the install remedy).
        """
        req_user, identity_source = _resolve_upload_user(
            request, identity=web_config.identity, process_user=user
        )
        receipt = await _do_upload(
            handlers,
            web_config=web_config,
            user=req_user,
            file=file,
            url=url,
            text=text,
            domain=domain,
            tags=tags,
        )
        return UploadReceipt(**receipt, identity_source=identity_source)

    @app.post(
        "/api/upload-batch",
        tags=["api"],
        summary="Capture N files in one drag-and-drop batch (multipart/form).",
        response_model=BatchUploadReceipt,
    )
    async def api_upload_batch(
        request: Request,
        files: list[UploadFile],
        domain: Annotated[str | None, Form()] = None,
        tags: Annotated[str | None, Form()] = None,
    ) -> BatchUploadReceipt:
        """Capture multiple files in one batch → N independent inbox appends (ADR-0025).

        Each file flows through the SAME per-file extract→provenance→:meth:`AgoraHandlers.remember`
        path as :func:`_do_upload` (single-writer unchanged — the face only appends to the inbox,
        ADR-0002/0020). BEST-EFFORT, not atomic: a bad file yields its own ``error`` receipt
        while the good files still queue (the inbox is append-only per-event). Per-batch caps
        (``upload.max_files`` count, ``upload.total_bytes`` total) are enforced UP-FRONT and reject
        the whole batch with 413 before any write (count is known; the total is checked as each file
        is read, stopping + reporting on the first overflow). The stamped user is per-request when
        ``web.identity.trusted_header`` is configured (issue #67; an invalid header value → 400
        BEFORE any write), else the process ``--user``.
        """
        req_user, identity_source = _resolve_upload_user(
            request, identity=web_config.identity, process_user=user
        )
        results = await _do_upload_batch(
            handlers,
            web_config=web_config,
            user=req_user,
            files=files,
            domain=domain,
            tags=tags,
        )
        return BatchUploadReceipt(results=results, identity_source=identity_source)

    # --- dashboard JSON API (read-only meta face; DESIGN §5.3 / ADR-0003) -----------------------
    # The first-class, documented JSON for the three dashboard panels — the SAME transport-free
    # AgoraHandlers aggregations the HTML fragments below render. Read-only: no write path, no new
    # canonical data; KB-health reuses the deterministic lint() verbatim (curator never disagrees).
    @app.get("/api/dashboard/health", tags=["api"], summary="KB-health panel (counts/lint/tags).")
    def api_dashboard_health() -> dict[str, object]:
        """Note counts, status/tag distribution, contested/orphan + lint signals, last run."""
        return handlers.health()

    @app.get(
        "/api/dashboard/curator", tags=["api"], summary="Curator-status panel (queue/backend/log)."
    )
    def api_dashboard_curator() -> dict[str, object]:
        """Inbox depth, throughput, counters, active backend, and the log.md work-log timeline."""
        return handlers.curator_status()

    @app.get(
        "/api/dashboard/harvester", tags=["api"], summary="Harvester-status panel (connectors)."
    )
    def api_dashboard_harvester() -> dict[str, object]:
        """Whether harvesting is enabled + per-connector last scan / candidate tally."""
        return handlers.harvester_status()

    @app.get("/api/dashboard/gold", tags=["api"], summary="Gold-pack panel (medallion, ADR-0027).")
    def api_dashboard_gold() -> dict[str, object]:
        """The medallion gold tier: the derived pack's presence, freshness vs silver, size, age."""
        return handlers.gold_status()

    @app.get(
        "/api/gold/{pack}",
        tags=["api"],
        summary="One built gold context pack, served byte-identically (ADR-0027 Phase C).",
    )
    def api_gold_pack(pack: str) -> dict[str, object]:
        """Return ``{status: "ok", pack, text, meta}`` — the built ``_kb/gold/<pack>.md`` verbatim.

        A THIN pass-through to :meth:`AgoraHandlers.gold_pack` — the SAME handler the MCP
        ``kb_context`` tool / ``agora://gold/{pack}`` resource / ``gold_context`` prompt wrap (the
        ADR-0019/0021 two-face lock; no logic in the route). ``text`` is byte-identical to the
        built artifact (never reassembled — ADR-0027 decision 3); ``meta`` carries the sidecar
        fields. A not-built pack or an invalid (traversal-unsafe) name maps to 404 with the
        handler's actionable note (build guidance + the packs that do exist).
        """
        payload = handlers.gold_pack(pack)
        if payload["status"] != "ok":
            raise HTTPException(status_code=404, detail=str(payload["note"]))
        return payload

    # --- Prometheus exposition (the operational half of the observability split) ----------------
    # NOT a JSON API endpoint: this serves the Prometheus text exposition format
    # (CONTENT_TYPE_LATEST), the operational time-series half of DESIGN §5.3's two-way split (heavy
    # graphing lives in external Grafana — AGPL sidecar, never bundled — ADR-0019 §5). It is a CHEAP
    # scrape: a fresh read of state.json / cursors / the inbox dir count via the meta seams, NEVER
    # lint()/health() (the heavy content/health path, which stays in the dashboard). The
    # prometheus-client dep is the optional `metrics` extra, imported lazily inside render_latest —
    # when it is absent the route returns 503 with the install remedy instead of crashing startup.
    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        """Prometheus exposition for the repo (text/plain; 503 without the metrics extra).

        Cheap per-scrape read of already-materialized metadata (inbox depth, cumulative curator
        counters, harvester cursors, last-run timestamps, active backend) — no lint(), no whole-tree
        scan. Returns the Prometheus ``CONTENT_TYPE_LATEST`` media type; a missing
        ``prometheus-client`` (optional ``metrics`` extra) yields 503 with the install remedy.
        """
        from agora_kb.faces.web.metrics import MetricsUnavailable, render_latest

        try:
            body, content_type = render_latest(repo)
        except MetricsUnavailable as exc:
            return Response(content=str(exc), status_code=503, media_type="text/plain")
        return Response(content=body, media_type=content_type)

    # ============================================================================================
    # HTMX / Jinja2 server-rendered UI (ADR-0019 §2).
    # ============================================================================================
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request) -> _TemplateResponse:
        """Home: search box, a status strip, and the subject/notes list (from ``browse()``)."""
        browse = handlers.browse()
        status = handlers.status()
        return templates.TemplateResponse(
            request,
            "home.html",
            {"browse": browse, "status": status},
        )

    @app.get("/search", response_class=HTMLResponse, include_in_schema=False)
    def search(request: Request, q: str = "") -> _TemplateResponse:
        """Return an HTML FRAGMENT of ranked hits (``hx-get`` target swaps it in)."""
        result = handlers.query(q) if q.strip() else {"query": q, "status": "not_found", "hits": []}
        return templates.TemplateResponse(
            request,
            "_hits.html",
            {"result": result},
        )

    @app.get("/note/{rel_path:path}", response_class=HTMLResponse, include_in_schema=False)
    def note_page(request: Request, rel_path: str) -> _TemplateResponse:
        """Render ONE note read-only: frontmatter header + body markdown→HTML (XSS-safe)."""
        payload = handlers.note(rel_path)
        if payload is None:
            return templates.TemplateResponse(
                request,
                "note.html",
                {"note": None, "rel_path": rel_path, "body_html": ""},
                status_code=404,
            )
        body_html = render_note_body(str(payload.get("body", "")), notes=handlers.browse()["notes"])
        return templates.TemplateResponse(
            request,
            "note.html",
            {"note": payload, "rel_path": rel_path, "body_html": body_html},
        )

    @app.get("/graph", response_class=HTMLResponse, include_in_schema=False)
    def graph_page(request: Request, subject: str | None = None) -> _TemplateResponse:
        """The interactive knowledge-graph page (a per-route force-graph canvas, ADR-0019 §7).

        Builds the JSON ``api_src`` server-side (``/api/graph`` + an optional ``?subject=`` when a
        non-empty subject is selected) and renders ``graph.html`` with the subject-filter chips.
        The canvas fetches ``api_src`` client-side via ``graph.js``; this route adds no graph logic.
        A disabled graph feature 404s (ADR-0025), matching ``/api/graph``.
        """
        if not web_config.features.graph_enabled:
            raise HTTPException(status_code=404, detail="graph feature is disabled")
        api_src = "/api/graph"
        active = (subject or "").strip() or None
        if active:
            api_src = f"{api_src}?subject={urllib.parse.quote(active)}"
        return templates.TemplateResponse(
            request,
            "graph.html",
            {
                "subjects": handlers.browse()["subjects"],
                "active_subject": active,
                "api_src": api_src,
            },
        )

    @app.get("/upload", response_class=HTMLResponse, include_in_schema=False)
    def upload_form(request: Request) -> _TemplateResponse:
        """The multi-modal capture form (file | url | text, + domain/tags).

        The domain selector is the WRITE-side vocabulary and it is deliberately NOT the read side's
        ``subjects`` facet: ``domain`` on this form reaches ``Inbox.write`` and survives schema 2
        only as the ``raw/<domain>/`` SHARD KEY (ADR-0041 D2.2 leg 3), so its legal values are the
        taxonomy's declared ``domains`` — not "the subjects some note happens to carry today".
        Reading the taxonomy also lets a fresh KB offer its domains before any note exists, which
        the old ``browse()``-derived list could not. A config error degrades to an empty list (the
        field is optional — "let the curator decide" is the first option), never a 500 on a form.
        """
        try:
            domains = list(load_repo_config(repo.layout).taxonomy.domains)
        except Exception:  # noqa: BLE001 — an unreadable taxonomy must not break the capture form.
            domains = []
        return templates.TemplateResponse(
            request,
            "upload.html",
            {"domains": domains},
        )

    @app.post("/upload", response_class=HTMLResponse, include_in_schema=False)
    async def upload_submit(
        request: Request,
        file: list[UploadFile] | None = None,
        url: Annotated[str | None, Form()] = None,
        text: Annotated[str | None, Form()] = None,
        domain: Annotated[str | None, Form()] = None,
        tags: Annotated[str | None, Form()] = None,
    ) -> _TemplateResponse:
        """Run the SAME upload pipeline as the JSON API and return the receipt fragment (ADR-0025).

        The ``multiple`` file input submits 0..N ``file`` parts. With ≥2 real files this runs the
        batch path (best-effort per-file outcomes → ``_batch_receipt.html``); with ≤1 file (plus the
        url/text single-capture form) it runs the single :func:`_do_upload` → ``_receipt.html``. An
        empty file input degrades to the url/text path (FastAPI sends an empty-filename part).
        The stamped user is per-request under ``web.identity.trusted_header`` (issue #67) — same
        rules as the JSON API; an invalid header value propagates as a plain 400 (a proxy-misconfig
        signal, not a form-validation outcome, so no receipt fragment is rendered).
        """
        # The identity_source audit field is a JSON-API surface; the HTMX fragment omits it.
        req_user, _ = _resolve_upload_user(request, identity=web_config.identity, process_user=user)
        real_files = [f for f in (file or []) if f.filename]
        if len(real_files) > 1:
            results = await _do_upload_batch(
                handlers,
                web_config=web_config,
                user=req_user,
                files=real_files,
                domain=domain,
                tags=tags,
            )
            return templates.TemplateResponse(
                request,
                "_batch_receipt.html",
                {"results": results, "error": None, "status_code": 200},
            )

        single = real_files[0] if real_files else None
        try:
            receipt = await _do_upload(
                handlers,
                web_config=web_config,
                user=req_user,
                file=single,
                url=url,
                text=text,
                domain=domain,
                tags=tags,
            )
        except HTTPException as exc:
            return templates.TemplateResponse(
                request,
                "_receipt.html",
                {"receipt": None, "error": str(exc.detail), "status_code": exc.status_code},
                status_code=exc.status_code,
            )
        return templates.TemplateResponse(
            request,
            "_receipt.html",
            {"receipt": receipt, "error": None, "status_code": 200},
        )

    # --- dashboard (read-only meta face; DESIGN §5.3) -------------------------------------------
    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request) -> _TemplateResponse:
        """The three-panel read-only dashboard (KB health + curator + harvester status).

        Renders each panel's initial content server-side; HTMX then refreshes the curator/harvester
        panels on a 5s poll and the (heavier) health panel on load + a manual button (see
        ``dashboard.html``). All three read already-existing metadata only.
        """
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "health": handlers.health(),
                "curator": handlers.curator_status(),
                "harvester": handlers.harvester_status(),
                "gold": handlers.gold_status(),
            },
        )

    @app.get("/dashboard/health", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_health(request: Request) -> _TemplateResponse:
        """HTML FRAGMENT of the KB-health panel (the manual-refresh / on-load hx-get target)."""
        return templates.TemplateResponse(request, "_health.html", {"health": handlers.health()})

    @app.get("/dashboard/curator", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_curator(request: Request) -> _TemplateResponse:
        """HTML FRAGMENT of the curator-status panel (the 5s-poll hx-get target)."""
        return templates.TemplateResponse(
            request, "_curator.html", {"curator": handlers.curator_status()}
        )

    @app.get("/dashboard/harvester", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_harvester(request: Request) -> _TemplateResponse:
        """HTML FRAGMENT of the harvester-status panel (the 5s-poll hx-get target)."""
        return templates.TemplateResponse(
            request, "_harvester.html", {"harvester": handlers.harvester_status()}
        )

    @app.get("/dashboard/gold", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_gold(request: Request) -> _TemplateResponse:
        """HTML FRAGMENT of the gold-pack (medallion) panel (the 5s-poll hx-get target)."""
        return templates.TemplateResponse(request, "_gold.html", {"panel": handlers.gold_status()})

    return app


# --- per-request identity resolution (issue #67, ADR-0025 appendix) -----------------------------
def _resolve_upload_user(
    request: Request, *, identity: WebIdentityConfig, process_user: str
) -> tuple[str, str]:
    """Resolve the user to stamp into ``source = web:<user>`` for ONE write request.

    Returns ``(user, identity_source)`` where ``identity_source`` is ``"header"`` or ``"process"``.
    The decision table (issue #67):

    - ``identity.trusted_header`` unset (the default) → the process ``user`` — every request header
      is IGNORED (no opt-in, no trust: a client-forgeable header must never steer provenance).
    - header configured but ABSENT on the request → the process ``user`` fallback (personal
      deployments behind no proxy keep byte-identical behaviour).
    - header present MORE THAN ONCE → 400. An append-mode proxy (Apache ``RequestHeader append``,
      HAProxy ``add-header``) puts the client's forged copy FIRST, so any pick-one rule is a
      spoofing vector (first-wins would stamp the forgery outright); duplicate identity headers
      are themselves the proxy-misconfiguration signal the 400 policy exists to surface.
    - header PRESENT exactly once → its value (after the optional ``strip_domain`` truncation at
      the first ``@``) MUST be a non-empty conservative token (:data:`_REMOTE_USER_RE`, ≤
      :data:`_REMOTE_USER_MAX_LEN`). An invalid value raises 400 — NOT a silent fallback: a
      present-but-garbage identity header is either a forgery attempt or a misconfigured proxy,
      and silently attributing the write to the process user would poison provenance (the audit
      trail would say ``web:local`` for a request that claimed to be someone). The accepted charset
      is strictly narrower than the inbox model's ``web:.+`` source rule, so a value the face
      accepts can never be rejected downstream by the pydantic :class:`InboxItem`.
    """
    header = identity.trusted_header
    if header is None:
        return process_user, "process"
    values = request.headers.getlist(header)
    if not values:
        return process_user, "process"
    if len(values) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f"duplicate identity header {header!r} ({len(values)} occurrences) — an "
                "append-mode proxy lets a client-forged copy shadow the authenticated one; "
                "configure the proxy to SET (replace) the header, never append."
            ),
        )
    raw = values[0]
    value = raw.split("@", 1)[0] if identity.strip_domain else raw
    if not value or len(value) > _REMOTE_USER_MAX_LEN or not _REMOTE_USER_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid identity header {header!r}: value must be a non-empty token "
                "([A-Za-z0-9] then [A-Za-z0-9._@-], max "
                f"{_REMOTE_USER_MAX_LEN} chars) — check the reverse-proxy configuration."
            ),
        )
    return value, "header"


# --- the shared upload pipeline (one body for /api/upload, /api/upload-batch, and the HTMX POST) --
async def _do_upload(
    handlers: AgoraHandlers,
    *,
    web_config: WebConfig,
    user: str,
    file: UploadFile | None,
    url: str | None,
    text: str | None,
    domain: str | None,
    tags: str | None,
) -> dict[str, object]:
    """Extract → provenance-stamp → ``remember``; return the ``{id, queued, inbox_depth}`` receipt.

    Precedence: an uploaded ``file`` wins, then ``url``, then raw ``text``. The chosen input is
    turned into markdown (``file`` via the shared :func:`_file_to_markdown`; ``url`` via
    :func:`extract`; ``text`` used verbatim), a deterministic provenance header is prepended
    (ADR-0020), and the result is appended to the inbox with ``source = web:<user>``. The per-file
    size limit + the allowed-extension gate come from the operator's :class:`WebConfig` (ADR-0025).
    Raises :class:`fastapi.HTTPException` for the documented error cases (no input/empty → 400,
    url capture disabled by the operator → 403, oversize → 413, blocked extension → 415, extractor
    failure — incl. an SSRF-blocked URL or a decompression-bomb archive → 422, missing dependency
    → 503, and a non-kebab tag or other inbox-model validation failure → 422). The URL fetch runs
    with the extractor's SSRF guard ON (never ``allow_private`` — issue #66).
    """
    url_val = (url or "").strip() or None
    text_val = text if (text and text.strip()) else None
    max_bytes = web_config.upload.max_bytes

    if file is not None and file.filename:
        data = await file.read()
        markdown = _file_to_markdown(
            data,
            filename=file.filename,
            mime=file.content_type,
            max_bytes=max_bytes,
            allowed=web_config.extensions.allowed,
            max_uncompressed_bytes=web_config.upload.max_uncompressed_bytes,
        )
    elif url_val is not None:
        if not web_config.upload.url_enabled:
            # The operator's team-deployment switch (issue #66 / the #68 guide): server-side URL
            # fetching is OFF wholesale — refuse BEFORE any resolve/connect happens.
            raise HTTPException(
                status_code=403,
                detail=(
                    "url capture is disabled by the operator "
                    "(web.upload.url_enabled: false in _kb/repo.yaml)."
                ),
            )
        doc = _extract(url=url_val)
        markdown = (
            _provenance_header(
                source_url=doc.source_url or url_val,
                title=doc.title,
                extractor=doc.extractor,
                content_sha256=doc.content_sha256,
            )
            + doc.markdown
        )
    elif text_val is not None:
        encoded = text_val.encode("utf-8")
        if len(encoded) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"text too large: {len(encoded)} bytes > {max_bytes} limit",
            )
        markdown = (
            _provenance_header(
                source_url=None,
                title=None,
                extractor="text",
                content_sha256=content_sha256(text_val),
            )
            + text_val
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="provide exactly one of: a file upload, a url, or text.",
        )

    return _remember_markdown(handlers, markdown, user=user, domain=domain, tags=tags)


async def _do_upload_batch(
    handlers: AgoraHandlers,
    *,
    web_config: WebConfig,
    user: str,
    files: list[UploadFile],
    domain: str | None,
    tags: str | None,
) -> list[FileReceipt]:
    """Capture N files as N independent inbox appends; one :class:`FileReceipt` each (ADR-0025).

    Per-batch caps are enforced UP-FRONT: the file COUNT is known immediately
    (``upload.max_files``), and the running TOTAL is checked as each file's bytes are read
    (``upload.total_bytes``) — either overflow rejects the WHOLE batch with 413 before any inbox
    write (the count/total are batch-level policy, not per-file). Within the cap each file flows
    through the SAME per-file path as :func:`_do_upload` (size limit, extension gate, extract,
    provenance, :meth:`AgoraHandlers.remember`); a per-file failure is captured as that file's own
    ``error`` :class:`FileReceipt` (BEST-EFFORT, not atomic — a bad file never blocks a good one,
    the inbox is append-only per-event, ADR-0002/0020). The shared ``domain``/``tags`` apply to
    every file; an invalid shared tag fails the whole batch up-front (it would fail every file).
    """
    if not files:
        raise HTTPException(status_code=400, detail="provide at least one file in the batch.")
    if len(files) > web_config.upload.max_files:
        raise HTTPException(
            status_code=413,
            detail=(
                f"too many files: {len(files)} > {web_config.upload.max_files} per-batch limit."
            ),
        )
    # Validate the shared tags ONCE up-front: an invalid tag would fail every file identically, so
    # surface it as a single clean batch error rather than N duplicate per-file errors.
    _parse_tags(tags)

    # Read every file first, enforcing the running per-batch total cap BEFORE any inbox write (so a
    # too-large batch is rejected whole, never partially queued).
    read: list[tuple[str, bytes, str | None]] = []
    running_total = 0
    for f in files:
        data = await f.read()
        running_total += len(data)
        if running_total > web_config.upload.total_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"batch too large: {running_total} bytes > "
                    f"{web_config.upload.total_bytes} per-batch limit."
                ),
            )
        read.append((f.filename or "", data, f.content_type))

    results: list[FileReceipt] = []
    for filename, data, mime in read:
        try:
            markdown = _file_to_markdown(
                data,
                filename=filename,
                mime=mime,
                max_bytes=web_config.upload.max_bytes,
                allowed=web_config.extensions.allowed,
                max_uncompressed_bytes=web_config.upload.max_uncompressed_bytes,
            )
            receipt = _remember_markdown(handlers, markdown, user=user, domain=domain, tags=tags)
        except HTTPException as exc:
            results.append(FileReceipt(filename=filename or "(unnamed)", error=str(exc.detail)))
            continue
        results.append(
            FileReceipt(
                filename=filename or "(unnamed)",
                id=str(receipt["id"]),
                queued=bool(receipt["queued"]),
            )
        )
    return results


def _file_to_markdown(
    data: bytes,
    *,
    filename: str,
    mime: str | None,
    max_bytes: int,
    allowed: list[str] | None,
    max_uncompressed_bytes: int,
) -> str:
    """Size-check + extension-gate + extract one file's bytes → provenance-stamped markdown.

    The single per-file branch shared by :func:`_do_upload` and :func:`_do_upload_batch` (DRY, no
    behaviour drift). Enforces the per-file ``max_bytes`` (413) and the optional ``allowed``
    extension gate (415) at the FACE — ``extract`` itself stays format-driven — then prepends the
    deterministic provenance header (ADR-0020). ``max_uncompressed_bytes`` is the operator's
    decompression-bomb cap for zip-based formats (issue #53), enforced INSIDE the extractor and
    surfaced as its 422 :class:`ExtractorError` (a content property, not a face-level 413 wire
    size).
    """
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"upload too large: {len(data)} bytes > {max_bytes} limit",
        )
    _check_allowed_ext(filename, allowed)
    doc = _extract(
        data=data, filename=filename, mime=mime, max_uncompressed_bytes=max_uncompressed_bytes
    )
    return (
        _provenance_header(
            source_url=doc.source_url,
            title=doc.title,
            extractor=doc.extractor,
            content_sha256=doc.content_sha256,
        )
        + doc.markdown
    )


def _check_allowed_ext(filename: str, allowed: list[str] | None) -> None:
    """Gate a filename against the operator's ``extensions.allowed`` list at the face (ADR-0025).

    ``allowed=None`` → no gate (the extractor's built-in supported set governs). A LIST → the file's
    lowercased extension MUST be in it, else 415 (Unsupported Media Type) with a clear message —
    gated BEFORE ``extract`` so the extractor stays format-driven and never sees a blocked file.
    """
    if allowed is None:
        return
    ext = os.path.splitext(filename)[1].lower() if filename else ""
    if ext not in allowed:
        raise HTTPException(
            status_code=415,
            detail=(
                f"file extension {ext or '(none)'!r} is not allowed; "
                f"allowed extensions: {sorted(allowed)}."
            ),
        )


def _remember_markdown(
    handlers: AgoraHandlers,
    markdown: str,
    *,
    user: str,
    domain: str | None,
    tags: str | None,
) -> dict[str, object]:
    """Append provenance-stamped ``markdown`` to the inbox; return the ``remember`` receipt.

    The single write seam shared by the single + batch paths: validates tags (kebab-case, 422),
    normalizes the domain, and calls :meth:`AgoraHandlers.remember` with ``source = web:<user>``
    (the only write path — the curator alone materializes ``raw/``, single-writer ADR-0002/0020).
    """
    parsed_tags = _parse_tags(tags)
    dom = (domain or "").strip() or None
    try:
        return handlers.remember(
            markdown,
            source=f"web:{user}",
            domain=dom,
            tags=parsed_tags,
        )
    except ValueError as exc:
        # Defense in depth: any inbox-model validation that slips past the face's own pre-checks
        # (a malformed domain/target, etc.) surfaces as a clean 422 here rather than a raw 500 out
        # of the pydantic InboxItem deep in handlers.remember(). Tags are already pre-validated in
        # _parse_tags, so this is the belt to that pre-check's braces.
        raise HTTPException(status_code=422, detail=f"could not capture upload: {exc}") from exc


def _extract(**kwargs: object):  # noqa: ANN201 - returns ExtractedDoc; thin wrapper
    """Call :func:`extract`, mapping its failures to the documented HTTP statuses.

    :class:`ExtractorUnavailable` (a missing ``ingest`` dependency) → 503 with the install remedy;
    :class:`ExtractorError` (malformed/garbage/untrusted input) → 422; a :class:`ValueError`
    (unsupported/ambiguous input shape) → 400.
    """
    try:
        return extract(**kwargs)  # type: ignore[arg-type]
    except ExtractorUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"{exc} — {_INSTALL_INGEST}") from exc
    except ExtractorError as exc:
        raise HTTPException(status_code=422, detail=f"could not extract upload: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# A kebab-case tag: [a-z0-9] words joined by single '-' (mirrors core.models.InboxItem._KEBAB_RE,
# DATA-MODEL §1). Validated HERE at the face boundary so untrusted form input that violates it
# returns a clean 422 instead of a raw 500 from the pydantic model deep in handlers.remember().
_KEBAB_TAG_RE = re.compile(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z")


def _parse_tags(tags: str | None) -> list[str] | None:
    """Split a comma/space-separated ``tags`` form value into a validated kebab-case list.

    Returns ``None`` for an empty/whitespace value (no tags attached). Each parsed tag MUST be
    kebab-case (the same rule the inbox model enforces, DATA-MODEL §1); a non-kebab tag (uppercase,
    punctuation, …) raises :class:`fastapi.HTTPException` (422) so both ``POST /api/upload`` and the
    HTMX ``POST /upload`` surface a clean client error rather than the raw 500 the model's
    ``ValueError`` would otherwise produce deep inside :meth:`AgoraHandlers.remember`.
    """
    if not tags or not tags.strip():
        return None
    parts = [t.strip() for t in re.split(r"[,\s]+", tags) if t.strip()]
    invalid = [t for t in parts if not _KEBAB_TAG_RE.match(t)]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=(
                f"invalid tag(s) {invalid}: tags must be kebab-case "
                "([a-z0-9] words joined by '-'), e.g. single-writer, inbox-design."
            ),
        )
    return parts or None


def _provenance_header(
    *,
    source_url: str | None,
    title: str | None,
    extractor: str,
    content_sha256: str,
) -> str:
    """Return a deterministic provenance frontmatter block to PREPEND to a captured body (ADR-0020).

    The block survives into ``raw/`` when the curator materializes the capture (the curator alone
    writes ``raw/`` — single-writer ADR-0002), so the origin (source url/title, extractor, content
    hash) is recoverable from the markdown itself without storing the original binary verbatim (the
    ADR-0020 deferral). Deterministic field order + omitted-when-absent so the same upload yields
    the same header.
    """
    lines = ["---", "captured-by: web", f"extractor: {extractor}"]
    if title:
        lines.append(f"source-title: {_yaml_scalar(title)}")
    if source_url:
        lines.append(f"source-url: {_yaml_scalar(source_url)}")
    lines.append(f"content-sha256: {content_sha256}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + "\n"


def _yaml_scalar(value: str) -> str:
    """Quote a single-line YAML scalar so a colon/special char cannot break the frontmatter.

    Newlines are collapsed to spaces (a provenance scalar is one line by construction) and the
    value is double-quoted with backslashes/quotes escaped — enough for the deterministic provenance
    header, which only carries a title/url.
    """
    flat = " ".join(value.splitlines()).strip()
    escaped = flat.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --- markdown → HTML (XSS-safe), with intra-wiki link rewriting ---------------------------------
def render_note_body(body: str, *, notes: list[dict[str, object]] | None = None) -> str:
    """Render a note ``body`` markdown → sanitized HTML, rewriting intra-wiki links to web routes.

    Uses ``markdown-it-py`` with **raw HTML disabled** (``html=False``) so a ``<script>`` embedded
    in (untrusted, harvested-candidate) note content is escaped, never executed (the XSS guard
    ADR-0019 §2 calls for). After rendering, intra-wiki body links ``[Title](relative.md)`` are
    rewritten to ``/note/<resolved rel_path>`` (resolved against the known note set by basename,
    falling back to the basename), while external links (``http(s)://``, ``mailto:``, anchors, and
    any non-``.md`` target) are left untouched.

    Imported lazily? No — this module already requires the ``web`` extra; ``markdown-it-py`` ships
    in it. A dependency-light ``import agora_kb`` never reaches here (it is behind the lazy
    ``agora web`` import of ``faces.web``).
    """
    from markdown_it import MarkdownIt

    # Strip the curator's internal body-region markers BEFORE rendering: with html=False markdown-it
    # would otherwise escape them into VISIBLE text in the note (they are an AUTHOR-pass transaction
    # mechanism, not content). breaks=True renders a single newline as <br> so the curator's prose
    # (long unwrapped lines, one section/bullet per line) reads line-by-line instead of collapsing
    # consecutive lines into one run-on paragraph (CommonMark's default soft-break-as-space).
    rewritten = _rewrite_wiki_links(_strip_body_sentinels(body), notes=notes or [])
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "breaks": True})
    return md.render(rewritten)


# The curator's per-region body markers (INGEST contract): `<!-- agora:body:start id=<id> -->` …
# `<!-- agora:body:end id=<id> -->`. Internal transaction mechanism — never shown to a reader.
_BODY_SENTINEL_RE = re.compile(r"[ \t]*<!--\s*agora:body:(?:start|end)\b[^>]*-->[ \t]*")


def _strip_body_sentinels(body: str) -> str:
    """Remove the `<!-- agora:body:start/end id=… -->` region markers from a note body for display.

    They are internal curator markers, not content; leaving them in would (under markdown-it
    html=False) render as escaped, visible text. Removing them and trimming leaves the prose; any
    resulting blank lines collapse to a single paragraph break in markdown.
    """
    return _BODY_SENTINEL_RE.sub("", body).strip()


def _rewrite_wiki_links(body: str, *, notes: list[dict[str, object]]) -> str:
    """Rewrite ``[Title](relative.md)`` body links to ``/note/<rel_path>`` (basename-resolved).

    A target is intra-wiki iff it is a local ``.md`` path (no URL scheme, not an anchor). Its
    basename (filename minus dir minus ``.md``) is matched against the known notes' basenames to
    recover the canonical ``rel_path``; failing a match, the link points at ``/note/<basename>.md``
    (the route resolves it to ``None`` and the reader shows a not-found note rather than 500).
    External links (scheme-bearing, anchors, non-``.md``) are returned verbatim. Operates on the
    RAW markdown so it composes with the later HTML-escaping render (the URL is what changes, never
    note prose).

    ``wiki/people/**`` is OUTSIDE this identity space (ADR-0041 D3.3: *"a people note is addressed
    by path, never by ``[[basename]]``"*), so a people row never SEEDS the resolver — it stays
    browsable and readable at its own ``/note/<rel_path>``, it just cannot capture a link the
    curator wrote to a note of the same name. The row's ``kind`` is the test, and it is exactly the
    schema-gated one the rest of the read side uses: ``kind == "person"`` is derived from the
    directory and only on schema 2, so a schema-1 repo owning an ordinary ``people`` DOMAIN is
    untouched.
    """
    by_basename: dict[str, str] = {}
    for n in notes:
        if n.get("kind") == "person":
            continue
        base = n.get("basename")
        rel = n.get("rel_path")
        if isinstance(base, str) and isinstance(rel, str):
            by_basename.setdefault(base, rel)

    def _sub(m: re.Match[str]) -> str:
        text = m.group("text")
        target = m.group("target").strip()
        if not _is_intra_wiki_target(target):
            return m.group(0)
        # Drop any #fragment for basename resolution; keep it on the rewritten URL.
        path_part, _, frag = target.partition("#")
        basename = path_part.rsplit("/", 1)[-1]
        if basename.endswith(".md"):
            basename = basename[: -len(".md")]
        rel_path = by_basename.get(basename, f"{basename}.md")
        url = f"/note/{rel_path}"
        if frag:
            url = f"{url}#{frag}"
        return f"[{text}]({url})"

    return _MDLINK_RE.sub(_sub, body)


def _is_intra_wiki_target(target: str) -> bool:
    """True iff ``target`` is a local ``.md`` note link (not external/anchor/scheme-bearing)."""
    t = target.strip()
    if not t or t.startswith("#"):
        return False
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", t):  # has a URL scheme (http:, mailto:, …)
        return False
    if t.startswith("//"):  # protocol-relative
        return False
    path_part = t.split("#", 1)[0].split("?", 1)[0]
    return path_part.endswith(".md")
