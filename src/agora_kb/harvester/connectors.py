"""Harvester read-adapters — connectors that scan an agent's memory into candidate facts (ADR-0007).

A **connector** is the read-side mirror of the curator's write-adapter brain: it pulls knowledge
*out of* another agent's memory system and hands the harvester a list of :class:`HarvestedFact`,
which the orchestrator writes into the inbox as gated candidates (``kind=candidate``,
``confidence=low``) for the curator's keep/merge/drop review (ADR-0007 mechanism 2). This module
holds the :class:`Connector` Protocol (the Phase-4 extension seam for Letta/mem0 API connectors) and
the only Phase-2 implementation, :class:`FileConnector`, which diffs a markdown ``MEMORY.md`` file
since the last scan (DESIGN §6, ARCHITECTURE §3.3).

Security posture — harvested memory is **untrusted input** (ADR-0007; INGEST-CONTRACT §8 treats all
captured/harvested text as potential prompt-injection / memory-poisoning):

* **Path safety.** ``~`` is expanded deterministically, the glob is resolved with symlinks followed,
  and every match must resolve *within* the glob's non-wildcard base root — a ``**`` cannot follow a
  symlink out to ``~/.ssh`` or another tenant's files. Match count and per-file size are capped so a
  large or hostile tree cannot exhaust the host.
* **Opaque body text.** Each fact is neutralized before it leaves the connector: agora structural
  sentinels (``<!-- agora:body:… -->``, the curator's region markers) are stripped so poisoned
  memory cannot smuggle engine structure into the candidate bundle the planning brain reads.
* **Identity validation.** The ``agent`` token (the ``<agent>`` in ``harvest:<agent>``) is validated
  to a safe path component at construction, so it can never escape the inbox/cursor namespace.

The fact's identity (``fact_key``) is the canonical ``content_sha256`` of its text (DATA-MODEL §11.2
normalization), so it equals the curator's tier-2 dedup key for the same content and can double as
the inbox ``event_key`` for pending-delivery idempotency (ADR-0017).
"""

from __future__ import annotations

import glob as _glob
import os.path
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from agora_kb.core.hashing import content_sha256
from agora_kb.core.layout import validate_writer

__all__ = [
    "Scope",
    "HarvestedFact",
    "ConnectorScan",
    "Connector",
    "FileConnector",
    "ConnectorError",
]

# A markdown ATX heading line (``# …`` … ``###### …``) — CONTEXT only, never emitted as a fact.
_HEADING_RE = re.compile(r"^#{1,6}\s")
# A top-level (column-0, un-indented) markdown list item: ``- `` / ``* `` / ``+ `` / ``1. `` /
# ``1) ``.
_TOP_LIST_RE = re.compile(r"^([-*+]|\d+[.)])\s+")
# A block that is ONLY a list marker (an empty bullet placeholder, e.g. ``-`` / ``*`` / ``1.``) —
# not a fact; dropped by _build_fact so a content-free bullet never becomes a junk candidate.
_MARKER_ONLY_RE = re.compile(r"\A([-*+]|\d+[.)])\Z")
# An agora structural sentinel HTML comment (the curator's body-region markers, apply.py grammar).
# Stripped from harvested text so a poisoned memory bullet cannot inject a fake region into the
# candidate bundle the planning brain reads (defense-in-depth; the curator's diff gate is the real
# integrity boundary).
_AGORA_SENTINEL_RE = re.compile(r"<!--\s*agora:[^>]*-->", re.IGNORECASE)
# Glob magic characters: the "base root" is the longest leading path prefix free of these chars.
_GLOB_MAGIC = set("*?[")


class ConnectorError(ValueError):
    """A connector is misconfigured (bad name/scope/path) or its source cannot be safely read."""


class Scope(StrEnum):
    """Harvest scope (ADR-0007 mechanism 3): a source's privacy class.

    A ``personal`` source may feed only a personal repo; ``team`` harvesting requires an
    explicitly-designated team source. Enforced by the harvester's scope gate (see
    :mod:`agora_kb.harvester.harvester`).
    """

    personal = "personal"
    team = "team"


