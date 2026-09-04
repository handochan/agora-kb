"""Tests for the KB wiki schema 1 → 2 converter (``agora import --from-kb`` / ADR-0041 D6).

The converter is the ONE crossing between the two schemas, so the bar these tests hold it to is not
"it produced something that lints" but **"it produced the same knowledge base the native schema-2
writer would have produced from the same content"**. That is what
:func:`test_the_conversion_and_the_native_build_agree_on_ranking` asserts, against the committed
``tests/rank_golden/golden_v2*.json`` record: the converted repo and the natively-built one are
indistinguishable to the ranker, field for field, in both ADR-0012 §8 frontmatter modes.

Everything else here is a rule of D6 checked on its own terms — the kind directories (rule 1), the
path domain becoming ``subjects:`` (rule 2), the ``-moc`` map rename and its link rewrites (rule 3),
the same-date journal merge (rule 4), ``raw/`` copied byte-identically with ``sources:`` untouched
(rule 5), the minted ``kb_id`` (rule 6), and the hard, NAMED collision failure (rule 7) — plus the
contract the converter inherits from ``agora import``: the source is never modified.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agora_kb.config import load_kb_identity, read_canonical_kb_schema_version
from agora_kb.core import frontmatter
from agora_kb.core.layout import RepoLayout
from agora_kb.core.rank_snapshot import diff_snapshots, load_queries, snapshot
from agora_kb.core.wiki import Wiki
from agora_kb.ingest.kb_convert import KbConvertError, convert_kb
from agora_kb.schema.lint import lint
from tests.rank_golden import regen
from tests.rank_golden.corpus import CORPUS, DOMAINS
from tests.support.kb_builder import FIXTURE_KB_ID, NoteSpec, build_kb, v2_basename

_IMPORT_DATE = "2026-01-15"
_VERSION_SENTINEL = "<any>"


def _fm(repo: Path, rel_path: str) -> dict:
    """Parse one destination note's frontmatter (asserting the file is there)."""
    path = repo / rel_path
    assert path.is_file(), f"missing {rel_path}; have {sorted(_note_paths(repo))}"
    parsed, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
    return parsed


def _body(repo: Path, rel_path: str) -> str:
    _parsed, body = frontmatter.parse((repo / rel_path).read_text(encoding="utf-8"))
    return body


def _note_paths(repo: Path) -> set[str]:
    """Every wiki note path in ``repo``, POSIX-relative (the schema doc is not a note)."""
    paths = {p.relative_to(repo).as_posix() for p in repo.rglob("wiki/**/*.md") if p.is_file()}
    if (repo / "index.md").is_file():
        paths.add("index.md")
    return paths


def _tree_digest(root: Path) -> str:
    """A content digest of every non-``.git`` file under ``root`` — the never-modified check."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts or not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@pytest.fixture(scope="module")
def golden_v1(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The rank-golden corpus materialised in the SCHEMA-1 layout — the converter's input.

    The same ``corpus.py`` content the schema-2 golden is built from, which is what makes the
    snapshot comparison below a statement about the CONVERSION rather than about two corpora.
    """
    root = tmp_path_factory.mktemp("from-kb") / regen.repo_name(1)
    build_kb(root, CORPUS, schema_version=1, domains=DOMAINS)
    return root


