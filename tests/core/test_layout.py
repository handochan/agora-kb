"""Tests for repo layout + writer validation (tenant isolation, DESIGN §7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.core.layout import InvalidWriterError, RepoLayout, validate_writer


def test_paths(tmp_path: Path) -> None:
    lo = RepoLayout(tmp_path)
    assert lo.kb_dir == tmp_path / "_kb"
    assert lo.inbox_dir == tmp_path / "_kb" / "inbox"
    assert lo.state_file == tmp_path / "_kb" / "state.json"
    assert lo.lock_file == tmp_path / "_kb" / "curator.lock"
    assert lo.inbox_writer_dir("dochan") == tmp_path / "_kb" / "inbox" / "dochan"
    assert (
        lo.inbox_item_path("dochan", "2026-06-13T10-22-33.481Z--a1b2c3")
        == tmp_path / "_kb" / "inbox" / "dochan" / "2026-06-13T10-22-33.481Z--a1b2c3.md"
    )


def test_root_is_absolute(tmp_path: Path) -> None:
    lo = RepoLayout(Path("."))
    assert lo.root.is_absolute()


@pytest.mark.parametrize("good", ["dochan", "claude-code", "web_user", "a", "A1.b-c"])
def test_valid_writers(good: str) -> None:
    assert validate_writer(good) == good


@pytest.mark.parametrize("bad", ["..", ".", "../evil", "a/b", "/abs", "", ".hidden", "a b"])
def test_invalid_writers_rejected(bad: str) -> None:
    with pytest.raises(InvalidWriterError):
        validate_writer(bad)


def test_path_traversal_blocked_in_layout(tmp_path: Path) -> None:
    lo = RepoLayout(tmp_path)
    with pytest.raises(InvalidWriterError):
        lo.inbox_writer_dir("../../etc")