@dataclass(frozen=True)
class HarvestedFact:
    """One candidate fact pulled from an agent's memory (ADR-0007).

    ``text`` is the (neutralized) markdown body that becomes the inbox item's content. ``fact_key``
    is the canonical :func:`~agora_kb.core.hashing.content_sha256` of that text — the harvester uses
    it as the inbox ``event_key`` (pending-delivery idempotency) and it equals the curator's tier-2
    content-dedup key for identical text (ADR-0017). ``domain`` / ``tags`` are optional curator
    hints (the file connector supplies neither in v1; they are carried for future connectors).
    """

    text: str
    fact_key: str
    domain: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConnectorScan:
    """The result of one :meth:`Connector.scan` (the input the orchestrator turns into a cursor).

    ``unchanged`` is True iff the source content hashed to the caller-supplied
    ``last_content_sha256`` (the whole-source fast no-op: nothing is emitted). ``content_sha256`` is
    the new whole-source hash to persist in the cursor (``None`` when no source file matched).
    ``source_path`` is the operator-declared source identity recorded in the cursor (DATA-MODEL §6).
    ``facts`` is empty when ``unchanged`` or when nothing matched. ``notes`` are human-facing scan
    findings (no match, a skipped/truncated fact, a path rejected for safety) surfaced in the
    report.
    """

    unchanged: bool
    content_sha256: str | None
    source_path: str
    facts: tuple[HarvestedFact, ...] = ()
    notes: tuple[str, ...] = ()


@runtime_checkable
class Connector(Protocol):
    """Read-adapter seam (ADR-0004 symmetry with the write-adapter ``Backend`` Protocol).

    A connector exposes its identity (``name`` / ``agent`` / ``scope``) and a single
    :meth:`scan` that returns the new/changed facts since ``last_content_sha256``. The Phase-4
    Letta/mem0 API connectors (DATA-MODEL §8) slot in here without touching the orchestrator.
    """

    @property
    def name(self) -> str:
        """The ``adapters.yaml`` connector key (e.g. ``file:claude-code``)."""

    @property
    def agent(self) -> str:
        """The ``<agent>`` token used in ``harvest:<agent>`` (validated, safe path component)."""

    @property
    def scope(self) -> Scope:
        """The source's privacy class (personal | team)."""

    def scan(self, *, last_content_sha256: str | None) -> ConnectorScan:
        """Return facts new/changed since ``last_content_sha256`` plus the new whole-source hash."""


