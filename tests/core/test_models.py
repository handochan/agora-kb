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
