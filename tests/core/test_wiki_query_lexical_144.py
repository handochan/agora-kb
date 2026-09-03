"""The write-path seam: ``Wiki.query_lexical`` (issue #144, ADR-0012 §0a).

``Wiki.query`` is not a read-only concern — the curator's WRITE path called it once per candidate
to build the ``related/`` view that decides MERGE targets, so any future ranking tier on the read
face would silently change what the curator merges (and a mis-merge is permanent: the closed
ADR-0011 op vocabulary has no DELETE). ``query_lexical`` is the model-free oracle extracted as its
own public name so the write path can be pinned to it forever.

These tests pin the two halves of that contract:

* **equivalence today** — ``query_lexical`` is byte-for-byte what ``query`` computes now, so the
  extraction changed no behaviour;
* **independence** — ``query_lexical`` does not route through ``query``, so a future tier added to
  ``query`` cannot reach the write path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.core.layout import RepoLayout
from agora_kb.core.wiki import MAX_HITS, QueryResult, Wiki
from tests.core.test_wiki import _build_repo

# Questions spanning the interesting statuses: a multi-hit `ok`, a single-hit `ok`, an
# all-stopword question and a question with no evidence at all (both `not_found`).
QUESTIONS = (
    "curator concurrency compare and swap",
    "inbox append-only per-writer",
    "the and of",
    "",
    "quantum chromodynamics tractor",
)


@pytest.fixture()
def layout(tmp_path: Path) -> RepoLayout:
    return _build_repo(tmp_path / "personal")


# --- equivalence: the extraction is behaviour-preserving -----------------------------------------
@pytest.mark.parametrize("question", QUESTIONS)
def test_query_lexical_equals_query_today(layout: RepoLayout, question: str) -> None:
    """Today the read face IS the lexical oracle — same object, same JSON bytes."""
    wiki = Wiki(layout)
    assert wiki.query_lexical(question) == wiki.query(question)
    assert wiki.query_lexical(question).model_dump_json() == wiki.query(question).model_dump_json()


def test_query_lexical_honours_limit(layout: RepoLayout) -> None:
    """``limit`` is keyword-only and caps hits exactly as ``query``'s does."""
    question = "curator concurrency inbox append-only roadmap"
    wiki = Wiki(layout)
    full = wiki.query_lexical(question, limit=MAX_HITS)
    assert full.status == "ok"
    assert len(full.hits) > 1  # otherwise the cap below proves nothing

    capped = wiki.query_lexical(question, limit=1)
    assert capped.hits == full.hits[:1]
    assert wiki.query_lexical(question, limit=0).hits == ()


def test_query_lexical_default_limit_matches_query(layout: RepoLayout) -> None:
    question = "curator concurrency"
    wiki = Wiki(layout)
    assert wiki.query_lexical(question) == wiki.query_lexical(question, limit=MAX_HITS)


def test_query_lexical_is_deterministic_across_instances(layout: RepoLayout) -> None:
    """A fresh :class:`Wiki` over the same bytes produces the same result — no instance state."""
    question = "curator concurrency compare and swap"
    first = Wiki(layout).query_lexical(question)
    second = Wiki(layout).query_lexical(question)
    assert first.model_dump_json() == second.model_dump_json()


# --- independence: the oracle never routes through the read face --------------------------------
def test_query_lexical_does_not_call_query(
    layout: RepoLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a future model tier landing on ``query``: the oracle must not notice.

    If ``query_lexical`` ever delegates upward, this raises — which is exactly the regression the
    curator write path must never suffer (#144).
    """

    def exploded(self: Wiki, *args: object, **kwargs: object) -> None:
        raise AssertionError("query_lexical must not route through the read face's query()")

    monkeypatch.setattr(Wiki, "query", exploded)
    result = Wiki(layout).query_lexical("curator concurrency compare and swap")
    assert result.status == "ok"
    assert result.hits


def test_query_still_delegates_to_the_lexical_oracle(
    layout: RepoLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read face has no implementation of its own today — it delegates DOWN to the oracle.

    The direction is the point (integrity review of #144): ``query_lexical`` holds the body, so the
    only place a future ranking tier can be written is ``query``'s own body — which is where both
    docstrings say it belongs. If the two were ever re-plumbed through a shared private helper,
    "add the tier to query" would most naturally mean editing that helper, silently re-coupling the
    write path.
    """
    question = "inbox append-only per-writer"
    sentinel = Wiki(layout).query_lexical(question, limit=1)
    seen: list[object] = []

    def spy(self: Wiki, q: str, *, limit: int = MAX_HITS) -> QueryResult:
        seen.append((q, limit))
        return sentinel

    monkeypatch.setattr(Wiki, "query_lexical", spy)
    assert Wiki(layout).query(question) is sentinel
    assert seen == [(question, MAX_HITS)]


def test_query_lexical_docstring_states_the_no_model_pin() -> None:
    """The 'never a model tier' promise is the whole point of the seam; keep it written down."""
    doc = Wiki.query_lexical.__doc__ or ""
    assert "NEVER" in doc
    assert "ADR-0012 §0a" in doc
    assert "#144" in doc
