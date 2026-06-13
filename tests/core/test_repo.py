"""Tests for the repo/git primitives (ADR-0008 transactional publish).

Real git integration: skipped if ``git`` is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agora_kb.core.repo import GitError, Repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

WHEN = datetime(2026, 6, 13, 3, 0, 12, tzinfo=UTC)


@pytest.fixture()
def repo(tmp_path: Path) -> Repo:
    r = Repo.resolve(tmp_path / "kb")
    r.init(when=WHEN)
    return r


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    ).stdout.strip()


# --- init -------------------------------------------------------------------------------------
def test_init_creates_repo_on_main_with_commit(tmp_path: Path) -> None:
    r = Repo.resolve(tmp_path / "kb")
    assert not r.is_initialized()
    head = r.init(when=WHEN)
    assert r.is_initialized()
    assert r.current_branch() == "main"
    assert len(head) == 40
    assert head == r.head_commit() == r.branch_commit("main")


def test_init_is_idempotent(repo: Repo) -> None:
    head = repo.head_commit()
    assert repo.init(when=WHEN) == head


def test_init_with_fixed_when_is_reproducible(tmp_path: Path) -> None:
    # identical tree + message + identity + date, no parent => identical root commit sha
    a = Repo.resolve(tmp_path / "a").init(when=WHEN)
    b = Repo.resolve(tmp_path / "b").init(when=WHEN)
    assert a == b


def test_kb_is_gitignored_and_content_is_tracked(repo: Repo) -> None:
    inbox_item = repo.layout.inbox_writer_dir("dochan") / "e.md"
    inbox_item.parent.mkdir(parents=True, exist_ok=True)
    inbox_item.write_text("x", encoding="utf-8")
    assert "_kb/" not in _git(repo.root, "status", "--porcelain")  # spool never tracked
    tracked = _git(repo.root, "ls-files").split()
    assert ".gitignore" in tracked
    assert "index.md" in tracked  # curated content IS tracked (ADR-0001)
    assert _git(repo.root, "show", "HEAD:index.md").startswith("---")


# --- worktree lifecycle -----------------------------------------------------------------------
def test_worktree_is_detached_at_commit(repo: Repo) -> None:
    base = repo.head_commit()
    wt = repo.create_worktree(at=base)
    try:
        assert wt.exists()
        assert _git(wt, "rev-parse", "HEAD") == base
        rc = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"], cwd=str(wt), capture_output=True, check=False
        ).returncode
        assert rc != 0  # detached HEAD has no symbolic ref
    finally:
        repo.remove_worktree(wt)
    assert not wt.exists()


def test_worktree_context_manager_cleans_up(repo: Repo) -> None:
    seen: dict[str, Path] = {}
    with repo.worktree(at=repo.head_commit()) as wt:
        assert wt.exists()
        seen["wt"] = wt
    assert not seen["wt"].exists()
    assert str(seen["wt"]) not in _git(repo.root, "worktree", "list")  # admin entry pruned


def test_two_worktrees_coexist(repo: Repo) -> None:
    base = repo.head_commit()
    a = repo.create_worktree(at=base)
    b = repo.create_worktree(at=base)
    try:
        assert a != b and a.exists() and b.exists()
        (a / "a.md").write_text("a", encoding="utf-8")
        (b / "b.md").write_text("b", encoding="utf-8")
        assert repo.commit_worktree(a, "a", when=WHEN) != repo.commit_worktree(b, "b", when=WHEN)
    finally:
        repo.remove_worktree(a)
        repo.remove_worktree(b)


def test_remove_worktree_is_idempotent(repo: Repo) -> None:
    wt = repo.create_worktree(at=repo.head_commit())
    repo.remove_worktree(wt)
    repo.remove_worktree(wt)  # second call must not raise
    assert not wt.exists()


# --- commit -----------------------------------------------------------------------------------
def test_commit_worktree_advances_only_detached_head(repo: Repo) -> None:
    base = repo.head_commit()
    with repo.worktree(at=base) as wt:
        (wt / "wiki").mkdir(parents=True, exist_ok=True)
        (wt / "wiki" / "note.md").write_text("hello", encoding="utf-8")
        new = repo.commit_worktree(wt, "feat: add note", when=WHEN)
        assert new != base
        assert _git(wt, "rev-parse", "HEAD") == new  # worktree HEAD advanced
        assert repo.branch_commit("main") == base  # curated branch did NOT move yet


def test_commit_sets_explicit_author_and_date(repo: Repo) -> None:
    with repo.worktree(at=repo.head_commit()) as wt:
        (wt / "f.md").write_text("x", encoding="utf-8")
        new = repo.commit_worktree(wt, "msg", when=WHEN, author_name="Bot", author_email="b@x")
        assert _git(wt, "show", "-s", "--format=%an", new) == "Bot"
        assert _git(wt, "show", "-s", "--format=%ae", new) == "b@x"
        # compare the instant (epoch) so offset rendering (Z vs +00:00) is irrelevant
        epoch = str(int(WHEN.timestamp()))
        assert _git(wt, "show", "-s", "--format=%at", new) == epoch
        assert _git(wt, "show", "-s", "--format=%ct", new) == epoch


def test_commit_worktree_rejects_naive_datetime(repo: Repo) -> None:
    with repo.worktree(at=repo.head_commit()) as wt:
        (wt / "f.md").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError):
            repo.commit_worktree(wt, "msg", when=datetime(2026, 6, 13, 3, 0, 12))  # noqa: DTZ001


def test_commit_worktree_on_no_changes_raises(repo: Repo) -> None:
    with repo.worktree(at=repo.head_commit()) as wt:
        with pytest.raises(GitError):  # an empty diff is a no-op, not a publish
            repo.commit_worktree(wt, "noop", when=WHEN)


def test_commit_worktree_is_reproducible(tmp_path: Path) -> None:
    shas = []
    for name in ("r1", "r2"):
        r = Repo.resolve(tmp_path / name)
        r.init(when=WHEN)
        with r.worktree(at=r.head_commit()) as wt:
            (wt / "note.md").write_text("same", encoding="utf-8")
            shas.append(r.commit_worktree(wt, "same msg", when=WHEN))
    assert shas[0] == shas[1]  # same base+tree+identity+date+message => same sha


# --- compare-and-swap publish -----------------------------------------------------------------
def test_cas_publishes_when_base_unchanged(repo: Repo) -> None:
    base = repo.head_commit()
    with repo.worktree(at=base) as wt:
        (wt / "wiki").mkdir(parents=True, exist_ok=True)
        (wt / "wiki" / "n.md").write_text("hello", encoding="utf-8")
        new = repo.commit_worktree(wt, "publish me", when=WHEN)
    assert repo.compare_and_swap_branch(expected=base, new=new) is True
    assert repo.branch_commit("main") == new  # curated branch advanced atomically
    assert _git(repo.root, "show", "main:wiki/n.md") == "hello"  # content landed on the branch


def test_cas_fails_when_base_moved(repo: Repo) -> None:
    base = repo.head_commit()
    with repo.worktree(at=base) as wt:
        (wt / "other.md").write_text("x", encoding="utf-8")
        intervening = repo.commit_worktree(wt, "intervening", when=WHEN)
    assert repo.compare_and_swap_branch(expected=base, new=intervening) is True  # first publish
    # a second run still thinks `base` is the tip; CAS with a real new commit must refuse
    with repo.worktree(at=base) as wt2:
        (wt2 / "late.md").write_text("y", encoding="utf-8")
        late = repo.commit_worktree(wt2, "late", when=WHEN)
    assert repo.compare_and_swap_branch(expected=base, new=late) is False  # base is stale
    assert repo.branch_commit("main") == intervening  # unchanged by the failed CAS


def test_cas_rejects_bogus_or_destructive_new(repo: Repo) -> None:
    base = repo.head_commit()
    for bad in ["0" * 40, "abc", "main", "Z" * 40, base[:10]]:
        with pytest.raises(ValueError):  # incl. the all-zero oid that would DELETE the ref
            repo.compare_and_swap_branch(expected=base, new=bad)
    with pytest.raises(ValueError):
        repo.compare_and_swap_branch(expected="0" * 40, new=base)  # expected is validated too
    assert repo.branch_commit("main") == base  # nothing changed


def test_cas_false_when_branch_absent(repo: Repo) -> None:
    base = repo.head_commit()
    assert repo.compare_and_swap_branch(expected=base, new=base, branch="nope") is False


# --- publish-state detection (recovery) -------------------------------------------------------
def test_is_published(repo: Repo) -> None:
    base = repo.head_commit()
    with repo.worktree(at=base) as wt:
        (wt / "n.md").write_text("x", encoding="utf-8")
        new = repo.commit_worktree(wt, "m", when=WHEN)
    assert repo.is_published(base) is True  # base is the current tip
    assert repo.is_published(new) is False  # not yet published
    assert repo.compare_and_swap_branch(expected=base, new=new) is True
    assert repo.is_published(new) is True  # reachable from the branch after CAS


def test_is_published_rejects_bad_sha(repo: Repo) -> None:
    with pytest.raises(ValueError):
        repo.is_published("not-a-sha")


def test_resolve_uninitialized(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()  # exists but is not a git repo
    r = Repo.resolve(empty)
    assert not r.is_initialized()
    with pytest.raises(GitError):
        r.head_commit()


def test_git_error_on_missing_cwd(tmp_path: Path) -> None:
    # a non-existent working dir surfaces as GitError, not a raw OSError
    r = Repo.resolve(tmp_path / "does-not-exist")
    with pytest.raises(GitError):
        r.head_commit()
