"""Atomic, crash-durable file writes shared across the core (write path + curator state/manifest).

Two modes, one durability guarantee:
- ``exclusive=True`` (create-only): for **immutable** files such as inbox events — uses ``os.link``
  so a second write to the same path raises ``FileExistsError`` instead of clobbering, which holds
  the append-only invariant (invariant 3) even under a same-id race.
- ``exclusive=False`` (overwrite): for **mutable single-writer** state such as ``_kb/state.json``,
  rewritten in place under the curator lock — uses ``os.replace`` (atomic same-filesystem swap).

Both write to a uniquely-named temp file in the destination directory, fsync the file, link/replace
into place, then fsync the parent directory so the created/replaced directory entry survives a crash
(ADR-0002: a captured item is durable immediately). The temp file is always cleaned up.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

__all__ = ["atomic_write_text", "fsync_dir"]


def fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory so a created/removed entry is durable (POSIX only)."""
    if os.name != "posix":
        return
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(dest: Path, text: str, *, exclusive: bool) -> None:
    """Durably write ``text`` to ``dest`` via temp-file + link/replace + directory fsync.

    With ``exclusive=True`` the destination must not already exist (raises ``FileExistsError``);
    with ``exclusive=False`` an existing destination is atomically replaced.
    """
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if exclusive:
            os.link(tmp, dest)  # create-only: raises FileExistsError if dest exists
        else:
            os.replace(tmp, dest)  # atomic overwrite on the same filesystem
    finally:
        tmp.unlink(missing_ok=True)
    fsync_dir(dest.parent)
