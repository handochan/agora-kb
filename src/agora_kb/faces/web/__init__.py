"""web face — the FastAPI + HTMX app over the core API (ADR-0019 / DESIGN §5.2).

Browse, search, and capture (file/url/text upload) for humans, plus a first-class JSON API under
``/api/*`` — the durable contract the MCP face also consumes (ADR-0019 §1/§6). A **thin face**
(ADR-0003): it calls the shared :class:`~agora_kb.faces.mcp_server.AgoraHandlers` (core read) and
the inbox/extractor write path, and NEVER touches ``wiki/`` / git / ``raw/`` directly. The curator
remains the sole writer (single-writer ADR-0002).

The implementation in :mod:`agora_kb.faces.web.app` imports ``fastapi``/``jinja2`` at top level
(the optional ``web`` extra), so **importing this subpackage must not require fastapi**:
``build_app`` is re-exported via a lazy ``__getattr__`` that imports :mod:`.app` only on first
access. This keeps
``import agora_kb`` (and the dependency-light core CLI) working without the web stack installed
(invariant 4 / ADR-0005); ``agora web`` lazy-imports this subpackage the same way ``agora serve``
lazy-imports the MCP face.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["build_app"]

if TYPE_CHECKING:
    from .app import build_app


def __getattr__(name: str) -> Any:
    """Lazily resolve ``build_app`` from :mod:`.app` (which requires the ``web`` extra).

    Deferring the ``from .app import …`` until attribute access keeps ``import
    agora_kb.faces.web`` cheap and dependency-free; only ``faces.web.build_app`` (used by
    ``agora web``) pulls in fastapi/jinja2.
    """
    if name == "build_app":
        from .app import build_app

        return build_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
