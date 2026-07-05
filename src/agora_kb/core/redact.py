"""Deterministic, model-free outbound redaction (ADR-0023 decision 5 / O5).

The inbox is append-only and immutable (invariant #3): once a harvested fact is persisted its bytes
can never be scrubbed. So any PII-bearing source — the future ADR-0026 session distiller, a
networked connector — must strip secrets/PII **before** the fact is persisted. This module is that
shared, model-free primitive: a fixed registry of statically-compiled regexes (zero model, zero
network, no copyleft — pure stdlib ``re``, invariant #4 / ADR-0005) that returns the redacted text
plus per-class hit COUNTS. It NEVER retains, hashes, or emits the secret itself — the metadata is
class names and integer counts only, so the redaction-event counter and the ``agora harvest
--dry-run`` preview can report what happened without becoming a new unscrubbable secret-bearing
artifact.

**Reversibility.** Redaction is a one-way gate by construction: the matched bytes are discarded at
substitution time. No sidecar map, no reversible token, no offsets. A reverse map would itself be an
unscrubbable secret store, and a SHA of a short/low-entropy credential is a brute-force oracle — so
identity (``fact_key``) is taken over the REDACTED text, never the raw secret (that wiring lands
with the session connector, #25; this module only provides the primitive).

**Determinism + idempotence** (both pytest done-criteria): the registry is a fixed, ordered tuple
of compiled patterns applied most-specific-first; there is no randomness, clock, or set-iteration
in the output path, so ``redact`` is byte-identical across runs and processes. The
``[REDACTED:<class>]`` placeholder is constructed so a second pass yields byte-identical text.

**Scope (#39).** This is the module + policy only. The live write-path invocation, the repo.yaml
config surface + kill-switch, the ``HarvestCursor.redacted`` counter source, and the loop-proof e2e
land with the ``session:`` connector (#25); see the ADR-0023 "Redaction v1 policy" addendum.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agora_kb.core.sentinel import strip_agora_sentinels

__all__ = [
    "RedactionHit",
    "RedactionResult",
    "RedactionPolicy",
    "DEFAULT_POLICY",
    "KNOWN_CLASSES",
    "DEFAULT_ON_CLASSES",
    "redact",
    "sanitize",
]


@dataclass(frozen=True)
class RedactionHit:
    """One redaction class and how many of its matches were redacted. Metadata only — no secret."""

    cls: str
    count: int


@dataclass(frozen=True)
class RedactionResult:
    """Redacted text + per-class hit metadata. Frozen/hashable; carries NO secret bytes."""

    text: str
    hits: tuple[RedactionHit, ...]

    @property
    def redacted(self) -> bool:
        """True if at least one secret/PII class was redacted."""
        return bool(self.hits)

    @property
    def total(self) -> int:
        """Total number of redacted matches across all classes."""
        return sum(h.count for h in self.hits)

    @property
    def classes(self) -> tuple[str, ...]:
        """The distinct classes present, sorted — the set a per-fact counter increments (#25)."""
        return tuple(h.cls for h in self.hits)

    def counts_by_class(self) -> dict[str, int]:
        """Per-class match counts as a plain dict (class → count)."""
        return {h.cls: h.count for h in self.hits}


@dataclass(frozen=True)
class RedactionPolicy:
    """Which classes to redact + optional literal allow/deny lists (ADR-0023 decision 5).

    ``classes`` — the enabled class names (a rule whose class is absent is skipped). ``allow`` —
    literal strings NEVER redacted: an allow entry must equal the matched SECRET, which for a
    grouped rule (``bearer_token``) is the captured token, not the whole ``Authorization: Bearer …``
    match — an escape hatch for a documented sample credential in a note. ``deny`` — extra
    literal strings ALWAYS redacted, applied AFTER the rules over the already-substituted text (so a
    pathological deny literal overlapping a ``[REDACTED:…]`` placeholder is the caller's own doing).
    The mechanism is fully general; the opinionated #25 config loader is the layer that guarantees
    the structural-secret tier is never dropped.
    """

    classes: frozenset[str]
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


def _placeholder(cls: str) -> str:
    return f"[REDACTED:{cls}]"


@dataclass(frozen=True)
class _Rule:
    """A redaction rule: its class, compiled pattern, and which group holds the secret.

    ``group == 0`` replaces the whole match with the placeholder; ``group == N`` replaces only that
    capture group (keeping the surrounding context, e.g. the ``Authorization: Bearer `` prefix).
    ``pregate`` is a cheap required substring — the pattern is skipped unless it is present, which
    keeps the one multi-line pattern (PEM) off the hot path for ordinary text.
    """

    cls: str
    pattern: re.Pattern[str]
    group: int = 0
    pregate: str | None = None

    def apply(self, text: str, allow: frozenset[str]) -> tuple[str, int]:
        """Redact this rule's matches in ``text``; return (new_text, redaction_count)."""
        if self.pregate is not None and self.pregate not in text:
            return text, 0
        ph = _placeholder(self.cls)
        count = 0

        def _repl(m: re.Match[str]) -> str:
            nonlocal count
            secret = m.group(self.group) if self.group else m.group(0)
            if secret in allow:  # suppressed by the allow-list — leave verbatim, do not count
                return m.group(0)
            count += 1
            if self.group:
                whole = m.group(0)
                base = m.start()
                gs, ge = m.span(self.group)
                return whole[: gs - base] + ph + whole[ge - base :]
            return ph

        return self.pattern.sub(_repl, text), count


# --- The redaction registry (ADR-0023 "Redaction v1 policy" — Balanced-9, tightened) -------------
#
# Ordered most-specific-first. Design notes (adversarial-review hardened):
#   * PEM is FIRST and whole-block, so it consumes a key before any inner token can match. The gap
#     is NON-CROSSING (``(?!-----BEGIN)``): it CANNOT cross another opener, which (a) stops an
#     illustrative "BEGIN … PRIVATE KEY-----" in prose from swallowing to a LATER real block's END,
#     and (b) makes the scan LINEAR — each opener scans only as far as the next opener, so total
#     work is O(n) even on adversarial many-opener input. So NO numeric length cap is used: a cap
#     would FAIL OPEN, leaking a key whose body exceeds it, while buying no ReDoS safety the
#     non-crossing lookahead does not already provide. The ``pregate`` keeps this one multi-line
#     pattern off the hot path for ordinary text. Every pattern here is linear — no nested unbounded
#     quantifiers (no catastrophic backtracking).
#   * Structural single-token classes use a leading ``(?<![A-Za-z0-9])`` boundary and a ``{n,}``
#     MAXIMAL run instead of a trailing ``\b`` — a trailing ``\b`` would MISS a key glued to a word
#     char (``AKIA…EXAMPLE7``), leaking it; consuming the maximal run redacts it instead. RESIDUAL
#     (a precision tradeoff, tested): the leading boundary also skips a key glued to a PRECEDING
#     word char (``wordAKIA…``) — but keys in practice are preceded by a separator (``=`` / ``:`` /
#     quote / space / line start), and dropping the boundary would false-positive on a coincidental
#     alnum-embedded prefix, so the boundary is kept and the miss is a documented, tested contract
#     (#25 may revisit for the live write path).
#   * Stripe (``sk_``) is ordered before OpenAI/Anthropic (``sk-``); the two are already disjoint
#     (underscore vs hyphen) but the order is pinned for determinism.
#   * OpenAI/Anthropic is TIGHTENED: the ``sk-ant-``/``sk-proj-`` context variants allow ``-``/``_``
#     but the bare ``sk-`` variant is HYPHEN-FREE, so a kebab-case slug (``sk-learn-…``) is no hit.
_SK_LATIN = r"[A-Za-z0-9]"

_RULES: tuple[_Rule, ...] = (
    _Rule(
        cls="pem_private_key",
        pattern=re.compile(
            r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----"
            r"(?:(?!-----BEGIN)[\s\S])*?"
            r"-----END[A-Z0-9 ]*PRIVATE KEY-----",
        ),
        pregate="PRIVATE KEY-----",
    ),
    _Rule(
        cls="aws_access_key_id",
        pattern=re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16,}"),
    ),
    _Rule(
        cls="github_token",
        pattern=re.compile(
            r"(?<![A-Za-z0-9])(?:gh[opsru]_[A-Za-z0-9]{36,}|github_pat_[0-9A-Za-z_]{60,})"
        ),
    ),
    _Rule(
        cls="slack_token",
        pattern=re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"),
    ),
    _Rule(
        cls="google_api_key",
        pattern=re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{35,}"),
    ),
    _Rule(
        cls="stripe_secret_key",
        pattern=re.compile(r"(?<![A-Za-z0-9])[sr]k_(?:live|test)_[0-9A-Za-z]{16,}"),
    ),
    _Rule(
        cls="openai_anthropic_key",
        pattern=re.compile(
            r"(?<![A-Za-z0-9])sk-(?:(?:ant|proj)-[A-Za-z0-9_-]{20,}|" + _SK_LATIN + r"{20,})"
        ),
    ),
    _Rule(
        cls="jwt",
        pattern=re.compile(
            r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"
        ),
    ),
    _Rule(
        cls="bearer_token",
        pattern=re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([A-Za-z0-9._~+/=-]{16,})"),
        group=2,
    ),
    # OPT-IN (shipped in the registry, NOT in DEFAULT_POLICY): a broad assignment-anchored secret
    # detector. Tightly gated on a secret-noun key + assignment operator; the value group refuses to
    # re-match an existing placeholder (``(?!\[REDACTED:)``) so it is strictly idempotent.
    _Rule(
        cls="generic_assigned_secret",
        pattern=re.compile(
            r"(?i)((?:pass(?:word|wd)?|secret|api[_-]?key|access[_-]?token|client[_-]?secret"
            r"|private[_-]?token)\b\s*[:=]\s*[\"']?)(?!\[REDACTED:)([^\s\"'#]{8,})"
        ),
        group=2,
    ),
)

