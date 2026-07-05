"""Tests for the deterministic redaction module (agora_kb.core.redact, ADR-0023 decision 5).

Locks the pytest done-criteria: per-class strip rules, precision (no false-positive corruption of
ordinary curated content), determinism + idempotence, and the six-surface no-secret-retention
guarantee. Adversarial-review hardened: glued-token no-leak, non-crossing/bounded PEM, tightened
``sk-``, and a ReDoS timing bound.
"""

from __future__ import annotations

import time

import pytest

from agora_kb.core.redact import (
    DEFAULT_ON_CLASSES,
    DEFAULT_POLICY,
    KNOWN_CLASSES,
    RedactionHit,
    RedactionPolicy,
    RedactionResult,
    redact,
    sanitize,
)

# One representative sample per default-on class, and the sensitive substring that must NOT survive.
_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEpAIBAAKCAQEAsecretBODYbytes123\n"
    "abcd/efgh+ijkl==\n"
    "-----END RSA PRIVATE KEY-----"
)
_SAMPLES: dict[str, tuple[str, str]] = {
    # class: (text containing the secret, the sensitive substring that must be gone from output)
    "pem_private_key": (_PEM, "MIIEpAIBAAKCAQEAsecretBODYbytes123"),
    "aws_access_key_id": ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    "github_token": ("ghp_" + "a" * 36, "ghp_" + "a" * 36),
    "slack_token": ("xoxb-123456789012-abcdefABCDEF", "xoxb-123456789012-abcdefABCDEF"),
    "google_api_key": ("AIza" + "b" * 35, "AIza" + "b" * 35),
    "stripe_secret_key": ("sk_live_" + "0" * 24, "sk_live_" + "0" * 24),
    "openai_anthropic_key": ("sk-proj-" + "a" * 24, "sk-proj-" + "a" * 24),
    "jwt": (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    ),
    "bearer_token": ("Authorization: Bearer abcdefghijklmnop12345", "abcdefghijklmnop12345"),
}


# --- positive matrix: every default-on class redacts its secret --------------------------------


@pytest.mark.parametrize("cls", sorted(DEFAULT_ON_CLASSES))
def test_default_on_class_redacts(cls: str) -> None:
    text, secret = _SAMPLES[cls]
    result = redact(f"before {text} after")
    assert result.redacted
    assert cls in result.counts_by_class()
    assert secret not in result.text
    assert f"[REDACTED:{cls}]" in result.text
    assert "before" in result.text and "after" in result.text


def test_bearer_keeps_the_header_prefix() -> None:
    result = redact("Authorization: Bearer abcdefghijklmnop12345")
    assert result.text == "Authorization: Bearer [REDACTED:bearer_token]"


def test_pem_collapses_the_whole_block_to_one_placeholder() -> None:
    result = redact(f"x\n{_PEM}\ny")
    assert result.text == "x\n[REDACTED:pem_private_key]\ny"
    assert result.counts_by_class() == {"pem_private_key": 1}


# --- glued-token no-leak (adversarial finding: a trailing \b would MISS and leak) ----------------


def test_key_glued_to_trailing_word_char_is_still_fully_redacted() -> None:
    # A trailing \b would fail to match AKIA…EXAMPLE7 / AKIA…EXAMPLEfoo, leaking the key. The
    # maximal run consumes it instead — the key must be gone from the output either way.
    out = redact("a AKIAIOSFODNN7EXAMPLE7 b AKIAIOSFODNN7EXAMPLEfoo c").text
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert out.count("[REDACTED:aws_access_key_id]") == 2


def test_key_glued_to_a_preceding_word_char_is_a_documented_miss() -> None:
    # KNOWN, CONSCIOUS residual (module docstring): the leading (?<![A-Za-z0-9]) boundary skips a
    # key glued to a PRECEDING word char (keys are ~always preceded by =/:/quote/space/line-start,
    # dropping the boundary would false-positive on a coincidental alnum-embedded prefix). Redaction
    # on the LIVE write path (#25) may revisit this; here we lock the contract so it is deliberate.
    assert not redact("wordAKIAIOSFODNN7EXAMPLE").redacted
    # ... but the common separators all keep it a hit:
    for sep in ("=", ": ", '"', " ", "\n"):
        assert redact(f"key{sep}AKIAIOSFODNN7EXAMPLE").redacted


