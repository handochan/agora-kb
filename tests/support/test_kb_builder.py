"""The fixture builder is only useful if the repo it builds is one the real system accepts.

So these tests hold it to the production gates rather than to a private notion of "valid": the
corpus is linted by the REAL L1 linter (:func:`agora_kb.schema.lint.lint`, the same code path the
curator runs at ADR-0011 §4.4 and the dashboard reuses verbatim) and opened by the REAL read path
(:class:`agora_kb.core.wiki.Wiki`). A fixture that only satisfied a hand-rolled checker could pin
ranking behaviour over a repo the curator would reject, which would make gate B meaningless.

They also pin the two properties the Stratum layout flip depends on: ``schema_version=2`` is a
loud NotImplementedError (UNIT 2's seam, not a silent v1 fallback), and building the same corpus
twice produces byte-identical files (no wall clock, no ordering wobble).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agora_kb.core.layout import RepoLayout
from agora_kb.core.rank_snapshot import QuerySpec, load_queries
from agora_kb.core.wiki import Wiki
from agora_kb.schema.lint import lint
from tests.rank_golden.corpus import CORPUS, DOMAINS
from tests.support.kb_builder import NoteSpec, build_kb

QUERIES_YAML = Path(__file__).resolve().parents[1] / "rank_golden" / "queries.yaml"


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> RepoLayout:
    root = tmp_path_factory.mktemp("rank-golden") / "personal"
    build_kb(root, CORPUS, domains=DOMAINS)
    return RepoLayout(root)


# --- the production gates -------------------------------------------------------------------


def test_corpus_lints_clean_with_zero_errors(built: RepoLayout) -> None:
    """The REAL L1 linter, dashboard-style (no ``run_date``), must find nothing at all.

    No ``run_date`` is passed on purpose: the run-relative half of L1-12/L1-14 compares every date
    against a single injected "today", which a corpus carrying three differently-dated dailies
    cannot satisfy and does not need to — those checks belong to a curator RUN, not to a fixture.
    """
    result = lint(built)
    errors = [f for f in result.findings if f.severity == "error"]
    assert errors == [], errors
    assert result.findings == (), result.findings
    assert result.ok


def test_wiki_opens_the_corpus(built: RepoLayout) -> None:
    """The read path parses every note and answers a query over them."""
    wiki = Wiki(built)
    notes = wiki.list_notes()
    # 39 themes + 3 dailies written from specs, plus the 3 MOCs and the index the builder generates.
    assert len(notes) == 46
    kinds = {n.type for n in notes}
    assert kinds == {"index", "moc", "theme", "daily"}
    result = wiki.query("unbilled receivables")
    assert result.status == "ok"
    assert result.hits


def test_every_domain_has_a_moc_and_the_index_lists_them_all(built: RepoLayout) -> None:
    """The v1 layout shape ``core.wiki._is_moc_path`` keys the whole structural score off."""
    for domain in DOMAINS:
        assert (built.root / "wiki" / domain / f"{domain}-moc.md").is_file()
    index_body = built.index_file.read_text(encoding="utf-8")
    for domain in DOMAINS:
        assert f"wiki/{domain}/{domain}-moc.md" in index_body


def test_non_stub_theme_sources_resolve_to_real_raw_artifacts(built: RepoLayout) -> None:
    """L1-7/L1-8 are enforced by lint above; this pins WHERE the evidence lands (``raw/``)."""
    raw_files = sorted(p.name for p in (built.root / "raw").rglob("*.md"))
    assert raw_files
    assert (built.root / "raw" / "finance" / "unbilled-receivables-recognition.md").is_file()


# --- determinism + the UNIT 2 seam ------------------------------------------------------------


def test_two_builds_are_byte_identical(tmp_path: Path) -> None:
    """No wall clock, no ordering wobble — a golden recorded twice must record the same bytes."""
    a = build_kb(tmp_path / "a", CORPUS, domains=DOMAINS)
    b = build_kb(tmp_path / "b", CORPUS, domains=DOMAINS)
    rel_a = sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())
    rel_b = sorted(p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file())
    assert rel_a == rel_b
    for rel in rel_a:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel


def test_stratum_layout_is_not_implemented_yet(tmp_path: Path) -> None:
    """``schema_version=2`` is UNIT 2's seam: a loud refusal, never a silent v1 build."""
    with pytest.raises(NotImplementedError, match="Stratum layout lands in UNIT 2"):
        build_kb(tmp_path / "v2", CORPUS, schema_version=2, domains=DOMAINS)
    assert not (tmp_path / "v2").exists()


