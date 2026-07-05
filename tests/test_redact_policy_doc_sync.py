"""Doc/code lockstep for the redaction policy (issue #39).

The ADR-0023 "Redaction v1 policy" addendum carries a policy TABLE that MUST match the shipped
registry, so the two never drift: the default-on rows equal ``redact.DEFAULT_ON_CLASSES`` and the
default-on ∪ opt-in rows equal ``redact.KNOWN_CLASSES`` (the deferred rows are documented but not in
the registry). Also guards that the ADR-0027 §8 de-stale actually happened.
"""

from __future__ import annotations

import re
from pathlib import Path

from agora_kb.core.redact import DEFAULT_ON_CLASSES, KNOWN_CLASSES

_ROOT = Path(__file__).resolve().parents[1]
_ADR_0023 = _ROOT / "docs" / "adr" / "0023-context-harvester-connectors.md"
_ADR_0027 = _ROOT / "docs" / "adr" / "0027-gold-context-packs.md"

# A policy-table row: | `class_name` | tier | … |
_ROW = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|\s*(default-on|opt-in|deferred)\s*\|", re.MULTILINE)


def _table_tiers() -> dict[str, set[str]]:
    text = _ADR_0023.read_text(encoding="utf-8")
    tiers: dict[str, set[str]] = {"default-on": set(), "opt-in": set(), "deferred": set()}
    for cls, tier in _ROW.findall(text):
        tiers[tier].add(cls)
    return tiers


def test_default_on_table_matches_code() -> None:
    tiers = _table_tiers()
    assert tiers["default-on"] == set(DEFAULT_ON_CLASSES), (
        "ADR-0023 default-on policy table drifted from redact.DEFAULT_ON_CLASSES"
    )


def test_registry_equals_default_on_plus_opt_in() -> None:
    tiers = _table_tiers()
    assert tiers["default-on"] | tiers["opt-in"] == set(KNOWN_CLASSES), (
        "ADR-0023 (default-on ∪ opt-in) drifted from redact.KNOWN_CLASSES"
    )


def test_deferred_classes_are_not_in_the_registry() -> None:
    tiers = _table_tiers()
    assert tiers["deferred"], "the addendum must document the deferred classes"
    assert tiers["deferred"].isdisjoint(KNOWN_CLASSES)


def test_addendum_points_to_sentinel_home_and_cites_reserved_adrs() -> None:
    text = _ADR_0023.read_text(encoding="utf-8")
    assert "core/sentinel.py" in text  # the sentinel canonical-home is recorded
    assert "ADR-0027 §8" in text  # §8 is cited, not restated
    assert "ADR-0026" in text and "ADR-0030" in text  # reserved ADR IDs, not just issue numbers


def test_adr_0027_section_8_was_de_staled() -> None:
    text = _ADR_0027.read_text(encoding="utf-8")
    # the stale pre-#37 framing must be gone; the machinery's new home must be recorded.
    assert "consumer duty — NET-NEW code" not in text
    assert "connectors.py:596" not in text
    assert "core/sentinel.py" in text
