"""The B3 SHIP GATE: the ``source_links:`` mirror must not move the ranker (#169 WAVE B, R-1).

WHY THIS FILE EXISTS. ``Wiki.query_lexical`` is not only the read face. ``curator/bundle.py`` feeds
it every candidate's text to build the planning brain's ``related/`` view, and that view is what
picks ``MERGE_INTO_THEME`` targets — *"a wrong merge is permanent — the closed ADR-0011 op
vocabulary has no DELETE"*. So a write-path change that shifts ranking by a hair changes, silently
and forever, what the curator folds into what. WAVE B adds exactly such a change (APPLY stamps a
derived ``source_links:`` mirror of ``sources:``), and the design brief chose the mirror over the
two body-carried alternatives on measured ranking impact alone.

WHY THE RANK GOLDENS CANNOT STAND IN FOR IT. ``tests/support/kb_builder.py`` renders frontmatter and
body directly and never calls APPLY, so ``tests/rank_golden/*.json`` is structurally blind to every
byte the curator's write path emits. Citing a green golden as evidence of neutrality would be
citing a test that cannot fail for this reason. The assertions here are the real ones.

WHAT IS PINNED, in three layers:

1. **the parser** — two note texts differing ONLY by the mirror parse to equal ``field_tokens`` /
   ``headings`` / ``outlinks``, and to an equal ``_note_to_dict`` (the reader-cache record, which is
   why ``CACHE_SCHEMA_VERSION`` stays 3 — the mirror never reaches the cached shape);
2. **the rejected alternative** — the same note carrying a body ``## Sources`` block parses to
   DIFFERENT tokens. This is the control that gives layer 1 its meaning: the test suite would pass
   just as green if ``_parse_note`` ignored the body too, so the body case has to be shown to move.
3. **the oracle** — over a whole corpus, ``query_lexical`` returns byte-identical results with and
   without the mirror. This is the property the curator actually depends on.

The corpus half of the gate (the owner's real KB, 24 queries) is measured out-of-tree by the
scratchpad harness the brief mandates; its headline is 0/24 score changes and 0/24 order changes for
the mirror against 9/24 order changes for a body ``## Sources`` block. That was a ONE-OFF,
owner-side run on 2026-09-05 over a private corpus and is **not reproducible from this repository**
— cite it as the reason the decision was taken, never as a figure a reader can re-derive. The
assertions in this file are the reproducible half, and they are what a future change has to keep
green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.core import frontmatter
from agora_kb.core.layout import RepoLayout
from agora_kb.core.wiki import Wiki, _Note, _note_to_dict, _parse_note
from agora_kb.curator.apply import _stamp_source_links
from agora_kb.schema.lint import SOURCE_LINKS_KEY
from tests.core.test_wiki import _build_repo

# A claim-bearing concept in the schema-2 layout, shaped like what APPLY writes: `sources:` carries
# the `raw/` artefact the note was distilled from, and the body carries D3 markdown links.
NOTE_PATH = "wiki/concepts/curator-concurrency.md"
NOTE_BASENAME = "curator-concurrency"
RAW_SOURCE = "raw/general/curator-concurrency.md"

BASE_NOTE = """---
title: Curator Concurrency
kind: concept
subjects:
- general
aliases:
- single-writer curator
tags:
- curator
- concurrency
status: active
summary: The curator is the single writer; concurrency is a compare-and-swap on the branch tip.
sources:
- raw/general/curator-concurrency.md
related:
- '[[inbox-design]]'
---

# Curator Concurrency

The curator is the only writer of the wiki. See [Inbox design](inbox-design.md).

## Compare and swap

A run reads the branch tip, computes a diff, and swaps only if the tip is unchanged.
"""


def _with_mirror(text: str) -> str:
    """Return ``text`` re-emitted with the REAL APPLY mirror stamped on its frontmatter.

    Deliberately routed through ``curator.apply._stamp_source_links`` and ``frontmatter.render``
    rather than a hand-written string: this test has to fail if the producer's output shape changes,
    not merely if some string a test author once typed changes.
    """
    fm, body = frontmatter.parse(text)
    _stamp_source_links(fm)
    return frontmatter.render(fm, body)


def _with_body_sources(text: str) -> str:
    """Return ``text`` with the REJECTED alternative appended: a body ``## Sources`` block."""
    fm, body = frontmatter.parse(text)
    body = body.rstrip("\n") + f"\n\n## Sources\n\n1. [{RAW_SOURCE}](../../{RAW_SOURCE})\n"
    return frontmatter.render(fm, body)


def _parse(text: str) -> _Note:
    return _parse_note(NOTE_PATH, NOTE_BASENAME, False, text)


# --- layer 0: the fixtures actually differ -------------------------------------------------------
def test_the_mirror_fixture_differs_only_inside_the_frontmatter() -> None:
    """Guard the guard: the two texts must differ, and only in the frontmatter block."""
    mirrored = _with_mirror(BASE_NOTE)
    assert mirrored != BASE_NOTE
    assert f"{SOURCE_LINKS_KEY}:" in mirrored
    assert f"[[{RAW_SOURCE}]]" in mirrored

    base_fm, base_body = frontmatter.parse(BASE_NOTE)
    mirror_fm, mirror_body = frontmatter.parse(mirrored)
    assert mirror_body == base_body  # the mirror is frontmatter-only — no body byte moves
    assert mirror_fm[SOURCE_LINKS_KEY] == [f"[[{RAW_SOURCE}]]"]
    assert mirror_fm["sources"] == base_fm["sources"]  # provenance of record is untouched


