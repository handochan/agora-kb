"""Tests for the deterministic, model-free read path (core.wiki, ADR-0012).

The fixture corpus mirrors ADR-0012 §10 (the `personal` repo: index.md, an ai-tech map linking two
concepts via the ADR-0014 D3 standard-markdown BODY links `[Title](relative.md)`, two concept notes
with frontmatter + headings, and a deprecated roadmap note) — materialised in the **KB wiki schema-2
kind-first layout** (ADR-0041 D1): `wiki/maps/<subject>.md` for the map (the `-moc` filename suffix
is gone) and `wiki/concepts/<slug>.md` for the concepts, with the subject carried in `subjects:`
frontmatter rather than in a path segment (D2.2/D3.2). The read path seeds the `linked-theme` graph
from those markdown body links (path→basename), so retrieval-as-navigation works on a produced repo.
The
SCORING FORMULA is normative; the exact float numbers are not byte-pinned cross-machine, so we
assert ordering, field correctness, the lexical-evidence gate, the not_found floor, determinism,
tie-break, and limit — not magic score constants.
"""

from __future__ import annotations

from pathlib import Path

from agora_kb.core.layout import RepoLayout
from agora_kb.core.wiki import (
    QueryResult,
    SearchHit,
    Wiki,
    _is_map_path,
    _is_moc_path,
    _parse_note,
    _path_kind,
)

# --- fixture corpus (ADR-0012 §10) --------------------------------------------------------------

# ADR-0014 D3: the produced MOC/index BODY child bullets are STANDARD MARKDOWN LINKS
# `[Title](relative.md)`. The read path seeds the `linked-theme` graph from these markdown links
# (path→basename) exactly as it did from `[[ ]]`, and the link TEXT supplies the MOC-label tokens.
INDEX_MD = """\
# personal

- [AI Tech MOC](wiki/maps/ai-tech.md)
"""

# ADR-0041 D5: the map is recognised by its DIRECTORY (`wiki/maps/`), and its subject scope is the
# `subjects:` frontmatter list — the frontmatter read that replaced the v1 `<domain>-moc.md` path
# read. The bullets are POSIX-relative from the map's own directory, as APPLY writes them.
AI_TECH_MOC = """\
---
status: active
kind: map
subjects: [ai-tech]
---
# AI Tech

- [Curator concurrency](../concepts/curator-concurrency.md) — single-writer curator serializes
- [Inbox design](../concepts/inbox-design.md) — append-only per-writer inbox
"""

CURATOR_CONCURRENCY = """\
---
status: active
tags: [single-writer, concurrency]
---
# Curator Concurrency

The curator acquires a per-repo flock on curator.lock so exactly one writer
advances the curated branch.

## Compare and swap
Concurrency control is enforced by compare-and-swap on the branch ref.
"""

INBOX_DESIGN = """\
---
status: active
tags: [inbox, append-only]
---
# Inbox Design

The inbox is append-only and per-writer namespaced. Items are never edited or reordered.
"""

ROADMAP = """\
---
status: deprecated
tags: [roadmap]
---
# Roadmap

Phase 1 is the personal MVP milestone.
"""


#: The schema-2 paths of the fixture corpus, named once so a rename is a one-line edit.
MOC_PATH = "wiki/maps/ai-tech.md"
CURATOR_PATH = "wiki/concepts/curator-concurrency.md"
INBOX_PATH = "wiki/concepts/inbox-design.md"
ROADMAP_PATH = "wiki/concepts/roadmap.md"


