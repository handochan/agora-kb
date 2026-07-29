"""Filesystem layout for a single knowledge repo.

Pure path resolution over the per-repo layout defined in docs/DESIGN.md §3. The knowledge itself
(`raw/`, `wiki/`, `index.md`, `log.md`) is git-tracked; `_kb/` is the git-ignored operational spool
(inbox / processing / processed / failed / state / lock). This module computes paths only — it never
touches git or the network and creates no directories on its own (callers create what they write).

Writer namespacing is a hard tenant-isolation boundary (DESIGN §7, invariant 5): a writer name is a
directory component, validated to a safe charset with no path separators or ``..`` so a caller can
never escape the repo's inbox. Event ids are validated by :mod:`agora_kb.core.ids`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "RepoLayout",
    "InvalidWriterError",
    "validate_writer",
    "safe_path_component",
]

# A writer is a single path component: starts alphanumeric, then alphanumerics plus ._- . No
# slashes, no leading dot, and ".."/"." are rejected by the leading-alphanumeric anchor. This is
# what keeps a write confined to ``_kb/inbox/<writer>/`` (tenant isolation, DESIGN §7).
_WRITER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_WRITER_MAX = 128  # keep within a single filesystem path component (NAME_MAX is ~255 bytes)


class InvalidWriterError(ValueError):
    """Raised when a writer name is unsafe as a filesystem path component."""


def validate_writer(writer: str) -> str:
    """Return ``writer`` unchanged if it is a safe path component, else raise.

    Guards the inbox namespace against path traversal (``..``, ``/``) so a write can never land
    outside ``_kb/inbox/<writer>/`` (tenant isolation, DESIGN §7).
    """
    if not isinstance(writer, str) or not _WRITER_RE.match(writer) or len(writer) > _WRITER_MAX:
        raise InvalidWriterError(
            f"invalid writer {writer!r}: must match {_WRITER_RE.pattern}, "
            f"be 1-{_WRITER_MAX} chars, with no path separators or '..'"
        )
    return writer


def safe_path_component(value: str) -> str:
    """Validate ``value`` as a safe single path component (same charset/guards as a writer).

    Reused by the harvester to confine a connector's cursor file to ``_kb/harvest/`` (ADR-0007):
    a DATA-MODEL §8 connector key like ``file:claude-code`` contains a ``:`` that is not a safe
    filename, so the caller sanitizes it (``:`` → ``-``) and passes the *derived* stem here to
    reject any residual path separator / ``..`` before it is interpolated into a path. Raises
    :class:`InvalidWriterError` on an unsafe component (the same guard the inbox namespace uses).
    """
    return validate_writer(value)


@dataclass(frozen=True)
class RepoLayout:
    """Resolves the canonical paths of one knowledge repo rooted at ``root``.

    ``root`` is the repo working directory. All paths are derived; nothing is created here.
    """

    root: Path

    def __post_init__(self) -> None:
        # Normalise to an absolute path without resolving symlinks (the repo root itself may be a
        # symlinked checkout; we only need a stable absolute base for joining).
        object.__setattr__(self, "root", Path(self.root).absolute())

    # --- git-tracked knowledge ------------------------------------------------------------------
    @property
    def index_file(self) -> Path:
        return self.root / "index.md"

    @property
    def log_file(self) -> Path:
        return self.root / "log.md"

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def wiki_dir(self) -> Path:
        return self.root / "wiki"

    @property
    def schema_file(self) -> Path:
        return self.root / "AGENTS.md"

    # --- _kb/ operational spool (git-ignored) ---------------------------------------------------
    @property
    def kb_dir(self) -> Path:
        return self.root / "_kb"

    @property
    def inbox_dir(self) -> Path:
        return self.kb_dir / "inbox"

    @property
    def processing_dir(self) -> Path:
        return self.kb_dir / "processing"

    @property
    def processed_dir(self) -> Path:
        return self.kb_dir / "processed"

    @property
    def failed_dir(self) -> Path:
        return self.kb_dir / "failed"

    @property
    def requeued_dir(self) -> Path:
        """Archived retry-budget records ``_kb/requeued/`` (issue #99, ADR-0011 §5.1).

        MUST live outside :attr:`failed_dir`: the budget derivation is
        ``failed_dir.rglob("error.json")`` and ``rglob`` descends into dotted dirs, so a tidier
        ``_kb/failed/.archive/`` would still be counted and ``agora requeue --reset-attempts``
        would silently reset nothing (verified; locked by a regression test). Not created here.
        """
        return self.kb_dir / "requeued"

    def requeued_record_path(self, *, date: str, run_id: str) -> Path:
        """Archive destination of one run's error record — the ``_kb/failed/`` twin (#99).

        The shape is preserved EXACTLY (``<date>/<run-id>/error.json``) so the mapping from a
        stored ``last_failure.record_path`` to its archived twin is structural rather than a string
        guess, and so two runs on one date can never collide. Both components come from a directory
        name read off disk — ``_kb/failed/`` is operator-editable — so both go through
        :func:`safe_path_component` (the DESIGN §7 guard the inbox/harvest/gold/index namespaces
        use); an unsafe component raises :class:`InvalidWriterError` and the caller reports + skips.
        """
        return (
            self.requeued_dir
            / safe_path_component(date)
            / safe_path_component(run_id)
            / "error.json"
        )

    @property
    def harvest_dir(self) -> Path:
        """Per-connector harvester cursor directory ``_kb/harvest/`` (ADR-0007, DATA-MODEL §6)."""
        return self.kb_dir / "harvest"

    @property
    def index_cache_dir(self) -> Path:
        """Derived READER cache directory ``_kb/index/`` (ADR-0012 §2, issue #26).

        Git-ignored, NEVER canonical, fully rebuildable from the markdown at the curated commit
        (invariant #1). Holds one parsed-note cache per repo (``<repo>.notes.json``; the ADR-0012 §9
        FTS5/ripgrep candidate accelerators are deferred to a load-avoiding reader, issue #28). Not
        created here (this module only computes paths); the writer mkdirs before writing.
        """
        return self.kb_dir / "index"

    @property
    def gold_dir(self) -> Path:
        """Derived gold context-pack directory ``_kb/gold/`` (ADR-0027, issue #37).

        Git-ignored, NEVER canonical, a pure function of ``(curated commit, pack spec)`` and fully
        rebuildable from the wiki (invariant #1) — same derived-state posture as ``_kb/harvest/``
        (cursor) and ``_kb/index/`` (reader cache), DATA-MODEL §6. Holds one ``<pack>.md`` pack plus
        its ``<pack>.meta.json`` sidecar per pack. Not created here (this module only computes
        paths); the writer mkdirs before writing.
        """
        return self.kb_dir / "gold"

    def gold_pack_path(self, pack: str) -> Path:
        """Path of one gold pack ``_kb/gold/<pack>.md`` (ADR-0027 decision 3, issue #37).

        ``pack`` is the pack name (v1 ships an implicit ``default`` pack). It is validated as a safe
        single path component (:func:`safe_path_component`) BEFORE the extension is appended, so a
        malformed/hostile pack name can never escape ``_kb/gold/`` — the same path-traversal guard
        the inbox / harvest / index namespaces use (DESIGN §7).
        """
        stem = safe_path_component(pack)
        return self.gold_dir / f"{stem}.md"

    def gold_meta_path(self, pack: str) -> Path:
        """Path of a gold pack's sidecar meta ``_kb/gold/<pack>.meta.json`` (ADR-0027 decision 3).

        The sidecar carries the wall-clock ``generated_at``/age, the ``(curated_sha, spec_hash)``
        invalidation key, and the per-input provenance — everything the byte-identical pack body
        must NOT carry so its bytes stay stable on rebuild (prompt-cache economics). ``pack`` is
        validated as a safe path component (as in :meth:`gold_pack_path`) before the extension.
        """
        stem = safe_path_component(pack)
        return self.gold_dir / f"{stem}.meta.json"

    @property
    def backup_state_path(self) -> Path:
        """Last push-only backup result ``_kb/backup.json`` (issue #64).

        Non-canonical operational metadata (the same derived-state posture as ``_kb/harvest/``
        cursors, DATA-MODEL §6): the outcome + instant of the last ``agora sync`` / watch-tick
        backup push, read by the ``agora doctor`` observability line. Expendable — losing it only
        blanks that line. Not created here (this module only computes paths).
        """
        return self.kb_dir / "backup.json"

    @property
    def state_file(self) -> Path:
        return self.kb_dir / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.kb_dir / "curator.lock"

    # --- inbox addressing -----------------------------------------------------------------------
    def inbox_writer_dir(self, writer: str) -> Path:
        """Per-writer inbox directory ``_kb/inbox/<writer>/`` (writer validated)."""
        return self.inbox_dir / validate_writer(writer)

    def inbox_item_path(self, writer: str, event_id: str) -> Path:
        """Path of one inbox event ``_kb/inbox/<writer>/<event_id>.md`` (writer validated)."""
        return self.inbox_writer_dir(writer) / f"{event_id}.md"

    # --- harvester cursor addressing ------------------------------------------------------------
    def harvest_cursor_path(self, connector: str) -> Path:
        """Path of one connector's cursor ``_kb/harvest/<stem>.json`` (DATA-MODEL §6, ADR-0007).

        ``connector`` is the operator's ``adapters.yaml`` connector key (e.g. ``file:claude-code``),
        which is NOT a safe filename — DATA-MODEL §8 keys contain a ``:``. The colon is sanitized to
        ``-`` and the resulting stem is validated as a safe single path component
        (:func:`safe_path_component`) so a malformed/hostile connector key can never escape
        ``_kb/harvest/`` (the same path-traversal guard the inbox namespace uses, DESIGN §7).
        """
        stem = safe_path_component(connector.replace(":", "-"))
        return self.harvest_dir / f"{stem}.json"

    # --- derived reader cache addressing (ADR-0012 §2, issue #26) --------------------------------
    def index_notes_path(self, repo: str | None = None) -> Path:
        """Path of the parsed-note cache ``_kb/index/<repo>.notes.json`` (ADR-0012 §2, issue #26).

        ``repo`` defaults to the repo directory name (the ``SearchHit.repo`` identity). It is run
        through :func:`safe_path_component` BEFORE the extension is appended, so an unusual repo dir
        name can never escape ``_kb/index/`` (the same traversal guard the inbox/harvest namespaces
        use, DESIGN §7). A name that is not a safe component raises :class:`InvalidWriterError`; the
        read path treats that as "no cache" and falls back to a full scan (query never crashes).

        A repo directory named e.g. ``My Knowledge`` or ``내지식`` is perfectly legal on disk but is
        NOT a safe component, so no cache file can be addressed for it (issue #108). The raise
        carries an OPERATOR-facing message — what did not happen, why, and what still works — so
        every call site (read fallbacks, ``agora index status``/``clear``, and the ``build_cache``
        write path) can surface the same explanation verbatim instead of a bare regex mismatch.
        The message is deliberately plain prose with NO parentheses and NO raw regex: every one of
        those call sites interpolates it INSIDE its own ``(...)``, so a nested group or a
        ``\\A[A-Za-z0-9]...`` blob would land in front of the operator verbatim. The remedy clause
        follows the actual input — renaming the directory only helps when the stem CAME from the
        directory name (``repo is None``); an explicit stem is the caller's to fix.
        """
        name = repo if repo is not None else self.root.name
        try:
            stem = safe_path_component(name)
        except InvalidWriterError as exc:
            remedy = (
                "rename the repo directory to a safe name to enable the cache"
                if repo is None
                else "pass a cache stem that is a safe component to enable the cache"
            )
            raise InvalidWriterError(
                f"repo name {name!r} is not usable as a cache filename component: it must start "
                f"with a letter or digit and contain only letters, digits, '.', '_' or '-', up to "
                f"{_WRITER_MAX} characters. No cache file can be addressed for this repo, so "
                f"search/query fall back to a full scan; {remedy}"
            ) from exc
        return self.index_cache_dir / f"{stem}.notes.json"
