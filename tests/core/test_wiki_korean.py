"""Korean/CJK retrieval tests (issue #56, ADR-0012 addendum).

Before #56 the §3 tokenizer's ``[a-z0-9]+`` alphabet silently dropped every CJK codepoint: a
Korean question tokenized to ``[]`` (instant ``not_found``) and a Korean note's field_tokens were
all empty (``lex = 0`` forever). The addendum adds NFC normalization + character bigrams over CJK
runs (unigram for a length-1 run), plus the ``aliases`` (3.0) / ``summary`` (2.0) scoring fields.

The corpus here is Korean-heavy on purpose (ADR-0027 decision 5 documents the owner's KB as
Korean-heavy): Korean titles/bodies under English slugs (the ADR-0022-addendum filename posture),
an English-titled note reachable ONLY via its Korean alias, and a summary-only evidence probe.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from agora_kb.core.layout import RepoLayout
from agora_kb.core.wiki import Wiki, _scan_tokens, _tokenize, _tokenize_tags

# --- tokenizer units (the #56 addendum contract) -------------------------------------------------


def test_tokenize_hangul_run_bigrams() -> None:
    assert _tokenize("큐레이터") == ["큐레", "레이", "이터"]


def test_tokenize_particle_variant_overlaps_stem() -> None:
    # 조사 변형: `큐레이터가` vs `큐레이터` share 3 of 4 bigrams — the partial-match principle.
    with_particle = set(_tokenize("큐레이터가"))
    stem = set(_tokenize("큐레이터"))
    assert stem <= with_particle
    assert with_particle - stem == {"터가"}


def test_tokenize_single_char_run_is_unigram() -> None:
    assert _tokenize("밤") == ["밤"]


def test_tokenize_mixed_script_splits_ascii_and_cjk() -> None:
    assert _tokenize("AI에이전트") == ["ai", "에이", "이전", "전트"]


def test_tokenize_cjk_punctuation_splits_runs() -> None:
    # U+3002 (。) is INSIDE the shared CJK ranges but is category Po → it splits the run, so no
    # bigram bridges the sentence boundary.
    toks = _tokenize("메모리。그림")
    assert toks == ["메모", "모리", "그림"]
    assert "리그" not in toks and "리。" not in toks


def test_tokenize_nfc_normalizes_decomposed_hangul() -> None:
    nfd = unicodedata.normalize("NFD", "큐레이터")
    assert nfd != "큐레이터"  # genuinely decomposed jamo
    assert _tokenize(nfd) == _tokenize("큐레이터")


def test_tokenize_ascii_rule_unchanged() -> None:
    # The English/digit tokenization is byte-invariant vs the original §3 rule.
    assert _tokenize("The Curator acquires curator.lock 42 times") == [
        "curator",
        "acquires",
        "curator",
        "lock",
        "42",
        "times",
    ]
    assert _scan_tokens("what is the") == ["what", "is", "the"]  # unfiltered scan keeps stopwords
    assert _tokenize("what is the") == []  # the stopword filter still applies


def test_tokenize_tags_kebab_rule_unchanged_and_korean_tags_visible() -> None:
    assert _tokenize_tags(("single-writer", "inbox")) == [
        "single-writer",
        "single",
        "writer",
        "inbox",
    ]
    assert _tokenize_tags(("큐레이터",)) == ["큐레", "레이", "이터"]


# --- Korean fixture corpus -----------------------------------------------------------------------

KO_INDEX_MD = """\
# personal

- [에이전트 기술 MOC](wiki/ai-tech/ai-tech-moc.md)
"""

KO_MOC = """\
---
status: active
---
# 에이전트 기술

- [큐레이터 동시성](themes/curator-concurrency.md) — 단일 작성자 큐레이터는 쓰기를 직렬화한다
- [Memory Hub](themes/memory-hub.md) — 크로스 세션 지식 공유
- [AI 에이전트](themes/ai-agent.md) — 도구를 호출하는 에이전트
"""

KO_CURATOR = """\
---
status: active
tags: [concurrency]
summary: 큐레이터는 저장소 잠금으로 쓰기를 직렬화한다
---
# 큐레이터 동시성

큐레이터는 저장소 잠금을 획득하여 정확히 하나의 작성자만 브랜치를 전진시킨다.

## 충돌 처리
브랜치 참조에 대한 비교 교환으로 동시성 제어를 강제한다.
"""

# English title/body; the KOREAN alias is the only Korean token source in this note, so the
# alias field alone must carry the 메모리/허브 match (weight 3.0).
KO_MEMORY_HUB = """\
---
status: active
aliases: [메모리 허브]
tags: [memory]
summary: 세션 간 지식을 보존하는 공유 저장 계층
---
# Memory Hub

Agents share one memory hub for cross-session knowledge.
"""

KO_AI_AGENT = """\
---
status: active
---
# AI 에이전트

AI 에이전트는 도구를 호출하여 작업을 수행한다.
"""

# Two pure-Korean H2s in ONE note: both slugs are "" under the ASCII _slug rule, and the anchor
# for a heading hit must stay the documented "" — never a fabricated dedup suffix ("-1").
KO_DEPLOY = """\
---
status: active
---
# 배포 파이프라인

배포는 단계적으로 진행된다.

## 충돌 처리
충돌 시 병합 전략을 적용한다.