# --- the builder fails loudly rather than emitting a repo that would fail lint ------------------


def _minimal(**overrides: object) -> list[NoteSpec]:
    base = NoteSpec(kind="theme", domain="finance", title="One", body="Body.", slug="one")
    other = NoteSpec(kind="theme", domain="finance", title="Two", body="Body.", slug="two")
    for key, value in overrides.items():
        setattr(other, key, value)
    return [base, other]


def test_duplicate_basename_is_a_build_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate basename"):
        build_kb(tmp_path / "dup", _minimal(slug="one"))


def test_alias_colliding_with_a_basename_is_a_build_error(tmp_path: Path) -> None:
    """L1-15 requires basenames ∪ aliases to be globally unique."""
    with pytest.raises(ValueError, match="collides"):
        build_kb(tmp_path / "alias", _minimal(aliases=["one"]))


def test_moc_child_that_is_not_a_theme_of_that_domain_is_a_build_error(tmp_path: Path) -> None:
    specs = [
        *_minimal(),
        NoteSpec(kind="moc", domain="finance", title="finance MOC", body="", children=["nope"]),
    ]
    with pytest.raises(ValueError, match="not a theme"):
        build_kb(tmp_path / "moc", specs)


# --- queries.yaml is the other half of the fixture, so it is checked against the corpus --------


def test_queries_yaml_matches_the_corpus() -> None:
    """Ids unique, shape complete, and every positive names a basename the corpus really has."""
    entries = yaml.safe_load(QUERIES_YAML.read_text(encoding="utf-8"))
    assert isinstance(entries, list)

    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids)), "duplicate query id"
    assert [i for i in ids if i.startswith("q146-")] == ["q146-1", "q146-2", "q146-3", "q146-4"]
    assert len([i for i in ids if i.startswith("p")]) == 30
    assert len([i for i in ids if i.startswith("n")]) == 10

    required = {"id", "question", "expect", "note", "rationale"}
    # A SUBSET check, not equality: `observed_rank` is a first-class `QuerySpec` field and the
    # documented way to record a positive whose expected note is not rank 1. An exact-key assertion
    # would turn the only sanctioned way to be honest about a ranking gap into a red test, and the
    # pressure at that moment is to tweak the corpus until the note comes first instead.
    optional = {"observed_rank", "tags"}
    basenames = {s.basename() for s in CORPUS if s.kind in ("theme", "daily")}
    basenames |= {f"{d}-moc" for d in DOMAINS} | {"index"}
    for entry in entries:
        keys = set(entry)
        assert required <= keys <= (required | optional), entry["id"]
        assert entry["question"].strip(), entry["id"]
        assert entry["expect"] in ("ok", "not_found"), entry["id"]
        assert entry["rationale"].strip(), entry["id"]
        if "observed_rank" in entry:
            assert entry["expect"] == "ok", entry["id"]
            assert isinstance(entry["observed_rank"], int), entry["id"]
            assert entry["observed_rank"] >= 1, entry["id"]
        if entry["expect"] == "ok":
            assert entry["note"] in basenames, entry["id"]
        else:
            assert entry["note"] is None, entry["id"]


def test_a_declared_observed_rank_survives_the_loader_and_the_query_file() -> None:
    """The ``observed_rank`` escape hatch is exercised end-to-end, not merely documented.

    Two claims. (1) The production loader accepts the key: ``QuerySpec`` declares it and
    ``load_queries`` is ``extra="forbid"``, so a typo'd variant would raise here. (2) The committed
    query file actually USES it — a mechanism that is only ever tested on a synthetic entry is a
    mechanism nobody has run, which is how a documented procedure turns out to be impossible the
    first time someone needs it (``test_kb_builder``'s own key assertion used to make it so).
    """
    specs = load_queries(QUERIES_YAML)
    declared = [s for s in specs if s.observed_rank is not None]
    assert declared, "no query declares observed_rank; the known-gap mechanism is untested"
    assert all(s.expect == "ok" and s.note for s in declared)

    synthetic = yaml.safe_load(
        "- id: synthetic\n  question: q\n  expect: ok\n  note: n\n  observed_rank: 3\n"
    )
    assert QuerySpec.model_validate(synthetic[0]).observed_rank == 3


