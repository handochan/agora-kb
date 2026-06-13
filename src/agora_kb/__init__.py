"""Agora — self-hostable, OSS, multi-tenant shared memory hub for AI agents.

Architecture (see docs/DESIGN.md): one core API (write→inbox, read→wiki, meta) with three faces
(MCP, web, dashboard) and three adapter families (input/extractors, read/harvesters, write/curator
brains). CQRS + single-writer curator; repo = tenant boundary; markdown + git is the source of truth.

This package is in the design phase: module docstrings describe responsibilities; implementation
follows docs/ROADMAP.md (Phase 1 = core + MCP face first).
"""

__version__ = "0.0.0"
