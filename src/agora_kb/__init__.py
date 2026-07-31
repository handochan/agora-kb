"""Agora — self-hostable, OSS, multi-tenant shared memory hub for AI agents.

Architecture (see docs/DESIGN.md): one core API (write→inbox, read→wiki, meta) with three faces
(MCP, web, dashboard) and three adapter families (input/extractors, read/harvesters, write/curator
brains). CQRS + single-writer curator; repo = tenant boundary; markdown + git is the source of
truth.

Phases 1, 2, 3, 3.5 and 3.6 have shipped (core + CLI + MCP face + pluggable curator brains +
harvester + ingest extractors + web face + gold packs + deployability); Phase 4 (auth +
multi-tenancy) is next. See docs/ROADMAP.md for what each phase covers.
"""

# THE version of Agora — single source of truth (issue #101). Everything else derives from this
# literal: `pyproject.toml` declares `dynamic = ["version"]` and points `[tool.hatch.version]` at
# this file (so `uv build` artifact names and the installed dist metadata are downstream of it), and
# `cli.build_parser` / `cli._cmd_doctor` read it directly rather than through
# `importlib.metadata` — which is the whole point of this direction, since the common dogfooder
# path (`git clone` + `uv run agora`) has no install metadata to read.
#
# Format is PEP 440, NOT SemVer-with-a-dash: `0.1.0b1`, never `0.1.0-beta.1`. PyPI normalizes the
# latter to the former, which would silently skew the sdist/wheel filenames away from the git tag.
# Release convention (see CHANGELOG.md): tag `v<this string>`, tag only CI-green commits, bump this
# literal in its own commit immediately before the tag, and never move a tag once pushed.
__version__ = "0.1.0b1"
