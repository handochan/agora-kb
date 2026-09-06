"""Unit tests for the ONE ``_meta/taxonomy.yaml`` renderer and its append-only merge rule (#174).

``taxonomy_document_text`` and ``merge_allowed_tags`` were extracted out of :func:`emit_schema` so
that the SECOND legitimate writer of that file — the §5.2 admin evolution path,
``agora repo upgrade --restamp --tags-from-vault`` — cannot drift from repo-init's key order or
dump settings. Two writers of one file is exactly how a repo ends up with two shapes of its own
closed vocabulary, so the extraction's whole value is byte-identity, and byte-identity is what a
test asserting through ``yaml.safe_load`` cannot see. These assert on the TEXT.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agora_kb.core.layout import RepoLayout
from agora_kb.schema.emit import (
    Taxonomy,
    emit_schema,
    merge_allowed_tags,
    taxonomy_document_text,
)

#: The exact bytes repo-init has always written for the default document. A golden STRING rather
#: than a round trip: a change to ``sort_keys`` / ``allow_unicode`` / the key order is invisible to
#: ``yaml.safe_load`` and visible here, which is the only reason this file exists.
_DEFAULT_TEXT = "schema_version: 1\ntaxonomy_policy: open\ndomains: []\nallowed_tags: {}\n"


def test_the_renderer_is_the_bytes_repo_init_writes(tmp_path: Path) -> None:
    """The refactor's load-bearing claim: emit's file and the helper's string are the same bytes."""
    layout = RepoLayout(tmp_path)
    emit_schema(layout)

    on_disk = (layout.root / "_meta" / "taxonomy.yaml").read_text(encoding="utf-8")

    assert on_disk == _DEFAULT_TEXT
    assert (
        taxonomy_document_text(
            schema_version=1, taxonomy_policy="open", domains=[], allowed_tags={}
        )
        == on_disk
    )


def test_the_renderer_matches_emit_for_a_populated_taxonomy(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    tax = Taxonomy(
        schema_version=2,
        taxonomy_policy="capped:5",
        allowed_tags=("architecture", "concurrency"),
        domains=("ai-tech", "general"),
    )
    emit_schema(layout, taxonomy=tax)

    assert (layout.root / "_meta" / "taxonomy.yaml").read_text(
        encoding="utf-8"
    ) == taxonomy_document_text(
        schema_version=2,
        taxonomy_policy="capped:5",
        domains=("ai-tech", "general"),
        allowed_tags={"architecture": {}, "concurrency": {}},
    )


def test_unicode_tags_are_written_verbatim_not_escaped() -> None:
    """``allow_unicode=True`` is part of the contract: a Korean domain must stay readable."""
    text = taxonomy_document_text(
        schema_version=2, taxonomy_policy="open", domains=["지식"], allowed_tags={}
    )

    assert "지식" in text
    assert "\\u" not in text


# --- the merge rule ------------------------------------------------------------------------------
def test_an_existing_descriptor_keeps_its_value_and_its_position() -> None:
    """Append-only: the widening must not flatten a per-tag descriptor to ``{}`` or re-sort a file
    the operator ordered by hand — re-sorting would put lines unrelated to the run in its diff."""
    merged = merge_allowed_tags({"zebra": {"desc": "x"}, "alpha": {}}, ["beta", "zebra"])

    assert merged == {"zebra": {"desc": "x"}, "alpha": {}, "beta": {}}
    assert list(merged) == ["zebra", "alpha", "beta"]


def test_new_keys_land_in_sorted_order_whatever_order_they_arrived_in() -> None:
    """Determinism: the same recovery run must produce the same diff twice."""
    assert list(merge_allowed_tags({}, ["z", "a", "m"])) == ["a", "m", "z"]
    assert list(merge_allowed_tags({}, {"m", "a", "z"})) == ["a", "m", "z"]


def test_a_list_shaped_allowed_tags_is_promoted_to_the_mapping_form() -> None:
    """The other shape every reader tolerates, normalised to the one the emitter writes."""
    assert merge_allowed_tags(["a"], []) == {"a": {}}
    assert merge_allowed_tags(["b", "a"], ["c"]) == {"b": {}, "a": {}, "c": {}}


def test_absent_or_unreadable_degenerates_to_an_empty_mapping() -> None:
    """The conservative direction, and the one ``lint._load_taxonomy`` takes for a missing file."""
    assert merge_allowed_tags(None, []) == {}
    assert merge_allowed_tags(None, ["a"]) == {"a": {}}


def test_the_merged_mapping_renders_as_the_document_the_next_read_parses() -> None:
    """End of the loop: merge → render → parse gives back exactly the merged mapping."""
    merged = merge_allowed_tags({"architecture": {"desc": "how it is built"}}, ["infra"])

    text = taxonomy_document_text(
        schema_version=2, taxonomy_policy="open", domains=["general"], allowed_tags=merged
    )

    assert yaml.safe_load(text) == {
        "schema_version": 2,
        "taxonomy_policy": "open",
        "domains": ["general"],
        "allowed_tags": {"architecture": {"desc": "how it is built"}, "infra": {}},
    }
