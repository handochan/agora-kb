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

__all__ = ["RepoLayout", "InvalidWriterError", "validate_writer"]

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
