"""Session-transcript readers — the ``SessionReader`` seam + a Claude Code JSONL reader (ADR-0023).

The read side of ADR-0023's `session:` connector (issue #25). A **session** is an agent's raw
conversation transcript — a fundamentally noisier, larger, higher-PII shape than a hand-curated
``MEMORY.md`` — where durable knowledge an agent discovered but never wrote down lives (lessons,
decisions, files touched, error→fix pairs). This module is the ADR-0004 read-adapter's *parsing*
half, kept separate from the salience distillation and the connector's path-safety / redaction
envelope (both in :mod:`agora_kb.harvester.connectors`):

* :class:`SessionReader` — a tool-agnostic Protocol (invariant #6) turning one session file's TEXT
  into a stream of normalized :class:`TurnRecord`\\ s. Taking text (not a path) keeps it a **pure,
  model-free transform** — trivially unit-testable, and the connector owns the untrusted-input path
  safety (glob containment, symlink-escape, size caps) before a byte reaches a reader.
* :class:`ClaudeCodeJsonlReader` — the first concrete reader (ADR-0023 O4/B): Claude Code stores
  per-project transcripts as JSONL under ``~/.claude/projects/**/*.jsonl``, one typed record per
  line. Codex/Gemini/Hermes analogues slot in behind the same Protocol without touching the
  orchestrator or the distiller.
* :data:`SESSION_READERS` — the NAMED registry that makes that slot-in reachable from operator
  config (issue #147). Until it existed the seam was real but unwired: ``build_connectors`` never
  injected a reader, so every ``session:<agent>`` connector in the world was parsed as Claude Code
  JSONL — the engine privileging ONE agent's transcript format, which is exactly what invariant #6
  forbids. A connector now declares ``format:`` and the registry resolves the parser; the engine
  reacts to that DECLARED format, never to the ``<agent>`` half of the connector key.

**Role flattening (ADR-0023 §7 — injection safety).** A whole transcript is a much larger
prompt-injection surface than a memory bullet; an embedded ``assistant``/``system`` turn may be
hostile prior input. The reader normalizes every content-bearing line to one of three flat roles
(``user`` / ``assistant`` / ``tool``) and drops all structural/operational record types, so no
source line can smuggle an engine-structure role. The connector's ``_neutralize`` sentinel-strip +
the candidate gate remain the real boundaries; this is defense-in-depth.

**What is surfaced vs skipped (precision-first, model-free).** User + assistant *text* blocks are
surfaced verbatim; ``tool_use`` blocks are surfaced as a **bounded** ``tool``-role summary (tool
name + one identifying scalar arg — the "files-touched / command-run" salience signal) rather than
full input. Deliberately dropped as low-signal / high-noise for v1: extended ``thinking`` blocks,
``tool_result`` bodies (often multi-MB file dumps), and every non-user/assistant record type
(``mode`` / ``attachment`` / ``system`` / ``file-history-snapshot`` / ``queue-operation`` / …). An
opt-in LLM digest for higher recall is an explicit future stage (ADR-0023 O1/C), never the default.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "TurnRecord",
    "SessionReader",
    "ClaudeCodeJsonlReader",
    "SessionFormatError",
    "SESSION_READERS",
    "implemented_session_formats",
    "is_implemented_format",
    "DEFAULT_SESSION_FORMAT",
    "build_session_reader",
]

# Tool-input keys checked, in order, for the ONE most-identifying scalar arg summarizing a tool_use.
# Bounded to a small allowlist so a large/hostile input (e.g. a Write body, a pasted blob) is never
# surfaced whole — the salient signal is "which file / command / query", not the payload.
_TOOL_ID_KEYS: tuple[str, ...] = (
    "file_path",
    "path",
    "notebook_path",
    "command",
    "pattern",
    "query",
    "url",
)
# Cap the identifying arg so a pathological value cannot bloat a turn (the connector redacts + caps
# again downstream; this keeps the reader's own output bounded).
_TOOL_ARG_MAX = 200


@dataclass(frozen=True)
class TurnRecord:
    """One normalized turn from a session transcript (the unit the salience distiller consumes).

    ``role`` is FLATTENED to ``user`` | ``assistant`` | ``tool`` (ADR-0023 §7) — no ``system`` or
    other structural role reaches the distiller. ``text`` is the turn's plain-text content (a
    ``tool`` turn's text is a bounded ``name(key=value)`` summary, never the raw tool payload).
    ``timestamp`` is the source record's ISO-ish stamp (or ``None``). ``tool_name`` is set only for
    a ``tool`` turn. Frozen + hashable so a distiller may dedup turns cheaply.
    """

    role: str
    text: str
    timestamp: str | None = None
    tool_name: str | None = None


@runtime_checkable
class SessionReader(Protocol):
    """Read one session file's TEXT into normalized turn records (ADR-0023 O4/B, invariant #6).

    A pure, model-free transform: no filesystem, no network, no model. The connector performs all
    untrusted-input path safety and reads the (size-capped) bytes; the reader only parses. Codex /
    Gemini / Hermes readers implement this same Protocol and slot in without touching the
    orchestrator or the distiller.
    """

    def read_turns(self, text: str) -> Iterator[TurnRecord]:
        """Yield the file's content-bearing turns in source order (tolerant; never raises)."""


