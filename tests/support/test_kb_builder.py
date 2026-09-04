"""The fixture builder is only useful if the repo it builds is one the real system accepts.

So these tests hold it to the production gates rather than to a private notion of "valid": the
corpus is linted by the REAL L1 linter (:func:`agora_kb.schema.lint.lint`, the same code path the
curator runs at ADR-0011 §4.4 and the dashboard reuses verbatim) and opened by the REAL read path
(:class:`agora_kb.core.wiki.Wiki`). A fixture that only satisfied a hand-rolled checker could pin
ranking behaviour over a repo the curator would reject, which would make gate B meaningless.

Both layouts are held to the same gate. ``schema_version=1`` is the ADR-0010 v1 layout the gate-B
golden was recorded over; ``schema_version=2`` is the ADR-0041 kind-first layout, and it is linted
by the SAME :func:`lint` call — which dispatches on the ``_meta/taxonomy.yaml`` the builder emitted,
so nothing here tells the linter which ruleset to use and a fixture that emitted the wrong
``schema_version`` could not pass by accident.

Three properties beyond "it lints" are pinned here, because they are what makes a ranking delta
attributable to the flip:

* the two layouts carry the SAME note basenames apart from two documented renames (the map's
  ``-moc`` suffix and D2.6's journal merge) — asserted as a mapping, not asserted away;
* building either layout twice produces byte-identical files (no wall clock, no ordering wobble);
* the v1 tree the builder writes is byte-identical to the pre-Stratum builder's, pinned by a digest
  over the notes and evidence it authors.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from agora_kb.config import MAX_SUPPORTED_KB_SCHEMA_VERSION
from agora_kb.core.frontmatter import parse
from agora_kb.core.layout import RepoLayout
from agora_kb.core.rank_snapshot import QuerySpec, load_queries
from agora_kb.core.wiki import Wiki
from agora_kb.schema.lint import lint
from tests.rank_golden.corpus import CORPUS, DOMAINS
from tests.support.kb_builder import FIXTURE_KB_ID, NoteSpec, build_kb, v2_basename

QUERIES_YAML = Path(__file__).resolve().parents[1] / "rank_golden" / "queries.yaml"


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> RepoLayout:
    root = tmp_path_factory.mktemp("rank-golden") / "personal"
    build_kb(root, CORPUS, schema_version=1, domains=DOMAINS)
    return RepoLayout(root)


@pytest.fixture(scope="module")
def built_v2(tmp_path_factory: pytest.TempPathFactory) -> RepoLayout:
    """The SAME corpus content, materialized under the ADR-0041 kind-first layout."""
    root = tmp_path_factory.mktemp("rank-golden-v2") / "personal"
    build_kb(root, CORPUS, schema_version=2, domains=DOMAINS)
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
    a = build_kb(tmp_path / "a", CORPUS, schema_version=1, domains=DOMAINS)
    b = build_kb(tmp_path / "b", CORPUS, schema_version=1, domains=DOMAINS)
    rel_a = sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())
    rel_b = sorted(p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file())
    assert rel_a == rel_b
    for rel in rel_a:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel


#: Paths the BUILDER authors, as opposed to the ones ``emit_schema`` authors. The v1 digest below
#: covers exactly these: ``AGENTS.md`` (+ its symlinks), ``_meta/`` and ``_templates/`` come out of
#: the production schema emitter, so folding them in would make this test fail for a schema-doc
#: change that has nothing to do with the builder — and pass for nothing extra, since
#: ``test_corpus_lints_clean_with_zero_errors`` already runs L1-17 over them.
_BUILDER_AUTHORED = ("index.md", "wiki/", "raw/")

#: Digest of the v1 tree the builder writes for :data:`CORPUS`. This is the byte-identity claim
#: wave W2.1 makes: schema-1 repos keep exactly today's bytes. If it goes red, either the v1 layout
#: moved (a bug — the golden was recorded over it) or something the builder DERIVES from production
#: code moved (then re-record here, and re-record ``tests/rank_golden/golden_v1*.json`` if the
#: goldens move with it).
#:
#: RE-RECORDED ONCE, deliberately, for ADR-0041 D4.4 (the pathsafe slugger swap). The pre-swap
#: value was ``19a60061c19fd8eb798227a320288f17fdee2a8b1b3facfa341502335c38fbc6``. ``build_kb``
#: derives a husk's basename through the PRODUCTION slugger on purpose (see ``kb_builder.slugify``
#: — "if the production slugger ever changes, this fixture changes with it"), so widening the slug
#: charset renames exactly the three Korean-titled husks: ``note-<sha8>`` → ``미수금-정산-메모``
#: and friends. Nothing about the v1 LAYOUT moved — the same files in the same directories with the
#: same bytes inside them — which is why this is a re-record rather than a bug. The gate-B goldens
#: (``tests/rank_golden``) were re-run and are UNAFFECTED: the husks are husks, so they surface in
#: no probe's hits.
_V1_TREE_DIGEST = "f5653abb85287e18e7ab21a62c0ae285a1f8868e4ad692305c38bb814c277678"


def _tree_digest(root: Path) -> str:
    """SHA-256 over the builder-authored files: sorted ``<rel>\\0<sha256(bytes)>\\0`` records."""
    parts = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if not any(rel == p or rel.startswith(p) for p in _BUILDER_AUTHORED):
            continue
        parts.append(f"{rel}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\0")
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def test_the_v1_tree_is_byte_identical_to_the_pre_stratum_builder(tmp_path: Path) -> None:
    """W2.1 is ADDITIVE: adding the schema-2 layout must not move a single v1 byte.

    A digest rather than a re-run of the old builder, because the old builder is gone from the tree
    — this is the record of what it produced, and the only honest way to reset it is to establish
    that the v1 layout was *meant* to move.
    """
    root = build_kb(tmp_path / "v1", CORPUS, schema_version=1, domains=DOMAINS)
    assert _tree_digest(root) == _V1_TREE_DIGEST


# --- the schema-2 (ADR-0041) layout ------------------------------------------------------------


def test_the_v2_corpus_lints_clean_with_zero_errors(built_v2: RepoLayout) -> None:
    """The REAL linter, on the REAL schema-2 ruleset, must find nothing at all.

    No ``schema_version=`` is passed: :func:`lint` reads it from the ``_meta/taxonomy.yaml`` the
    builder emitted (ADR-0010 §5.1), so this also asserts the builder declared the schema it built.
    A v1 tree graded by this ruleset is a pile of L1-22 findings, so passing is not vacuous.
    """
    result = lint(built_v2)
    errors = [f for f in result.findings if f.severity == "error"]
    assert errors == [], errors
    assert result.findings == (), result.findings
    assert result.ok


def test_the_v2_layout_is_kind_first(built_v2: RepoLayout) -> None:
    """Segment 1 under ``wiki/`` is the KIND, and no subject survives in a path (D1/D3.2)."""
    root = built_v2.root
    assert (root / "wiki" / "concepts" / "unbilled-receivables-recognition.md").is_file()
    assert (root / "wiki" / "maps" / "finance.md").is_file()
    assert (root / "wiki" / "notes" / "2026" / "01" / "2026-01-12.md").is_file()
    assert built_v2.index_file.is_file()
    # The v1 subject directories are gone — that is the flip, not a side effect of it.
    for domain in DOMAINS:
        assert not (root / "wiki" / domain).exists()
    assert not (root / "wiki" / "maps" / "finance-moc.md").exists()
    # `raw/` never moves (D1.4): the sources: chain of every note stays resolvable verbatim.
    assert (root / "raw" / "finance" / "unbilled-receivables-recognition.md").is_file()


def test_the_empty_kind_containers_ship_empty(built_v2: RepoLayout) -> None:
    """``summaries/``/``entities/`` exist with no population (OD-7/OD-8); so does ``people/``.

    The container is the schema and the population is not — shipping the directory now is what
    stops the tier needing a second migration when its contract lands.
    """
    for kind in ("summaries", "entities", "people"):
        directory = built_v2.root / "wiki" / kind
        assert directory.is_dir(), kind
        assert list(directory.iterdir()) == [], kind


def test_the_two_layouts_carry_the_same_basenames_modulo_two_renames(
    built: RepoLayout, built_v2: RepoLayout
) -> None:
    """The corpus survives the flip intact — and the exceptions are enumerated, not waved at.

    Basenames are the identity the gate-B golden is keyed on (``tests/rank_golden``), precisely so
    the record survives a change that renames every path. Two renames are unavoidable and both are
    ADR decisions rather than fixture choices: the map loses its ``-moc`` suffix to the directory
    (D5) and the per-domain dailies merge into one journal per ``run_date`` (D2.6).
    """

    def basenames(layout: RepoLayout) -> set[str]:
        found = {p.stem for p in (layout.root / "wiki").rglob("*.md")}
        return found | {layout.index_file.stem}

    v1, v2 = basenames(built), basenames(built_v2)

    renamed_from = {s.basename() for s in CORPUS if s.kind in ("moc", "daily")}
    renamed_from |= {f"{d}-moc" for d in DOMAINS}
    renamed_to = {v2_basename(s) for s in CORPUS if s.kind in ("moc", "daily")}
    renamed_to |= set(DOMAINS)

    assert v1 - renamed_from == v2 - renamed_to, "a basename moved that no ADR renames"
    assert renamed_from <= v1
    assert renamed_to <= v2
    # And the renames really are the two the ADR names, not a third one hiding in the set algebra.
    assert v1 - v2 == {f"{d}-moc" for d in DOMAINS} | {
        s.basename() for s in CORPUS if s.kind == "daily"
    }
    assert v2 - v1 == set(DOMAINS) | {v2_basename(s) for s in CORPUS if s.kind == "daily"}


def test_v2_frontmatter_carries_the_adr_0041_common_base(built_v2: RepoLayout) -> None:
    """D2: ``kind`` mirrors the directory, the subject is FRONTMATTER, ``kb:`` names the repo."""
    fm, _body = parse(
        (built_v2.root / "wiki" / "concepts" / "unbilled-receivables-recognition.md").read_text(
            encoding="utf-8"
        )
    )
    assert fm["kind"] == "concept"
    assert fm["type"] == "concept", "OD-3: type: is emitted as a derived OKF mirror of kind"
    assert fm["kb"] == FIXTURE_KB_ID
    assert fm["subjects"] == ["finance"]
    assert fm["derived"] is False
    assert fm["provenance"] == {"writers": [], "agents": []}
    assert fm["status"] == "active"
    assert fm["sources"] == ["raw/finance/unbilled-receivables-recognition.md"]

    index_fm, _ = parse(built_v2.index_file.read_text(encoding="utf-8"))
    assert index_fm["kind"] == "index"
    assert index_fm["subjects"] == [], "the root map is filed under no subject (D2.2)"
    assert index_fm["children"] == [f"[[{d}]]" for d in DOMAINS]

    map_fm, _ = parse((built_v2.root / "wiki" / "maps" / "finance.md").read_text(encoding="utf-8"))
    assert map_fm["kind"] == "map"
    assert map_fm["subjects"] == ["finance"], "a map's subject scope is its own frontmatter (D5)"


def test_the_kb_identity_file_is_written_through_the_production_writer(
    built_v2: RepoLayout,
) -> None:
    """``_meta/kb.yaml`` is the ADR-0041 D1.5 closed key set — and policy is not in it."""
    doc = yaml.safe_load(built_v2.kb_meta_file.read_text(encoding="utf-8"))
    assert set(doc) == {"kb_id", "name", "declared_kind"}
    assert doc["kb_id"] == FIXTURE_KB_ID
    assert "kind" not in doc and "harvest" not in doc


def test_two_v2_builds_are_byte_identical(tmp_path: Path) -> None:
    """Including ``_meta/kb.yaml``: the fixture ``kb_id``/name are FIXED, never minted."""
    a = build_kb(tmp_path / "a", CORPUS, schema_version=2, domains=DOMAINS)
    b = build_kb(tmp_path / "b", CORPUS, schema_version=2, domains=DOMAINS)
    rel_a = sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())
    rel_b = sorted(p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file())
    assert rel_a == rel_b
    assert "_meta/kb.yaml" in rel_a
    for rel in rel_a:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel


def test_same_dated_dailies_merge_into_one_journal(tmp_path: Path) -> None:
    """D2.6 / D6 step 4: one journal per ``run_date``, repo-wide, sections in domain order.

    The committed corpus dates its three dailies differently, so the merge is only ever exercised
    by a fixture built for it — which is why this one exists rather than being asserted about
    ``CORPUS``.
    """
    date = "2026-01-20"
    specs = [
        NoteSpec(kind="theme", domain="alpha", title="Alpha one", body="A.", slug="alpha-one"),
        NoteSpec(kind="theme", domain="beta", title="Beta one", body="B.", slug="beta-one"),
        NoteSpec(
            kind="daily",
            domain="beta",
            title="beta daily",
            slug=f"beta-{date}",
            summary="beta run.",
            tags=["shared"],
            body="Beta consolidated two captures.",
            extra_frontmatter={"date": date},
        ),
        NoteSpec(
            kind="daily",
            domain="alpha",
            title="alpha daily",
            slug=f"alpha-{date}",
            summary="alpha run.",
            tags=["shared", "alpha-only"],
            body="Alpha consolidated one capture.",
            extra_frontmatter={"date": date},
        ),
    ]
    root = build_kb(tmp_path / "merged", specs, schema_version=2, domains=["alpha", "beta"])
    journals = sorted((root / "wiki" / "notes").rglob("*.md"))
    assert [p.relative_to(root).as_posix() for p in journals] == [f"wiki/notes/2026/01/{date}.md"]
    fm, body = parse(journals[0].read_text(encoding="utf-8"))
    assert fm["kind"] == "note"
    assert fm["date"] == date
    assert fm["subjects"] == ["alpha", "beta"], "subjects union, in domain order"
    assert fm["tags"] == ["shared", "alpha-only"], "tags union, first-seen order"
    assert fm["summary"] == "alpha run. beta run."
    # DOMAIN order, not spec order: the beta daily was listed first and still comes second.
    assert body.index("## alpha daily") < body.index("## beta daily")
    assert "Alpha consolidated one capture." in body
    assert "Beta consolidated two captures." in body
    assert lint(RepoLayout(root)).ok


def test_summary_entity_and_person_specs_land_in_their_kind_trees(tmp_path: Path) -> None:
    """The three kinds with no v1 antecedent, and the one lint refuses to grade (D3.3)."""
    specs = [
        NoteSpec(kind="theme", domain="alpha", title="Alpha one", body="A.", slug="alpha-one"),
        NoteSpec(
            kind="summary",
            domain="alpha",
            title="Alpha long form",
            body="A long pass over alpha.",
            slug="alpha-long-form",
        ),
        NoteSpec(
            kind="entity",
            domain="alpha",
            title="Acme",
            body="A registered entity.",
            slug="acme",
            status="stub",
        ),
        NoteSpec(
            kind="person",
            domain="alpha",
            person="hando",
            title="Reading notes",
            body="Whatever a human wants, however they want it.",
            slug="reading-notes",
        ),
    ]
    root = build_kb(tmp_path / "kinds", specs, schema_version=2, domains=["alpha"])
    assert (root / "wiki" / "summaries" / "alpha-long-form.md").is_file()
    assert (root / "wiki" / "entities" / "acme.md").is_file()
    assert (root / "wiki" / "people" / "hando" / "reading-notes.md").is_file()
    # A summary is an admitted map child (D1.3); an entity is NOT, so it stays an orphan by design.
    map_fm, map_body = parse((root / "wiki" / "maps" / "alpha.md").read_text(encoding="utf-8"))
    assert map_fm["children"] == ["[[alpha-one]]", "[[alpha-long-form]]"]
    assert "acme" not in map_body
    assert lint(RepoLayout(root)).ok


def test_a_broken_people_note_does_not_fail_the_lint(tmp_path: Path) -> None:
    """``wiki/people/**`` is permanently ungraded (D3.3) — asserted through the REAL linter."""
    specs = [
        NoteSpec(kind="theme", domain="alpha", title="Alpha one", body="A.", slug="alpha-one"),
        NoteSpec(
            kind="person",
            domain="alpha",
            person="hando",
            title="Half-written",
            body="A link to [[nothing-at-all]] and a CRLF habit.",
            slug="half-written",
        ),
    ]
    root = build_kb(tmp_path / "people", specs, schema_version=2, domains=["alpha"])
    broken = root / "wiki" / "people" / "hando" / "half-written.md"
    broken.write_bytes(b"\xef\xbb\xbfnot even frontmatter\r\n")  # BOM + CRLF + unparseable
    result = lint(RepoLayout(root))
    assert result.ok, result.findings
    assert result.findings == ()


# --- the builder refuses what the layout cannot hold ------------------------------------------


def test_schema_2_only_kinds_are_rejected_by_a_v1_build(tmp_path: Path) -> None:
    """A ``summary`` under schema 1 has no directory — a loud refusal, not a silent misfile."""
    specs = [NoteSpec(kind="summary", domain="finance", title="S", body="B.", slug="s")]
    with pytest.raises(ValueError, match="only in KB wiki schema 2"):
        build_kb(tmp_path / "v1-summary", specs, schema_version=1)


def test_kb_identity_arguments_are_rejected_by_a_v1_build(tmp_path: Path) -> None:
    """``_meta/kb.yaml`` does not exist in v1: a silently-ignored kwarg is a fixture that lies."""
    with pytest.raises(ValueError, match="schema-2 only"):
        build_kb(tmp_path / "v1-kb", CORPUS, schema_version=1, domains=DOMAINS, kb_name="other")


def test_a_reserved_underscore_domain_is_a_build_error(tmp_path: Path) -> None:
    """L1-23's namespace reservation, enforced at build time so the fixture fails at its cause."""
    specs = [NoteSpec(kind="theme", domain="_blob", title="X", body="B.", slug="x")]
    with pytest.raises(ValueError, match="RESERVED"):
        build_kb(tmp_path / "reserved", specs, schema_version=2, domains=["_blob"])


def test_an_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown schema_version"):
        build_kb(tmp_path / "v3", CORPUS, schema_version=3, domains=DOMAINS)


def test_the_default_schema_version_is_the_one_this_build_writes(tmp_path: Path) -> None:
    """A fixture that does not say which layout it means gets the one production produces.

    Pinned against :data:`agora_kb.config.MAX_SUPPORTED_KB_SCHEMA_VERSION` rather than the literal
    ``2`` so the next flip moves the default and this test together, and asserted on the TREE (a
    ``wiki/concepts/`` directory exists, no ``wiki/finance/themes/`` does) rather than on the
    signature, because the default only matters through the bytes it lays down.
    """
    root = build_kb(tmp_path / "default", _minimal(), domains=["finance"])
    declared = yaml.safe_load((root / "_meta" / "taxonomy.yaml").read_text(encoding="utf-8"))
    assert declared["schema_version"] == MAX_SUPPORTED_KB_SCHEMA_VERSION == 2
    assert (root / "wiki" / "concepts").is_dir()
    assert not (root / "wiki" / "finance").exists()


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


def test_map_child_that_is_not_an_admitted_child_of_that_domain_is_a_build_error(
    tmp_path: Path,
) -> None:
    """The DEFAULT (schema-2) refusal: D1.3 admits concept/summary/map children and nothing else."""
    specs = [
        *_minimal(),
        NoteSpec(kind="moc", domain="finance", title="finance MOC", body="", children=["nope"]),
    ]
    with pytest.raises(ValueError, match="not an admitted child"):
        build_kb(tmp_path / "map", specs)


def test_moc_child_that_is_not_a_theme_of_that_domain_is_a_build_error(tmp_path: Path) -> None:
    """The same refusal under the v1 layout, which words it in the v1 vocabulary ("theme")."""
    specs = [
        *_minimal(),
        NoteSpec(kind="moc", domain="finance", title="finance MOC", body="", children=["nope"]),
    ]
    with pytest.raises(ValueError, match="not a theme"):
        build_kb(tmp_path / "moc", specs, schema_version=1)


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
    # #57 husks. Their basenames are DERIVED (no `slug=`), so this also asserts what the production
    # slugger does with a purely-Korean title. Since ADR-0041 D4.4 that is: NAME IT IN KOREAN. It
    # used to be "decline, and hand over to the note-<sha8> floor", and the inversion is the point
    # of the swap — the floor still exists (a seed with no admissible character at all reaches it,
    # `tests/test_ollama_brain.py`), it is simply no longer where non-Latin knowledge lands.
    husks = [s for s in themes if s.slug is None]
    assert len(husks) == 3
    assert all(not s.basename().startswith("note-") for s in husks), [s.basename() for s in husks]
    assert all(any("가" <= ch <= "힣" for ch in s.basename()) for s in husks), [
        s.basename() for s in husks
    ]
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