def test_queries_yaml_carries_the_required_korean_and_verbatim_probes() -> None:
    """#56 coverage (Korean + mixed script) and the four #146 queries, verbatim."""
    entries = yaml.safe_load(QUERIES_YAML.read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in entries}

    assert by_id["q146-1"]["question"] == "unbilled receivables"
    assert by_id["q146-2"]["question"] == "investing index funds"
    assert by_id["q146-3"]["question"] == "cooking finance investing"
    assert by_id["q146-4"]["question"] == "finance segment reporting thresholds"

    def has_hangul(text: str) -> bool:
        return any("가" <= ch <= "힣" for ch in text)

    def has_ascii_word(text: str) -> bool:
        return any(("a" <= ch.lower() <= "z") for ch in text)

    positives = [e for e in entries if e["id"].startswith("p")]
    negatives = [e for e in entries if e["id"].startswith("n")]
    korean_positives = [e for e in positives if has_hangul(e["question"])]
    assert len(korean_positives) >= 6
    assert len([e for e in korean_positives if has_ascii_word(e["question"])]) >= 2
    assert len([e for e in negatives if has_hangul(e["question"])]) >= 2


def test_corpus_carries_the_shapes_the_golden_is_meant_to_cover() -> None:
    """The corpus inventory is an assertion: lose a shape and the golden stops covering it."""
    themes = [s for s in CORPUS if s.kind == "theme"]
    assert len(CORPUS) == 46
    assert len([s for s in CORPUS if s.kind == "daily"]) == 3
    # #57 husks. Their basenames are DERIVED (no `slug=`), so this also asserts that the production
    # slugger really does decline a purely-Korean title and hand over to the hash fallback.
    husks = [s for s in themes if s.slug is None]
    assert len(husks) == 3
    assert all(s.basename().startswith("note-") for s in husks), [s.basename() for s in husks]
    assert len([s for s in themes if s.status == "stub"]) >= 6
    assert len([s for s in themes if s.status == "contested"]) == 1
    # Both halves of the ADR-0012 §8 table. `deprecated` (−0.15) is the ONLY negative term in the
    # ranker; without a note carrying it, removing the demotion changes nothing anywhere.
    assert len([s for s in themes if s.status == "deprecated"]) >= 2
    assert len([s for s in themes if s.aliases]) == 3
    # The #146 path: a MOC bullet whose LABEL shares no token with the note it points at is what
    # makes `_passes_gate`'s d_moc==0 branch the sole reason a candidate is admitted (see p25).
    labelled = [s for s in themes if s.moc_label]
    assert len(labelled) == 1
    (husk,) = labelled
    own = f"{husk.title} {husk.summary} {' '.join(husk.tags)} {husk.body}".lower()
    assert not {w for w in husk.moc_label.lower().split()} & set(own.split())

    def has_hangul(text: str) -> bool:
        return any("가" <= ch <= "힣" for ch in text)

    korean = [s for s in themes if has_hangul(s.title) and has_hangul(s.body)]
    assert len(korean) >= 6

    # The engineering MOC catalogues stubs and nothing else — the stubs-only-domain shape.
    eng_moc = next(s for s in CORPUS if s.kind == "moc" and s.domain == "engineering")
    by_base = {s.basename(): s for s in themes}
    assert eng_moc.children
    assert all(by_base[c].status == "stub" for c in eng_moc.children)

    # Orphans: themes no MOC lists (their only reachability is lexical).
    listed = {c for s in CORPUS if s.kind == "moc" for c in s.children}
    orphans = set(by_base) - listed
    assert "bond-ladder-basics" in orphans
    assert "coffee-extraction-yield" in orphans
    # The two floor probes (p28/p29) both need an ORPHAN carrying the −0.15 demotion: a MOC-linked
    # note takes 0.35 * 0.7 ≈ 0.245 from structure alone and can never land near FLOOR = 0.18.
    assert "polling-interval-sizing-deprecated" in orphans
    assert "clock-skew-drift-stub" in orphans