def _summarize_tool_use(name: str, tool_input: object) -> str:
    """Reduce a ``tool_use`` block to a bounded ``name(key=value)`` summary (or bare ``name``).

    Surfaces the single most-identifying scalar arg (a touched file, a run command) so "files
    touched" / "commands run" become salience signals, WITHOUT surfacing the full input — a Write
    body or a pasted blob must never flow verbatim into a candidate. Returns just ``name`` when no
    allowlisted scalar arg is present.
    """
    if isinstance(tool_input, dict):
        for key in _TOOL_ID_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                arg = " ".join(value.split())[:_TOOL_ARG_MAX]
                return f"{name}({key}={arg})"
    return name


class ClaudeCodeJsonlReader:
    """Parse a Claude Code per-project JSONL transcript into normalized turns (ADR-0023 O4/B).

    Each line is one typed JSON record; only ``user`` / ``assistant`` records carry a ``message``
    with content. Every other ``type`` (``mode`` / ``attachment`` / ``system`` / ``last-prompt`` /
    ``ai-title`` / ``queue-operation`` / ``pr-link`` / ``permission-mode`` /
    ``file-history-snapshot`` / …) is operational and skipped. Parsing is fully tolerant: an
    unparseable line, a record with no ``message``, an unexpected content shape, or an empty turn is
    silently skipped rather than raising — a session file is untrusted, possibly truncated input.

    ``message.content`` is either a plain string (one text turn) or a list of typed blocks
    (``text`` → a text turn; ``tool_use`` → a bounded ``tool`` summary; ``thinking`` and
    ``tool_result`` are dropped as low-signal/high-noise for v1).
    """

    #: Record ``type``s that carry a conversational ``message`` (everything else is operational).
    _CONTENT_TYPES = frozenset({"user", "assistant"})
    #: The flat roles a message may map to (ADR-0023 §7); else falls back to the record type.
    _FLAT_ROLES = frozenset({"user", "assistant"})

    def read_turns(self, text: str) -> Iterator[TurnRecord]:
        """Yield content-bearing turns from a JSONL transcript, tolerantly skipping the rest."""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue  # a truncated / non-JSON line — a session file is untrusted input.
            if not isinstance(record, dict):
                continue
            if record.get("type") not in self._CONTENT_TYPES:
                continue  # operational record (mode/attachment/system/…): no conversation content.
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            # Flatten the role: trust it only if it is one of the two conversational roles, else
            # fall back to the record type — a crafted message.role can never assert an engine role.
            raw_role = message.get("role")
            role = raw_role if raw_role in self._FLAT_ROLES else str(record.get("type"))
            timestamp = record.get("timestamp")
            timestamp = timestamp if isinstance(timestamp, str) else None
            yield from self._turns_from_content(message.get("content"), role, timestamp)

    def _turns_from_content(
        self, content: object, role: str, timestamp: str | None
    ) -> Iterator[TurnRecord]:
        """Normalize one message's ``content`` (str or block list) into zero or more turns."""
        if isinstance(content, str):
            body = content.strip()
            if body:
                yield TurnRecord(role=role, text=body, timestamp=timestamp)
            return
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                body = block.get("text")
                if isinstance(body, str) and body.strip():
                    yield TurnRecord(role=role, text=body.strip(), timestamp=timestamp)
            elif btype == "tool_use":
                name = block.get("name")
                if isinstance(name, str) and name.strip():
                    summary = _summarize_tool_use(name.strip(), block.get("input"))
                    yield TurnRecord(
                        role="tool", text=summary, timestamp=timestamp, tool_name=name.strip()
                    )
            # thinking / tool_result / unknown blocks are intentionally dropped (v1 noise control).


# --- the format registry (issue #147, invariant #6) ---------------------------------------------