@pytest.fixture(scope="module")
def converted(golden_v1: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``golden_v1`` converted to schema 2, in a directory named like the golden record's repo.

    ``Wiki.repo`` is the layout root's DIRECTORY NAME and lands in the snapshot header, so the
    destination has to be named ``regen.repo_name(2)`` for the record to be comparable at all —
    the same reason ``tests.rank_golden.test_golden`` builds under that name. ``kb_id`` is pinned to
    the fixture id because a minted ULID is time-seeded and would make the repo unreproducible.
    """
    dest = tmp_path_factory.mktemp("from-kb-out") / regen.repo_name(2)
    convert_kb(
        golden_v1,
        dest,
        import_date=_IMPORT_DATE,
        kb_id=FIXTURE_KB_ID,
        name="agora-fixture",
    )
    return dest


# --- the acceptance bar: conversion and native build agree ---------------------------------------


@pytest.mark.parametrize(
    ("fm", "golden_path"),
    [
        pytest.param(True, regen.GOLDEN_V2_FM_ON, id="fm-on"),
        pytest.param(False, regen.GOLDEN_V2_FM_OFF, id="fm-off"),
    ],
)
def test_the_conversion_and_the_native_build_agree_on_ranking(
    converted: Path, fm: bool, golden_path: Path
) -> None:
    """A snapshot over the CONVERTED repo equals the committed schema-2 golden, field for field.

    This is the proof the whole unit exists for. ``golden_v2*.json`` was recorded over a repo
    ``tests.support.kb_builder`` materialised NATIVELY in the kind-first layout; this record is
    taken over a repo the converter produced from the SAME content in the v1 layout. Their equality
    says the two paths into schema 2 — write it, or convert into it — land on one knowledge base,
    which no per-rule assertion below can say on its own: a converter can satisfy every rule of D6
    individually and still produce a repo that ranks differently, because ranking is a property of
    the whole corpus (ADR-0012 stage 1 seeds ``d_moc`` from ``wiki/maps/**``, and in-degree is
    global).

    Both ADR-0012 §8 frontmatter modes are pinned for the reason ``test_golden`` pins both: the
    boost can mask a structural change in one column and not the other.
    """
    queries = load_queries(regen.QUERIES_PATH)
    fresh = _masked(snapshot(Wiki(RepoLayout(converted)), queries, fm=fm))
    golden = _masked(json.loads(golden_path.read_text(encoding="utf-8")))

    differences = diff_snapshots(golden, fresh)
    assert not differences, (
        f"the converted repo ranks differently from the natively-built schema-2 corpus "
        f"({len(differences)} differences vs {golden_path.name}):\n" + "\n".join(differences)
    )
    assert fresh == golden


def _masked(record: dict[str, Any]) -> dict[str, Any]:
    """A copy with the volatile ``agora_version`` masked; everything else is compared verbatim."""
    return {**record, "header": {**record["header"], "agora_version": _VERSION_SENTINEL}}


# --- D6 rules 1 + 2: kind directories and the path domain as `subjects:` --------------------------


def test_the_destination_lints_clean_under_the_v2_ruleset(converted: Path) -> None:
    """The converted repo passes the SCHEMA-2 lint with zero findings — errors and warnings."""
    result = lint(RepoLayout(converted))
    assert result.ok is True, [(f.code, f.path, f.message) for f in result.findings]
    assert result.findings == ()
    assert read_canonical_kb_schema_version(RepoLayout(converted)) == 2


def test_every_v1_note_lands_under_its_kind_with_its_path_domain_as_a_subject(
    golden_v1: Path, converted: Path
) -> None:
    """D6 rules 1 + 2 over the WHOLE corpus, not a sample.

    Rule 1 is checked as "the note is at the path its kind implies"; rule 2 as ``subjects:`` ==
    ``[<the v1 path domain>]`` — *not* ``[]``. The v1 path domain is a genuine curator assertion and
    discarding it would lose exactly what the flip is supposed to preserve; ``[]`` is the initial
    value for a NEW unclassifiable note (D2.2), and here only the root map — which has no path
    domain at all — legitimately gets it.
    """
    expected_kind_dir = {"theme": "wiki/concepts", "moc": "wiki/maps"}
    seen = 0
    for spec in CORPUS:
        if spec.kind not in expected_kind_dir:
            continue
        rel = f"{expected_kind_dir[spec.kind]}/{v2_basename(spec)}.md"
        fm = _fm(converted, rel)
        assert fm["kind"] == ("concept" if spec.kind == "theme" else "map")
        assert fm["type"] == fm["kind"], "type: is the derived OKF mirror of kind (OD-3)"
        assert fm["subjects"] == [spec.domain], rel
        assert fm["kb"] == FIXTURE_KB_ID
        assert fm["derived"] is False
        assert fm["provenance"] == {"writers": [], "agents": []}
        seen += 1
    assert seen == sum(1 for spec in CORPUS if spec.kind in expected_kind_dir)

    # The root map is the ONE note with no subject, and it says so rather than claiming one.
    assert _fm(converted, "index.md")["kind"] == "index"
    assert _fm(converted, "index.md")["subjects"] == []

    # Nothing was left behind in the v1 tree, and nothing was invented: the destination's note set
    # is exactly the v1 note set, re-pathed.
    v1_notes = _note_paths(golden_v1)
    v2_notes = _note_paths(converted)
    assert len(v2_notes) == len(v1_notes) - _merged_away(CORPUS)
    assert not any(path.startswith(("wiki/finance/", "wiki/cooking/")) for path in v2_notes)


def _merged_away(corpus: list[NoteSpec]) -> int:
    """How many notes the D6 rule-4 journal merge collapses (dailies minus distinct dates)."""
    dailies = [spec for spec in corpus if spec.kind == "daily"]
    return len(dailies) - len({v2_basename(spec) for spec in dailies})


# --- D6 rule 3: the map rename, and every link that named the old basename ------------------------


def test_the_moc_rename_carries_every_link_with_it(converted: Path) -> None:
    """``wiki/<d>/<d>-moc.md`` → ``wiki/maps/<d>.md``, and no ``[[<d>-moc]]`` survives anywhere.

    The rename is the half that is easy; the half that loses knowledge if it is missed is the
    REWRITE. ``children:`` on the root index, ``related:`` on any note that pointed at a map, and
    every body link are all keyed on the basename (ADR-0010 D5), so a rename without a rewrite
    silently disconnects the whole map tier from the index while every file still lints.
    """
    index = _fm(converted, "index.md")
    assert index["children"] == [f"[[{domain}]]" for domain in DOMAINS]
    for domain in DOMAINS:
        assert (converted / f"wiki/maps/{domain}.md").is_file()
        assert not (converted / f"wiki/{domain}").exists()

    stale = [
        path
        for path in sorted(_note_paths(converted))
        if "-moc" in (converted / path).read_text(encoding="utf-8")
    ]
    assert stale == [], f"a v1 `-moc` basename survived the conversion in {stale}"

    # The root map's body bullets point at the maps' NEW paths, so the links work on disk too.
    body = _body(converted, "index.md")
    for domain in DOMAINS:
        assert f"(wiki/maps/{domain}.md)" in body


def test_report_lists_every_rename_and_every_merge(golden_v1: Path, tmp_path: Path) -> None:
    """The report enumerates what moved — the listing D6 obliges the converter to produce."""
    report = convert_kb(golden_v1, tmp_path / "out", import_date=_IMPORT_DATE)

    renames = dict(report.renames)
    for domain in DOMAINS:
        assert renames[f"{domain}-moc"] == domain
    dailies = [spec for spec in CORPUS if spec.kind == "daily"]
    for spec in dailies:
        assert renames[spec.basename()] == v2_basename(spec)
    assert len(renames) == len(DOMAINS) + len(dailies)

    merged = {date: sources for date, sources in report.merges}
    assert set(merged) == {v2_basename(spec) for spec in dailies}
    assert report.summary["source_notes"] == len(_note_paths(golden_v1))
    assert report.summary["notes"] == report.summary["source_notes"] - _merged_away(CORPUS)
    assert report.summary["renamed"] == len(renames)
    assert report.lint.ok is True


# --- D6 rule 4: same-date dailies merge into one journal ------------------------------------------


def _daily(domain: str, date: str, title: str, body: str, summary: str) -> NoteSpec:
    return NoteSpec(
        kind="daily",
        domain=domain,
        slug=f"{domain}-{date}",
        title=title,
        summary=summary,
        extra_frontmatter={"date": date},
        body=body,
    )


def test_same_date_dailies_from_different_domains_merge_into_one_journal(tmp_path: Path) -> None:
    """D6 rule 4 / D2.6: one journal per ``run_date``, repo-wide.

    v1 namespaced a daily ``<domain>-YYYY-MM-DD`` for one stated reason — bare dates would collide
    across domains. Schema 2 takes the domain out of the path, so the reason is gone and the
    collision becomes a MERGE: sections concatenate in domain order, ``sources:`` unions, ``run_id``
    comes from the first, and every contributing domain survives as a ``subjects:`` entry. Each
    contributor's title becomes its section heading, because a merge that discards the titles of the
    notes it merges is a lossy migration wearing a merge's name.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    specs = [
        NoteSpec(kind="theme", domain="cooking", title="Braise", body="Cooking prose."),
        NoteSpec(kind="theme", domain="finance", title="Ledger", body="Finance prose."),
        _daily("finance", "2026-01-12", "finance daily", "Finance work.", "Finance journal."),
        _daily("cooking", "2026-01-12", "cooking daily", "Cooking work.", "Cooking journal."),
        _daily("finance", "2026-01-13", "second finance daily", "More work.", "Second journal."),
    ]
    build_kb(src, specs, schema_version=1, domains=["cooking", "finance"])

    report = convert_kb(src, dest, import_date=_IMPORT_DATE, kb_id=FIXTURE_KB_ID)

    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]
    merged = _fm(dest, "wiki/notes/2026/01/2026-01-12.md")
    assert merged["kind"] == "note"
    assert merged["date"] == "2026-01-12"
    assert merged["title"] == "2026-01-12"  # basenamed and titled by its date (D2.6)
    # Every contributing domain keeps its assertion — the journal is the note schema 2 genuinely
    # makes multi-subject, in the taxonomy's own domain order.
    assert merged["subjects"] == ["cooking", "finance"]
    body = _body(dest, "wiki/notes/2026/01/2026-01-12.md")
    assert body.index("## cooking daily") < body.index("## finance daily")
    assert "Cooking work." in body and "Finance work." in body
    # The un-merged date is a journal too, on its own shard — the degenerate case is an identity.
    assert _fm(dest, "wiki/notes/2026/01/2026-01-13.md")["subjects"] == ["finance"]
    # Both v1 basenames are gone; both dates are the new identity.
    assert dict(report.renames)["finance-2026-01-12"] == "2026-01-12"
    assert dict(report.renames)["cooking-2026-01-12"] == "2026-01-12"
    assert report.summary["merged_journals"] == 1
    assert report.summary["merged_sources"] == 2
    record = next(n for n in report.notes if n.dest_path.endswith("2026-01-12.md"))
    assert set(record.src_paths) == {
        "wiki/cooking/daily/cooking-2026-01-12.md",
        "wiki/finance/daily/finance-2026-01-12.md",
    }


# --- D6 rule 5: raw/ copied byte-identically, sources: NOT rewritten ------------------------------


def test_raw_is_copied_byte_identically_and_sources_are_untouched(
    golden_v1: Path, converted: Path
) -> None:
    """The payoff of D3.4 and the reason the conversion is cheap.

    ``raw/<domain>/…`` is byte-identical to v1 because ``raw/`` never moves (D1.4): the ``<domain>``
    segment survives as a SHARD KEY and nothing reads a subject out of it. That is what lets every
    ``sources:`` string stay exactly as the v1 curator wrote it — and it is checked here as an
    equality over the whole tree rather than a spot check, because one rewritten string is one
    permanently unresolvable citation (lint L1-8).
    """
    v1_raw = {
        p.relative_to(golden_v1).as_posix(): p.read_bytes()
        for p in sorted((golden_v1 / "raw").rglob("*"))
        if p.is_file()
    }
    v2_raw = {
        p.relative_to(converted).as_posix(): p.read_bytes()
        for p in sorted((converted / "raw").rglob("*"))
        if p.is_file()
    }
    assert v1_raw == v2_raw
    assert v1_raw, "the corpus must actually have raw/ evidence for this to mean anything"

    cited = [
        source
        for spec in CORPUS
        if spec.kind == "theme"
        for source in _fm(converted, f"wiki/concepts/{v2_basename(spec)}.md")["sources"]
    ]
    assert cited, "concepts must cite raw/ artifacts"
    for source in cited:
        assert source.startswith("raw/")
        assert (converted / source).is_file(), f"{source} does not resolve (L1-8)"


# --- D6 rule 6: a NEW kb_id, minted and stamped ---------------------------------------------------


def test_a_new_kb_id_is_minted_and_stamped_into_every_note(golden_v1: Path, tmp_path: Path) -> None:
    """The destination is a NEW knowledge base, not a continuation (D6 rule 6 / D1.5)."""
    first = convert_kb(golden_v1, tmp_path / "a", import_date=_IMPORT_DATE)
    second = convert_kb(golden_v1, tmp_path / "b", import_date=_IMPORT_DATE)

    assert first.kb_id != second.kb_id, "each conversion mints its own KB identity"
    identity = load_kb_identity(RepoLayout(tmp_path / "a"))
    assert identity is not None and identity.kb_id == first.kb_id
    stamped = {_fm(tmp_path / "a", path)["kb"] for path in _note_paths(tmp_path / "a")}
    assert stamped == {first.kb_id}


# --- D6 rule 7: collisions are a HARD failure with a NAMED list -------------------------------


def test_a_basename_collision_is_a_hard_failure_naming_the_notes(tmp_path: Path) -> None:
    """Never a silent rename — a renamed basename disconnects every ``[[basename]]`` edge to it.

    The collision this exercises is one the CONVERSION introduces rather than one the source had:
    ``wiki/finance/finance-moc.md`` and ``wiki/finance/themes/finance.md`` are distinct basenames in
    v1 (``finance-moc`` and ``finance``) and become the same one under D6 rule 3.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [NoteSpec(kind="theme", domain="finance", slug="finance", title="Finance", body="Prose.")],
        schema_version=1,
        domains=["finance"],
    )
    before = _tree_digest(src)

    with pytest.raises(KbConvertError) as excinfo:
        convert_kb(src, dest, import_date=_IMPORT_DATE)

    message = str(excinfo.value)
    assert "'finance'" in message, "the colliding BASENAME must be named"
    assert "wiki/finance/finance-moc.md" in message
    assert "wiki/finance/themes/finance.md" in message
    assert "D6 rule 7" in message
    assert not dest.exists(), "nothing is written when the plan cannot be carried out"
    assert _tree_digest(src) == before


# --- the tolerant read, and the one kind that cannot be tolerated --------------------------------


def test_a_note_that_declares_no_kind_becomes_a_concept_and_is_reported(tmp_path: Path) -> None:
    """A stray ``wiki/README.md`` converts (as a concept) rather than stranding the whole KB.

    ADR-0014 D1's tolerant read applied where it belongs: the note names no ``type:`` to
    translate, so no row of the frozen D2.5 table is overruled, and refusing an entire conversion
    over one file nobody meant as knowledge would strand a real KB. The result is REPORTED, and the
    destination lint grades it honestly rather than the converter pretending it is fine.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [NoteSpec(kind="theme", domain="general", title="Alpha", body="Prose.")],
        schema_version=1,
        domains=["general"],
    )
    (src / "wiki" / "README.md").write_text("# Readme\n\nHow this vault is organised.\n", "utf-8")

    report = convert_kb(src, dest, import_date=_IMPORT_DATE, kb_id=FIXTURE_KB_ID)

    record = next(n for n in report.notes if n.dest_path == "wiki/concepts/README.md")
    assert record.kind == "concept"
    assert any("no usable 'type:'" in w for w in record.warnings)
    assert _fm(dest, "wiki/concepts/README.md")["kind"] == "concept"
    # It has no raw/ evidence, so the honest outcome is an L1-7 the operator can act on — NOT a
    # silent pass, and NOT a refusal that would have cost them the other 46 notes.
    assert {f.code for f in report.lint.findings} == {"L1-7"}


def test_an_undated_daily_is_a_hard_failure_naming_it(tmp_path: Path) -> None:
    """An undated ``type: daily`` has no legal schema-2 path; it is refused, never re-kinded.

    The asymmetry with the tolerant case above is the point: this note DOES declare its kind, and
    D2.6 makes a journal's basename, its ``date:`` and its ``<yyyy>/<mm>`` shard three views of one
    value (lint L1-14 asserts the identity). Converting it to a concept would be the converter
    overruling D6 rule 1 on the one note where it was told what the kind is; inventing a date would
    be worse.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [
            NoteSpec(kind="theme", domain="general", title="Alpha", body="Prose."),
            _daily("general", "2026-01-12", "general daily", "Work.", "Journal."),
        ],
        schema_version=1,
        domains=["general"],
    )
    daily = src / "wiki" / "general" / "daily" / "general-2026-01-12.md"
    daily.rename(daily.with_name("undated.md"))
    text = (src / "wiki" / "general" / "daily" / "undated.md").read_text(encoding="utf-8")
    (src / "wiki" / "general" / "daily" / "undated.md").write_text(
        text.replace("date: '2026-01-12'\n", "").replace("date: 2026-01-12\n", ""),
        encoding="utf-8",
    )

    with pytest.raises(KbConvertError) as excinfo:
        convert_kb(src, dest, import_date=_IMPORT_DATE)

    message = str(excinfo.value)
    assert "wiki/general/daily/undated.md" in message
    assert "D2.6" in message
    assert not dest.exists()


def test_canonical_non_wiki_content_survives_the_crossing(tmp_path: Path) -> None:
    """``assets/`` and ``log.md`` are CANONICAL (D1) and are copied, not dropped.

    D6 names neither, because neither moves — which is exactly why a converter can quietly lose
    them. ``log.md`` records runs against the SOURCE repo and keeping it is the honest option: a
    converted KB whose knowledge appears from nowhere reads as if it had no history.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [NoteSpec(kind="theme", domain="general", title="Alpha", body="Prose.")],
        schema_version=1,
        domains=["general"],
    )
    (src / "log.md").write_text("# Run log\n\n- 2026-01-12 run f1x7ur\n", encoding="utf-8")
    (src / "assets").mkdir()
    (src / "assets" / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

    report = convert_kb(src, dest, import_date=_IMPORT_DATE)

    assert report.lint.ok is True, [(f.code, f.path, f.message) for f in report.lint.findings]
    assert (dest / "log.md").read_bytes() == (src / "log.md").read_bytes()
    assert (dest / "assets" / "diagram.png").read_bytes() == b"\x89PNG\r\n\x1a\nfake"
    assert report.summary["asset_files"] == 1


def test_a_journal_date_colliding_with_a_concept_is_a_hard_failure(tmp_path: Path) -> None:
    """A concept basenamed like a date collides with the journal D2.6 names by that date.

    The other shape of D6 rule 7, and the one the merge itself creates: two same-date dailies MERGE
    (that is rule 4, not a collision), but the date they merge onto is a basename like any other, so
    a concept already holding it is a genuine L1-1 duplicate. Named, never renamed — and the message
    says which dailies are behind the journal so the operator can see both sides.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [
            NoteSpec(
                kind="theme",
                domain="general",
                slug="2026-01-12",
                title="A note about that day",
                body="Prose.",
            ),
            _daily("general", "2026-01-12", "general daily", "Work.", "Journal."),
        ],
        schema_version=1,
        domains=["general"],
    )

    with pytest.raises(KbConvertError) as excinfo:
        convert_kb(src, dest, import_date=_IMPORT_DATE)

    message = str(excinfo.value)
    assert "'2026-01-12'" in message
    assert "wiki/general/themes/2026-01-12.md" in message
    assert "wiki/general/daily/general-2026-01-12.md" in message  # named through the merge
    assert not dest.exists()


# --- the contract inherited from `agora import`: the source is never modified ---------------------


def test_the_source_repo_is_never_modified(golden_v1: Path, tmp_path: Path) -> None:
    """``agora import``'s standing promise, held by the converter: SRC is read-only."""
    before = _tree_digest(golden_v1)
    convert_kb(golden_v1, tmp_path / "out", import_date=_IMPORT_DATE)
    assert _tree_digest(golden_v1) == before


def test_the_destination_history_starts_fresh(golden_v1: Path, tmp_path: Path) -> None:
    """The destination is a git repo of its own, with the import commit contract's single commit."""
    from agora_kb.core.repo import Repo

    dest = tmp_path / "out"
    convert_kb(golden_v1, dest, import_date=_IMPORT_DATE)
    repo = Repo(RepoLayout(dest))
    assert repo.is_initialized()
    assert repo.head_commit()


def test_the_converted_repo_accepts_the_write_the_source_refused(tmp_path: Path) -> None:
    """The crossing does its job: a write that bounced off the v1 repo lands on the new one.

    ADR-0041 D6 makes a schema-1 repo READ-ONLY for this build, with one message naming this
    converter, and puts the refusal on ``Inbox.write`` itself so it covers ``kb_remember``, the web
    upload and every future writer at once. This test states both halves in one place — the source
    refuses, the destination accepts — because "the conversion produced a lint-clean tree" is not
    the same claim as "the operator can use it", and only the second one is what the owner is
    re-importing two live KBs to obtain.
    """
    from agora_kb.config import ReadOnlySchemaVersionError
    from agora_kb.core.inbox import Inbox

    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [NoteSpec(kind="theme", domain="general", title="Alpha", body="Prose.")],
        schema_version=1,
        domains=["general"],
    )

    with pytest.raises(ReadOnlySchemaVersionError) as excinfo:
        Inbox(RepoLayout(src)).write(text="A fact.", writer="test-agent", source="agent:test")
    assert "agora import --from-kb" in str(excinfo.value)

    convert_kb(src, dest, import_date=_IMPORT_DATE)

    item = Inbox(RepoLayout(dest)).write(text="A fact.", writer="test-agent", source="agent:test")
    assert item is not None
    assert Inbox(RepoLayout(dest)).depth() == 1
    # ...and the source's inbox is still untouched, because nothing was written there.
    assert Inbox(RepoLayout(src)).depth() == 0


# --- the refusals ---------------------------------------------------------------------------------


def test_a_schema_2_source_is_refused(tmp_path: Path) -> None:
    """The converter crosses 1 → 2 and nothing else; a schema-2 source is not a migration."""
    src, dest = tmp_path / "v2src", tmp_path / "out"
    build_kb(src, [NoteSpec(kind="theme", domain="general", title="A", body="Prose.")])

    with pytest.raises(KbConvertError, match="not a KB wiki schema-1 repo"):
        convert_kb(src, dest, import_date=_IMPORT_DATE)
    assert not dest.exists()


def test_a_source_that_is_not_an_agora_repo_is_refused(tmp_path: Path) -> None:
    """No ``_meta/taxonomy.yaml`` means nothing declares a schema — refused, never guessed at."""
    src, dest = tmp_path / "plain", tmp_path / "out"
    (src / "wiki").mkdir(parents=True)
    (src / "wiki" / "note.md").write_text("# Note\n", encoding="utf-8")

    with pytest.raises(KbConvertError, match="no _meta/taxonomy.yaml"):
        convert_kb(src, dest, import_date=_IMPORT_DATE)
    assert not dest.exists()


def test_an_occupied_destination_is_refused(golden_v1: Path, tmp_path: Path) -> None:
    """There is NO in-place migrator: the destination is a new repo (D6).

    An EMPTY directory is accepted because it is indistinguishable from an absent one for every
    purpose the rule serves — there is nothing to clobber and no second layout to mix with — and
    ``mkdir -p`` is a normal reflex. Anything with a file in it is refused.
    """
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "README.md").write_text("mine\n", encoding="utf-8")

    with pytest.raises(KbConvertError, match="already exists and is not empty"):
        convert_kb(golden_v1, occupied, import_date=_IMPORT_DATE)
    assert (occupied / "README.md").read_text(encoding="utf-8") == "mine\n"

    empty = tmp_path / "empty"
    empty.mkdir()
    report = convert_kb(golden_v1, empty, import_date=_IMPORT_DATE)
    assert report.lint.ok is True


def test_a_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        convert_kb(tmp_path / "nope", tmp_path / "out", import_date=_IMPORT_DATE)


def test_a_reserved_domain_name_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """A source domain beginning with ``_`` cannot become a schema-2 domain (D1.4 / L1-23).

    ``raw/<domain>/`` and ``raw/_blob/`` share ONE namespace, so a KB declaring a domain literally
    named ``_blob`` would make APPLY write event notes into the content-addressed tree. Schema 1 has
    no such rule, so the refusal has to happen HERE — at the crossing — rather than at the
    destination's first write, which is far too late to do anything about it.
    """
    src, dest = tmp_path / "v1", tmp_path / "out"
    build_kb(
        src,
        [NoteSpec(kind="theme", domain="general", title="A", body="Prose.")],
        schema_version=1,
        domains=["general"],
    )
    taxonomy = src / "_meta" / "taxonomy.yaml"
    taxonomy.write_text(
        taxonomy.read_text(encoding="utf-8").replace("- general", "- general\n- _blob"),
        encoding="utf-8",
    )

    with pytest.raises(KbConvertError, match="_blob"):
        convert_kb(src, dest, import_date=_IMPORT_DATE)
    assert not dest.exists()


def test_a_destination_nested_in_the_source_is_refused(tmp_path: Path) -> None:
    """``agora import --from-kb <kb> <kb>/converted`` is refused — D6's source is NEVER modified.

    Not a hypothetical reflex: ``mkdir -p ~/kb/converted`` is the obvious thing to type, and without
    the guard the converter planted a whole schema-2 repo — its own ``.git`` included — inside the
    tree it had just promised not to touch, then printed ``<src> was NOT modified``. The source's
    next ``git add -A`` would have committed it.
    """
    src = tmp_path / "v1"
    build_kb(
        src,
        [NoteSpec(kind="theme", domain="general", title="Alpha", body="Prose.")],
        schema_version=1,
        domains=["general"],
    )
    before = _tree_digest(src)
    listing_before = sorted(p.name for p in src.iterdir())

    with pytest.raises(KbConvertError, match="inside the source repo"):
        convert_kb(src, src / "converted", import_date=_IMPORT_DATE)

    assert _tree_digest(src) == before
    assert sorted(p.name for p in src.iterdir()) == listing_before
    assert not (src / "converted").exists()

    # ...and the mirror, refused from the other side for the same reason.
    with pytest.raises(KbConvertError, match="inside the destination"):
        convert_kb(src, src.parent, import_date=_IMPORT_DATE)


def test_a_body_link_to_raw_evidence_keeps_its_file_and_is_respelled(tmp_path: Path) -> None:
    """A body link to a REAL non-note file keeps THAT FILE, and its spelling moves with the note.

    Two properties, and the first is the one rule 5 protects: the basename fallback exists for a
    link that resolves to nothing, and applied to a link that DOES resolve —
    ``../../../raw/general/alpha.md``, the operator citing their own evidence inline — it re-pointed
    the citation at whatever wiki note shared the filename stem, in the converter whose rule 5
    exists to leave ``raw/`` references exactly as they are (D3.4). That must never happen.

    The second is what "leave it alone" cost, and it is not free: the file is copied to the
    IDENTICAL repo-relative path, but the LINKING note moved two directory levels, so
    ``../../../raw/…`` written from ``wiki/general/themes/`` resolves ABOVE the destination root
    from ``wiki/concepts/``. Byte-identical is a dead link. The target is therefore RE-SPELLED for
    the note's new directory and must resolve on disk — which rewrites no provenance at all,
    because the file identity is unchanged: the same bytes at the same repo-relative path.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [
            NoteSpec(
                kind="theme",
                domain="general",
                title="Alpha",
                body="The capture lives at [the capture](../../../raw/general/alpha.md).",
            )
        ],
        schema_version=1,
        domains=["general"],
    )
    assert (src / "raw" / "general" / "alpha.md").is_file(), "fixture must ground the theme in raw/"

    convert_kb(src, dest, import_date=_IMPORT_DATE)

    body = _body(dest, "wiki/concepts/alpha.md")
    # STILL the raw capture — never re-pointed at the wiki note that shares the stem (rule 5).
    assert "[the capture](../../raw/general/alpha.md)" in body
    assert "concepts/alpha.md)" not in body.split("The capture lives at ", 1)[1]
    # …and it now resolves, which is the whole point of re-spelling it.
    assert (dest / "wiki" / "concepts" / "../../raw/general/alpha.md").resolve().is_file()


def test_a_body_link_and_image_into_assets_are_respelled_for_the_new_directory(
    tmp_path: Path,
) -> None:
    """``assets/`` is copied at its own path too, so links AND image embeds into it are re-spelled.

    The image half is the silent one: ``![alt](…)`` is excluded from the note link graph on purpose
    (ADR-0010 §3.5), so a broken embed trips no lint rule and shows the reader nothing — the note
    just renders a hole. Both forms have to survive the flip, and neither may become a graph edge.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [
            NoteSpec(
                kind="theme",
                domain="general",
                title="Alpha",
                body=(
                    "See [pic](../../../assets/pic.png).\n\n"
                    "![diagram](../../../assets/pic.png)\n\n"
                    "Outside: [y](../../../../outside.md) and [x](https://example.com)."
                ),
            )
        ],
        schema_version=1,
        domains=["general"],
    )
    (src / "assets").mkdir(exist_ok=True)
    (src / "assets" / "pic.png").write_bytes(b"\x89PNG stub")

    convert_kb(src, dest, import_date=_IMPORT_DATE)

    body = _body(dest, "wiki/concepts/alpha.md")
    assert "[pic](../../assets/pic.png)" in body
    assert "![diagram](../../assets/pic.png)" in body
    assert (dest / "wiki" / "concepts" / "../../assets/pic.png").resolve().is_file()
    # An escape out of the repo and an external URL are left BYTE-IDENTICAL — neither is carried,
    # so there is no destination path to re-spell them to.
    assert "[y](../../../../outside.md)" in body
    assert "[x](https://example.com)" in body


def test_a_v1_body_status_literal_is_normalised_away_and_reported(tmp_path: Path) -> None:
    """A v1 ``body_status: authored`` is DROPPED, not carried — L1-4 admits only ``pending``.

    ADR-0010's ADR-0041 amendment banner names this obligation for the D6 importer specifically:
    v1's lint never graded the value, so a repo that lints clean at the source converted into one
    that fails its own lint on note one.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [NoteSpec(kind="theme", domain="general", title="Alpha", body="Prose.")],
        schema_version=1,
        domains=["general"],
    )
    note = src / "wiki" / "general" / "themes" / "alpha.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            "title: Alpha", "title: Alpha\nbody_status: authored"
        ),
        encoding="utf-8",
    )

    report = convert_kb(src, dest, import_date=_IMPORT_DATE)

    assert "body_status" not in _fm(dest, "wiki/concepts/alpha.md")
    record = next(n for n in report.notes if n.dest_path == "wiki/concepts/alpha.md")
    assert any("body_status" in w for w in record.warnings)
    assert report.lint.ok is True, [(f.code, f.message) for f in report.lint.findings]


def test_a_path_domain_the_taxonomy_no_longer_declares_converts_with_empty_subjects(
    tmp_path: Path,
) -> None:
    """Rule 2's one exception, stated where the code implements it rather than only in a docstring.

    L1-5 grades ``subjects:`` against the destination taxonomy, which IS the source's own copied
    verbatim, so writing an undeclared domain would mint a repo that fails its own lint. The note
    converts with ``subjects: []`` and the discarded assertion is REPORTED — and, since the
    conversion could not carry it into ``subjects:``, the retired ``domain:`` key is kept as the
    only surviving record of it.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [
            NoteSpec(kind="theme", domain="general", title="Alpha", body="Prose."),
            NoteSpec(kind="theme", domain="legacy", title="Old", body="Prose."),
        ],
        schema_version=1,
        domains=["general", "legacy"],
    )
    taxonomy = src / "_meta" / "taxonomy.yaml"
    taxonomy.write_text(
        taxonomy.read_text(encoding="utf-8").replace("- legacy\n", ""), encoding="utf-8"
    )

    report = convert_kb(src, dest, import_date=_IMPORT_DATE)

    fm = _fm(dest, "wiki/concepts/old.md")
    assert fm["subjects"] == []
    record = next(n for n in report.notes if n.dest_path == "wiki/concepts/old.md")
    assert any("not in the source taxonomy domains" in w for w in record.warnings)
    # The declared-domain note is unaffected: rule 2's normal case still writes the subject.
    assert _fm(dest, "wiki/concepts/alpha.md")["subjects"] == ["general"]


def test_the_retired_domain_key_is_dropped_once_subjects_carries_it(tmp_path: Path) -> None:
    """``domain:`` is the v1 topic key; its successor is ``subjects:`` (D2.2), so it does not ride
    along as a second, drift-prone subject carrier — least of all on a MERGED journal, where it
    would assert one domain over a note that carries two."""
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [NoteSpec(kind="theme", domain="general", title="Alpha", body="Prose.")],
        schema_version=1,
        domains=["general"],
    )
    note = src / "wiki" / "general" / "themes" / "alpha.md"
    text = note.read_text(encoding="utf-8")
    if "domain:" not in text:
        note.write_text(text.replace("title: Alpha", "title: Alpha\ndomain: general"), "utf-8")

    report = convert_kb(src, dest, import_date=_IMPORT_DATE)

    fm = _fm(dest, "wiki/concepts/alpha.md")
    assert "domain" not in fm
    assert fm["subjects"] == ["general"]
    record = next(n for n in report.notes if n.dest_path == "wiki/concepts/alpha.md")
    assert any("retired v1 'domain:" in w for w in record.warnings)


def test_source_content_the_conversion_does_not_carry_is_named_in_the_report(
    tmp_path: Path,
) -> None:
    """A KB that is also somebody's Obsidian vault has files D6 does not carry — and is TOLD so.

    Nothing is destroyed (the source is untouched) and nothing is copied: the conversion owes the
    D1 canonical set, not an operator's ``README.md``. What it owes beyond that is honesty —
    ``converted N note(s) … lint: clean`` reads as a complete crossing.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [NoteSpec(kind="theme", domain="general", title="Alpha", body="Prose.")],
        schema_version=1,
        domains=["general"],
    )
    (src / "README.md").write_text("# My KB\n", encoding="utf-8")
    (src / "docs").mkdir()
    (src / "docs" / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (src / ".obsidian").mkdir()
    (src / ".obsidian" / "app.json").write_text("{}\n", encoding="utf-8")

    report = convert_kb(src, dest, import_date=_IMPORT_DATE)

    assert set(report.skipped) == {"README.md", "docs/", ".obsidian/"}
    assert report.summary["skipped_paths"] == 3
    assert not (dest / "README.md").exists()
    # ...and the SOURCE still has every one of them.
    assert (src / "README.md").is_file() and (src / ".obsidian" / "app.json").is_file()


def test_the_converted_repo_git_tracks_every_kind_container(tmp_path: Path) -> None:
    """The six ``wiki/<kind>/`` containers survive a git round-trip, exactly as ``repo init``'s do.

    The directory IS the kind under schema 2 (D3.1), so the containers are the schema's own
    statement of what kinds exist — including the two that ship EMPTY (summaries, entities) and the
    one only a human populates (people). Git cannot track an empty directory, so a bare ``mkdir``
    would leave the unpopulated ones out of the conversion's own commit: a converted repo and an
    init'd one would be DIFFERENT trees at the same schema, and the containers would vanish on the
    first ``agora sync`` + clone.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [NoteSpec(kind="theme", domain="general", title="Alpha", body="Prose.")],
        schema_version=1,
        domains=["general"],
    )

    convert_kb(src, dest, import_date=_IMPORT_DATE, kb_id=FIXTURE_KB_ID)

    tracked = subprocess.run(
        ["git", "ls-files", "wiki/"], cwd=dest, capture_output=True, text=True, check=True
    ).stdout.split()
    for name in ("concepts", "summaries", "notes", "maps", "entities", "people"):
        assert f"wiki/{name}/.gitkeep" in tracked, f"wiki/{name}/ is not in the conversion's commit"


def test_the_root_index_writes_empty_subjects_without_a_warning(tmp_path: Path) -> None:
    """The root ``index.md`` gets ``subjects: []`` STRUCTURALLY — reported nowhere, and correctly.

    D6 rule 2's shipped form (ADR-0041's *D6 rule 2 as shipped* addendum) writes ``[]`` in two
    cases and reports exactly ONE of them: an UNDECLARED path domain is dropped with a per-note
    warning, because that is a real curator assertion going away; the root index has no path domain
    at all, so there was never a subject to carry and a warning would fire on every conversion ever
    run. This pins the asymmetry the module docstring states.
    """
    src, dest = tmp_path / "v1", tmp_path / "v2"
    build_kb(
        src,
        [NoteSpec(kind="theme", domain="general", title="Alpha", body="Prose.")],
        schema_version=1,
        domains=["general"],
    )

    report = convert_kb(src, dest, import_date=_IMPORT_DATE, kb_id=FIXTURE_KB_ID)

    assert _fm(dest, "index.md")["subjects"] == []
    assert report.warnings == ()
    index_record = next(n for n in report.notes if n.dest_path == "index.md")
    assert index_record.subjects == () and index_record.warnings == ()
