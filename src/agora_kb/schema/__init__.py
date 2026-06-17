"""schema — the KB wiki schema emitted into each knowledge repo (AGENTS.md + symlinks, templates):
frontmatter, links, INGEST/QUERY/LINT, taxonomy. Also the deterministic L1 lint that gates the
curator commit and feeds the dashboard (ADR-0010, ADR-0011 §4.4)."""

from .emit import Taxonomy, emit_schema
from .lint import LintFinding, LintResult, lint
from .notes import (
    Note,
    body_link_basenames,
    child_bullets,
    heading_slug,
    note_basename,
    parse_all_notes,
    wikilinks,
)

__all__ = [
    "Note",
    "parse_all_notes",
    "wikilinks",
    "child_bullets",
    "body_link_basenames",
    "heading_slug",
    "note_basename",
    "Taxonomy",
    "emit_schema",
    "LintFinding",
    "LintResult",
    "lint",
]