# --- layer 1: the mirror is invisible to the parser ----------------------------------------------
@pytest.mark.parametrize("attr", ["field_tokens", "headings", "outlinks"])
def test_the_mirror_changes_no_parsed_ranking_input(attr: str) -> None:
    """``_parse_note`` reads title/aliases/tags/summary/subjects/related/children.

    Never the mirror.
    """
    base = _parse(BASE_NOTE)
    mirrored = _parse(_with_mirror(BASE_NOTE))
    assert getattr(mirrored, attr) == getattr(base, attr)


def test_the_mirror_leaves_the_reader_cache_record_identical() -> None:
    """Equal ``_note_to_dict`` is why ``CACHE_SCHEMA_VERSION`` stays 3.

    The mirror never lands in the cached shape.
    """
    assert _note_to_dict(_parse(_with_mirror(BASE_NOTE))) == _note_to_dict(_parse(BASE_NOTE))


def test_the_mirror_does_not_become_an_outlink() -> None:
    """``raw/`` is not the ``[[basename]]`` identity space — the mirror must add no graph edge."""
    base = _parse(BASE_NOTE)
    mirrored = _parse(_with_mirror(BASE_NOTE))
    assert mirrored.outlinks == base.outlinks
    assert not any("curator-concurrency" == t for t in mirrored.outlinks)  # no self-edge


# --- layer 2: the rejected alternative DOES move the parser --------------------------------------
def test_a_body_sources_block_changes_tokens_headings_and_outlinks() -> None:
    """The control. Without it, layer 1 would pass on a parser that read nothing at all.

    This is the measured reason D18 chose the frontmatter mirror: a body ``## Sources`` block
    injects a heading, body tokens (the label, and — with a path label — the note's own slug
    re-entering its own body field), and a link target into the graph. On the owner's KB that
    moved the top-5 order on 9 of 24 queries; here it moves all three parsed inputs.
    """
    base = _parse(BASE_NOTE)
    blocked = _parse(_with_body_sources(BASE_NOTE))

    assert blocked.field_tokens != base.field_tokens
    assert blocked.field_tokens["body"] != base.field_tokens["body"]
    assert blocked.field_tokens["headings"] != base.field_tokens["headings"]
    assert [h.text for h in blocked.headings] != [h.text for h in base.headings]
    assert blocked.outlinks != base.outlinks
    assert _note_to_dict(blocked) != _note_to_dict(base)


# --- layer 3: the oracle the curator merges on ---------------------------------------------------
# Questions spanning the interesting statuses over the §10 fixture corpus (the same shape
# `tests/core/test_wiki_query_lexical_144.py` uses), so the comparison covers ok, multi-hit ok,
# all-stopword and no-evidence results rather than one lucky query.
QUESTIONS = (
    "curator concurrency compare and swap",
    "inbox append-only per-writer",
    "roadmap phases",
    "single-writer curator branch tip",
    "the and of",
    "quantum chromodynamics tractor",
)


def _parsed_notes(layout: RepoLayout) -> list[tuple[Path, dict[str, object], str]]:
    """Every note in the corpus that HAS frontmatter, tolerantly (the fixture's index has none)."""
    out: list[tuple[Path, dict[str, object], str]] = []
    for path in sorted(layout.root.rglob("*.md")):
        try:
            fm, body = frontmatter.parse(path.read_text(encoding="utf-8"))
        except frontmatter.FrontmatterError:
            continue
        out.append((path, fm, body))
    return out


def _seed_sources(layout: RepoLayout) -> None:
    """Give every note a ``raw/`` source, so the mirror has something to hold."""
    for path, fm, body in _parsed_notes(layout):
        fm["sources"] = [f"raw/general/{path.stem}.md"]
        path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def _stamp_corpus(layout: RepoLayout) -> int:
    stamped = 0
    for path, fm, body in _parsed_notes(layout):
        before = dict(fm)
        _stamp_source_links(fm)
        if fm != before:
            stamped += 1
        path.write_text(frontmatter.render(fm, body), encoding="utf-8")
    return stamped


def test_query_lexical_is_byte_identical_across_a_mirrored_corpus(tmp_path: Path) -> None:
    """The gate itself: the curator's ``related/`` oracle cannot tell a mirrored corpus apart."""
    plain = _build_repo(tmp_path / "plain")
    mirrored = _build_repo(tmp_path / "mirrored")
    _seed_sources(plain)
    _seed_sources(mirrored)
    assert _stamp_corpus(mirrored) > 0  # otherwise this test compares two identical corpora

    scored = 0
    for question in QUESTIONS:
        before = Wiki(plain).query_lexical(question)
        after = Wiki(mirrored).query_lexical(question)
        scored += len(before.hits)
        assert after.status == before.status, question
        assert [(h.path, h.score, h.match_reason, h.anchor, h.line) for h in after.hits] == [
            (h.path, h.score, h.match_reason, h.anchor, h.line) for h in before.hits
        ], question
    # Non-vacuity: a corpus that answered nothing would compare two empty result sets forever.
    assert scored >= 5
