"""Core data models for the write path (DATA-MODEL §1).

The unit of capture is an **inbox item** — the immutable "event" in the append-only log. It is
stored as a markdown file with YAML frontmatter at ``_kb/inbox/<writer>/<id>.md``. These models
validate and serialize that frontmatter; (de)serialization to the on-disk markdown lives in
:mod:`agora_kb.core.frontmatter`.

Invariants enforced here: events are immutable (model is frozen) and carry no mutable processing
status (lifecycle is by file location only, DATA-MODEL §1); ``source`` and ``target`` accept the
fixed enum *plus* the parametric ``web:<user>`` / ``harvest:<agent>`` (source) and ``team:<name>``
(target) forms.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from .ids import is_valid_event_id

__all__ = ["Kind", "Confidence", "InboxItem", "FIXED_SOURCES"]

# Fixed (non-parametric) values of the inbox `source` enum (DATA-MODEL §1). The parametric
# `web:<user>` and `harvest:<agent>` forms are validated separately.
FIXED_SOURCES = frozenset(
    {"claude-code", "codex", "qwen", "gemini", "opencode", "hermes", "manual"}
)

# \A...\Z (not ^...$) so a trailing newline cannot pass validation ($ matches before a final \n).
_WEB_RE = re.compile(r"\Aweb:.+\Z")
_HARVEST_RE = re.compile(r"\Aharvest:.+\Z")
_TEAM_RE = re.compile(r"\Ateam:[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_KEBAB_RE = re.compile(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z")


class Kind(StrEnum):
    """Capture kind. Harvested facts enter as candidates, gated before promotion (ADR-0007)."""

    capture = "capture"
    candidate = "candidate"


class Confidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class InboxItem(BaseModel):
    """One immutable inbox event (DATA-MODEL §1).

    Construct via :meth:`agora_kb.core.inbox.Inbox.write`, which fills ``id``, ``created`` and
    ``content_sha256``. The model is frozen and forbids unknown fields so an event cannot accrue
    mutable status.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    source: str
    writer: str
    target: str = "personal"
    cwd: str | None = None
    domain: str | None = None
    tags: tuple[str, ...] = ()
    created: datetime
    kind: Kind = Kind.capture
    confidence: Confidence | None = None
    event_key: str | None = None
    content_sha256: str
    raw_ref: str | None = None
    body: str

    # --- validation ----------------------------------------------------------------------------
    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not is_valid_event_id(v):
            raise ValueError(f"invalid event id: {v!r}")
        return v

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str) -> str:
        if v in FIXED_SOURCES or _WEB_RE.match(v) or _HARVEST_RE.match(v):
            return v
        raise ValueError(
            f"invalid source {v!r}: expected one of {sorted(FIXED_SOURCES)} "
            "or 'web:<user>'/'harvest:<agent>'"
        )

    @field_validator("target")
    @classmethod
    def _check_target(cls, v: str) -> str:
        if v == "personal" or _TEAM_RE.match(v):
            return v
        raise ValueError(f"invalid target {v!r}: expected 'personal' or 'team:<name>'")

    @field_validator("content_sha256")
    @classmethod
    def _check_sha(cls, v: str) -> str:
        if not _SHA256_RE.match(v):
            raise ValueError(f"content_sha256 must be 64 lowercase hex chars, got {v!r}")
        return v

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for t in v:
            if not _KEBAB_RE.match(t):
                raise ValueError(f"tag {t!r} must be kebab-case ([a-z0-9] words joined by '-')")
        return v

    @field_validator("created")
    @classmethod
    def _check_created(cls, v: datetime) -> datetime:
        # Persisted as a UTC instant; require tz-awareness so serialization is unambiguous.
        if v.tzinfo is None:
            raise ValueError("created must be timezone-aware (UTC)")
        return v.astimezone(UTC)

    @field_serializer("created")
    def _ser_created(self, v: datetime) -> str:
        # DATA-MODEL §1 form: 2026-06-13T10:22:33Z (second precision, explicit Z).
        return v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- frontmatter view ----------------------------------------------------------------------
    def to_frontmatter(self) -> dict[str, object]:
        """Ordered frontmatter dict (DATA-MODEL §1 field order), omitting absent optionals.

        ``body`` is excluded — it is the markdown content below the frontmatter, not a key.
        """
        fm: dict[str, object] = {
            "id": self.id,
            "source": self.source,
            "writer": self.writer,
        }
        if self.cwd is not None:
            fm["cwd"] = self.cwd
        fm["target"] = self.target
        if self.domain is not None:
            fm["domain"] = self.domain
        if self.tags:
            fm["tags"] = list(self.tags)
        fm["created"] = self._ser_created(self.created)
        fm["kind"] = self.kind.value
        if self.confidence is not None:
            fm["confidence"] = self.confidence.value
        if self.event_key is not None:
            fm["event_key"] = self.event_key
        fm["content_sha256"] = self.content_sha256
        if self.raw_ref is not None:
            fm["raw_ref"] = self.raw_ref
        return fm
