"""Tests for atomic, durable writes (atomicio)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.core.atomicio import atomic_write_text, fsync_dir


def test_exclusive_creates(tmp_path: Path) -> None:
    dest = tmp_path / "a.txt"
    atomic_write_text(dest, "hello", exclusive=True)
    assert dest.read_text(encoding="utf-8") == "hello"


def test_exclusive_refuses_overwrite(tmp_path: Path) -> None:
    dest = tmp_path / "a.txt"
    atomic_write_text(dest, "first", exclusive=True)
    with pytest.raises(FileExistsError):
        atomic_write_text(dest, "second", exclusive=True)
    assert dest.read_text(encoding="utf-8") == "first"
    assert list(tmp_path.glob(".*tmp*")) == []  # temp cleaned up on the refused write


def test_overwrite_replaces(tmp_path: Path) -> None:
    dest = tmp_path / "a.txt"
    atomic_write_text(dest, "first", exclusive=False)
    atomic_write_text(dest, "second", exclusive=False)
    assert dest.read_text(encoding="utf-8") == "second"


def test_no_temp_left_on_success(tmp_path: Path) -> None:
    atomic_write_text(tmp_path / "a.txt", "x", exclusive=True)
    assert list(tmp_path.glob(".*tmp*")) == []


def test_no_temp_left_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        atomic_write_text(tmp_path / "a.txt", "x", exclusive=False)
    assert list(tmp_path.glob(".*tmp*")) == []
    assert not (tmp_path / "a.txt").exists()


def test_no_temp_left_on_exclusive_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.link", lambda *a, **k: (_ for _ in ()).throw(OSError("link boom")))
    with pytest.raises(OSError):
        atomic_write_text(tmp_path / "a.txt", "x", exclusive=True)
    assert list(tmp_path.glob(".*tmp*")) == []
    assert not (tmp_path / "a.txt").exists()


def test_writes_lf_and_utf8(tmp_path: Path) -> None:
    dest = tmp_path / "a.txt"
    atomic_write_text(dest, "café\n한글", exclusive=True)
    assert dest.read_bytes() == "café\n한글".encode()


def test_fsync_dir_is_safe(tmp_path: Path) -> None:
    fsync_dir(tmp_path)  # must not raise on a normal directory
