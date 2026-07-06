"""The ``session:`` connector — distill agent SESSION transcripts into gated candidates (#25).

The XL half of ADR-0023's session ask: a read-adapter behind the same :class:`~agora_kb.harvester.\
connectors.Connector` Protocol as :class:`~agora_kb.harvester.connectors.FileConnector`, but for the
noisier, larger, higher-PII **transcript** shape (``~/.claude/projects/**/*.jsonl``) that
:func:`~agora_kb.harvester.connectors._segment`'s top-level-bullet model is wrong for (ADR-0023
"Reuse FileConnector._segment for transcripts (rejected)"). The orchestrator, cursor, candidate
gate, and scope gate are reused **unchanged** — this only adds a new connector type.

Pipeline (all deterministic + model-free — the curator's two acts stay the only delegated steps):

1. **Path-safe glob.** Reuses :func:`~agora_kb.harvester.connectors._resolve_glob_files` — the same
   ``~``-expand / symlink-escape-containment / gold-exclusion / size+count caps as ``file:``. The
   whole-source hash over the raw file bytes is the §6 no-op (ADR-0023 decision 8).
2. **Parse.** Drives a :class:`~agora_kb.harvester.session_sources.SessionReader` (default
   :class:`~agora_kb.harvester.session_sources.ClaudeCodeJsonlReader`) into flat-role turn records
   — role attribution FLATTENED so a transcript turn cannot impersonate engine structure (ADR-0023
   §7).
3. **Distill (precision-first salience).** Whole agora sentinel SPANS are dropped from each
   assistant turn BEFORE paragraph-splitting (ADR-0027 §8 consumer duty — the loop-break's verbatim
   half: a gold pack the agent echoed contributes ZERO facts), then one candidate is emitted per
   **assistant** reflection paragraph carrying an explicit durable-knowledge marker (lesson / gotcha
   / root-cause / the-fix / TIL / note-to-self / …). Deliberately low-recall: the harvester is a
   pure transform and a raw transcript has a far worse signal-to-noise ratio than a ``MEMORY.md``,
   so v1 keeps precision high and leaves an LLM digest for higher recall as an explicit opt-in
   future stage (ADR-0023 O1/C). User turns (highest-PII, request-shaped) are deferred.
4. **Neutralize + redact BEFORE the hash.** Each fact body is sentinel-stripped
   (:func:`~agora_kb.harvester.connectors._neutralize`) and then run through
   :func:`agora_kb.core.redact.redact` at this connector boundary — so ``fact_key`` is a
   ``content_sha256`` of the **redacted** text, never the raw secret (ADR-0023 decision 5 / addendum
   §2; the inbox is append-only + un-scrubbable, invariant #3). The per-class hits ride on the
   :class:`~agora_kb.harvester.connectors.HarvestedFact` so the orchestrator can bump
   ``HarvestCursor.redacted``. ``domain`` / ``tags`` are left ``None`` — the curator decides filing.

**Privacy + noise posture (documented v1 trade-offs, validated on a real corpus).** Redaction
targets *secrets* (the Balanced-9 structural tier); *general PII* — names, education, addresses — is
NOT redacted in v1 (email/phone are deferred, ADR-0023 addendum §1), so a reflection that happens to
quote personal text still lands as a candidate. This is bounded, not open: the fail-closed
personal-scope gate keeps it in the OWNER's own repo (never cross-tenant), and every fact is a
``kind=candidate`` the curator can DROP. The whole-source hash re-reads the corpus on any change, so
after a session grows the surviving marker paragraphs are re-proposed; ``event_key`` idempotency
absorbs still-pending re-floods, and an already-processed fact re-proposed is the documented
candidate-gate cost (ADR-0017). Both are why **reliable DROP is a validation requirement**
(ADR-0023 decision 3), not an assumption.
"""

from __future__ import annotations

import re

from agora_kb.core.hashing import content_sha256
from agora_kb.core.redact import DEFAULT_POLICY, RedactionPolicy, redact
from agora_kb.core.sentinel import strip_sentinel_spans