class SessionFormatError(ValueError):
    """A ``session:`` connector declared a format this build cannot parse.

    Raised by :func:`build_session_reader` for an unknown name *and* for a registered-but-unbuilt
    placeholder. Both are FAIL-LOUD: silently falling back to the Claude Code parser would feed a
    foreign transcript to the wrong grammar and yield a silent zero-fact harvest (or, worse,
    garbage facts) with no signal that the format was never supported.
    """


def _unimplemented(fmt: str, hint: str) -> Callable[[], SessionReader]:
    """Registry factory for a format whose SLOT exists but whose parser does not (yet).

    The name is registered so the intent is documented in the ONE place a grammar is added, and a
    contributor writing the parser only has to swap this factory for a class. It raises at BUILD
    time (not mid-scan) so the failure is attributed to config, not to a transcript.

    A slot is NOT part of the operator-facing vocabulary: :func:`is_implemented_format` marks it,
    ``config._SESSION_FORMATS`` holds only the implemented names, and ``load_connector_specs``
    therefore rejects a declared slot at CONFIG-LOAD time with "unknown format". That ordering is
    load-bearing: reaching :func:`build_session_reader` with a slot name would raise inside
    ``build_connectors``, which builds EVERY connector before ``agora harvest`` applies its
    ``--connector`` filter — so one aspirational line in ``adapters.yaml`` would disable harvesting
    for unrelated, healthy connectors, on a repo ``agora doctor`` had just called healthy.
    """

    def factory() -> SessionReader:
        raise SessionFormatError(
            f"session format {fmt!r} is a registered slot with no reader in this build ({hint}); "
            f"implement a SessionReader for it and register it in SESSION_READERS"
        )

    factory.agora_unimplemented = True  # type: ignore[attr-defined]
    return factory


#: The default format when a ``session:`` connector declares none - today's behaviour, unchanged.
DEFAULT_SESSION_FORMAT = "claude-code-jsonl"

#: Transcript format name -> zero-arg factory returning a :class:`SessionReader`.
#:
#: The one place a new agent's transcript grammar is added. Keys are FORMAT names, deliberately not
#: agent names: two agents that share a grammar share one entry, and one agent that changes grammar
#: gets a second entry - neither of which the engine can express if it dispatches on WHO the agent
#: is (invariant #6). Keep the IMPLEMENTED keys in sync with ``config._SESSION_FORMATS`` (the config
#: seam holds the same names as plain strings so it never imports the harvester - the same split
#: ``_SCOPE_VALUES`` already uses; a sync test asserts the two never drift). A registered SLOT with
#: no parser (:func:`_unimplemented`) is deliberately NOT in that vocabulary - see
#: :func:`is_implemented_format`.
SESSION_READERS: dict[str, Callable[[], SessionReader]] = {
    DEFAULT_SESSION_FORMAT: ClaudeCodeJsonlReader,
    # Codex writes its own JSONL rollout files with a DIFFERENT record/payload shape than Claude
    # Code's. Left a documented placeholder rather than a guessed parser: a reader written against
    # an unverified grammar fails SILENTLY (zero facts on a real corpus) instead of loudly, which
    # is a worse outcome than an explicit not-implemented error. NOT declarable in adapters.yaml
    # until the parser exists (`is_implemented_format` / `config._SESSION_FORMATS`).
    "codex-jsonl": _unimplemented("codex-jsonl", "Codex rollout JSONL"),
}


def is_implemented_format(fmt: str) -> bool:
    """True iff ``fmt`` is registered AND this build actually carries its parser.

    The SSOT for "can this name be declared in ``adapters.yaml``". Derived from the registry itself
    (a placeholder factory is marked by :func:`_unimplemented`), so a slot becoming real is one
    edit - swap the factory - and everything else follows.
    """
    factory = SESSION_READERS.get(fmt)
    return factory is not None and not getattr(factory, "agora_unimplemented", False)


def implemented_session_formats() -> tuple[str, ...]:
    """The sorted format names an operator may declare today (see :func:`is_implemented_format`)."""
    return tuple(sorted(name for name in SESSION_READERS if is_implemented_format(name)))


def build_session_reader(fmt: str | None = None) -> SessionReader:
    """Resolve a declared transcript format to a reader; ``None`` -> :data:`DEFAULT_SESSION_FORMAT`.

    The single construction point for session readers, so the ``session:`` connector's own default
    and the config-driven injection can never disagree about what "unset" means.
    """
    name = DEFAULT_SESSION_FORMAT if fmt is None else fmt
    factory = SESSION_READERS.get(name)
    if factory is None:
        raise SessionFormatError(
            f"unknown session format {name!r}; known formats are {sorted(SESSION_READERS)}"
        )
    return factory()
