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
"""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastapi import FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from agora_kb.config import WebConfig, load_web_config
from agora_kb.core import Repo
from agora_kb.core.hashing import content_sha256
from agora_kb.faces.mcp_server import AgoraHandlers
from agora_kb.ingest.extractors import (
    ExtractorError,
    ExtractorUnavailable,
    extract,
)

if TYPE_CHECKING:
    from starlette.templating import _TemplateResponse

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


# --- JSON API response models (the documented ADR-0019 §1 contract) -----------------------------
class UploadReceipt(BaseModel):
    """The receipt returned by ``POST /api/upload`` — the inbox write outcome (DESIGN §2.2).

    Mirrors :meth:`AgoraHandlers.remember`'s ``{id, queued, inbox_depth}``: ``queued`` is True iff
    a new immutable inbox event was appended (eventual consistency — searchable after the next
    curator run, not now).
    """

    id: str
    queued: bool
    inbox_depth: int


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
    """The receipt for ``POST /api/upload-batch`` — one :class:`FileReceipt` per submitted file."""

    results: list[FileReceipt]


def build_app(*, repo_path: Path, writer: str = "web", user: str = "local") -> FastAPI:
    """Construct the FastAPI web face over ``repo_path`` (mirrors ``mcp_server.build_server``).

    Resolves the repo, builds one :class:`AgoraHandlers` (the shared core seam), mounts the Jinja2
    templates + vendored static assets, and registers the JSON API + HTMX routes. ``writer`` is the
    inbox namespace for captures; ``user`` is the identity stamped into the ``web:<user>`` source —
    both are params (not request-derived) so Phase-4 auth threads identity here without a rewrite.
    """
    repo = Repo.resolve(repo_path)
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

    @app.get("/api/notes", tags=["api"], summary="List every wiki note + domains.")
    def api_notes() -> dict[str, object]:
        """Return ``{notes: [...], domains: [...]}`` — the browse listing (no body)."""
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
        domain: Annotated[
            str | None, Query(description="Restrict the global graph to one domain.")
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
            domain=domain,
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
        file: UploadFile | None = None,
        url: Annotated[str | None, Form()] = None,
        text: Annotated[str | None, Form()] = None,
        domain: Annotated[str | None, Form()] = None,
        tags: Annotated[str | None, Form()] = None,
    ) -> UploadReceipt:
        """Extract one of (file | url | text) → markdown, then append it to the inbox.

        Exactly one input is used (precedence file > url > text). The extracted markdown gets a
        deterministic provenance header (ADR-0020) and is written via :meth:`AgoraHandlers.remember`
        with ``source = web:<user>``. Returns the ``{id, queued, inbox_depth}`` receipt — the item
        is searchable only after the next curator run (eventual consistency, DESIGN §2.2).

        Errors map to HTTP: unsupported/empty input → 400, oversize → 413, malformed/garbage input
        → 422 (:class:`ExtractorError`), a missing ``ingest`` dependency → 503
        (:class:`ExtractorUnavailable`, with the install remedy).
        """
        receipt = await _do_upload(
            handlers,
            web_config=web_config,
            user=user,
            file=file,
            url=url,
            text=text,
            domain=domain,
            tags=tags,
        )
        return UploadReceipt(**receipt)

    @app.post(
        "/api/upload-batch",
        tags=["api"],
        summary="Capture N files in one drag-and-drop batch (multipart/form).",
        response_model=BatchUploadReceipt,
    )
    async def api_upload_batch(
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
        is read, stopping + reporting on the first overflow).
        """
        results = await _do_upload_batch(
            handlers,
            web_config=web_config,
            user=user,
            files=files,
            domain=domain,
            tags=tags,
        )
        return BatchUploadReceipt(results=results)

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
        """Home: search box, a status strip, and the domain/notes list (from ``browse()``)."""
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
    def graph_page(request: Request, domain: str | None = None) -> _TemplateResponse:
        """The interactive knowledge-graph page (a per-route force-graph canvas, ADR-0019 §7).

        Builds the JSON ``api_src`` server-side (``/api/graph`` + an optional ``?domain=`` when a
        non-empty domain is selected) and renders ``graph.html`` with the domain-filter chips. The
        canvas fetches ``api_src`` client-side via ``graph.js``; this route adds no graph logic.
        A disabled graph feature 404s (ADR-0025), matching ``/api/graph``.
        """
        if not web_config.features.graph_enabled:
            raise HTTPException(status_code=404, detail="graph feature is disabled")
        api_src = "/api/graph"
        active = (domain or "").strip() or None
        if active:
            api_src = f"{api_src}?domain={urllib.parse.quote(active)}"
        return templates.TemplateResponse(
            request,
            "graph.html",
            {
                "domains": handlers.browse()["domains"],
                "active_domain": active,
                "api_src": api_src,
            },
        )

    @app.get("/upload", response_class=HTMLResponse, include_in_schema=False)
    def upload_form(request: Request) -> _TemplateResponse:
        """The multi-modal capture form (file | url | text, + domain/tags)."""
        return templates.TemplateResponse(
            request,
            "upload.html",
            {"browse": handlers.browse()},
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
        """
        real_files = [f for f in (file or []) if f.filename]
        if len(real_files) > 1:
            results = await _do_upload_batch(
                handlers,
                web_config=web_config,
                user=user,
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
                user=user,
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

    return app


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
    oversize → 413, blocked extension → 415, extractor failure → 422, missing dependency → 503, and
    a non-kebab tag or other inbox-model validation failure → 422).
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
        )
    elif url_val is not None:
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
) -> str:
    """Size-check + extension-gate + extract one file's bytes → provenance-stamped markdown.

    The single per-file branch shared by :func:`_do_upload` and :func:`_do_upload_batch` (DRY, no
    behaviour drift). Enforces the per-file ``max_bytes`` (413) and the optional ``allowed``
    extension gate (415) at the FACE — ``extract`` itself stays format-driven — then prepends the
    deterministic provenance header (ADR-0020).
    """
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"upload too large: {len(data)} bytes > {max_bytes} limit",
        )
    _check_allowed_ext(filename, allowed)
    doc = _extract(data=data, filename=filename, mime=mime)
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
    """
    by_basename: dict[str, str] = {}
    for n in notes:
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
