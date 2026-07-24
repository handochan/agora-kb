"""Tests for the deterministic, model-free read path (core.wiki, ADR-0012).

The fixture corpus mirrors ADR-0012 §10 (the `personal` repo: index.md, an ai-tech MOC linking two
themes via the ADR-0014 D3 standard-markdown BODY links `[Title](relative.md)`, two theme notes with
frontmatter + headings, and a deprecated roadmap note). The read path seeds the `linked-theme` graph
from those markdown body links (path→basename), so retrieval-as-navigation works on a produced repo.
The
SCORING FORMULA is normative; the exact float numbers are not byte-pinned cross-machine, so we
assert ordering, field correctness, the lexical-evidence gate, the not_found floor, determinism,
tie-break, and limit — not magic score constants.
"""

from __future__ import annotations

from pathlib import Path

from agora_kb.core.layout import RepoLayout
from agora_kb.core.wiki import QueryResult, SearchHit, Wiki

# --- fixture corpus (ADR-0012 §10) --------------------------------------------------------------

# ADR-0014 D3: the produced MOC/index BODY child bullets are STANDARD MARKDOWN LINKS
# `[Title](relative.md)`. The read path seeds the `linked-theme` graph from these markdown links
# (path→basename) exactly as it did from `[[ ]]`, and the link TEXT supplies the MOC-label tokens.
INDEX_MD = """\
# personal

- [AI Tech MOC](wiki/ai-tech/ai-tech-moc.md)
"""