# --- precision / negative matrix: ordinary curated content must NOT be redacted -----------------


@pytest.mark.parametrize(
    "text",
    [
        "commit f846dd6a1b2c3d4e5f60718293a4b5c6d7e8f90a landed",  # 40-hex git sha
        "id 550e8400-e29b-41d4-a716-446655440000 here",  # uuid
        "ping alice@example.com about the release",  # email (PII deferred, not default-on)
        "bump to 1.2.3 (build 20240101) and tag v2",  # version / date digits
        "see the sk-learn-model-training-and-evaluation-pipeline notes",  # kebab slug, not a key
        "the data URI iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ was inline",  # base64 blob
        "password rotation policy is documented in the runbook",  # 'password' with no assignment
        "normal prose with no secrets whatsoever.",
    ],
)
def test_precision_no_false_positive_on_curated_content(text: str) -> None:
    result = redact(text)
    assert not result.redacted, f"false positive on: {text!r} -> {result.counts_by_class()}"
    assert result.text == text


# --- determinism + golden vector ----------------------------------------------------------------


def test_determinism_two_calls_identical() -> None:
    text = "aws AKIAIOSFODNN7EXAMPLE gh ghp_" + "a" * 36
    assert redact(text) == redact(text)


def test_golden_vector_frozen() -> None:
    # A frozen expected string pins byte-stable output (guards against set/dict-iteration drift).
    text = "aws AKIAIOSFODNN7EXAMPLE gh ghp_" + "a" * 36
    result = redact(text)
    assert result.text == "aws [REDACTED:aws_access_key_id] gh [REDACTED:github_token]"
    assert result.hits == (
        RedactionHit("aws_access_key_id", 1),
        RedactionHit("github_token", 1),
    )


def test_hits_sorted_by_class() -> None:
    # stripe + openai + aws in scrambled order -> hits come out class-sorted.
    text = "sk-proj-" + "a" * 24 + " sk_live_" + "0" * 24 + " AKIAIOSFODNN7EXAMPLE"
    result = redact(text)
    assert result.classes == ("aws_access_key_id", "openai_anthropic_key", "stripe_secret_key")


# --- idempotence (incl. the opt-in class, per the adversarial finding) --------------------------


def test_idempotence_default_policy() -> None:
    text = f"{_PEM}\nAuthorization: Bearer abcdefghijklmnop12345\nAKIAIOSFODNN7EXAMPLE"
    once = redact(text).text
    assert redact(once).text == once


def test_idempotence_with_generic_enabled() -> None:
    # generic_assigned_secret's value group refuses to re-match a placeholder, so a second pass is
    # byte-identical text (the contract is TEXT-idempotence, not 'no rule ever re-matches').
    policy = RedactionPolicy(classes=DEFAULT_ON_CLASSES | {"generic_assigned_secret"})
    text = "password: hunter2secret and api_key = ZYXWVUT9876"
    once = redact(text, policy=policy).text
    assert "hunter2secret" not in once and "ZYXWVUT9876" not in once
    assert redact(once, policy=policy).text == once


def test_placeholder_format_is_pure_class_name() -> None:
    # No index/offset/count/hash in the placeholder — a pure function of the class name.
    result = redact("AKIAIOSFODNN7EXAMPLE and AKIAIOSFODNN7EXAMPLF")
    assert result.text == "[REDACTED:aws_access_key_id] and [REDACTED:aws_access_key_id]"


# --- opt-in class: shipped in the registry, OFF by default --------------------------------------


def test_generic_assigned_secret_off_by_default() -> None:
    assert not redact("password: hunter2secret").redacted
    assert "generic_assigned_secret" not in DEFAULT_ON_CLASSES
    assert "generic_assigned_secret" in KNOWN_CLASSES


def test_generic_assigned_secret_on_when_enabled() -> None:
    policy = RedactionPolicy(classes={"generic_assigned_secret"})
    result = redact('client_secret="s3cr3tvalue123"', policy=policy)
    assert "s3cr3tvalue123" not in result.text
    assert result.counts_by_class() == {"generic_assigned_secret": 1}