class FileConnector:
    """Diff a markdown ``MEMORY.md`` memory file into candidate facts (ADR-0007, DESIGN §6).

    The only Phase-2 connector. It reads the markdown file(s) the operator's ``path`` glob matches,
    segments them into facts (one per top-level list item or prose paragraph; headings are context,
    never facts), and returns the lot — or nothing, when the whole-source hash is unchanged since
    the last scan (the fast no-op). It does **not** follow the markdown links many memory bullets
    carry to sibling files; v1 harvests the bullet's own summary line (a documented Phase-2
    limitation,
    ADR-0017). All path resolution and reads follow the untrusted-input posture documented at the
    module level.
    """

    def __init__(
        self,
        *,
        name: str,
        path: str,
        scope: Scope,
        max_files: int = 64,
        max_file_bytes: int = 1 << 20,
        max_facts: int = 512,
        max_fact_bytes: int = 4096,
    ) -> None:
        if not name.startswith("file:") or len(name) <= len("file:"):
            raise ConnectorError(
                f"file connector name must be 'file:<agent>' (e.g. file:claude-code), got {name!r}"
            )
        agent = name[len("file:") :]
        # The agent becomes BOTH the inbox source suffix ('harvest:<agent>') AND the writer
        # namespace ('harvest-<agent>'); validate it ONCE to a safe path component so neither can
        # escape the inbox/cursor namespace (the loose models._HARVEST_RE alone is NOT sufficient
        # — ADR-0017).
        try:
            validate_writer(agent)
            # The DERIVED inbox writer ('harvest-<agent>') must ALSO be a safe component — the
            # 8-char prefix can push a long-but-valid agent past _WRITER_MAX. Reject it loudly at
            # build time rather than as an uncaught error mid-write (ADR-0017).
            validate_writer(f"harvest-{agent}")
        except ValueError as exc:
            raise ConnectorError(
                f"connector {name!r}: unsafe agent token {agent!r} ({exc})"
            ) from exc
        if not isinstance(path, str) or not path.strip():
            raise ConnectorError(f"connector {name!r}: 'path' must be a non-empty string")
        # Require an absolute (or ~-rooted) source path so the symlink-escape containment root is a
        # stable declared tree, not the ambient process CWD at scan time (ADR-0017 path-safety).
        if not os.path.isabs(os.path.expanduser(path)):
            raise ConnectorError(
                f"connector {name!r}: 'path' must be absolute or ~-rooted (got {path!r})"
            )
        self._name = name
        self._agent = agent
        self._scope = Scope(scope)
        self._path = path
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_facts = max_facts
        self._max_fact_bytes = max_fact_bytes

    @property
    def name(self) -> str:
        return self._name

    @property
    def agent(self) -> str:
        return self._agent

    @property
    def scope(self) -> Scope:
        return self._scope

    # --- scan -----------------------------------------------------------------------------------
    def scan(self, *, last_content_sha256: str | None) -> ConnectorScan:
        """Resolve the path glob, hash the source, and segment new content into facts.

        Returns ``unchanged=True`` (no facts) when the combined source hash equals
        ``last_content_sha256``; otherwise the segmented + neutralized facts plus the new hash.
        Never raises on a missing source (a connector pointed at a not-yet-existing memory file is a
        no-op with a note), but DOES surface path-safety rejections as notes.
        """
        notes: list[str] = []
        # _resolve_matches appends its own notes (including "no files matched" ONLY when the glob
        # produced zero raw matches — a safety/size skip stands alone as the unambiguous signal).
        matches = self._resolve_matches(notes)
        if not matches:
            return ConnectorScan(
                unchanged=last_content_sha256 is None,
                content_sha256=None,
                source_path=self._path,
                facts=(),
                notes=tuple(notes),
            )

        # Whole-source hash over the concatenation of every matched file (sorted for determinism),
        # so the last_content_sha256 fast no-op covers a multi-file glob too (DATA-MODEL §6).
        combined = "\n\0\n".join(text for _, text in matches)
        sha = content_sha256(combined)
        if last_content_sha256 is not None and sha == last_content_sha256:
            return ConnectorScan(
                unchanged=True,
                content_sha256=sha,
                source_path=self._path,
                facts=(),
                notes=tuple(notes),
            )

        facts: list[HarvestedFact] = []
        capped = False
        for _, text in matches:
            for block in _segment(text):
                fact = self._build_fact(block, notes)
                if fact is None:
                    continue
                facts.append(fact)
                if len(facts) >= self._max_facts:
                    capped = True
                    break
            if capped:
                break
        if capped:
            notes.append(f"reached max_facts={self._max_facts}; remaining facts skipped this scan")

        return ConnectorScan(
            unchanged=False,
            content_sha256=sha,
            source_path=self._path,
            facts=tuple(facts),
            notes=tuple(notes),
        )

    # --- internals ------------------------------------------------------------------------------
    def _build_fact(self, block: str, notes: list[str]) -> HarvestedFact | None:
        """Neutralize + size-bound one segmented block into a :class:`HarvestedFact`."""
        text = _neutralize(block).strip()
        if not text or _MARKER_ONLY_RE.match(text):
            return None  # empty, or a content-free bullet whose only content is the list marker
        encoded = text.encode("utf-8")
        if len(encoded) > self._max_fact_bytes:
            # Truncate on a UTF-8 boundary (keep the head — the bullet/summary is the salient part).
            text = encoded[: self._max_fact_bytes].decode("utf-8", errors="ignore").rstrip()
            notes.append(f"truncated a fact to max_fact_bytes={self._max_fact_bytes}")
            if not text:
                return None
        return HarvestedFact(text=text, fact_key=content_sha256(text))

    def _resolve_matches(self, notes: list[str]) -> list[tuple[Path, str]]:
        """Expand the glob safely and read each matched file; return ``(path, text)`` pairs.

        Path safety (untrusted-input posture): ``~`` is expanded, the non-wildcard base root is
        resolved, and every match must resolve *within* that root (symlink-escape guard). Non-files,
        oversized files, and out-of-root matches are skipped with a note; the match count is capped.
        """
        pattern = os.path.expanduser(self._path)
        root = _glob_base_root(pattern)
        try:
            resolved_root = root.resolve()
        except OSError:
            notes.append(f"cannot resolve base root for {self._path!r}")
            return []

        out: list[tuple[Path, str]] = []
        had_raw = False
        # iglob (lazy) + early-stop bounds the directory walk by max_files — a hostile/large tree
        # reachable from the base root cannot force a full eager enumeration (ADR-0017 path-safety).
        for raw in _glob.iglob(pattern, recursive=True):
            had_raw = True
            if len(out) >= self._max_files:
                notes.append(f"reached max_files={self._max_files}; remaining matches skipped")
                break
            p = Path(raw)
            try:
                resolved = p.resolve()
            except OSError:
                continue
            if not _within(resolved, resolved_root):
                notes.append(f"skipped {raw!r}: resolves outside {root} (symlink-escape guard)")
                continue
            if not resolved.is_file():
                continue
            try:
                size = resolved.stat().st_size
            except OSError:
                continue
            if size > self._max_file_bytes:
                notes.append(f"skipped {raw!r}: {size} bytes exceeds max_file_bytes")
                continue
            try:
                text = resolved.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = resolved.read_bytes().decode("utf-8", errors="replace")
                notes.append(f"{raw!r} was not valid UTF-8; decoded with replacement")
            except OSError:
                continue
            out.append((resolved, text))
        if not had_raw:
            # Distinguish a genuine zero-match from "matched but all skipped" (a safety/size skip
            # already emitted its own, unambiguous note) so the security signal is not muddied.
            notes.append(f"no files matched {self._path!r}")
        # Sort the (bounded) kept set for a deterministic combined-source hash when cap is unhit.
        out.sort(key=lambda pt: str(pt[0]))
        return out


