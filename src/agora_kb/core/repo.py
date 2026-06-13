"""Repo/tenant model + git primitives for the transactional curator run (ADR-0008, DESIGN §4).

A knowledge repo is a git repository whose *curated* content (``index.md``, ``log.md``, ``raw/``,
``wiki/``, the schema doc) is the source of truth (ADR-0001); ``_kb/`` is a git-ignored operational
spool. This module wraps the git operations the single-writer curator needs, **without** depending
on the host's global git config: identity and dates are passed explicitly and global/system config +
hooks are neutralized, so the publish step is hermetic, reproducible, and credential-free.

The transactional publish (ADR-0008 steps 4-5): the backend edits a temporary **detached** worktree
at the current curated commit; deterministic code commits it (advancing only the detached HEAD, not
the branch), then **compare-and-swaps** the curated branch ref from the run's base commit to the new
commit (:meth:`Repo.compare_and_swap_branch`). The CAS is the durable publish point — a reader only
ever resolves a published commit, never the backend's mutable worktree.

Caller contract (the curator/worker owns the run loop):
- Worktree cleanup is the caller's duty — prefer the :meth:`Repo.worktree` context manager, which
  guarantees teardown; :meth:`create_worktree`/:meth:`remove_worktree` are the explicit form.
- An unpublished detached commit is reachable only via the sha returned by :meth:`commit_worktree`;
  persist it (manifest ``published_commit``, DATA-MODEL §5) BEFORE teardown, then use
  :meth:`is_published` on restart to drive ADR-0008 step-6 recovery.
- :meth:`commit_worktree` stages the whole worktree (``git add -A``); call it only AFTER the
  deterministic allowlist/diff validation (ADR-0011 §4) has rejected out-of-allowlist changes.

Git operations shell ``git`` via argv arrays (never a shell string), matching the no-shell rule used
for backend adapters (ADR-0008 step 3, DATA-MODEL §8).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .layout import RepoLayout

__all__ = ["Repo", "GitError"]

_DEFAULT_BRANCH = "main"
_DEFAULT_AUTHOR_NAME = "Agora Curator"
_DEFAULT_AUTHOR_EMAIL = "curator@agora.local"
_GITIGNORE = (
    "# Agora operational spool — rebuildable, never canonical (ADR-0001).\n_kb/\n.DS_Store\n"
)
# A schema-compliant root index.md (the ADR-0010 `index` note frontmatter) so a freshly-initialized
# repo lints clean (schema.lint L1-4); the curator's APPLY later fills children/updated, preserving
# these keys. {date} is the init date (YYYY-MM-DD).
_SEED_INDEX = (
    "---\n"
    "title: Index\n"
    "type: index\n"
    "aliases: []\n"
    "tags: []\n"
    "created: '{date}'\n"
    "updated: '{date}'\n"
    "status: active\n"
    "summary: Knowledge base index.\n"
    "children: []\n"
    "---\n\n# Knowledge base\n"
)
# A full git object id: 40 hex (sha-1) or 64 hex (sha-256). Used to reject names/short-shas and,
# crucially, the all-zero oid that `git update-ref` would interpret as a ref DELETE.
_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")


def _require_commit_sha(value: str) -> None:
    if not isinstance(value, str) or not _SHA_RE.match(value) or set(value) == {"0"}:
        raise ValueError(f"expected a full non-zero hex object id, got {value!r}")


class GitError(RuntimeError):
    """A git command failed to run or exited non-zero (carries command/returncode/stderr)."""

    def __init__(
        self,
        message: str,
        *,
        command: tuple[str, ...] | None = None,
        returncode: int | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


class Repo:
    """One knowledge repo: layout + the git operations the curator transaction needs."""

    def __init__(self, layout: RepoLayout, *, branch: str = _DEFAULT_BRANCH) -> None:
        self._layout = layout
        self._branch = branch

    @classmethod
    def resolve(cls, root: Path | str, *, branch: str = _DEFAULT_BRANCH) -> Repo:
        """Build a :class:`Repo` for the repo rooted at ``root`` (no git calls)."""
        return cls(RepoLayout(Path(root)), branch=branch)

    @property
    def layout(self) -> RepoLayout:
        return self._layout

    @property
    def root(self) -> Path:
        return self._layout.root

    @property
    def branch(self) -> str:
        return self._branch

    # --- git plumbing ---------------------------------------------------------------------------
    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        # core.hooksPath=<devnull> on EVERY call neutralizes host-global/repo hooks so a planted or
        # ambient hook can never run arbitrary code during the deterministic publish (ADR-0008).
        cmd = ["git", "-c", f"core.hooksPath={os.devnull}", *args]
        try:
            cp = subprocess.run(  # noqa: S603 (argv list, no shell)
                cmd, cwd=str(cwd or self.root), capture_output=True, text=True, env=env
            )
        except OSError as exc:  # e.g. cwd does not exist — surface as GitError, not a raw OSError
            raise GitError(
                f"git {' '.join(args)} could not run: {exc}", command=tuple(args)
            ) from exc
        if check and cp.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed (rc={cp.returncode}): {cp.stderr.strip()}",
                command=tuple(args),
                returncode=cp.returncode,
                stderr=cp.stderr.strip(),
            )
        return cp

    def _commit_env(self, *, name: str, email: str, when: datetime | None) -> dict[str, str]:
        """Build a hermetic commit environment: explicit identity, no host global/system config,
        and (when given) explicit author+committer dates so the commit sha is reproducible
        (ADR-0010 D1)."""
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        if when is not None:
            stamp = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            env["GIT_AUTHOR_DATE"] = stamp
            env["GIT_COMMITTER_DATE"] = stamp
        return env

    # --- lifecycle ------------------------------------------------------------------------------
    def is_initialized(self) -> bool:
        """True iff ``root`` is a git repo with at least one commit on the curated branch."""
        if not (self.root / ".git").exists():
            return False
        return self._git("rev-parse", "--verify", "HEAD", check=False).returncode == 0

    def init(self, *, when: datetime | None = None) -> str:
        """Initialize a knowledge repo: ``git init`` on the curated branch, ignore ``_kb/``, and
        make the initial commit (so a curated ref + HEAD exist for worktrees and CAS). Idempotent —
        returns the current commit if already initialized. ``when`` pins the commit's date."""
        if self.is_initialized():
            return self.head_commit()
        self.root.mkdir(parents=True, exist_ok=True)
        self._git("init", "-b", self._branch)
        (self.root / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
        when_resolved = when or datetime.now(UTC)
        index = self.layout.index_file
        if not index.exists():
            seed_date = when_resolved.astimezone(UTC).strftime("%Y-%m-%d")
            index.write_text(_SEED_INDEX.format(date=seed_date), encoding="utf-8")
        env = self._commit_env(
            name=_DEFAULT_AUTHOR_NAME, email=_DEFAULT_AUTHOR_EMAIL, when=when_resolved
        )
        self._git("add", "-A", env=env)
        self._commit("chore: initialize agora knowledge repo", env=env)
        return self.head_commit()

    # --- reads ----------------------------------------------------------------------------------
    def head_commit(self) -> str:
        """Full sha of the current ``HEAD`` (the curated tip in the main working copy)."""
        return self._git("rev-parse", "HEAD").stdout.strip()

    def branch_commit(self, branch: str | None = None) -> str:
        """Full sha at the tip of a branch ref (defaults to the curated branch)."""
        ref = f"refs/heads/{branch or self._branch}"
        return self._git("rev-parse", "--verify", ref).stdout.strip()

    def current_branch(self) -> str:
        """Name of the checked-out branch in the main working copy."""
        return self._git("symbolic-ref", "--short", "HEAD").stdout.strip()

    def is_published(self, commit: str, *, branch: str | None = None) -> bool:
        """True iff ``commit`` is reachable from the curated branch tip (i.e. it was published).

        Drives ADR-0008 step-6 recovery: with the run's new-commit sha persisted in the manifest
        (DATA-MODEL §5 ``published_commit``), this answers "did the CAS land?" on restart so a
        published run is finalized without re-invoking the backend. Uses ``git merge-base
        --is-ancestor`` (a commit is its own ancestor, so the published tip itself counts)."""
        _require_commit_sha(commit)
        ref = f"refs/heads/{branch or self._branch}"
        return self._git("merge-base", "--is-ancestor", commit, ref, check=False).returncode == 0

    # --- transactional worktree ----------------------------------------------------------------
    def create_worktree(self, *, at: str) -> Path:
        """Add a temporary **detached** worktree checked out at commit ``at``; return its path.

        Detached so commits made inside it advance only that worktree's HEAD — the curated branch
        ref does not move until :meth:`compare_and_swap_branch` publishes (ADR-0008). The worktree
        lives under a private (0700) holder dir; the caller MUST eventually call
        :meth:`remove_worktree` (or use :meth:`worktree`)."""
        holder = Path(tempfile.mkdtemp(prefix="agora-wt-"))  # mode 0700
        wt = holder / "tree"
        try:
            self._git("worktree", "add", "--detach", str(wt), at)
        except GitError:
            shutil.rmtree(holder, ignore_errors=True)
            raise
        return wt

    def remove_worktree(self, path: Path) -> None:
        """Remove a worktree added by :meth:`create_worktree`, its private holder dir, and the git
        admin entry. Idempotent — safe to call on an already-removed worktree."""
        self._git("worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        holder = path.parent
        if holder.name.startswith("agora-wt-"):  # only ever remove our own holder dirs
            shutil.rmtree(holder, ignore_errors=True)
        self._git("worktree", "prune", check=False)

    @contextmanager
    def worktree(self, *, at: str) -> Iterator[Path]:
        """Context manager: a detached worktree at ``at`` that is always torn down on exit."""
        wt = self.create_worktree(at=at)
        try:
            yield wt
        finally:
            self.remove_worktree(wt)

    def commit_worktree(
        self,
        worktree: Path,
        message: str,
        *,
        when: datetime,
        author_name: str = _DEFAULT_AUTHOR_NAME,
        author_email: str = _DEFAULT_AUTHOR_EMAIL,
    ) -> str:
        """Stage all changes in ``worktree`` (``git add -A``) and commit them; return the new sha.

        Identity, dates, and config are hermetic (see :meth:`_commit_env`) so the commit is
        reproducible and credential-free (ADR-0010 D1). The commit advances only the detached
        worktree HEAD; publish via :meth:`compare_and_swap_branch` afterwards. Raises
        :class:`GitError` if there is nothing to commit (an empty diff is a no-op, not a publish).
        Precondition: the deterministic allowlist/diff validation (ADR-0011 §4) has already run,
        since ``git add -A`` stages every change in the worktree."""
        if when.tzinfo is None:
            raise ValueError("commit timestamp must be timezone-aware (UTC)")
        env = self._commit_env(name=author_name, email=author_email, when=when)
        self._git("add", "-A", cwd=worktree, env=env)
        self._commit(message, cwd=worktree, env=env)
        return self._git("rev-parse", "HEAD", cwd=worktree).stdout.strip()

    def compare_and_swap_branch(
        self, *, expected: str, new: str, branch: str | None = None
    ) -> bool:
        """Atomically move the curated branch ref ``expected → new``; return False if it has moved.

        Wraps ``git update-ref <ref> <new> <expected>``, whose old-value guard makes the swap a true
        CAS: if another writer advanced the branch since ``expected`` was read, the update is
        rejected and nothing changes (the run must rebase/retry). The optimistic-concurrency
        backstop of the single-writer model (ADR-0002/0008). Both ``expected`` and ``new`` must be
        full, non-zero object ids (a malformed or all-zero ``new`` would otherwise make
        ``update-ref`` DELETE the branch). This is an UPDATE only; creating a new branch ref
        (review-mode/PR) is out of scope."""
        _require_commit_sha(expected)
        _require_commit_sha(new)
        ref = f"refs/heads/{branch or self._branch}"
        rc = self._git("update-ref", ref, new, expected, check=False).returncode
        return rc == 0

    # --- internals ------------------------------------------------------------------------------
    def _commit(
        self, message: str, *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> None:
        commit_env = env or self._commit_env(
            name=_DEFAULT_AUTHOR_NAME, email=_DEFAULT_AUTHOR_EMAIL, when=None
        )
        # --no-verify belt-and-suspenders with the global core.hooksPath override in _git.
        self._git("commit", "--no-gpg-sign", "--no-verify", "-m", message, cwd=cwd, env=commit_env)