AI_TECH_MOC = """\
---
status: active
---
# AI Tech

- [Curator concurrency](themes/curator-concurrency.md) — single-writer curator serializes writes
- [Inbox design](themes/inbox-design.md) — append-only per-writer inbox
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


def _build_repo(root: Path) -> RepoLayout:
    """Materialize the §10 fixture corpus under ``root`` and return its layout."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(INDEX_MD, encoding="utf-8")
    ai = root / "wiki" / "ai-tech"
    themes = ai / "themes"
    themes.mkdir(parents=True)
    (ai / "ai-tech-moc.md").write_text(AI_TECH_MOC, encoding="utf-8")
    (themes / "curator-concurrency.md").write_text(CURATOR_CONCURRENCY, encoding="utf-8")
    (themes / "inbox-design.md").write_text(INBOX_DESIGN, encoding="utf-8")
    personal = root / "wiki" / "personal"
    personal.mkdir(parents=True)
    (personal / "roadmap.md").write_text(ROADMAP, encoding="utf-8")
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
    assert top.path == "wiki/ai-tech/themes/curator-concurrency.md"
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
    moc_body = (layout.root / "wiki" / "ai-tech" / "ai-tech-moc.md").read_text(encoding="utf-8")
    assert "](themes/curator-concurrency.md)" in moc_body  # the produced D3 body-link form
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
    assert "wiki/ai-tech/themes/inbox-design.md" not in paths


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
    wikidir = root / "wiki" / "general"
    wikidir.mkdir(parents=True)
    (root / "index.md").write_text(
        "# personal\n\n- [General MOC](wiki/general/general-moc.md)\n", encoding="utf-8"
    )
    (root / "wiki" / "general" / "general-moc.md").write_text(
        "# General\n\n- [Note A](themes/note-a.md)\n- [Design note](themes/design-note.md)\n",
        encoding="utf-8",
    )
    # note-a: high-idf "zephyrumquux" in the BODY under a low-idf "Design notes" heading.
    (wikidir / "note-a.md").write_text(
        "# Note A\n\n## Design notes\nThe term zephyrumquux is unique here.\n", encoding="utf-8"
    )
    # design-note drives down idf("design") so it is the WEAK term.
    (wikidir / "design-note.md").write_text(
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
    wikidir = root / "wiki" / "general"
    wikidir.mkdir(parents=True)
    # index has no links and does not match the query (so it is not an eligible hit either).
    (root / "index.md").write_text("# personal\n", encoding="utf-8")
    filler = " ".join(f"fillerword{i}" for i in range(120))
    for i in range(12):
        (wikidir / f"doc{i}.md").write_text(
            f"# Doc {i}\n\n{filler} commonword more filler text here.\n", encoding="utf-8"
        )
    result = Wiki(RepoLayout(root)).query("commonword")
    assert result.status == "not_found"
    assert result.hits == ()


def test_hyphenated_domain_focus_restricts_moc_seeding(tmp_path: Path) -> None:
    # ADR-0012 §4: a question carrying a <domain> kebab name seeds only that domain's MOC. The
    # hyphenated domain "ai-tech" tokenizes to {ai, tech}; a query containing both tokens focuses
    # seeding on ai-tech, so a note in the OTHER (cooking) domain is no longer a d_moc=0 child of
    # its MOC and therefore cannot be 'linked-theme' (it falls back to 'heading'/'lexical').
    root = tmp_path / "personal"
    (root / "wiki" / "ai-tech").mkdir(parents=True)
    (root / "wiki" / "cooking").mkdir(parents=True)
    (root / "index.md").write_text(
        "# personal\n\n- [AI Tech MOC](wiki/ai-tech/ai-tech-moc.md)\n"
        "- [Cooking MOC](wiki/cooking/cooking-moc.md)\n",
        encoding="utf-8",
    )
    (root / "wiki" / "ai-tech" / "ai-tech-moc.md").write_text(
        "# AI Tech\n\n- [Agents](themes/agents.md)\n", encoding="utf-8"
    )
    (root / "wiki" / "ai-tech" / "agents.md").write_text(
        "---\ntags: [agents]\n---\n# Agents\n\nAgents and tooling.\n", encoding="utf-8"
    )
    (root / "wiki" / "cooking" / "cooking-moc.md").write_text(
        "# Cooking\n\n- [Recipes](themes/recipes.md)\n", encoding="utf-8"
    )
    (root / "wiki" / "cooking" / "recipes.md").write_text(
        "---\ntags: [recipes]\n---\n# Recipes\n\nRecipes about cooking.\n", encoding="utf-8"
    )
    wiki = Wiki(RepoLayout(root))

    # No focus: "recipes" alone -> recipes is a d_moc=0 child of cooking-moc -> linked-theme.
    unfocused = wiki.query("recipes")
    rec_unfocused = next(h for h in unfocused.hits if h.path.endswith("recipes.md"))
    assert rec_unfocused.match_reason == "linked-theme"

    # Focused on ai-tech (both {ai, tech} present): cooking-moc is NOT seeded, so recipes is not a
    # d_moc=0 child and cannot be linked-theme.
    focused = wiki.query("ai tech recipes")
    rec_focused = next(h for h in focused.hits if h.path.endswith("recipes.md"))
    assert rec_focused.match_reason != "linked-theme"


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
    themes = rootb / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True)
    personal = rootb / "wiki" / "personal"
    personal.mkdir(parents=True)
    # write in reverse / scrambled order
    (personal / "roadmap.md").write_text(ROADMAP, encoding="utf-8")
    (themes / "inbox-design.md").write_text(INBOX_DESIGN, encoding="utf-8")
    (themes / "curator-concurrency.md").write_text(CURATOR_CONCURRENCY, encoding="utf-8")
    (rootb / "wiki" / "ai-tech" / "ai-tech-moc.md").write_text(AI_TECH_MOC, encoding="utf-8")
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
    wikidir = root / "wiki" / "general"
    wikidir.mkdir(parents=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(
        "# personal\n\n- [General MOC](wiki/general/general-moc.md)\n", encoding="utf-8"
    )
    (root / "wiki" / "general" / "general-moc.md").write_text(
        "# General\n\n- [Zeta note](themes/zeta-note.md)\n- [Alpha note](themes/alpha-note.md)\n",
        encoding="utf-8",
    )
    same_body = "# Title\n\nWidget calibration tolerance specification details here.\n"
    (wikidir / "alpha-note.md").write_text(
        "---\ntags: [widget]\n---\n" + same_body, encoding="utf-8"
    )
    (wikidir / "zeta-note.md").write_text(
        "---\ntags: [widget]\n---\n" + same_body, encoding="utf-8"
    )
    result = Wiki(RepoLayout(root)).query("widget calibration tolerance")
    assert result.status == "ok"
    twins = [h.path for h in result.hits if h.path.endswith("-note.md")]
    # both present with equal scores → alpha sorts before zeta
    assert twins.index("wiki/general/alpha-note.md") < twins.index("wiki/general/zeta-note.md")
    scores = {h.path: h.score for h in result.hits}
    assert scores["wiki/general/alpha-note.md"] == scores["wiki/general/zeta-note.md"]


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
    wikidir = root / "wiki" / "general"
    wikidir.mkdir(parents=True)
    (root / "index.md").write_text(
        "# personal\n\n- [General MOC](wiki/general/general-moc.md)\n", encoding="utf-8"
    )
    (root / "wiki" / "general" / "general-moc.md").write_text(
        "# General\n\n- [Alpha note](themes/alpha-note.md)\n- [Zeta note](themes/zeta-note.md)\n",
        encoding="utf-8",
    )
    same_body = "# Title\n\nWidget calibration tolerance specification details here.\n"
    (wikidir / "alpha-note.md").write_text(
        "---\nstatus: deprecated\ntags: [widget]\n---\n" + same_body, encoding="utf-8"
    )
    (wikidir / "zeta-note.md").write_text(
        "---\nstatus: active\ntags: [widget]\n---\n" + same_body, encoding="utf-8"
    )
    result = Wiki(RepoLayout(root)).query("widget calibration tolerance")
    assert result.status == "ok"
    twins = [h.path for h in result.hits if h.path.endswith("-note.md")]
    assert twins.index("wiki/general/zeta-note.md") < twins.index("wiki/general/alpha-note.md")
    scores = {h.path: h.score for h in result.hits}
    gap = scores["wiki/general/zeta-note.md"] - scores["wiki/general/alpha-note.md"]
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
    themes = root / "wiki" / "general" / "themes"
    themes.mkdir(parents=True)
    (root / "index.md").write_text(
        "# personal\n\n- [General MOC](wiki/general/general-moc.md)\n", encoding="utf-8"
    )
    moc_lines = ["# General", ""]
    for i in range(40):  # alias-free filler notes crush avgdl_aliases toward 0
        name = f"filler-{i:02d}"
        (themes / f"{name}.md").write_text(
            f"---\nstatus: active\nsummary: filler note {i} about an unrelated topic\n---\n"
            f"# Filler {i}\n\nUnrelated prose about topic number {i} and nothing else.\n",
            encoding="utf-8",
        )
        moc_lines.append(f"- [Filler {i}](themes/{name}.md)")
    (themes / "shared-store.md").write_text(  # the query phrase appears ONLY in its aliases:
        "---\nstatus: active\naliases: [memory hub]\n"
        "summary: a shared persistence layer for agents\n---\n"
        "# Shared Store\n\nAgents persist knowledge in a shared store across sessions.\n",
        encoding="utf-8",
    )
    moc_lines.append("- [Shared Store](themes/shared-store.md)")
    (themes / "session-notes.md").write_text(  # competitor: one passing body mention
        "---\nstatus: active\nsummary: notes about session behavior\n---\n"
        "# Session Notes\n\nSessions sometimes write to the memory hub in passing.\n",
        encoding="utf-8",
    )
    moc_lines.append("- [Session Notes](themes/session-notes.md)")
    (root / "wiki" / "general" / "general-moc.md").write_text(
        "\n".join(moc_lines) + "\n", encoding="utf-8"
    )
    result = Wiki(RepoLayout(root)).query("memory hub")
    assert result.status == "ok"
    ranked = [h.path for h in result.hits]
    alias_note = "wiki/general/themes/shared-store.md"
    body_note = "wiki/general/themes/session-notes.md"
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