def _build_repo(root: Path) -> RepoLayout:
    """Materialize the §10 fixture corpus under ``root`` (schema-2 layout) and return its layout."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(INDEX_MD, encoding="utf-8")
    maps = root / "wiki" / "maps"
    concepts = root / "wiki" / "concepts"
    maps.mkdir(parents=True)
    concepts.mkdir(parents=True)
    (root / MOC_PATH).write_text(AI_TECH_MOC, encoding="utf-8")
    (root / CURATOR_PATH).write_text(CURATOR_CONCURRENCY, encoding="utf-8")
    (root / INBOX_PATH).write_text(INBOX_DESIGN, encoding="utf-8")
    # An orphan concept: no map lists it, so it is reached lexically or not at all. (In v1 it lived
    # under a second domain; schema 2 files it by KIND and records the subject in frontmatter.)
    (root / ROADMAP_PATH).write_text(ROADMAP, encoding="utf-8")
    return RepoLayout(root)


# --- core behavior ------------------------------------------------------------------------------


def test_query_returns_expected_hit(tmp_path: Path) -> None:
    layout = _build_repo(tmp_path / "personal")
    wiki = Wiki(layout)
    result = wiki.query("curator concurrency control")

    assert isinstance(result, QueryResult)
    assert result.status == "ok"
    assert len(result.hits) >= 1
    top = result.hits[0]
    assert isinstance(top, SearchHit)
    assert top.repo == "personal"
    assert top.path == CURATOR_PATH
    # d_moc==0 child whose tags/title intersect the query → linked-theme, anchored at the H1.
    assert top.match_reason == "linked-theme"
    assert top.anchor == "curator-concurrency"
    assert top.line == 1
    assert 0.0 <= top.score <= 1.0
    assert "Curator Concurrency" in top.excerpt


def test_linked_theme_traverses_markdown_body_link(tmp_path: Path) -> None:
    # ADR-0014 D3 explicit: the MOC seeds its theme child via a STANDARD MARKDOWN BODY LINK
    # `- [Curator concurrency](themes/curator-concurrency.md)` (NOT a `[[ ]]` wikilink). The read
    # path must resolve that link path→basename, seed the theme at d_moc==0, and return
    # match_reason == linked-theme — proving the D3 grammar drives the nav graph (ADR-0012 §5).
    layout = _build_repo(tmp_path / "personal")
    moc_body = (layout.root / MOC_PATH).read_text(encoding="utf-8")
    assert "](../concepts/curator-concurrency.md)" in moc_body  # the produced D3 body-link form
    assert "[[curator-concurrency]]" not in moc_body  # no wikilink body edge
    result = Wiki(layout).query("curator concurrency control")
    top = next(h for h in result.hits if h.path.endswith("curator-concurrency.md"))
    assert top.match_reason == "linked-theme"


def test_lexical_evidence_gate_drops_inbox_design(tmp_path: Path) -> None:
    # ADR-0012 §10: inbox-design is a d_moc=0 child with struct=1.0 but lex=0 and q∩theme=∅, so the
    # gate DROPS it even though it is structurally strong. The query must not return it.
    layout = _build_repo(tmp_path / "personal")
    result = Wiki(layout).query("curator concurrency control")
    paths = {h.path for h in result.hits}
    assert INBOX_PATH not in paths


def test_not_found_unrelated_query(tmp_path: Path) -> None:
    # "quantum biology photosynthesis": every candidate has lex=0 and no d_moc=0 theme overlap →
    # all dropped by the gate → not_found (structural-only notes never clear the floor).
    layout = _build_repo(tmp_path / "personal")
    result = Wiki(layout).query("quantum biology photosynthesis")
    assert result.status == "not_found"
    assert result.hits == ()


def test_not_found_all_stopwords(tmp_path: Path) -> None:
    layout = _build_repo(tmp_path / "personal")
    result = Wiki(layout).query("what is the and or")
    assert result.status == "not_found"
    assert result.hits == ()


def test_not_found_empty_repo(tmp_path: Path) -> None:
    # core.wiki.query is callable on a freshly-initialized repo before any notes exist.
    root = tmp_path / "personal"
    root.mkdir()
    result = Wiki(RepoLayout(root)).query("anything at all")
    assert result.status == "not_found"
    assert result.hits == ()


def test_lexical_match_reason_and_body_anchor(tmp_path: Path) -> None:
    # "ref" matches only in the body line UNDER the "## Compare and swap" heading (it does not occur
    # in the title/tags or before that heading) → the lexical reason with the enclosing-heading slug
    # as anchor and a 1-based body line (ADR-0012 §6 reason 3).
    layout = _build_repo(tmp_path / "personal")
    result = Wiki(layout).query("ref enforced")
    assert result.status == "ok"
    hit = next(h for h in result.hits if h.path.endswith("curator-concurrency.md"))
    assert hit.match_reason == "lexical"
    assert hit.anchor == "compare-and-swap"
    assert hit.line >= 1
    assert "ref" in hit.excerpt.lower()


def test_pre_heading_lexical_match_has_empty_anchor(tmp_path: Path) -> None:
    # A body match BEFORE any enclosing heading yields anchor == "" (ADR-0012 §6 reason 3 / §0
    # widening of DATA-MODEL §9). "flock" appears only in the pre-H2 prose line.
    layout = _build_repo(tmp_path / "personal")
    result = Wiki(layout).query("flock acquires")
    assert result.status == "ok"
    hit = next(h for h in result.hits if h.path.endswith("curator-concurrency.md"))
    assert hit.match_reason == "lexical"
    assert hit.anchor == ""
    assert "flock" in hit.excerpt.lower()


def test_heading_match_reason(tmp_path: Path) -> None:
    # "compare swap" matches a heading text -> heading reason with that heading's slug + line.
    layout = _build_repo(tmp_path / "personal")
    result = Wiki(layout).query("compare swap")
    assert result.status == "ok"
    hit = next(h for h in result.hits if h.path.endswith("curator-concurrency.md"))
    assert hit.match_reason == "heading"
    assert hit.anchor == "compare-and-swap"
    # line of the "## Compare and swap" heading (1-based in the parsed body)
    assert hit.line >= 1


def test_top_idf_body_term_beats_weak_heading_term(tmp_path: Path) -> None:
    # ADR-0012 §6 reason 2 is gated on the HIGHEST-IDF matched term landing in title/headings, not
    # on ANY matched term hitting a heading. Here the high-idf term ("zephyrumquux") matches only in
    # the body, while a low-idf term ("design") matches a heading. The reason MUST be 'lexical'
    # (anchored at the enclosing heading of the body line), NOT 'heading'.
    root = tmp_path / "personal"
    concepts = root / "wiki" / "concepts"
    concepts.mkdir(parents=True)
    (root / "wiki" / "maps").mkdir(parents=True)
    (root / "index.md").write_text(
        "# personal\n\n- [General MOC](wiki/maps/general.md)\n", encoding="utf-8"
    )
    (root / "wiki" / "maps" / "general.md").write_text(
        "# General\n\n- [Note A](../concepts/note-a.md)\n"
        "- [Design note](../concepts/design-note.md)\n",
        encoding="utf-8",
    )
    # note-a: high-idf "zephyrumquux" in the BODY under a low-idf "Design notes" heading.
    (concepts / "note-a.md").write_text(
        "# Note A\n\n## Design notes\nThe term zephyrumquux is unique here.\n", encoding="utf-8"
    )
    # design-note drives down idf("design") so it is the WEAK term.
    (concepts / "design-note.md").write_text(
        "# Design Note\n\nDesign and design topics, design everywhere, design design.\n",
        encoding="utf-8",
    )
    result = Wiki(RepoLayout(root)).query("design zephyrumquux")
    assert result.status == "ok"
    hit = next(h for h in result.hits if h.path.endswith("note-a.md"))
    assert hit.match_reason == "lexical"
    # anchored at the enclosing heading above the matched body line, at the body line (not line 3).
    assert hit.anchor == "design-notes"
    assert hit.line == 4
    assert "zephyrumquux" in hit.excerpt.lower()


def test_not_found_eligible_but_below_floor(tmp_path: Path) -> None:
    # ADR-0012 §5 not_found gate (c): candidates pass the lexical-evidence gate (lex>0) yet the best
    # combined SCORE is still below the 0.18 floor. A common low-idf token in many LONG orphan notes
    # (d_moc=3, indeg=0 -> minimal struct) keeps every eligible candidate under the floor.
    root = tmp_path / "personal"
    concepts = root / "wiki" / "concepts"
    concepts.mkdir(parents=True)
    # index has no links and does not match the query (so it is not an eligible hit either).
    (root / "index.md").write_text("# personal\n", encoding="utf-8")
    filler = " ".join(f"fillerword{i}" for i in range(120))
    for i in range(12):
        (concepts / f"doc{i}.md").write_text(
            f"# Doc {i}\n\n{filler} commonword more filler text here.\n", encoding="utf-8"
        )
    result = Wiki(RepoLayout(root)).query("commonword")
    assert result.status == "not_found"
    assert result.hits == ()


def _two_subject_repo(root: Path, *, cooking_subjects: str = "[cooking]") -> RepoLayout:
    """Two maps under ``wiki/maps/``, each declaring one subject, each with one concept child."""
    (root / "wiki" / "maps").mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "index.md").write_text(
        "# personal\n\n- [AI Tech MOC](wiki/maps/ai-tech.md)\n"
        "- [Cooking MOC](wiki/maps/cooking.md)\n",
        encoding="utf-8",
    )
    (root / "wiki" / "maps" / "ai-tech.md").write_text(
        "---\nkind: map\nsubjects: [ai-tech]\n---\n"
        "# AI Tech\n\n- [Agents](../concepts/agents.md)\n",
        encoding="utf-8",
    )
    (root / "wiki" / "concepts" / "agents.md").write_text(
        "---\ntags: [agents]\n---\n# Agents\n\nAgents and tooling.\n", encoding="utf-8"
    )
    (root / "wiki" / "maps" / "cooking.md").write_text(
        f"---\nkind: map\nsubjects: {cooking_subjects}\n---\n"
        "# Cooking\n\n- [Recipes](../concepts/recipes.md)\n",
        encoding="utf-8",
    )
    (root / "wiki" / "concepts" / "recipes.md").write_text(
        "---\ntags: [recipes]\n---\n# Recipes\n\nRecipes about cooking.\n", encoding="utf-8"
    )
    return RepoLayout(root)


def test_hyphenated_subject_focus_restricts_map_seeding(tmp_path: Path) -> None:
    # ADR-0012 §4 as re-expressed by ADR-0041 D5: a question carrying a token that exactly matches a
    # DECLARED subject seeds only the maps whose `subjects:` contain it. The hyphenated subject
    # "ai-tech" tokenizes to {ai, tech} (a literal hyphenated token can never appear under the §3
    # alphabet, so "exactly matching" can only mean all of the subject's tokens being present — the
    # v1 reading, carried over verbatim). Under that focus the cooking map is not seeded, so its
    # child is no longer a d_moc=0 child and cannot be 'linked-theme'.
    wiki = Wiki(_two_subject_repo(tmp_path / "personal"))

    # No focus: "recipes" alone -> recipes is a d_moc=0 child of the cooking map -> linked-theme.
    unfocused = wiki.query("recipes")
    rec_unfocused = next(h for h in unfocused.hits if h.path.endswith("recipes.md"))
    assert rec_unfocused.match_reason == "linked-theme"

    # Focused on ai-tech (both {ai, tech} present): the cooking map is NOT seeded, so recipes is not
    # a d_moc=0 child and cannot be linked-theme.
    focused = wiki.query("ai tech recipes")
    rec_focused = next(h for h in focused.hits if h.path.endswith("recipes.md"))
    assert rec_focused.match_reason != "linked-theme"


def test_subject_focus_reads_frontmatter_not_the_path(tmp_path: Path) -> None:
    # The whole point of D3.2: the subject lives in `subjects:` and NOWHERE else. The map's own
    # BASENAME here says "cooking", but its declared subject is "ai-tech" — so an "ai tech" question
    # focuses ON it (its bullet child stays d_moc=0), which a path-reading filter could not do.
    root = tmp_path / "personal"
    wiki = Wiki(_two_subject_repo(root, cooking_subjects="[ai-tech]"))
    focused = wiki.query("ai tech recipes")
    rec = next(h for h in focused.hits if h.path.endswith("recipes.md"))
    assert rec.match_reason == "linked-theme"


def test_subject_focus_seeds_every_map_carrying_the_subject(tmp_path: Path) -> None:
    # What ADR-0041 D5 says genuinely moves: v1 bounded the focused seed set at ONE MOC per domain,
    # while `wiki/maps/` may hold several maps carrying the same subject. All of them seed.
    root = tmp_path / "personal"
    wiki = Wiki(_two_subject_repo(root, cooking_subjects="[cooking, ai-tech]"))
    focused = wiki.query("ai tech recipes agents")
    reasons = {h.path: h.match_reason for h in focused.hits if h.path.startswith("wiki/concepts/")}
    # Both maps declare ai-tech, so BOTH children stay d_moc=0 linked themes under the focus.
    assert reasons["wiki/concepts/agents.md"] == "linked-theme"
    assert reasons["wiki/concepts/recipes.md"] == "linked-theme"


def test_a_map_with_no_subjects_is_dropped_by_an_active_focus(tmp_path: Path) -> None:
    # `subjects: []` is a legal, honest value (D2.2) and it asserts nothing — so a map declaring
    # none is outside every subject scope and is not seeded once a focus is active. It still seeds
    # normally when the question focuses on nothing.
    root = tmp_path / "personal"
    wiki = Wiki(_two_subject_repo(root, cooking_subjects="[]"))
    assert (
        next(h for h in wiki.query("recipes").hits if h.path.endswith("recipes.md")).match_reason
        == "linked-theme"
    )
    focused = wiki.query("ai tech recipes")
    rec = next(h for h in focused.hits if h.path.endswith("recipes.md"))
    assert rec.match_reason != "linked-theme"


def test_a_schema_1_tree_is_readable_and_has_no_structural_seeds(tmp_path: Path) -> None:
    # ADR-0041 D5 "no shim": SUPPORTED_KB_SCHEMA_VERSIONS keeps 1, so a v1 tree must stay READABLE.
    # No v1 path is a map under the schema-2 predicate, so the corpus simply gets no level-0 seeds;
    # index.md still seeds level 1 and lexical evidence still scores. It must never crash.
    root = tmp_path / "personal"
    (root / "wiki" / "ai-tech" / "themes").mkdir(parents=True)
    (root / "index.md").write_text(
        "# personal\n\n- [AI Tech MOC](wiki/ai-tech/ai-tech-moc.md)\n", encoding="utf-8"
    )
    (root / "wiki" / "ai-tech" / "ai-tech-moc.md").write_text(
        "---\ntype: moc\nstatus: active\n---\n# AI Tech\n\n- [Agents](themes/agents.md)\n",
        encoding="utf-8",
    )
    (root / "wiki" / "ai-tech" / "themes" / "agents.md").write_text(
        "---\ntype: theme\ntags: [agents]\n---\n# Agents\n\nAgents and tooling here.\n",
        encoding="utf-8",
    )
    result = Wiki(RepoLayout(root)).query("agents tooling")
    assert result.status == "ok"
    hit = next(h for h in result.hits if h.path.endswith("themes/agents.md"))
    # Reachable purely on its own lexical evidence — never as a d_moc=0 linked theme, because the
    # v1 `<domain>-moc.md` is not a map under the schema-2 directory rule.
    assert hit.match_reason != "linked-theme"


# --- determinism, ordering, tie-break, limit ----------------------------------------------------


def test_determinism_identical_order_across_calls(tmp_path: Path) -> None:
    layout = _build_repo(tmp_path / "personal")
    wiki = Wiki(layout)
    r1 = wiki.query("curator concurrency control")
    r2 = wiki.query("curator concurrency control")
    r3 = wiki.query("curator concurrency control")
    assert r1.hits == r2.hits == r3.hits
    # explicit field-level identity (scores too)
    assert [(h.path, h.score, h.line, h.anchor) for h in r1.hits] == [
        (h.path, h.score, h.line, h.anchor) for h in r2.hits
    ]


def test_determinism_independent_of_file_creation_order(tmp_path: Path) -> None:
    # Order-permutation property: building the same corpus in a different filesystem-creation order
    # must yield identical SearchHits (catches order-dependent IDF/avgdl accumulation). The loader
    # sorts by path, so output is independent of creation order.
    a = _build_repo(tmp_path / "a")

    rootb = tmp_path / "b"
    (rootb / "wiki" / "concepts").mkdir(parents=True)
    (rootb / "wiki" / "maps").mkdir(parents=True)
    # write in reverse / scrambled order
    (rootb / ROADMAP_PATH).write_text(ROADMAP, encoding="utf-8")
    (rootb / INBOX_PATH).write_text(INBOX_DESIGN, encoding="utf-8")
    (rootb / CURATOR_PATH).write_text(CURATOR_CONCURRENCY, encoding="utf-8")
    (rootb / MOC_PATH).write_text(AI_TECH_MOC, encoding="utf-8")
    (rootb / "index.md").write_text(INDEX_MD, encoding="utf-8")
    b = RepoLayout(rootb)

    ra = Wiki(a).query("curator concurrency control")
    rb = Wiki(b).query("curator concurrency control")
    assert [(h.path, h.score, h.anchor, h.line, h.match_reason) for h in ra.hits] == [
        (h.path, h.score, h.anchor, h.line, h.match_reason) for h in rb.hits
    ]


def test_tie_break_by_path(tmp_path: Path) -> None:
    # Two notes with identical content (so identical lex/struct/score) must order by ascending
    # repo-relative POSIX path — the absolute tie-break (§7).
    root = tmp_path / "personal"
    concepts = root / "wiki" / "concepts"
    concepts.mkdir(parents=True)
    (root / "wiki" / "maps").mkdir(parents=True)
    (root / "index.md").write_text(
        "# personal\n\n- [General MOC](wiki/maps/general.md)\n", encoding="utf-8"
    )
    (root / "wiki" / "maps" / "general.md").write_text(
        "# General\n\n- [Zeta note](../concepts/zeta-note.md)\n"
        "- [Alpha note](../concepts/alpha-note.md)\n",
        encoding="utf-8",
    )
    same_body = "# Title\n\nWidget calibration tolerance specification details here.\n"
    (concepts / "alpha-note.md").write_text(
        "---\ntags: [widget]\n---\n" + same_body, encoding="utf-8"
    )
    (concepts / "zeta-note.md").write_text(
        "---\ntags: [widget]\n---\n" + same_body, encoding="utf-8"
    )
    result = Wiki(RepoLayout(root)).query("widget calibration tolerance")
    assert result.status == "ok"
    twins = [h.path for h in result.hits if h.path.endswith("-note.md")]
    # both present with equal scores → alpha sorts before zeta
    assert twins.index("wiki/concepts/alpha-note.md") < twins.index("wiki/concepts/zeta-note.md")
    scores = {h.path: h.score for h in result.hits}
    assert scores["wiki/concepts/alpha-note.md"] == scores["wiki/concepts/zeta-note.md"]


def test_limit_is_respected(tmp_path: Path) -> None:
    layout = _build_repo(tmp_path / "personal")
    wiki = Wiki(layout)
    # broad query that matches multiple notes lexically
    full = wiki.query("curator inbox roadmap concurrency", limit=20)
    assert full.status == "ok"
    limited = wiki.query("curator inbox roadmap concurrency", limit=1)
    assert len(limited.hits) == 1
    # the single returned hit is the top of the full ordering
    assert limited.hits[0].path == full.hits[0].path
    assert limited.hits[0].score == full.hits[0].score


def test_hits_are_score_descending(tmp_path: Path) -> None:
    layout = _build_repo(tmp_path / "personal")
    result = Wiki(layout).query("curator inbox concurrency append")
    assert result.status == "ok"
    scores = [h.score for h in result.hits]
    assert scores == sorted(scores, reverse=True)


def test_at_most_one_hit_per_path(tmp_path: Path) -> None:
    layout = _build_repo(tmp_path / "personal")
    result = Wiki(layout).query("curator concurrency control inbox")
    paths = [h.path for h in result.hits]
    assert len(paths) == len(set(paths))


def test_score_is_six_decimals(tmp_path: Path) -> None:
    layout = _build_repo(tmp_path / "personal")
    result = Wiki(layout).query("curator concurrency control")
    for h in result.hits:
        assert round(h.score, 6) == h.score


def test_fm_demotion_active_beats_deprecated_twin(tmp_path: Path) -> None:
    # ADR-0012 §8 Phase-1b, LIVE since the #56 addendum (FM_ENABLED=True): with identical bodies
    # (identical lex/struct), the ACTIVE twin must outrank the DEPRECATED twin by the fm gap —
    # path tie-break alone would order alpha (deprecated) first, so this proves fm differentiates.
    root = tmp_path / "personal"
    concepts = root / "wiki" / "concepts"
    concepts.mkdir(parents=True)
    (root / "wiki" / "maps").mkdir(parents=True)
    (root / "index.md").write_text(
        "# personal\n\n- [General MOC](wiki/maps/general.md)\n", encoding="utf-8"
    )
    (root / "wiki" / "maps" / "general.md").write_text(
        "# General\n\n- [Alpha note](../concepts/alpha-note.md)\n"
        "- [Zeta note](../concepts/zeta-note.md)\n",
        encoding="utf-8",
    )
    same_body = "# Title\n\nWidget calibration tolerance specification details here.\n"
    (concepts / "alpha-note.md").write_text(
        "---\nstatus: deprecated\ntags: [widget]\n---\n" + same_body, encoding="utf-8"
    )
    (concepts / "zeta-note.md").write_text(
        "---\nstatus: active\ntags: [widget]\n---\n" + same_body, encoding="utf-8"
    )
    result = Wiki(RepoLayout(root)).query("widget calibration tolerance")
    assert result.status == "ok"
    twins = [h.path for h in result.hits if h.path.endswith("-note.md")]
    assert twins.index("wiki/concepts/zeta-note.md") < twins.index("wiki/concepts/alpha-note.md")
    scores = {h.path: h.score for h in result.hits}
    gap = scores["wiki/concepts/zeta-note.md"] - scores["wiki/concepts/alpha-note.md"]
    # active +0.10 vs deprecated -0.15 on otherwise-identical components → exactly 0.25 apart.
    assert abs(gap - 0.25) < 1e-6


def test_sparse_alias_beats_single_body_mention(tmp_path: Path) -> None:
    # #56 review (FIELD_B): `aliases` is a sparse OPTIONAL field. Under plain corpus-wide avgdl
    # the BM25F length denominator explodes for the ONE note that has aliases (avgdl_aliases→0),
    # silently crushing the "title-equivalent 3.0" weight below a single passing body mention.
    # With the aliases length-normalization exemption (b=0) the alternate-title match must outrank
    # a note that merely mentions the phrase once in its body — on a mid-size corpus where the
    # collapse actually manifests (tiny fixtures keep avgdl high and cannot observe it).
    root = tmp_path / "personal"
    concepts = root / "wiki" / "concepts"
    concepts.mkdir(parents=True)
    (root / "wiki" / "maps").mkdir(parents=True)
    (root / "index.md").write_text(
        "# personal\n\n- [General MOC](wiki/maps/general.md)\n", encoding="utf-8"
    )
    moc_lines = ["# General", ""]
    for i in range(40):  # alias-free filler notes crush avgdl_aliases toward 0
        name = f"filler-{i:02d}"
        (concepts / f"{name}.md").write_text(
            f"---\nstatus: active\nsummary: filler note {i} about an unrelated topic\n---\n"
            f"# Filler {i}\n\nUnrelated prose about topic number {i} and nothing else.\n",
            encoding="utf-8",
        )
        moc_lines.append(f"- [Filler {i}](../concepts/{name}.md)")
    (concepts / "shared-store.md").write_text(  # the query phrase appears ONLY in its aliases:
        "---\nstatus: active\naliases: [memory hub]\n"
        "summary: a shared persistence layer for agents\n---\n"
        "# Shared Store\n\nAgents persist knowledge in a shared store across sessions.\n",
        encoding="utf-8",
    )
    moc_lines.append("- [Shared Store](../concepts/shared-store.md)")
    (concepts / "session-notes.md").write_text(  # competitor: one passing body mention
        "---\nstatus: active\nsummary: notes about session behavior\n---\n"
        "# Session Notes\n\nSessions sometimes write to the memory hub in passing.\n",
        encoding="utf-8",
    )
    moc_lines.append("- [Session Notes](../concepts/session-notes.md)")
    (root / "wiki" / "maps" / "general.md").write_text(
        "\n".join(moc_lines) + "\n", encoding="utf-8"
    )
    result = Wiki(RepoLayout(root)).query("memory hub")
    assert result.status == "ok"
    ranked = [h.path for h in result.hits]
    alias_note = "wiki/concepts/shared-store.md"
    body_note = "wiki/concepts/session-notes.md"
    assert alias_note in ranked and body_note in ranked
    assert ranked.index(alias_note) < ranked.index(body_note)


def test_searchhit_is_frozen_and_forbids_extra() -> None:
    hit = SearchHit(
        repo="personal",
        path="index.md",
        anchor="",
        line=1,
        excerpt="x",
        match_reason="lexical",
        score=0.5,
    )
    # frozen
    import pydantic

    try:
        hit.score = 0.6  # type: ignore[misc]
    except pydantic.ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("SearchHit should be frozen")


# --- the schema-2 map predicate (ADR-0041 D5) ---------------------------------------------------


def test_is_map_path_reads_the_directory_not_the_filename() -> None:
    """A note is a map iff it sits at ``wiki/maps/…`` with at least three segments (ADR-0041 D5).

    The ``-moc`` filename suffix is gone entirely — the kind marker moved into the directory — and
    a map may sit at any depth under ``wiki/maps/`` (D1.1 free sub-folders). ``index.md`` is NOT a
    map: it is the ROOT OF the map tier and keeps its own kind (D1.2), which is what preserves its
    ``d_moc = 1`` seeding level verbatim.
    """
    assert _is_map_path("wiki/maps/eng.md")
    assert _is_map_path("wiki/maps/team/eng.md")  # free sub-folder, still a map
    assert not _is_map_path("index.md")  # the root map seeds at level 1, not level 0
    assert not _is_map_path("wiki/concepts/eng.md")
    assert not _is_map_path("wiki/maps.md")  # no kind directory at all
    # And the v1 shape is no longer a map, which IS the flip.
    assert not _is_map_path("wiki/eng/eng-moc.md")


def test_path_kind_maps_every_schema_2_directory() -> None:
    kinds = {
        "index.md": "index",
        "wiki/concepts/a.md": "concept",
        "wiki/summaries/a.md": "summary",
        "wiki/notes/2026/01/2026-01-15.md": "note",
        "wiki/maps/a.md": "map",
        "wiki/entities/a.md": "entity",
        "wiki/people/hando/a.md": "person",  # read-first-class, never composed (D3.3)
    }
    assert {p: _path_kind(p) for p in kinds} == kinds
    # A v1 path declares no kind through the schema-2 directory rule (D5, "no shim").
    assert _path_kind("wiki/eng/themes/a.md") is None


def test_the_pre_stratum_predicate_name_is_the_same_object() -> None:
    """``core.gold`` seeds ``d_moc`` from this predicate — "one shared edit, not two" (D5).

    The old name is kept as an alias to the SAME function object so the two seeding sites can never
    drift apart: whichever name an importer holds, it holds the schema-2 rule.
    """
    assert _is_moc_path is _is_map_path


# --- the `[[basename]]` identity space (ADR-0041 D3.3) ------------------------------------------


def test_a_colliding_people_note_changes_neither_indegree_nor_score(tmp_path: Path) -> None:
    """``wiki/people/**`` is OUTSIDE the basename identity space (ADR-0041 D3.3).

    The collision is LEGAL by construction: D3.3 excludes people notes from lint's L1-1 duplicate
    rule precisely so a human's filename can never veto a curator basename. The oracle therefore
    has to resolve the map's link to the CURATED note regardless — otherwise the human note steals
    the concept's in-degree, inherits the map's ``d_moc = 0`` seed, and is returned as a
    ``linked-theme`` hit asserting a map link that does not exist, while the concept's frozen
    ADR-0012 score silently degrades.

    The control is the SAME BYTES under a non-colliding filename. That isolates the basename: both
    corpora hold the same tokens, so every BM25F statistic (IDF, per-field avgdl) is identical and
    the ONLY thing that could move the concept's score is the resolver. Comparing against the
    people-free corpus instead would be a weaker test that any added note fails.
    """
    control = _build_repo(tmp_path / "control")
    _write_people_note(control, "private-log")
    colliding = _build_repo(tmp_path / "colliding")
    _write_people_note(colliding, "curator-concurrency")

    a = Wiki(control).query("curator concurrency control")
    b = Wiki(colliding).query("curator concurrency control")
    hit_a = next(h for h in a.hits if h.path == CURATOR_PATH)
    hit_b = next(h for h in b.hits if h.path == CURATOR_PATH)
    assert hit_b.score == hit_a.score
    assert hit_b.match_reason == hit_a.match_reason == "linked-theme"
    assert b.hits[0].path == CURATOR_PATH

    # The people note never inherits the map's seed, so it is never a structural hit — which is
    # D3.3's sentence in its observable form: a link into `people/` does not resolve.
    assert all(
        h.match_reason != "linked-theme" for h in b.hits if h.path.startswith("wiki/people/")
    )


def _write_people_note(layout: RepoLayout, basename: str) -> None:
    """Plant one human-owned note whose body shares the curated concept's vocabulary."""
    people = layout.root / "wiki" / "people" / "hando"
    people.mkdir(parents=True, exist_ok=True)
    (people / f"{basename}.md").write_text(
        "---\nstatus: active\n---\n# My own curator concurrency notes\n\n"
        "Private thoughts about the per-repo flock and the compare-and-swap.\n",
        encoding="utf-8",
    )


PEOPLE_LOG_PATH = "wiki/people/hando/private-log.md"


def _people_log_body(*, link: bool) -> str:
    """The human note's bytes, with the pointer as a real markdown LINK or as plain prose."""
    pointer = (
        "- [Curator concurrency](../../concepts/curator-concurrency.md)"
        if link
        else "- Curator concurrency"
    )
    return f"---\nstatus: active\n---\n{pointer}\n"


def _write_people_log_note(layout: RepoLayout, *, link: bool) -> None:
    people = layout.root / "wiki" / "people" / "hando"
    people.mkdir(parents=True, exist_ok=True)
    (people / "private-log.md").write_text(_people_log_body(link=link), encoding="utf-8")


def test_a_people_note_casts_no_vote_in_the_link_graph(tmp_path: Path) -> None:
    """The D3.3 exclusion is SYMMETRIC: people are neither link TARGETS nor link SOURCES.

    ``by_basename`` keeps a human-owned note from being pointed AT; ``_compute_indegrees`` keeps it
    from POINTING. Without the second half an ungraded file under ``wiki/people/`` still raises a
    curated note's in-degree and moves the frozen ADR-0012 ``struct`` term — and the oracle then
    disagrees with :mod:`core.gold` (which drops people from the population BEFORE
    ``_compute_centrality``) and with the faces' ``health()``/``graph()`` (which skip people as
    reference sources): three answers to one question about which links exist, one of them gating a
    curator run.

    The control is the SAME VISIBLE TEXT with the link punctuation removed. Body tokenization
    strips a markdown link to its label, so both corpora hold identical tokens in every field —
    every BM25F statistic (IDF, per-field avgdl) is identical, and the ONLY thing that could move a
    score is whether the human note's link counted as an EDGE. The whole ordered hit list is
    compared, not just the link's target: in-degree feeds ``indeg_norm``'s DENOMINATOR too, so a
    stray vote moves the other notes' structural term even where the target's own is already 1.0.
    """
    # Non-vacuity: the two bodies really do differ by exactly one resolved outlink, so the test
    # cannot pass on a link form the parser never saw in the first place.
    linked = _parse_note(PEOPLE_LOG_PATH, "private-log", False, _people_log_body(link=True))
    plain = _parse_note(PEOPLE_LOG_PATH, "private-log", False, _people_log_body(link=False))
    assert linked.outlinks == ("curator-concurrency",)
    assert plain.outlinks == ()

    linking = _build_repo(tmp_path / "linking")
    _write_people_log_note(linking, link=True)
    control = _build_repo(tmp_path / "control")
    _write_people_log_note(control, link=False)

    a = Wiki(control).query("curator concurrency control")
    b = Wiki(linking).query("curator concurrency control")
    assert [(h.path, h.score, h.match_reason) for h in b.hits] == [
        (h.path, h.score, h.match_reason) for h in a.hits
    ]


def test_people_notes_stay_readable_on_their_own_lexical_merits(tmp_path: Path) -> None:
    """The exclusion is from the IDENTITY SPACE, not from the corpus (D3.3: read is first class)."""
    layout = _build_repo(tmp_path / "personal")
    people = layout.root / "wiki" / "people" / "hando"
    people.mkdir(parents=True)
    (people / "sourdough.md").write_text(
        "---\nstatus: active\n---\n# Sourdough starter\n\n"
        "My sourdough starter hydration log and feeding schedule.\n",
        encoding="utf-8",
    )
    result = Wiki(layout).query("sourdough starter hydration")
    assert [h.path for h in result.hits] == ["wiki/people/hando/sourdough.md"]


def test_subjects_reads_exactly_what_the_schema_note_parser_reads(tmp_path: Path) -> None:
    """ADR-0041 D3.2 leaves exactly ONE place a subject is recorded — so one reading of it.

    ``core.wiki`` feeds the stage-1 subject focus and ``schema.notes`` feeds the browse/graph
    facet. A looser reader here would put a map in one subject scope for RANKING and another (or
    none) for the FACET: the same map focusing a query while showing as "Unfiled" on ``/``.
    """
    from agora_kb.schema.notes import parse_all_notes

    layout = _build_repo(tmp_path / "personal")
    (layout.root / "_meta").mkdir(parents=True, exist_ok=True)
    (layout.root / "_meta" / "taxonomy.yaml").write_text(
        "schema_version: 2\ndomains: [ai-tech]\nallowed_tags: []\n", encoding="utf-8"
    )
    shapes = {
        "wiki/maps/list.md": "subjects: [finance, cooking]",
        "wiki/maps/scalar.md": "subjects: finance",  # a bare scalar is NOT the declared shape
        "wiki/maps/typed.md": "subjects: [2026-01-02]",  # an unquoted YAML date is not a str
        "wiki/maps/absent.md": "",
    }
    for rel, line in shapes.items():
        (layout.root / rel).write_text(
            f"---\nstatus: active\nkind: map\n{line}\n---\n# {rel}\n\nbody\n",
            encoding="utf-8",
        )

    oracle = {n.path: n.subjects for n in Wiki(layout)._load_notes(None)}
    facet = {n.rel_path: n.subjects for n in parse_all_notes(layout)}
    for rel in shapes:
        assert oracle[rel] == facet[rel], rel
    assert oracle["wiki/maps/list.md"] == ("finance", "cooking")
    assert oracle["wiki/maps/scalar.md"] == ()
    assert oracle["wiki/maps/typed.md"] == ()
    assert oracle["wiki/maps/absent.md"] == ()