@pytest.mark.parametrize(
    "text",
    [
        "password rotation policy is documented in the runbook",  # secret-noun, no assignment
        "the secret to good pasta is salt",  # 'secret' as prose, no operator
        "api_key: n/a",  # value too short (< 8 chars)
    ],
)
def test_generic_assigned_secret_precision_when_enabled(text: str) -> None:
    # Even the broad opt-in detector must not fire on ordinary prose that merely mentions a
    # secret-noun without an assignment-operator + long value.
    policy = RedactionPolicy(classes={"generic_assigned_secret"})
    assert not redact(text, policy=policy).redacted


def test_generic_assigned_secret_no_secret_retained() -> None:
    policy = RedactionPolicy(classes={"generic_assigned_secret"})
    secret = "SUPERSECRETVALUE123"
    result = redact(f"api_key = {secret}", policy=policy)
    surfaces = [result.text, repr(result), repr(result.hits), str(result.counts_by_class())]
    for surface in surfaces:
        assert secret not in surface


# --- ordering: Stripe before OpenAI, PEM whole-block first --------------------------------------


def test_stripe_underscore_not_swallowed_by_openai_rule() -> None:
    # sk_live_ (stripe) and sk- (openai) are disjoint but the order is pinned; a stripe key is
    # classed as stripe, never openai.
    result = redact("sk_live_" + "0" * 24)
    assert result.counts_by_class() == {"stripe_secret_key": 1}


def test_pem_non_crossing_gap_does_not_over_redact_prose() -> None:
    # An illustrative unmatched BEGIN mentioned in prose must NOT swallow the intervening lines up
    # to a LATER real block's END (the non-crossing gap fixes this over-redaction).
    text = (
        "the file starts with -----BEGIN PRIVATE KEY----- as a header.\n"
        "IMPORTANT LEGIT NOTE that must survive.\n"
        "another line to keep.\n"
        f"{_PEM}\n"
        "trailing note.\n"
    )
    out = redact(text).text
    assert "IMPORTANT LEGIT NOTE that must survive." in out
    assert "another line to keep." in out
    assert "MIIEpAIBAAKCAQEAsecretBODYbytes123" not in out  # the real key body is gone
    assert out.count("[REDACTED:pem_private_key]") == 1


def test_pem_large_body_still_collapses_no_fail_open() -> None:
    # REGRESSION (adversarial HIGH): a numeric gap cap would FAIL OPEN — a key body larger than the
    # cap never reaches -----END, so the whole block would fail to match and leak verbatim. The
    # non-crossing gap is UNBOUNDED (and linear), so a big key (RSA-16384-ish, ~20 KiB body) still
    # collapses to one placeholder with no body bytes surviving.
    big = (
        "-----BEGIN RSA PRIVATE KEY-----\n" + "MIIEpQzz" * 2600 + "\n-----END RSA PRIVATE KEY-----"
    )
    assert len(big) > 8192
    result = redact(f"leaked key:\n{big}\ntrailing")
    assert result.counts_by_class() == {"pem_private_key": 1}
    assert "MIIEpQzz" not in result.text
    assert result.text == "leaked key:\n[REDACTED:pem_private_key]\ntrailing"


# --- allow / deny -------------------------------------------------------------------------------


def test_allow_list_suppresses_a_documented_sample() -> None:
    policy = RedactionPolicy(classes=DEFAULT_ON_CLASSES, allow=("AKIAIOSFODNN7EXAMPLE",))
    result = redact("sample AKIAIOSFODNN7EXAMPLE only", policy=policy)
    assert not result.redacted
    assert result.text == "sample AKIAIOSFODNN7EXAMPLE only"


def test_allow_suppresses_a_grouped_rule_secret() -> None:
    # For a grouped rule (bearer_token) the allow entry must equal the captured SECRET (the token),
    # not the whole "Authorization: Bearer …" match — this pins that documented behavior.
    token = "abcdefghijklmnop12345"
    policy = RedactionPolicy(classes=DEFAULT_ON_CLASSES, allow=(token,))
    result = redact(f"Authorization: Bearer {token}", policy=policy)
    assert not result.redacted
    assert result.text == f"Authorization: Bearer {token}"


