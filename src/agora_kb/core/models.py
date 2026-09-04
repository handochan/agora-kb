"""Core data models for the write path (DATA-MODEL §1).

The unit of capture is an **inbox item** — the immutable "event" in the append-only log. It is
stored as a markdown file with YAML frontmatter at ``_kb/inbox/<writer>/<id>.md``. These models
validate and serialize that frontmatter; (de)serialization to the on-disk markdown lives in
:mod:`agora_kb.core.frontmatter`.

Invariants enforced here: events are immutable (model is frozen) and carry no mutable processing
status (lifecycle is by file location only, DATA-MODEL §1); ``source`` and ``target`` accept the
fixed enum *plus* the parametric ``web:<user>`` / ``harvest:<agent>`` / ``agent:<name>`` (source)
and ``team:<name>`` (target) forms.

An event may also carry :class:`Attachment` records — the original bytes of a captured artefact,
staged beside the event in the writer's own namespace and destined for ``raw/_blob/`` (ADR-0041
D4.2). The record is metadata only: the model never holds bytes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from .ids import is_valid_event_id
from .layout import validate_attachment_digest, validate_attachment_ext
from .pathsafe import DEFAULT_MAX_BYTES, safe_slug_component

__all__ = [
    "Kind",
    "Confidence",
    "Attachment",
    "InboxItem",
    "FIXED_SOURCES",
    "sanitize_attachment_filename",
    "normalize_media_type",
]

# Fixed (non-parametric) values of the inbox `source` enum (DATA-MODEL §1). Kept verbatim for
# BACK-COMPAT: every event already on disk carries one of these, and the names the engine shipped
# with must keep round-tripping. It is NOT the way a new agent becomes first-class — that is the
# parametric `agent:<name>` form below (invariant #6: the engine must never hold a blessed list of
# agent names, and adding one must never require a core PR). The parametric `web:<user>`,
# `harvest:<agent>` and `agent:<name>` forms are validated separately.
FIXED_SOURCES = frozenset(
    {"claude-code", "codex", "qwen", "gemini", "opencode", "hermes", "manual"}
)

# \A...\Z (not ^...$) so a trailing newline cannot pass validation ($ matches before a final \n).
_WEB_RE = re.compile(r"\Aweb:.+\Z")
_HARVEST_RE = re.compile(r"\Aharvest:.+\Z")
# `agent:<name>` — the tool-agnostic first-class capture source (issue #147, invariant #6). Any
# agent (aelix, copilot, a tool that does not exist yet) may stamp its OWN name here and get a
# `kind=capture` event, with no core change and no impersonation of a blessed name. The <name>
# token carries the SAME charset rule as `team:<name>` — the only parametric form that has ever
# constrained one — so a source cannot smuggle whitespace, a path separator or a newline into
# provenance. A BARE name (`aelix`) stays REJECTED: the prefix is what makes the claim explicit,
# so a typo'd fixed source can never be silently blessed as a new agent.
_AGENT_RE = re.compile(r"\Aagent:[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_TEAM_RE = re.compile(r"\Ateam:[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_KEBAB_RE = re.compile(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z")
# RFC 6838 `type/subtype`, PARAMETERS EXCLUDED (a `; charset=…` tail is dropped before matching).
# A media type is metadata a browser or a shell handed us about an untrusted upload, and it reaches
# the `raw/_blob/` sidecar's `media_type:` key (normalised to a bare lowercase `type/subtype` by
# `normalize_media_type` below — never verbatim), so it is admitted by an allowlist rather than
# merely escaped.
_MEDIA_TYPE_TOKEN = r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}"
_MEDIA_TYPE_RE = re.compile(rf"\A{_MEDIA_TYPE_TOKEN}/{_MEDIA_TYPE_TOKEN}\Z")


def normalize_media_type(value: str | None) -> str | None:
    """Return ``value`` as a bare lowercase ``type/subtype``, or ``None`` if it is not one.

    Total by construction (it never raises): the input is whatever a multipart form, an OS or a
    ``file(1)`` guess supplied, and a media type is never load-bearing — the extractor routes on it
    only as one signal among several, and the sidecar records it as provenance. Parameters are
    dropped the same way :func:`agora_kb.ingest.extractors.extract` drops them
    (``split(";", 1)[0].strip().lower()``), so the two agree on what "the mime" of an upload is.
    """
    if not isinstance(value, str):
        return None
    candidate = value.split(";", 1)[0].strip().lower()
    return candidate if _MEDIA_TYPE_RE.match(candidate) else None


def sanitize_attachment_filename(value: str | None) -> str | None:
    """Reduce a DISPLAY filename to one safe component, or ``None`` when nothing safe survives.

    The name is never a path — the attachment is addressed by its digest — but it is copied into
    the ``raw/_blob/`` YAML sidecar and summarised into the curator's brain bundle, so it crosses
    two boundaries that a raw upload filename has no business crossing unfiltered. It therefore
    goes through :func:`agora_kb.core.pathsafe.safe_slug_component`, the repo's ONE closed
    Unicode-category allowlist: separators, controls, bidi overrides, zero-width characters and the
    ``<`` of an ``<!-- agora:… -->`` sentinel are unreachable without being enumerated, a Korean
    name stays Korean, and the result cannot be mistaken for a path (``../../etc/passwd`` →
    ``etc-passwd``). Returning ``None`` rather than inventing a placeholder keeps the field
    omissible: an attachment with no representable name simply carries none, and its digest still
    names it exactly.
    """
    if not isinstance(value, str):
        return None
    return safe_slug_component(value, max_bytes=DEFAULT_MAX_BYTES) or None


class Kind(StrEnum):
    """Capture kind. Harvested facts enter as candidates, gated before promotion (ADR-0007)."""

    capture = "capture"
    candidate = "candidate"


class Confidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class Attachment(BaseModel):
    """One original-bytes attachment of an inbox event (DATA-MODEL §1, ADR-0041 D4.2).

    METADATA ONLY — the record names bytes, it never holds them. The bytes are staged beside the
    event at ``_kb/inbox/<writer>/_attach/<sha256>.<ext>`` by
    :meth:`agora_kb.core.inbox.Inbox.write` and materialised into
    ``raw/_blob/<ab>/<sha256>.<ext>`` by the deterministic APPLY pass, which is the only writer of
    ``raw/`` (ADR-0020 decision 3). ``sha256`` is the digest of the RAW bytes — *not*
    :func:`agora_kb.core.hashing.content_sha256`, which is a digest of normalised TEXT and would
    change the bytes it claims to identify.

    Frozen, ``extra='forbid'``, and every field validated, because the record is read back from a
    file that is only as trustworthy as the spool: the digest and the extension are interpolated
    into a path by :meth:`agora_kb.core.layout.RepoLayout.inbox_attachment_path` and its
    ``raw/_blob/`` twin.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha256: str
    ext: str
    filename: str | None = None
    media_type: str | None = None
    bytes: int

    @field_validator("sha256")
    @classmethod
    def _check_sha256(cls, v: str) -> str:
        return validate_attachment_digest(v)

    @field_validator("ext")
    @classmethod
    def _check_ext(cls, v: str) -> str:
        return validate_attachment_ext(v)

    @field_validator("filename")
    @classmethod
    def _check_filename(cls, v: str | None) -> str | None:
        # Idempotent re-application, so a hand-edited event cannot smuggle a newline or a sentinel
        # into the sidecar/bundle by writing the frontmatter directly.
        return sanitize_attachment_filename(v)

    @field_validator("media_type")
    @classmethod
    def _check_media_type(cls, v: str | None) -> str | None:
        return normalize_media_type(v)

    @field_validator("bytes")
    @classmethod
    def _check_bytes(cls, v: int) -> int:
        # 0 is admitted: an empty file is a legitimate (if useless) artefact, and refusing it here
        # would put a size policy in the model. The BYTE CAP lives at the write boundary
        # (`Inbox.write`), where the operator's configured limit is in scope.
        if v < 0:
            raise ValueError(f"attachment bytes must be non-negative, got {v}")
        return v

    def to_frontmatter(self) -> dict[str, object]:
        """Ordered frontmatter mapping for one attachment, omitting absent optionals."""
        fm: dict[str, object] = {"sha256": self.sha256, "ext": self.ext}
        if self.filename is not None:
            fm["filename"] = self.filename
        if self.media_type is not None:
            fm["media_type"] = self.media_type
        fm["bytes"] = self.bytes
        return fm


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
    attachments: tuple[Attachment, ...] = ()
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
        if v in FIXED_SOURCES or _WEB_RE.match(v) or _HARVEST_RE.match(v) or _AGENT_RE.match(v):
            return v
        raise ValueError(
            f"invalid source {v!r}: expected one of {sorted(FIXED_SOURCES)} "
            "or 'web:<user>'/'harvest:<agent>'/'agent:<name>'"
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

    @field_validator("attachments")
    @classmethod
    def _check_attachments(cls, v: tuple[Attachment, ...]) -> tuple[Attachment, ...]:
        # One event never names one content-addressed file twice: the second entry would address
        # the same staged file and the same `raw/_blob/` destination, so APPLY would write one
        # sidecar for two records and have to pick a `filename:`. Identical bytes under DIFFERENT
        # extensions stay two distinct attachments (ADR-0041 D1.4 admits that case by name).
        seen: set[tuple[str, str]] = set()
        for attachment in v:
            key = (attachment.sha256, attachment.ext)
            if key in seen:
                raise ValueError(
                    f"duplicate attachment {attachment.sha256}.{attachment.ext}: one event may "
                    "name a content-addressed file at most once"
                )
            seen.add(key)
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
        if self.attachments:
            # LAST, and omitted when empty: every event already on disk keeps a byte-identical
            # frontmatter, and the DATA-MODEL §1 key order stays a prefix of itself.
            fm["attachments"] = [a.to_frontmatter() for a in self.attachments]
        return fm
