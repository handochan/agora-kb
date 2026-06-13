"""YAML-frontmatter markdown (de)serialization.

On-disk inbox events (and, later, wiki notes) are markdown files that open with a ``---`` fenced
YAML frontmatter block followed by the body. This module is the single place that renders and parses
that shape, so the format stays consistent across the codebase.

Rendering preserves the caller's key order (``sort_keys=False``) — :meth:`InboxItem.to_frontmatter`
emits keys in the canonical DATA-MODEL §1 order — and keeps unicode literal (``allow_unicode=True``)
so bodies and values remain human- and agent-readable in git.
"""

from __future__ import annotations

import re

import yaml

__all__ = ["render", "parse", "FrontmatterError"]

# A fence is a line that is exactly "---" (trailing spaces/tabs allowed) — NOT "----" or "---foo".
_CLOSING_FENCE = re.compile(r"^---[ \t]*$", re.MULTILINE)


class FrontmatterError(ValueError):
    """Raised when a markdown document does not contain a well-formed frontmatter block."""


def render(frontmatter: dict[str, object], body: str) -> str:
    """Render a frontmatter dict + body into ``---\\n<yaml>---\\n\\n<body>\\n`` text."""
    block = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    body = body.rstrip("\n")
    return f"---\n{block}---\n\n{body}\n"


def parse(text: str) -> tuple[dict[str, object], str]:
    """Parse frontmatter markdown into ``(frontmatter, body)``.

    The opening and closing fences must each be a line that is exactly ``---`` (trailing spaces/tabs
    allowed); a ``----`` or ``---foo`` line is not a fence and will not split the document. Raises
    :class:`FrontmatterError` if the document does not open with a ``---`` fence closed by another.
    """
    nl = text.find("\n")
    first = text if nl == -1 else text[:nl]
    if first.strip() != "---":
        raise FrontmatterError("document does not start with a '---' frontmatter fence")
    rest = text[nl + 1 :] if nl != -1 else ""
    closing = _CLOSING_FENCE.search(rest)
    if closing is None:
        raise FrontmatterError("frontmatter block is not closed by a '---' line")
    yaml_text = rest[: closing.start()]
    body = rest[closing.end() :].lstrip("\n").rstrip("\n")
    loaded = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
    if not isinstance(loaded, dict):
        raise FrontmatterError("frontmatter is not a YAML mapping")
    return loaded, body