from .connectors import (
    ConnectorScan,
    HarvestedFact,
    Scope,
    _neutralize,
    _parse_connector_agent,
    _require_source_path,
    _resolve_glob_files,
)
from .session_sources import ClaudeCodeJsonlReader, SessionReader

__all__ = ["SessionConnector"]

# Explicit durable-knowledge markers (case-insensitive) an agent uses when it flags a lesson worth
# keeping — CALIBRATED on a real ~/.claude corpus to fire rarely (precision-first, ADR-0023 O1/B). A
# marker-free paragraph is ordinary narration and is not harvested; recall is deliberately low (the
# opt-in LLM digest, O1/C, is the future recall lever). Every alternative is a distinctive phrase,
# so the scan stays linear (no nested unbounded quantifier — no catastrophic backtracking).
_SALIENCE_RE = re.compile(
    r"(?i)(?:"
    r"\bTIL\b|today i learned|"
    r"lessons? learned|the lesson (?:here )?is|key lesson|"
    r"\bgotchas?\b|"
    r"key insight|the (?:key )?insight is|"
    r"key takeaways?|the takeaway is|"
    r"root[- ]cause|the culprit (?:was|is)|"
    r"note to self|worth remembering|for future reference|"
    r"the (?:real )?fix (?:was|is|turned out)|the (?:real )?bug (?:was|is)|"
    r"the trick (?:is|was)|the key (?:is|was) to|"
    r"turns out that|"
    r"i realized that|i realised that|the realization (?:was|is)|"
    r"the decision (?:was|is) to|we decided to"
    r")"
)
# A blank-line paragraph split (ADR-0017 §2 prose grouping, applied per assistant turn body).
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")


def _paragraphs(text: str) -> list[str]:
    """Split one turn's text into blank-line-separated paragraphs (the salience-scan unit)."""
    return [p.strip() for p in _PARA_SPLIT_RE.split(text) if p.strip()]