#: Every class known to the registry (default-on + opt-in). A policy naming an unknown class is a
#: silent no-op here; the #25 config loader is where an unknown class name FAILS LOUD.
KNOWN_CLASSES: frozenset[str] = frozenset(r.cls for r in _RULES)

#: The v1 default-on set (Balanced-9): high-precision structural secret classes. ``email`` /
#: ``phone_number`` / ``credit_card_pan`` / ``aws_secret_access_key`` / ``high_entropy_blob`` are
#: DEFERRED (see the ADR addendum); ``generic_assigned_secret`` ships opt-in only.
DEFAULT_ON_CLASSES: frozenset[str] = frozenset(
    {
        "pem_private_key",
        "aws_access_key_id",
        "github_token",
        "slack_token",
        "google_api_key",
        "stripe_secret_key",
        "openai_anthropic_key",
        "jwt",
        "bearer_token",
    }
)

#: The default policy: the Balanced-9 default-on set, empty allow/deny.
DEFAULT_POLICY = RedactionPolicy(classes=DEFAULT_ON_CLASSES)


def redact(text: str, *, policy: RedactionPolicy = DEFAULT_POLICY) -> RedactionResult:
    """Redact secrets/PII in ``text`` per ``policy``; return the redacted text + per-class counts.

    Deterministic and (given the placeholder never re-matches a rule's secret group) idempotent:
    ``redact(redact(x).text).text == redact(x).text``. Never retains or emits the matched secret.
    """
    allow = frozenset(policy.allow)
    out = text
    counts: dict[str, int] = {}
    for rule in _RULES:
        if rule.cls not in policy.classes:
            continue
        out, n = rule.apply(out, allow)
        if n:
            counts[rule.cls] = counts.get(rule.cls, 0) + n
    # deny-list: literal strings ALWAYS redacted (after the rules, so a denied literal that a rule
    # already covered is not double-counted — a rule hit removed it from ``out`` already).
    for lit in policy.deny:
        if not lit:
            continue
        out, n = re.subn(re.escape(lit), _placeholder("deny"), out)
        if n:
            counts["deny"] = counts.get("deny", 0) + n
    hits = tuple(RedactionHit(cls, counts[cls]) for cls in sorted(counts))
    return RedactionResult(text=out, hits=hits)


def sanitize(text: str, *, policy: RedactionPolicy = DEFAULT_POLICY) -> RedactionResult:
    """Full inbound sanitizer for a consumer of Agora's own output: sentinel span-drop THEN redact.

    Composes :func:`agora_kb.core.sentinel.strip_agora_sentinels` (phase-0, the ADR-0027 §8 consumer
    duty — drop Agora's own emitted packs so they never re-enter as facts) with :func:`redact`
    (phase-1, secret/PII). The two stay orthogonal and separately testable; the returned hits count
    secret/PII redactions only (span-drop is not a "redaction"). This is the entry point the future
    ``session:`` connector / networked callers use.
    """
    return redact(strip_agora_sentinels(text), policy=policy)
