"""Tests for frontmatter (de)serialization."""

from __future__ import annotations

import pytest

from agora_kb.core.frontmatter import FrontmatterError, parse, render


def test_parse_wraps_malformed_yaml_in_frontmatter_error() -> None:
    """Malformed YAML frontmatter raises the TYPED FrontmatterError, not a raw yaml.YAMLError.

    Real-world (Obsidian) notes carry ``links: [[a]], [[b]]`` in frontmatter — invalid YAML. The
    deterministic read/lint path must stay TOTAL: the parser wraps the yaml error so each consumer's
    existing FrontmatterError handling (lint finding / Wiki skip) applies instead of crashing.
    """
    obsidian = "---\ntitle: T\nlinks: [[economy-moc]], [[ai-tech-moc]]\n---\n\nbody\n"
    with pytest.raises(FrontmatterError, match="not valid YAML"):
        parse(obsidian)


def test_render_basic_shape() -> None:
    text = render({"id": "x", "n": 1}, "hello body")
    assert text.startswith("---\n")
    assert "id: x\n" in text
    assert text.endswith("hello body\n")
    assert "---\n\n" in text  # blank line between frontmatter and body


def test_render_preserves_key_order() -> None:
    text = render({"z": 1, "a": 2, "m": 3}, "b")
    assert text.index("z:") < text.index("a:") < text.index("m:")


def test_roundtrip() -> None:
    fm = {"id": "2026-06-13T10-22-33.481Z--a1b2c3", "tags": ["a", "b"], "n": 7}
    body = "multi\nline\nbody"
    rendered = render(fm, body)
    parsed_fm, parsed_body = parse(rendered)
    assert parsed_fm == fm
    assert parsed_body == body


def test_unicode_preserved() -> None:
    rendered = render({"t": "café 한글"}, "본문 émoji 🚀")
    fm, body = parse(rendered)
    assert fm["t"] == "café 한글"
    assert body == "본문 émoji 🚀"


@pytest.mark.parametrize("bad", ["no frontmatter here", "", "---\nkey: val\nno closing fence"])
def test_malformed_raises(bad: str) -> None:
    with pytest.raises(FrontmatterError):
        parse(bad)


def test_non_mapping_frontmatter_raises() -> None:
    with pytest.raises(FrontmatterError):
        parse("---\n- just\n- a\n- list\n---\nbody")


def test_closing_fence_is_full_line_only() -> None:
    # A "----" (4 dashes) line is NOT a fence: the block closes at the exact "---" line, and the
    # body's "----" rule line is preserved (guards against substring '\n---' mis-splitting).
    text = "---\ntitle: t\n---\nbody with a ---- rule:\n----\ndone"
    fm, body = parse(text)
    assert fm == {"title": "t"}
    assert body == "body with a ---- rule:\n----\ndone"


def test_fence_allows_trailing_spaces() -> None:
    fm, body = parse("---\nk: v\n---   \nbody")
    assert fm == {"k": "v"}
    assert body == "body"