def test_deny_list_forces_a_literal() -> None:
    policy = RedactionPolicy(classes=frozenset(), deny=("TopSecretProject",))
    result = redact("the TopSecretProject launch", policy=policy)
    assert result.text == "the [REDACTED:deny] launch"
    assert result.counts_by_class() == {"deny": 1}


# --- six-surface no-secret-retention ------------------------------------------------------------


@pytest.mark.parametrize("cls", sorted(DEFAULT_ON_CLASSES))
def test_no_secret_retained_on_any_surface(cls: str) -> None:
    text, secret = _SAMPLES[cls]
    result = redact(f"context {text} context")
    surfaces = [
        result.text,
        repr(result),
        repr(result.hits),
        str(result.counts_by_class()),
        "".join(h.cls for h in result.hits),
        "".join(str(h.count) for h in result.hits),
    ]
    for surface in surfaces:
        assert secret not in surface, f"{cls}: secret leaked into {surface[:60]!r}"


def test_result_is_frozen_and_hashable() -> None:
    result = redact("AKIAIOSFODNN7EXAMPLE")
    assert isinstance(hash(result), int)
    with pytest.raises((AttributeError, TypeError)):
        result.text = "mutated"  # type: ignore[misc]


# --- RedactionResult derived members ------------------------------------------------------------


def test_result_derived_members() -> None:
    clean = redact("nothing to see")
    assert not clean.redacted and clean.total == 0 and clean.classes == ()
    assert clean.counts_by_class() == {}
    multi = redact("AKIAIOSFODNN7EXAMPLE AKIAIOSFODNN7EXAMPLF ghp_" + "a" * 36)
    assert multi.total == 3
    assert multi.counts_by_class() == {"aws_access_key_id": 2, "github_token": 1}
    assert isinstance(multi, RedactionResult)


# --- ReDoS linearity (the non-crossing lookahead, NOT a numeric cap, provides the bound) --------


def test_pem_redos_linear_on_pathological_input() -> None:
    # Many unterminated BEGIN openers (the classic quadratic shape). Linearity comes from the
    # NON-CROSSING gap `(?!-----BEGIN)`: each opener scans only as far as the NEXT opener, so total
    # work is O(n) — no numeric cap is needed (a cap would fail open, see the large-body test). A
    # regression to a CROSSING/backtracking gap would blow the time budget here.
    evil = ("-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 20 + "\n") * 16000  # ~830 KiB, no END
    start = time.perf_counter()
    result = redact(evil)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"redact took {elapsed:.2f}s — possible ReDoS regression"
    assert not result.redacted  # no closer -> nothing matched


# --- sanitize composes span-drop THEN redact ----------------------------------------------------


def test_sanitize_drops_pack_span_then_redacts() -> None:
    text = (
        "<!-- agora:pack repo=r pack=p commit=c -->\n"
        "- a pack line carrying AKIAIOSFODNN7EXAMPLE\n"
        "<!-- agora:pack:end repo=r pack=p commit=c -->\n"
        "kept line with sk_live_" + "0" * 24 + "\n"
    )
    result = sanitize(text)
    # the whole pack span is gone (span-drop, not counted as a redaction) ...
    assert "pack line" not in result.text
    assert "AKIAIOSFODNN7EXAMPLE" not in result.text
    # ... and the surviving line's secret is redacted (counted).
    assert "sk_live_" not in result.text
    assert result.counts_by_class() == {"stripe_secret_key": 1}


def test_sanitize_equals_redact_when_no_sentinels() -> None:
    text = "just AKIAIOSFODNN7EXAMPLE here"
    assert sanitize(text) == redact(text)


# --- policy defaults ----------------------------------------------------------------------------


def test_default_policy_is_the_balanced_nine() -> None:
    assert DEFAULT_POLICY.classes == DEFAULT_ON_CLASSES
    assert DEFAULT_POLICY.allow == () and DEFAULT_POLICY.deny == ()
    assert len(DEFAULT_ON_CLASSES) == 9
    # the deferred/opt-in classes are NOT default-on
    for deferred in ("email", "phone_number", "credit_card_pan", "generic_assigned_secret"):
        assert deferred not in DEFAULT_ON_CLASSES
