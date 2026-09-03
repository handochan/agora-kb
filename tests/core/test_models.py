"""Direct tests for InboxItem validators (DATA-MODEL §1).

(pydantic v2 ValidationError subclasses ValueError, so ``pytest.raises(ValueError)`` covers it.)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agora_kb.core.hashing import content_sha256
from agora_kb.core.ids import new_event_id
from agora_kb.core.models import InboxItem, Kind

VALID_ID = new_event_id(now=datetime(2026, 6, 13, 10, 22, 33, tzinfo=UTC), rand_hex="a1b2c3")


def _item(**over: object) -> InboxItem:
    base: dict[str, object] = {
        "id": VALID_ID,
        "source": "manual",
        "writer": "dochan",
        "created": datetime(2026, 6, 13, 10, 22, 33, tzinfo=UTC),
        "content_sha256": content_sha256("x"),
        "body": "x",
    }
    base.update(over)
    return InboxItem(**base)


def test_minimal_valid_item() -> None:
    it = _item()
    assert it.kind is Kind.capture
    assert it.target == "personal"
    assert it.tags == ()


@pytest.mark.parametrize(
    "bad", ["", "XYZ", "a" * 63, "A" * 64, "g" * 64, content_sha256("x") + "\n"]
)
def test_bad_content_sha256_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        _item(content_sha256=bad)


@pytest.mark.parametrize("bad", [("Not Kebab",), ("UPPER",), ("trailing-",), ("a b",)])
def test_bad_tags_rejected(bad: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        _item(tags=bad)


def test_good_tags_accepted() -> None:
    assert _item(tags=("curator", "single-writer", "adr-0011")).tags == (
        "curator",
        "single-writer",
        "adr-0011",
    )


def test_naive_created_rejected() -> None:
    with pytest.raises(ValueError):
        _item(created=datetime(2026, 6, 13, 10, 22, 33))  # noqa: DTZ001 (intentionally naive)


@pytest.mark.parametrize("bad", ["bogus", "web:", "harvest:", "manual\n", "web:user\n"])
def test_bad_source_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        _item(source=bad)


@pytest.mark.parametrize("good", ["manual", "qwen", "claude-code", "web:dochan", "harvest:claude"])
def test_good_source_accepted(good: str) -> None:
    assert _item(source=good).source == good


@pytest.mark.parametrize("bad", ["other", "team:", "team:a/b", "personal\n"])
def test_bad_target_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        _item(target=bad)


def test_bad_id_rejected() -> None:
    with pytest.raises(ValueError):
        _item(id="not-an-id")


def test_frozen_is_immutable() -> None:
    it = _item()
    with pytest.raises(ValueError):
        it.body = "mutated"  # type: ignore[misc]


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValueError):
        _item(unexpected="nope")


# --- agent:<name> — the tool-agnostic source form (issue #147, invariant #6) --------------------


@pytest.mark.parametrize("good", ["agent:aelix", "agent:copilot", "agent:some-agent.v2"])
def test_agent_source_accepted_and_stamped_verbatim(good: str) -> None:
    """A new agent gets a first-class capture WITHOUT a core PR — and keeps its own name.

    The stamped value must be byte-identical to what the caller supplied: provenance that
    normalized or aliased the name would make two agents indistinguishable downstream.
    """
    item = _item(source=good)
    assert item.source == good
    assert item.to_frontmatter()["source"] == good


def test_agent_source_is_a_capture_not_a_gated_candidate() -> None:
    """`agent:<name>` is an assertion by the agent, so it defaults to `kind=capture`.

    This is the whole point of the form: `harvest:<agent>` (Agora PULLING from an agent) enters
    gated, and the gate's closed op set cannot create a note. An agent capturing under its own
    identity must not be forced through that door.
    """
    assert _item(source="agent:aelix").kind is Kind.capture


@pytest.mark.parametrize(
    "bad",
    [
        "aelix",  # a BARE name stays rejected — no silent blessing of an unprefixed source
        "agent:",  # the <name> token is required
        "agent:a b",  # whitespace never reaches provenance
        "agent:.hidden",  # must start alphanumeric (same rule as team:<name>)
        "agent:a/b",  # no path separators
        "agent:aelix\n",  # \A...\Z, so a trailing newline cannot slip through
    ],
)
def test_bad_agent_source_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        _item(source=bad)


def test_fixed_sources_are_not_widened_by_the_parametric_form() -> None:
    """The back-compat set stays exactly what it was — `agent:<name>` is the growth path.

    A regression here would mean someone added an agent name to the core again, which is the
    invariant-6 violation issue #147 closed.
    """
    from agora_kb.core.models import FIXED_SOURCES

    assert FIXED_SOURCES == frozenset(
        {"claude-code", "codex", "qwen", "gemini", "opencode", "hermes", "manual"}
    )