## 재시도 정책
실패한 단계는 지수 백오프로 다시 실행한다.
"""


def _build_ko_repo(root: Path) -> RepoLayout:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(KO_INDEX_MD, encoding="utf-8")
    themes = root / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True)
    (root / "wiki" / "ai-tech" / "ai-tech-moc.md").write_text(KO_MOC, encoding="utf-8")
    (themes / "curator-concurrency.md").write_text(KO_CURATOR, encoding="utf-8")
    (themes / "memory-hub.md").write_text(KO_MEMORY_HUB, encoding="utf-8")
    (themes / "ai-agent.md").write_text(KO_AI_AGENT, encoding="utf-8")
    (themes / "deploy-pipeline.md").write_text(KO_DEPLOY, encoding="utf-8")
    return RepoLayout(root)


# --- Korean probes -------------------------------------------------------------------------------


def test_korean_query_finds_korean_note(tmp_path: Path) -> None:
    wiki = Wiki(_build_ko_repo(tmp_path / "personal"))
    result = wiki.query("큐레이터 동시성")
    assert result.status == "ok"
    top = result.hits[0]
    assert top.path == "wiki/ai-tech/themes/curator-concurrency.md"
    # d_moc==0 child whose Korean title tokens intersect the query → linked-theme.
    assert top.match_reason == "linked-theme"


def test_mixed_script_query(tmp_path: Path) -> None:
    # `AI에이전트` (no space) must reach the note titled `AI 에이전트` — the ASCII run and the
    # CJK bigrams tokenize identically with or without the space.
    wiki = Wiki(_build_ko_repo(tmp_path / "personal"))
    result = wiki.query("AI에이전트")
    assert result.status == "ok"
    assert any(h.path.endswith("ai-agent.md") for h in result.hits)


def test_particle_variation_reaches_stem(tmp_path: Path) -> None:
    # Query with the subject particle (`큐레이터가`) reaches the note whose fields carry only the
    # bare stem `큐레이터` (or the DIFFERENT particle form `큐레이터는`) — 3 of 4 query bigrams
    # overlap, and the 4th (`터가`) exists nowhere in the corpus, so PARTIAL bigram overlap alone
    # must carry the match (an exact-surface-form matcher would fail here).
    for fixture in (KO_INDEX_MD, KO_MOC, KO_CURATOR, KO_MEMORY_HUB, KO_AI_AGENT, KO_DEPLOY):
        assert "큐레이터가" not in fixture  # guard: keep the probe unconfounded (#56 review)
    wiki = Wiki(_build_ko_repo(tmp_path / "personal"))
    result = wiki.query("큐레이터가")
    assert result.status == "ok"
    assert any(h.path.endswith("curator-concurrency.md") for h in result.hits)


def test_korean_alias_reaches_english_slug_note(tmp_path: Path) -> None:
    # `메모리 허브` occurs ONLY in memory-hub.md's aliases: — the alias field (weight 3.0) must
    # carry the match, and as a d_moc==0 linked theme the alias counts as an alternate title.
    # NOTE (#56 review): the CURATOR write path cannot produce this shape — #57's normalize_plan
    # slugifies aliases and SKIPS un-slugifiable (pure-Korean) ones — so Korean-alias retrieval
    # is a defensive capability for hand-edited / externally-generated repos (ADR-0012 addendum
    # A2); revisiting the #57 skip now that Korean aliases have search value is a follow-up.
    wiki = Wiki(_build_ko_repo(tmp_path / "personal"))
    result = wiki.query("메모리 허브")
    assert result.status == "ok"
    top = result.hits[0]
    assert top.path == "wiki/ai-tech/themes/memory-hub.md"
    assert top.match_reason == "linked-theme"


def test_summary_field_is_scored(tmp_path: Path) -> None:
    # `보존` occurs ONLY in memory-hub.md's summary: — frontmatter, stripped before body
    # tokenization, so pre-#56 this evidence was invisible.
    wiki = Wiki(_build_ko_repo(tmp_path / "personal"))
    result = wiki.query("지식 보존")
    assert result.status == "ok"
    assert any(h.path.endswith("memory-hub.md") for h in result.hits)


def test_korean_heading_hit_anchor_is_empty_not_fabricated(tmp_path: Path) -> None:
    # A pure-Korean heading has no ASCII-derivable slug (_slug → ""). The hit's anchor must be
    # the DOCUMENTED "" no-deep-link value — never a fabricated dedup suffix: before the #56
    # review fix, the SECOND empty-slug heading in a note was assigned slug "-1", which shipped
    # through SearchHit into MCP citations and web hrefs yet resolves nowhere under any slugger.
    wiki = Wiki(_build_ko_repo(tmp_path / "personal"))
    result = wiki.query("재시도 정책")
    assert result.status == "ok"
    top = result.hits[0]
    assert top.path == "wiki/ai-tech/themes/deploy-pipeline.md"
    assert top.match_reason == "heading"
    assert top.anchor == ""  # NOT "-1" — `## 재시도 정책` is the note's 2nd empty-slug heading
    assert top.line == 8  # the `## 재시도 정책` line


def test_korean_negative_probe_is_not_found(tmp_path: Path) -> None:
    # Honesty (§5): a Korean question with zero corpus evidence MUST be not_found, never a
    # structural-only hit.
    wiki = Wiki(_build_ko_repo(tmp_path / "personal"))
    result = wiki.query("양자 생물학 광합성")
    assert result.status == "not_found"
    assert result.hits == ()


def test_korean_determinism_across_calls(tmp_path: Path) -> None:
    wiki = Wiki(_build_ko_repo(tmp_path / "personal"))
    r1 = wiki.query("큐레이터 동시성")
    r2 = wiki.query("큐레이터 동시성")
    assert r1.hits == r2.hits
    assert [(h.path, h.score, h.anchor, h.line) for h in r1.hits] == [
        (h.path, h.score, h.anchor, h.line) for h in r2.hits
    ]
