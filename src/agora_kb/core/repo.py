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

Everything here is LOCAL except :meth:`Repo.push_backup` (issue #64), the single outbound
operation: a strictly push-only, fast-forward-only backup of the curated branch to a configured
remote. There is deliberately no pull/fetch/bidirectional code — multi-machine reconciliation is
deferred to the #46 topology ADR.
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
#
# The seed index is also the OKF v0.1 BUNDLE ROOT (ADR-0014 D2): OKF conformance is the DEFAULT
# posture at `repo init` (ratified decision #4), so the seed carries `okf_version: '0.1'` (bundle
# root ONLY, placed after `type` per the OKF spec), `description` mirroring `summary` (after
# `summary`; ratified decision #2), and a DETERMINISTIC `timestamp` == `<date>T00:00:00Z` (after
# `updated`; the run reads no wall clock — ADR-0010 D1 / ADR-0014 ratified decision #5). A fresh
# repo is therefore a conformant OKF bundle the moment it is initialized.
_SEED_INDEX = (
    "---\n"
    "title: Index\n"
    "type: index\n"
    "okf_version: '0.1'\n"
    "aliases: []\n"
    "tags: []\n"
    "created: '{date}'\n"
    "updated: '{date}'\n"
    "timestamp: '{date}T00:00:00Z'\n"
    "status: active\n"
    "summary: Knowledge base index.\n"
    "description: Knowledge base index.\n"
    "children: []\n"
    "---\n\n# Knowledge base\n"
)
# The KB WIKI SCHEMA 2 root map (ADR-0041 D1.2). Same OKF posture as the schema-1 seed above — it
# is still the bundle root — plus the D2 common base: `kind` (the DIRECTORY-mirroring kind, D2.1),
# `kb` (the `_meta/kb.yaml` ULID, D1.5) and `subjects: []`, a legal, honest empty subject list on a
# note that genuinely has no subject (D2.2). `children: []` with an empty body is an EMPTY ROOT MAP:
# L1-6 compares the declared set against the body child-bullet set and both are empty.
#
# `index.md` sits at the repo ROOT, not under `wiki/maps/`, so the directory rule cannot name it —
# which is exactly why it carries the `kind:` mirror and why `RepoLayout.note_path_for('index', …)`
# returns the root path. It is the root OF the map tier, not a member of it.
#
# `type: index` is retained as the OKF MIRROR of `kind`, not as the kind authority (D2.5 / OD-3):
# emitted for the same reason `description` mirrors `summary`, so an OKF/Obsidian consumer that
# keys on `type` still sees one. Nothing in Agora reads it under schema 2.
_SEED_INDEX_V2 = (
    "---\n"
    "title: Index\n"
    "kind: index\n"
    "type: index\n"
    "kb: {kb_id}\n"
    "okf_version: '0.1'\n"
    "subjects: []\n"
    "aliases: []\n"
    "tags: []\n"
    "created: '{date}'\n"
    "updated: '{date}'\n"
    "timestamp: '{date}T00:00:00Z'\n"
    "status: active\n"
    "summary: Knowledge base index.\n"
    "description: Knowledge base index.\n"
    # `derived` + `provenance` complete the D2 common base, in D2's own key order. They are here
    # rather than left to APPLY's first re-render because `_common_frontmatter`'s contract is that
    # the seed and a re-rendered note read IDENTICALLY in a diff: without them the first curate run
    # appended both keys AFTER `children`, so the bundle root was the one note whose frontmatter was
    # not the shape D2 states — and it stayed that way silently, since lint grades `provenance:`
    # only when present. `writers` is empty and honest (no authn plane before Phase 4); `agents` is
    # empty because nothing has contributed to a freshly seeded index.
    "derived: false\n"
    "provenance:\n"
    "  writers: []\n"
    "  agents: []\n"
    "children: []\n"
    "---\n\n# Knowledge base\n"
)
# A full git object id: 40 hex (sha-1) or 64 hex (sha-256). Used to reject names/short-shas and,
# crucially, the all-zero oid that `git update-ref` would interpret as a ref DELETE.
_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")
# Conservative sanity for a push destination (a git remote NAME or URL, issue #64): printable with
# no whitespace/control characters. Argv-array execution (never a shell) already prevents shell
# injection; this guard only rejects values that could be misparsed by git itself — an embedded
# newline/space smuggling extra arguments into an error message, or a leading "-" reading as a git
# option (belt-and-suspenders with the "--" end-of-options marker in :meth:`Repo.push_backup`).
_REMOTE_BAD_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")
# Upper bound for the ONE network-touching git operation (:meth:`Repo.push_backup`). Every other
# _git call is local and fast; a push can hang UNBOUNDED on a credential prompt, an ssh host-key /
# passphrase prompt, or a TCP blackhole — and in `agora watch` (backup.auto) an unbounded hang
# would stall the curation scheduler itself, breaking the best-effort contract. Generous enough
# for a slow first push of a markdown KB; callers (the watch tick) may pass a tighter bound.
_PUSH_TIMEOUT_SECONDS = 300.0


def _require_commit_sha(value: str) -> None:
    if not isinstance(value, str) or not _SHA_RE.match(value) or set(value) == {"0"}:
        raise ValueError(f"expected a full non-zero hex object id, got {value!r}")


def _printable(text: str) -> str:
    """Return ``text`` with any ``errors="surrogateescape"`` lone surrogate made printable.

    ``_git`` decodes git's stdout/stderr with ``errors="surrogateescape"`` (#85) so an invalid byte
    never raises out of the subprocess call; that is correct for round-tripping, but it means a
    string built from ``cp.stderr`` (e.g. a remote/server-hook message on ``push_backup``, which
    this process does not control) can carry a lone surrogate code point straight into a
    :class:`GitError` message. Embedding it directly (``f"{stderr}"``, as opposed to ``{stderr!r}``)
    then raises ``UnicodeEncodeError`` the moment an operator-facing ``print``/log tries to encode
    that message under a strict stream (a redirected cp949 console,
    ``PYTHONIOENCODING=utf-8:strict``) — turning a best-effort failure (a push, a watch tick) into
    a crash instead of a clean error.
    Re-encoding with ``surrogateescape`` and decoding back with ``replace`` (U+FFFD) is lossy but
    always printable, which is what an error MESSAGE needs; nothing here is byte-compared, so the
    lossiness costs nothing meaningful.
    """
    return text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")


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
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        # core.hooksPath=<devnull> on EVERY call neutralizes host-global/repo hooks so a planted or
        # ambient hook can never run arbitrary code during the deterministic publish (ADR-0008).
        # ``timeout`` (seconds; default unbounded — every local call is fast) exists for the one
        # network-touching operation, :meth:`push_backup`: on expiry the child is killed and the
        # hang surfaces as a plain :class:`GitError`, never an uncaught TimeoutExpired.
        cmd = ["git", "-c", f"core.hooksPath={os.devnull}", *args]
        try:
            cp = subprocess.run(  # noqa: S603 (argv list, no shell)
                cmd,
                cwd=str(cwd or self.root),
                capture_output=True,
                text=True,
                # git emits UTF-8 path bytes regardless of the host console codepage. WITHOUT an
                # explicit encoding, ``text=True`` decodes with the LOCALE encoding, so a cp949 /
                # latin-1 Windows console mis-decodes (or raises on) any non-ASCII path or commit
                # message git prints back (#85). Pin UTF-8 so the decode is host-independent.
                encoding="utf-8",
                # ``errors="strict"`` (the default) would let a byte sequence that is not valid
                # UTF-8 — realistic on ``push_backup``'s remote/server-hook output, which this
                # process does not control — raise UnicodeDecodeError. That is a ValueError, which
                # matches neither the TimeoutExpired/OSError handlers below nor any GitError catch
                # at a caller, so it would escape this method as a raw exception instead of the
                # GitError every caller (init/commit_worktree/push_backup/sync) is written against.
                # ``surrogateescape`` never raises and round-trips the exact bytes losslessly, so an
                # undecodable byte still reaches ``cp.stderr``/``cp.stdout`` — just as an
                # unprintable surrogate — for GitError's message to report.
                errors="surrogateescape",
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(
                f"git {' '.join(args)} timed out after {timeout:g}s (process killed; "
                f"the operation did not complete)",
                command=tuple(args),
            ) from exc
        except OSError as exc:  # e.g. cwd does not exist — surface as GitError, not a raw OSError
            raise GitError(
                f"git {' '.join(args)} could not run: {exc}", command=tuple(args)
            ) from exc
        if check and cp.returncode != 0:
            stderr = _printable(cp.stderr.strip())
            raise GitError(
                f"git {' '.join(args)} failed (rc={cp.returncode}): {stderr}",
                command=tuple(args),
                returncode=cp.returncode,
                stderr=stderr,
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

    def init(
        self, *, when: datetime | None = None, schema_version: int = 1, kb_id: str | None = None
    ) -> str:
        """Initialize a knowledge repo: ``git init`` on the curated branch, ignore ``_kb/``, and
        make the initial commit (so a curated ref + HEAD exist for worktrees and CAS). Idempotent —
        returns the current commit if already initialized. ``when`` pins the commit's date.

        ``schema_version`` selects the KB wiki schema the seed ``index.md`` is written in: ``1`` is
        ADR-0010's root index, ``2`` is ADR-0041 D1.2's root map. Anything ``>= 2`` REQUIRES
        ``kb_id`` — the ``_meta/kb.yaml`` ULID every schema-2 note mirrors into ``kb:`` (D1.5) —
        and raises :class:`ValueError` without one.

        **That is why the default is 1 rather than the version this build writes.** ``Repo.init``
        cannot MINT a ``kb_id``: D1.5 says it is stamped once at repo creation and never rewritten,
        which makes it the init COMMAND's fact (``agora repo init`` writes ``_meta/kb.yaml`` before
        calling this, then passes the id down). Defaulting to a version this method cannot serve
        unaided would turn every existing caller into a crash; defaulting to 1 keeps them
        byte-identical and makes "seed schema 2" the deliberate, identity-carrying act it is.

        Both seeds are written ONLY when ``index.md`` is absent, and the whole method returns early
        on an already-initialized repo — so a re-init can never rewrite a root map, and in
        particular can never restamp the ``kind:``/``type:`` schema mirror of a repo built at the
        other version.
        """
        if self.is_initialized():
            return self.head_commit()
        if schema_version >= 2 and not kb_id:
            raise ValueError(
                f"seeding a KB wiki schema {schema_version} repo requires kb_id: the "
                f"_meta/kb.yaml ULID is minted ONCE at repo creation and every note mirrors it "
                f"into `kb:` (ADR-0041 D1.5/D2)"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        self._git("init", "-b", self._branch)
        (self.root / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
        when_resolved = when or datetime.now(UTC)
        index = self.layout.index_file
        if not index.exists():
            seed_date = when_resolved.astimezone(UTC).strftime("%Y-%m-%d")
            seed = (
                _SEED_INDEX_V2.format(date=seed_date, kb_id=kb_id)
                if schema_version >= 2
                else _SEED_INDEX.format(date=seed_date)
            )
            index.write_text(seed, encoding="utf-8")
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

    def commit_committer_datetime(self, commit: str) -> datetime:
        """Return a commit's COMMITTER timestamp as a timezone-aware UTC :class:`datetime`.

        The gold-pack recency decay is anchored to the curated commit's committer instant — NEVER a
        wall clock — so a rebuild at a fixed commit is byte-identical (ADR-0027 decision 4). Uses
        ``git show -s --format=%cI`` (strict ISO-8601 committer date). ``commit`` must be a full
        non-zero object id (a branch tip resolved via :meth:`branch_commit`); a malformed id is
        rejected before it reaches git. Raises :class:`GitError` if the commit cannot be read."""
        _require_commit_sha(commit)
        out = self._git("show", "-s", "--format=%cI", commit).stdout.strip()
        try:
            return datetime.fromisoformat(out).astimezone(UTC)
        except ValueError as exc:  # a git that emitted a non-ISO committer date (should not happen)
            raise GitError(
                f"could not parse committer date {out!r} for {commit}", command=("show", commit)
            ) from exc

    # --- repo-owner sync / admin commit ---------------------------------------------------------
    def sync_to_branch(self, branch: str | None = None) -> str:
        """Fast-forward the MAIN working copy to the curated branch tip; return the new HEAD sha.

        After the curator advances the curated ref via :meth:`compare_and_swap_branch` (ADR-0008
        step 5), the durable publish lives at the branch tip; the repo-owner's main working copy
        must be brought up to that commit so the read path (``core.Wiki`` / ``kb_query``, which read
        the on-disk tree) resolves the PUBLISHED content (ADR-0008: "readers resolve a published
        commit"). FAST-FORWARD ONLY: this never creates a merge commit and never rewrites a fork.

        Two post-CAS shapes are reconciled, both SAFE (raise :class:`GitError` not clobber):

        * HEAD is strictly BEHIND the branch tip — ``git merge --ff-only`` advances HEAD + checks
          out the tip, refusing if the working tree is dirty or has diverged (no fast-forward).
        * HEAD already EQUALS the branch tip but the index/working tree are STALE — the
          single-writer CAS moves ``refs/heads/<branch>`` (which HEAD symbolically tracks), so the
          ref jumps ahead of the unmaterialized tree. ``git read-tree -m -u`` does a two-way merge
          to the tip that materializes the published files while REFUSING (GitError) to overwrite a
          conflicting uncommitted owner edit — the same never-clobber guarantee as ``--ff-only``.

        Hooks are neutralized by the global ``core.hooksPath`` override in :meth:`_git`. A genuine
        no-op (tip already checked out, tree clean) returns the unchanged HEAD. ``branch`` defaults
        to the curated branch.

        IDENTITY-INDEPENDENT (so omitting ``env=_commit_env`` is intentional, not an oversight):
        this is the only mutating git path in this module that does not pass through
        :meth:`_commit_env`, and that is safe because it creates NO commit object. ``merge
        --ff-only`` is a pure ref/worktree fast-forward that REFUSES (raising :class:`GitError`)
        rather than falling back to a merge commit, and ``read-tree -m -u`` is a two-way merge that
        only materializes a tree — neither authors a commit, so no committer identity is consumed
        and the hermeticity rationale of :meth:`_commit_env` does not apply. If a merge-commit
        fallback is ever added here, it MUST take ``env=_commit_env`` to stay hermetic."""
        ref = f"refs/heads/{branch or self._branch}"
        tip = self.branch_commit(branch)
        if self.head_commit() != tip:
            # HEAD is behind: a real fast-forward (advances HEAD, updates the tree, refuses dirty).
            self._git("merge", "--ff-only", ref)
            return self.head_commit()
        # HEAD already at the tip (the CAS moved the tracked ref under HEAD): materialize the stale
        # index/working tree to the tip without clobbering uncommitted owner edits (two-way merge).
        self._git("read-tree", "-m", "-u", tip)
        return self.head_commit()

    def commit_all(self, message: str, *, when: datetime) -> str:
        """Stage (``git add -A``) + commit the MAIN working tree on the current branch; return sha.

        For repo-init / admin operations (e.g. committing the schema emitted by
        :func:`agora_kb.schema.emit.emit_schema`) — NOT the transactional curator publish, which
        commits a detached worktree via :meth:`commit_worktree` and publishes via CAS. Identity,
        dates, and config are hermetic (see :meth:`_commit_env`) and hooks are neutralized
        (``--no-verify`` + the global ``core.hooksPath`` override), so the commit is reproducible
        and credential-free (ADR-0010 D1). ``when`` pins the author+committer date."""
        if when.tzinfo is None:
            raise ValueError("commit timestamp must be timezone-aware (UTC)")
        env = self._commit_env(name=_DEFAULT_AUTHOR_NAME, email=_DEFAULT_AUTHOR_EMAIL, when=when)
        self._git("add", "-A", env=env)
        self._commit(message, env=env)
        return self.head_commit()

    def push_backup(
        self,
        remote: str,
        *,
        branch: str | None = None,
        timeout: float | None = _PUSH_TIMEOUT_SECONDS,
        interactive: bool = True,
    ) -> str:
        """Push the curated branch tip to ``remote`` (push-only backup, issue #64); return its sha.

        The ONE outbound git operation in the codebase: local curated branch → a backup remote
        (``remote`` is a git remote NAME or URL, passed through as a single argv element after a
        ``--`` end-of-options marker — never a shell string). STRICTLY PUSH-ONLY: nothing here (or
        anywhere in this slice) pulls, fetches, or configures remotes — reconciling a remote that
        has moved ahead is the #46 multi-machine topology ADR's territory.

        Fast-forward only, NEVER ``--force``: a non-fast-forward rejection means the remote's
        branch is ahead of this repo's (another machine has likely pushed) and is surfaced as a
        :class:`GitError` that names issue #46 rather than overwritten — a backup must never
        destroy a sibling copy's history. Any other failure (unreachable remote, auth) is a plain
        :class:`GitError`. The caller owns the best-effort contract: a push failure must never fail
        the curation transaction that triggered it (the publish CAS is already durable locally).

        BOUNDED: the push runs under ``timeout`` seconds (default ``_PUSH_TIMEOUT_SECONDS``; the
        only network-touching git call in the codebase) — a credential prompt left unanswered, an
        ssh host-key/passphrase prompt, or a TCP blackhole becomes a :class:`GitError`, never an
        unbounded hang. ``interactive=False`` (the unattended ``agora watch`` auto-push) goes
        further and FAILS FAST instead of prompting at all: ``GIT_TERMINAL_PROMPT=0`` plus askpass
        neutralized plus ssh BatchMode (see :meth:`_non_interactive_push_env`), so a
        needs-credentials push errors immediately into the caller's best-effort record/warn path
        rather than stalling the scheduler until the timeout.

        Credentials are AMBIENT on purpose: unlike the hermetic commit paths, the push inherits the
        operator's environment/global config (credential helpers, ssh agent) — it authors no commit,
        so the reproducibility rationale of :meth:`_commit_env` does not apply
        (``interactive=False`` only disables *prompts*; helpers and the ssh agent still work).
        Hooks stay neutralized by the global ``core.hooksPath`` override in :meth:`_git`.
        """
        if not isinstance(remote, str) or not remote:
            raise ValueError("backup remote must be a non-empty string (a git remote name or URL)")
        if remote.startswith("-") or _REMOTE_BAD_CHARS.search(remote):
            raise ValueError(
                f"backup remote {remote!r} is not a plausible git remote name/URL "
                f"(whitespace/control characters and a leading '-' are rejected)"
            )
        name = branch or self._branch
        tip = self.branch_commit(name)
        # Explicit full refspec, and the SOURCE is the sha just read — not the live branch ref — so
        # the returned/recorded sha and the remote tip cannot diverge when a concurrent publish
        # advances the branch between this read and the push (the observability file's one job is
        # naming exactly which commit is backed up). No dependence on push.default either way.
        refspec = f"{tip}:refs/heads/{name}"
        env = None if interactive else self._non_interactive_push_env()
        cp = self._git("push", "--", remote, refspec, check=False, env=env, timeout=timeout)
        if cp.returncode != 0:
            stderr = _printable(cp.stderr.strip())
            if "non-fast-forward" in stderr or "[rejected]" in stderr:
                raise GitError(
                    f"push to {remote!r} rejected (non-fast-forward): the remote's {name!r} is "
                    f"ahead of this repo's — another machine has likely pushed. Push-only backup "
                    f"never forces; reconciling divergent copies is the multi-machine topology "
                    f"decision (issue #46), not this operation. git said: {stderr}",
                    command=("push", "--", remote, refspec),
                    returncode=cp.returncode,
                    stderr=stderr,
                )
            raise GitError(
                f"git push to {remote!r} failed (rc={cp.returncode}): {stderr}",
                command=("push", "--", remote, refspec),
                returncode=cp.returncode,
                stderr=stderr,
            )
        return tip

    def _non_interactive_push_env(self) -> dict[str, str]:
        """Push environment that FAILS FAST instead of prompting (the unattended watch auto-push).

        ``GIT_TERMINAL_PROMPT=0`` disables terminal credential prompts; ``GIT_ASKPASS`` /
        ``SSH_ASKPASS`` are overridden to :data:`os.devnull` (not executable, so an askpass attempt
        — including an ambient GUI helper an editor exported — errors instead of waiting on a
        dialog); ssh gets ``-oBatchMode=yes`` so host-key/passphrase prompts fail immediately.
        The ssh override is applied ONLY when the operator routes ssh no other way (no
        ``GIT_SSH_COMMAND``/``GIT_SSH`` in the environment and no ``core.sshCommand`` in git
        config) — an explicit operator transport is never clobbered. Non-interactive credential
        sources (credential helpers, the ssh agent) keep working; only *prompts* are disabled.
        """
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": os.devnull,
            "SSH_ASKPASS": os.devnull,
        }
        if "GIT_SSH_COMMAND" not in os.environ and "GIT_SSH" not in os.environ:
            has_config_ssh = (
                self._git("config", "--get", "core.sshCommand", check=False).returncode == 0
            )
            if not has_config_ssh:
                env["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes"
        return env

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
