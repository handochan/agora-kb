"""Regression: the structural term must never clear the floor on its own (issue #146).

CONFORMANCE BLOCK — any change to `_passes_gate`, `_structural`, or the combined-score assembly
in `core/wiki.py` MUST keep these tests passing UNMODIFIED. If a proposed change requires editing a
test in this file, the ranker's lexical-evidence guarantee got weaker, not simpler, and the diff
should say so out loud.

WHAT #146 MEASURED. On the live corpus, empty MOC stubs beat the correct answer in 3 of 4 queries,
and in one case an honest ``not_found`` turned into three stubs. STRATEGY-2026-08 §12 recorded it;
this file pins the mechanism.

THE MECHANISM. ``_passes_gate`` admits a candidate on either of two branches: ``lex > 0``, or
``d_moc == 0`` with the query overlapping the note's *theme token set* — which includes
``moc_label_tokens``, i.e. the LINK TEXT the MOC uses to point at the note. That second branch is
reachable with ``lex`` at exactly zero, because the MOC's link text is not one of the note's own
scoring fields (``_FIELDS`` = title/aliases/tags/headings/summary/body).

Before the fix, such a candidate still received the full structural score:

    combined = W_LEX*0 + W_STRUCT*struct + fm
             = 0.65*0    + 0.35*1.0      + 0.0    = 0.35   >  FLOOR = 0.18

so an empty husk surfaced as a hit. That contradicts ADR-0012 §6, which calls this gate a
*mandatory lexical-evidence* gate, and it contradicts the comment already standing in
``test_not_found_unrelated_query`` ("structural-only notes never clear the floor"), which described
intent the code did not deliver.

A SECOND HALF OF THE SAME DEFECT: ``FLOOR`` was applied only to ``best``, so once any one candidate
cleared it every other eligible candidate rode in behind — which is how a husk scoring 0.0 was
returned as a search result. The floor is now a property of a hit, not only of the best hit.

THE INVARIANT PINNED HERE: **structure amplifies lexical evidence, it never substitutes for it.**
A note with no lexical match scores at most its frontmatter boost, which is below the floor.

WHAT THIS DOES NOT FIX, pinned as a strict xfail below rather than left as folklore: a husk's mere
EXISTENCE can still flip an honest ``not_found`` to ``ok``, because the MOC bullet that points at it
puts the husk's topic words into the MOC's own body. That is the other half of #146 — thin pages
winning on *real* lexical matches — and it needs a content/length prior, not a gate change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.core.layout import RepoLayout
from agora_kb.core.wiki import FLOOR, W_STRUCT, Wiki

# --- fixture: the #57 `note-<sha8>` husk, linked from a MOC by a descriptive label ---------------
#
# This is the real shape, not a contrivance. A Korean/CJK-titled note slugs to `note-<sha8>` (the
# #57 fallback), and the MOC bullet carries the human-readable title. So the topic words live in the
# MOC's link TEXT while the note file itself can be an empty stub whose own fields match nothing.

INDEX_MD = """\
# personal

- [Eng MOC](wiki/eng/eng-moc.md)
"""

# Written exactly as `curator/apply.py::_update_moc` writes it: title + summary carry the domain
# token at field weights 3.0/2.0, and the body is nothing but link bullets.
ENG_MOC = """\
---
status: active
title: eng MOC
summary: Map of content for the eng domain.
---
# eng MOC

- [Deadlock recovery](themes/note-a1b2c3d4.md)
"""

# The husk: `d_moc == 0` (linked straight from the MOC) and structurally maximal, but its own
# title/aliases/tags/headings/summary/body share NO token with "deadlock recovery".
HUSK = """\
---
status: stub
---
# Untitled
"""

# A note that actually answers the query, for the ordering assertion.
SUBSTANTIVE = """\
---
status: active
tags: [locking]
---
# Deadlock recovery

A deadlock is recovered by dropping the younger advisory lock and letting the
older writer proceed; recovery is deterministic and leaves no partial write.