# --- module-level helpers (pure) ---------------------------------------------------------------


def _segment(text: str) -> list[str]:
    """Split a markdown memory file into facts (one per top-level list item or prose paragraph).

    Headings are CONTEXT and are never emitted, so the leading ``# Memory Index`` / title
    boilerplate real ``MEMORY.md`` files carry is skipped automatically. A top-level (column-0)
    list item carries its indented child lines (and blank-separated continuations that stay
    indented). Non-list, non-heading prose is grouped into paragraph blocks between blank lines.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        block = "\n".join(buf).strip("\n")
        if block.strip():
            blocks.append(block)
        buf.clear()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if _HEADING_RE.match(line):
            flush()  # a heading is context only, never a fact
            i += 1
            continue
        if _TOP_LIST_RE.match(line):
            flush()  # start a fresh top-level list-item fact
            buf.append(line)
            i += 1
            # Consume the item's children: indented lines, and blank lines followed by more indent.
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "":
                    j = i + 1
                    while j < n and lines[j].strip() == "":
                        j += 1
                    follows_indented = (
                        j < n
                        and lines[j][:1] in (" ", "\t")
                        and not _TOP_LIST_RE.match(lines[j])
                        and not _HEADING_RE.match(lines[j])
                    )
                    if follows_indented:
                        buf.append(nxt)
                        i += 1
                        continue
                    break
                if nxt[:1] in (" ", "\t") and not _HEADING_RE.match(nxt):
                    buf.append(nxt)
                    i += 1
                    continue
                break
            flush()
            continue
        if line.strip() == "":
            flush()
            i += 1
            continue
        buf.append(line)  # a non-heading, non-list, non-blank prose line
        i += 1
    flush()
    return blocks


def _neutralize(text: str) -> str:
    """Strip agora structural sentinels from untrusted harvested text (defense-in-depth, ADR-0017).

    Removes the curator's body-region marker comments (``<!-- agora:body:… -->``) so a poisoned
    memory bullet cannot inject a fake region into the candidate bundle. The candidate text is
    already treated as untrusted DATA by the planning prompt (INGEST-CONTRACT §8) and the curator's
    deterministic diff gate is the real integrity boundary; this is a cheap extra layer.
    """
    return _AGORA_SENTINEL_RE.sub("", text)


def _glob_base_root(pattern: str) -> Path:
    """Return the longest leading path prefix of ``pattern`` that contains no glob magic.

    For ``~/.claude/**/MEMORY.md`` (expanded) this is ``~/.claude``; for a plain file path with no
    magic it is the file's parent directory. Used as the containment root for the symlink-escape
    guard so a ``**`` match can never resolve outside the operator's declared source tree.
    """
    parts = Path(pattern).parts
    base_parts: list[str] = []
    for part in parts:
        if any(ch in _GLOB_MAGIC for ch in part):
            break
        base_parts.append(part)
    base = Path(*base_parts) if base_parts else Path(".")
    # A no-magic pattern resolves to a file; use its parent as the containment directory.
    if not base_parts or len(base_parts) == len(parts):
        return base.parent if base.parent != Path("") else Path(".")
    return base


def _within(path: Path, root: Path) -> bool:
    """True iff ``path`` is ``root`` itself or lives beneath it (both already resolved)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
