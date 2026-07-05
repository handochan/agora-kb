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
        """
        stem = safe_path_component(repo if repo is not None else self.root.name)
        return self.index_cache_dir / f"{stem}.notes.json"