## Recovery order
The recovery order is fixed so two hosts never both back off.
"""


def _build_repo(root: Path, *, with_substantive: bool = False) -> RepoLayout:
    (root / "wiki" / "eng" / "themes").mkdir(parents=True)
    (root / "index.md").write_text(INDEX_MD, encoding="utf-8")
    moc = ENG_MOC
    if with_substantive:
        moc += "- [Real answer](themes/real-answer.md)\n"
        (root / "wiki" / "eng" / "themes" / "real-answer.md").write_text(
            SUBSTANTIVE, encoding="utf-8"
        )
    (root / "wiki" / "eng" / "eng-moc.md").write_text(moc, encoding="utf-8")
    (root / "wiki" / "eng" / "themes" / "note-a1b2c3d4.md").write_text(HUSK, encoding="utf-8")
    return RepoLayout(root)


HUSK_PATH = "wiki/eng/themes/note-a1b2c3d4.md"


def test_146_husk_with_zero_lexical_evidence_is_not_a_hit(tmp_path: Path) -> None:
    """A MOC-linked note with lex == 0 must never appear in the hits."""
    layout = _build_repo(tmp_path / "personal")
    result = Wiki(layout).query("deadlock recovery")
    assert HUSK_PATH not in {h.path for h in result.hits}


def test_146_husk_never_outranks_a_note_that_actually_matches(tmp_path: Path) -> None:
    """The ordering half of #146: an empty stub must not beat the substantive answer."""
    layout = _build_repo(tmp_path / "personal", with_substantive=True)
    result = Wiki(layout).query("deadlock recovery")
    assert result.status == "ok"
    paths = [h.path for h in result.hits]
    assert "wiki/eng/themes/real-answer.md" in paths
    assert HUSK_PATH not in paths


def test_146_the_husk_itself_is_gone_from_both_corpora(tmp_path: Path) -> None:
    """Whatever the status, the husk is never among the hits — with or without a competing note."""
    a = Wiki(_build_repo(tmp_path / "a")).query("deadlock recovery")
    b = Wiki(_build_repo(tmp_path / "b", with_substantive=True)).query("deadlock recovery")
    assert HUSK_PATH not in {h.path for h in a.hits}
    assert HUSK_PATH not in {h.path for h in b.hits}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RESIDUAL of #146, deliberately pinned rather than hidden. The husk no longer appears as a "
        "hit, but its EXISTENCE still flips an honest not_found to ok: `moc_label_tokens` comes "
        "from the MOC's BODY link labels, so the bullet `- [Deadlock recovery](.../husk.md)` puts "
        "those words in the MOC's own body — a scoring field at weight 1.0. Delete the bullet and "
        "the query is not_found; keep it and the MOC is returned. The user is pointed at a map "
        "whose only relevant entry is empty. Fixing this needs a THIN-PAGE / CONTENT prior (the "
        "'mode (i)' half of #146: thin pages winning on real lexical matches), which the "
        "lexical-evidence fix deliberately does not attempt. When that lands, this test flips to "
        "passing and strict=True will say so out loud."
    ),
)
def test_146_residual_a_husk_still_flips_not_found_to_ok_via_its_moc_bullet(tmp_path: Path) -> None:
    with_husk = Wiki(_build_repo(tmp_path / "with-husk")).query("deadlock recovery")

    root = tmp_path / "no-husk"
    (root / "wiki" / "eng" / "themes").mkdir(parents=True)
    (root / "index.md").write_text(INDEX_MD, encoding="utf-8")
    (root / "wiki" / "eng" / "eng-moc.md").write_text(
        ENG_MOC.replace("- [Deadlock recovery](themes/note-a1b2c3d4.md)\n", ""), encoding="utf-8"
    )
    without_husk = Wiki(RepoLayout(root)).query("deadlock recovery")

    assert with_husk.status == without_husk.status


def test_146_structural_ceiling_is_below_the_floor(tmp_path: Path) -> None:
    """The arithmetic that made #146 possible, pinned as a property rather than a story.

    A maximal structural score (``struct == 1.0``: d_moc 0 and top in-degree) combined with zero
    lexical evidence must land BELOW ``FLOOR``. Before the fix this was 0.35 vs a floor of 0.18.
    """
    # The pre-fix arithmetic, stated so a future reader sees why this test exists: a maximal
    # structural score alone cleared the floor by nearly 2x.
    assert W_STRUCT * 1.0 > FLOOR, "fixture assumption: struct alone used to clear the floor"
    layout = _build_repo(tmp_path / "personal")
    result = Wiki(layout).query("deadlock recovery")
    husk = [h for h in result.hits if h.path == HUSK_PATH]
    assert husk == [], (
        "a candidate with lex == 0 and struct == 1.0 scored above the floor; "
        "structure must amplify lexical evidence, never substitute for it"
    )


def test_146_lexical_hits_are_unaffected(tmp_path: Path) -> None:
    """Blast-radius pin: the fix must change NOTHING for candidates that do have lexical evidence.

    The MOC itself matches "deadlock recovery" lexically (the words are in its body bullet), so it
    must still be returned, still with its structural boost intact.
    """
    layout = _build_repo(tmp_path / "personal")
    result = Wiki(layout).query("deadlock recovery")
    assert result.status == "ok"
    moc = next(h for h in result.hits if h.path == "wiki/eng/eng-moc.md")
    # struct == 1.0 for the MOC (d_moc 0, max in-degree), so its score must still carry W_STRUCT.
    assert moc.score > W_STRUCT, (
        "a lexically-matching note lost its structural boost; the fix over-reached"
    )