class SessionConnector:
    """Distill agent session transcripts into gated candidate facts (ADR-0023, issue #25).

    Constructed from a ``session:<agent>`` connector key. ``reader`` is the format parser (default
    :class:`ClaudeCodeJsonlReader`; the seam keeps the connector tool-agnostic — invariant #6).
    ``redact_policy`` is the connector-boundary redaction policy applied before ``fact_key`` is
    computed; ``None`` disables redaction (the operator's explicit kill-switch, accepting the
    un-scrubbable-inbox risk — ADR-0023 addendum §5). Size/count caps default larger than ``file:``
    because a single transcript is far larger than a ``MEMORY.md``.
    """

    def __init__(
        self,
        *,
        name: str,
        path: str,
        scope: Scope,
        reader: SessionReader | None = None,
        redact_policy: RedactionPolicy | None = DEFAULT_POLICY,
        max_files: int = 512,
        max_file_bytes: int = 16 << 20,
        max_facts: int = 512,
        max_fact_bytes: int = 4096,
    ) -> None:
        # Identity + path validation is the shared connector boundary (ADR-0017) — same helper the
        # file: connector uses, so the safe-agent / absolute-path rules never drift between the two.
        agent = _parse_connector_agent(name, "session:")
        _require_source_path(name, path)
        self._name = name
        self._agent = agent
        self._scope = Scope(scope)
        self._path = path
        self._reader = reader or ClaudeCodeJsonlReader()
        self._redact_policy = redact_policy
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
        """Resolve the transcript glob, hash the raw sources, and distill new content into facts.

        Returns ``unchanged=True`` (no facts) when the combined raw-source hash equals
        ``last_content_sha256`` (the §6 whole-source no-op — ADR-0023 decision 8; the ``event_key``
        idempotency absorbs any re-flood on a change). Never raises on a missing source (a connector
        pointed at a not-yet-existing transcript dir is a no-op with a note); path-safety rejections
        surface as notes.
        """
        notes: list[str] = []
        matches = _resolve_glob_files(
            self._path,
            max_files=self._max_files,
            max_file_bytes=self._max_file_bytes,
            notes=notes,
        )
        if not matches:
            return ConnectorScan(
                unchanged=last_content_sha256 is None,
                content_sha256=None,
                source_path=self._path,
                facts=(),
                notes=tuple(notes),
            )
        # Whole-source hash over the RAW transcript bytes (the same multi-file join FileConnector
        # uses); the reader/distiller are downstream of it, so a byte-identical corpus is a fast
        # no-op that reads but distills nothing.
        raw_texts = [text for _, text in matches]
        sha = content_sha256("\n\0\n".join(raw_texts))
        if last_content_sha256 is not None and sha == last_content_sha256:
            return ConnectorScan(
                unchanged=True, content_sha256=sha, source_path=self._path, notes=tuple(notes)
            )
        facts = self._collect_facts(raw_texts, notes)
        return ConnectorScan(
            unchanged=False,
            content_sha256=sha,
            source_path=self._path,
            facts=tuple(facts),
            notes=tuple(notes),
        )

    # --- internals ------------------------------------------------------------------------------
    def _collect_facts(self, raw_texts: list[str], notes: list[str]) -> list[HarvestedFact]:
        """Parse + distill every matched transcript into gated candidate facts (bounded)."""
        facts: list[HarvestedFact] = []
        for text in raw_texts:
            for para in self._distill(text):
                fact = self._build_fact(para, notes)
                if fact is None:
                    continue
                facts.append(fact)
                if len(facts) >= self._max_facts:
                    notes.append(
                        f"reached max_facts={self._max_facts}; remaining facts skipped this scan"
                    )
                    return facts
        return facts

    def _distill(self, text: str):
        """Yield marker-bearing assistant reflection paragraphs (precision-first, ADR-0023 O1/B)."""
        for turn in self._reader.read_turns(text):
            # v1: only the agent's OWN reflections. Tool turns are activity (files-touched), not
            # durable knowledge; user turns are request-shaped + highest-PII — both deferred.
            if turn.role != "assistant":
                continue
            # Drop whole agora sentinel SPANS from the WHOLE turn BEFORE splitting into paragraphs
            # (ADR-0027 §8 consumer duty — the loop-break's verbatim half): a gold pack the agent
            # echoed spans multiple paragraphs, so a per-paragraph strip (in _neutralize) would miss
            # it. Stripping the span here removes the pack content-and-all so it contributes ZERO
            # facts — mirroring FileConnector's whole-file span strip before segmentation.
            for para in _paragraphs(strip_sentinel_spans(turn.text)):
                if _SALIENCE_RE.search(para):
                    yield para

    def _build_fact(self, para: str, notes: list[str]) -> HarvestedFact | None:
        """Neutralize → redact (before the hash) → size-bound one paragraph into a HarvestedFact.

        Order matters: sentinel-strip and redact run on the FULL paragraph (best redaction coverage
        — no secret survives a truncation boundary), THEN the redacted text is size-bounded, and
        ``fact_key`` = ``content_sha256`` of that final redacted+bounded text (addendum §2 — never
        the raw secret). The reported ``redaction_hits`` reflect the redaction applied to the
        paragraph, feeding the orchestrator's ``HarvestCursor.redacted`` counter.
        """
        text = _neutralize(para).strip()
        if not text:
            return None
        if self._redact_policy is not None:
            result = redact(text, policy=self._redact_policy)
            text, hits = result.text, result.hits
        else:
            hits = ()
        encoded = text.encode("utf-8")
        if len(encoded) > self._max_fact_bytes:
            # Truncate on a UTF-8 boundary (keep the head — the salient lead of the reflection).
            text = encoded[: self._max_fact_bytes].decode("utf-8", errors="ignore").rstrip()
            notes.append(f"truncated a fact to max_fact_bytes={self._max_fact_bytes}")
            if not text:
                return None
        return HarvestedFact(text=text, fact_key=content_sha256(text), redaction_hits=hits)
