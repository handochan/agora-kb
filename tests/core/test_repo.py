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


# --- repo-owner sync (read-after-publish) -----------------------------------------------------
def _publish_via_cas(repo: Repo, rel: str, content: str) -> str:
    """Simulate one curator publish: commit ``rel`` in a detached worktree at base, then CAS the
    curated ref to it. Returns the new tip. The MAIN working copy is left STALE — the CAS moved the
    ref (which HEAD tracks) but never materialized the new tree, exactly the post-publish state the
    owner sync must reconcile."""
    base = repo.head_commit()
    with repo.worktree(at=base) as wt:
        path = wt / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        new = repo.commit_worktree(wt, f"publish {rel}", when=WHEN)
    assert repo.compare_and_swap_branch(expected=base, new=new) is True
    return new


def test_sync_to_branch_fast_forwards_a_behind_working_copy(repo: Repo) -> None:
    new = _publish_via_cas(repo, "wiki/n.md", "published")
    # The CAS advanced refs/heads/main (which HEAD tracks) but never materialized the tree, so the
    # published file is NOT on disk yet — the read path would miss it without the owner sync.
    assert not (repo.root / "wiki" / "n.md").exists()

    synced = repo.sync_to_branch()

    assert synced == new == repo.head_commit() == repo.branch_commit("main")
    # The published content is now materialized in the owner's on-disk tree (read-after-publish).
    assert (repo.root / "wiki" / "n.md").read_text(encoding="utf-8") == "published"
    assert _git(repo.root, "status", "--porcelain") == ""  # working tree reconciled, clean


def test_sync_to_branch_fast_forwards_a_genuinely_behind_detached_head(repo: Repo) -> None:
    # The pure --ff-only case: HEAD is left STRICTLY behind the branch tip (detached at base, so it
    # does NOT track the moved ref), so sync_to_branch fast-forwards it onto the materialized tip.
    base = repo.head_commit()
    new = _publish_via_cas(repo, "wiki/n.md", "published")
    subprocess.run(["git", "checkout", "-q", "--detach", base], cwd=str(repo.root), check=True)
    assert repo.head_commit() == base  # genuinely behind the branch tip

    synced = repo.sync_to_branch()

    assert synced == new == repo.head_commit()  # fast-forwarded HEAD to the published tip
    assert (repo.root / "wiki" / "n.md").read_text(encoding="utf-8") == "published"


def test_sync_to_branch_is_noop_when_already_at_tip(repo: Repo) -> None:
    head = repo.head_commit()
    assert _git(repo.root, "status", "--porcelain") == ""  # clean tree already at the tip
    assert repo.sync_to_branch() == head  # already at the curated tip + materialized
    assert repo.head_commit() == head
    assert _git(repo.root, "status", "--porcelain") == ""  # still clean (a true no-op)


def test_sync_to_branch_refuses_a_dirty_working_tree(repo: Repo) -> None:
    # The published commit MODIFIES index.md; the owner has an uncommitted edit to the SAME file, so
    # materializing the tip would overwrite it — the two-way merge refuses rather than clobbering.
    _publish_via_cas(repo, "index.md", "PUBLISHED index content\n")
    owner_edit = "uncommitted owner edit\n"
    (repo.root / "index.md").write_text(owner_edit, encoding="utf-8")
    with pytest.raises(GitError):
        repo.sync_to_branch()
    assert (repo.root / "index.md").read_text(encoding="utf-8") == owner_edit  # edit preserved


def test_sync_to_branch_refuses_a_diverged_working_copy(repo: Repo) -> None:
    base = repo.head_commit()
    published = _publish_via_cas(repo, "branch.md", "from-branch")
    # The owner commits divergent history: detach at base, commit locally, so HEAD is a fork of the
    # branch tip (neither is an ancestor of the other) — --ff-only cannot fast-forward it.
    subprocess.run(["git", "checkout", "-q", "--detach", base], cwd=str(repo.root), check=True)
    (repo.root / "local.md").write_text("from-owner", encoding="utf-8")
    diverged = repo.commit_all("owner local commit", when=WHEN)
    assert diverged != published
    with pytest.raises(GitError):  # --ff-only cannot fast-forward a diverged history
        repo.sync_to_branch()
    assert repo.head_commit() == diverged  # the owner's divergent HEAD is never rewritten


# --- admin commit (commit_all) ----------------------------------------------------------------
def test_commit_all_commits_the_working_tree_with_explicit_date(repo: Repo) -> None:
    (repo.root / "doc.md").write_text("admin content", encoding="utf-8")
    base = repo.head_commit()
    new = repo.commit_all("docs: admin commit", when=WHEN)
    assert new != base
    assert new == repo.head_commit() == repo.branch_commit("main")  # advanced the current branch
    assert _git(repo.root, "show", "HEAD:doc.md") == "admin content"
    # The explicit date is pinned on both author + committer (hermetic, ADR-0010 D1).
    epoch = str(int(WHEN.timestamp()))
    assert _git(repo.root, "show", "-s", "--format=%at", new) == epoch
    assert _git(repo.root, "show", "-s", "--format=%ct", new) == epoch


def test_commit_all_rejects_naive_datetime(repo: Repo) -> None:
    (repo.root / "doc.md").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        repo.commit_all("msg", when=datetime(2026, 6, 13, 3, 0, 12))  # noqa: DTZ001


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
