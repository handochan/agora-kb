"""Tests for the deterministic §3 APPLY + §4.2 AUTHOR-diff + §4.6 stray-link strip (ADR-0011).

The INGEST core is "success = a pure function of (plan, diff, manifest, lint)" — so every plan here
is HAND-AUTHORED and applied to a tmp worktree with ZERO model in the loop. We assert the EXACT
files / frontmatter / sentinels / map-children / contested callout APPLY produces, that the result
passes the deterministic lint (the SAME gate the worker runs, ADR-0011 §4.4), that §4.2 accepts a
clean PASS-2 body edit and rejects every tampering class, that §4.6 strips stray links while keeping
planned ones, and that APPLY is byte-deterministic (same plan -> same bytes).

**Every worktree here is KB WIKI SCHEMA 2** (ADR-0041): the taxonomy declares ``schema_version: 2``,
``_meta/kb.yaml`` carries the KB identity every note mirrors into ``kb:``, and the notes APPLY
writes land under ``wiki/concepts/``, ``wiki/notes/<yyyy>/<mm>/`` and ``wiki/maps/`` — the DIRECTORY
is the kind and the subject lives in ``subjects:``. ``lint()`` dispatches on the emitted taxonomy,
so "the result passes lint" means "passes the schema-2 ruleset" with no argument here saying so.
``raw/`` is UNMOVED and its paths are byte-identical to schema 1 (D1.4/D3.4), which several tests
assert directly because it is the property that makes the flip cheap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.config import KbIdentity, write_kb_identity
from agora_kb.core import frontmatter
from agora_kb.core.layout import RepoLayout
from agora_kb.curator.apply import (
    DEFAULT_MAX_BODY_BYTES,
    ApplyError,
    _source_links,
    apply_plan,
    body_sentinels,
    region_sentinel_id,
    strip_stray_wikilinks,
    validate_author_diff,
)
from agora_kb.curator.plan import Disposition, Plan
from agora_kb.schema.emit import Taxonomy, emit_schema
from agora_kb.schema.lint import lint
from agora_kb.schema.notes import body_link_basenames, child_bullets, wikilinks

RUN_ID = "2026-06-13T03-00-00.000Z--7f31ab"
RUN_DATE = "2026-06-13"
E1 = "2026-06-13T02-40-10.000Z--a1b2c3"
E2 = "2026-06-13T02-41-00.000Z--d4e5f6"
E3 = "2026-06-13T02-42-00.000Z--beef01"

TAXONOMY = Taxonomy(
    schema_version=2,
    taxonomy_policy="open",
    allowed_tags=("curator", "concurrency", "architecture"),
    domains=("ai-tech", "economy", "general"),
)

#: The ``_meta/kb.yaml`` ``kb_id`` every note APPLY writes must mirror into ``kb:`` (ADR-0041 D1.5).
#: A real canonical ULID (``KbIdentity`` validates it) but a FIXED one — a fixture that minted a
#: fresh id would make the byte-golden below unrepeatable.
KB_ID = "01J8ZQ3M4N5P6Q7R8S9T0V1W2X"


# --- worktree fixtures --------------------------------------------------------------------------


def _schema2_repo(root: Path) -> Path:
    """Emit a schema-2 repo skeleton at ``root``: taxonomy + schema doc + ``_meta/kb.yaml``.

    This is the PRODUCTION admin path — the same two writers ``agora repo init --schema 2`` and
    ``tests.support.kb_builder.build_kb(schema_version=2)`` use — rather than a third spelling of
    it, so the schema-doc header and the taxonomy agree (L1-17) and the identity file is written by
    the writer that enforces its closed key set. ``build_kb`` itself is not used here because these
    tests need a taxonomy with a FIXED tag/domain vocabulary and NO notes: APPLY is the producer
    under test, so a pre-populated corpus would grade the fixture instead.
    """
    root.mkdir(parents=True, exist_ok=True)
    layout = RepoLayout(root)
    emit_schema(layout, taxonomy=TAXONOMY)
    write_kb_identity(layout, KbIdentity(kb_id=KB_ID, name="agora-test", declared_kind="personal"))
    return root


def _worktree(tmp_path: Path) -> Path:
    """A schema-2 repo worktree with a populated raw/ for the source refs APPLY cites."""
    _schema2_repo(tmp_path)
    # Persist the raw/ artifacts that APPLY's sources: union cites (ADR-0010 D3), so lint L1-8
    # (source path exists) passes on the produced concepts. `raw/<domain>/<event_id>.md` is
    # BYTE-IDENTICAL to schema 1 — ADR-0041 D1.4/D3.4 never move raw/.
    for event in (E1, E2, E3):
        raw = tmp_path / "raw" / "ai-tech" / f"{event}.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f"raw capture {event}\n", encoding="utf-8")
    return tmp_path


# --- schema-2 path helpers (the flipped axis: the DIRECTORY is the kind, ADR-0041 D1) -----------


def _concept(wt: Path, basename: str) -> Path:
    """``wiki/concepts/<basename>.md`` — flat under its kind, subject-free (D1/D2.2)."""
    return wt / "wiki" / "concepts" / f"{basename}.md"


def _map_note(wt: Path, subject: str) -> Path:
    """``wiki/maps/<subject>.md`` — the ``-moc`` filename suffix is gone (D5)."""
    return wt / "wiki" / "maps" / f"{subject}.md"


def _journal(wt: Path, date: str = RUN_DATE) -> Path:
    """``wiki/notes/<yyyy>/<mm>/<date>.md`` — ONE journal per run_date, repo-wide (D2.6)."""
    return wt / "wiki" / "notes" / date[:4] / date[5:7] / f"{date}.md"


def _journal_rel(date: str = RUN_DATE) -> str:
    return f"wiki/notes/{date[:4]}/{date[5:7]}/{date}.md"


def _provenance(
    candidate_id: str,
    *event_ids: str,
    source: str = "claude-code",
    raw_ref: str | None = None,
    body: str | None = None,
) -> dict:
    return {
        candidate_id: [
            {
                "event_id": e,
                "source": source,
                "writer": "dochan",
                "cwd": "/tmp/psa",
                "raw_ref": raw_ref,
                "created": "2026-06-13T02-40-10.000Z",
                **({"body": body} if body is not None else {}),
            }
            for e in event_ids
        ]
    }


def _plan(*dispositions: Disposition, finished: bool = True) -> Plan:
    return Plan(
        schema_version=1, run_id=RUN_ID, finished=finished, dispositions=tuple(dispositions)
    )


def _create_theme(**overrides: object) -> Disposition:
    base: dict[str, object] = {
        "candidate_id": "c1",
        "event_ids": (E1,),
        "op": "CREATE_THEME",
        "domain": "ai-tech",
        "basename": "curator-concurrency",
        "title": "Curator concurrency model",
        "summary": "One curator advances the curated branch under a per-repo lock.",
        "status": "active",
        "tags": ("curator", "concurrency"),
        "aliases": ("single-curator model",),
        "links": (),
        "needs_prose": True,
        "reason": "New concept.",
    }
    base.update(overrides)
    return Disposition(**base)


# --- CREATE_THEME -------------------------------------------------------------------------------


def test_create_theme_writes_full_c2_frontmatter_and_sentinels(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    theme = _concept(wt, "curator-concurrency")
    assert theme.is_file()
    fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))

    # The ADR-0041 D2 COMMON BASE, all worker-materialized.
    assert fm["title"] == "Curator concurrency model"
    assert fm["kind"] == "concept"  # MIRRORS the directory (D2.1)
    assert fm["type"] == "concept"  # the derived OKF mirror of kind (OD-3), NOT the authority
    assert fm["kb"] == KB_ID  # the _meta/kb.yaml ULID (D1.5)
    assert fm["subjects"] == ["ai-tech"]  # the v1 PATH domain, now a frontmatter list (D2.2)
    assert fm["aliases"] == ["single-curator model"]
    assert fm["tags"] == ["curator", "concurrency"]
    assert str(fm["created"]) == RUN_DATE
    assert str(fm["updated"]) == RUN_DATE
    assert fm["status"] == "active"
    assert fm["summary"] == "One curator advances the curated branch under a per-repo lock."
    assert fm["derived"] is False  # D2.4: a curated note is not a proposal-plane artifact
    # D2.3's deliberately-unequal pair: `writers` are AUTHENTICATED principals (none exist before
    # the Phase-4 auth plane, so the honest value is empty), `agents` are SELF-DECLARATIONS.
    assert fm["provenance"] == {"writers": [], "agents": ["claude-code"]}
    # sources is the provenance UNION the WORKER writes — raw/ is UNMOVED (D1.4/D3.4).
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md"]
    assert fm["related"] == []
    assert fm["confidence"] == "high"
    assert fm["body_status"] == "pending"
    assert "origin" not in fm  # not harvested

    # Body sentinel pair keyed by the RUN-SCOPED region id (§3, region_sentinel_id).
    start, end = body_sentinels(region_sentinel_id(RUN_ID, "c1"))
    assert start in body and end in body
    assert body.index(start) < body.index(end)


def test_create_theme_produces_exact_bytes(tmp_path: Path) -> None:
    # Byte-exact golden: pins frontmatter KEY ORDER (ADR-0010 C2 shape), date quoting, the blank
    # line, and the sentinel start/placeholder/end each on their own line. A reordering, a dropped
    # placeholder line, or whitespace drift would all fail here (anchors the determinism test to a
    # KNOWN-correct output, not merely run-to-run stability).
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))
    theme = _concept(wt, "curator-concurrency")
    cid = region_sentinel_id(RUN_ID, "c1")  # the run-scoped persisted id, {run_id}--c1
    expected = (
        "---\n"
        "title: Curator concurrency model\n"
        # kind is the D2 common base; `type` is its DERIVED OKF mirror (OD-3), emitted for exactly
        # the reason `description` mirrors `summary` — a consumer reading one file in isolation.
        "kind: concept\n"
        "type: concept\n"
        f"kb: {KB_ID}\n"
        "subjects:\n"
        "- ai-tech\n"
        "aliases:\n"
        "- single-curator model\n"
        "tags:\n"
        "- curator\n"
        "- concurrency\n"
        f"created: '{RUN_DATE}'\n"
        f"updated: '{RUN_DATE}'\n"
        # OKF v0.1 (ADR-0014 D2): timestamp right after updated, description right after summary;
        # NO okf_version on a concept (bundle-root index.md only).
        f"timestamp: '{RUN_DATE}T00:00:00Z'\n"
        "status: active\n"
        "summary: One curator advances the curated branch under a per-repo lock.\n"
        "description: One curator advances the curated branch under a per-repo lock.\n"
        "derived: false\n"
        "provenance:\n"
        "  writers: []\n"
        "  agents:\n"
        "  - claude-code\n"
        "sources:\n"
        f"- raw/ai-tech/{E1}.md\n"
        # The #169 D18/D20 derived Obsidian mirror, IMMEDIATELY after the list it mirrors — a
        # proper subset of `sources:` (raw/ entries only), single-quoted by the YAML emitter
        # because `[` opens a flow sequence in plain style. Never authoritative (schema §3.4).
        "source_links:\n"
        f"- '[[raw/ai-tech/{E1}.md]]'\n"
        "related: []\n"
        "confidence: high\n"
        "body_status: pending\n"
        "---\n"
        "\n"
        f"<!-- agora:body:start id={cid} -->\n"
        "_summary pending_\n"
        f"<!-- agora:body:end id={cid} -->\n"
    )
    assert theme.read_text(encoding="utf-8") == expected


def test_create_theme_no_prose_has_no_sentinels_no_body_status(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme(needs_prose=False))
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))
    theme = _concept(wt, "curator-concurrency")
    fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert "body_status" not in fm
    assert "agora:body:start" not in body


def test_create_theme_harvest_origin_stamped(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    prov = _provenance("c1", E1, source="harvest:basic-memory")
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
    theme = _concept(wt, "curator-concurrency")
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["origin"] == "harvest:basic-memory"


def test_create_theme_with_link_materializes_related(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    # A stub target so the link resolves and lint passes.
    plan = _plan(
        _create_theme(
            candidate_id="c0",
            basename="single-writer-invariant",
            title="Single-writer invariant",
            summary="One writer.",
            tags=(),
            aliases=(),
            links=(),
            event_ids=(E2,),
        ),
        _create_theme(links=("single-writer-invariant",), event_ids=(E1,)),
    )
    prov = {**_provenance("c0", E2), **_provenance("c1", E1)}
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
    theme = _concept(wt, "curator-concurrency")
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["related"] == ["[[single-writer-invariant]]"]


def test_create_theme_creates_the_subject_map_and_updates_the_index(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    # The map is created LAZILY, at the FIRST concept of its subject (ADR-0041 D1.3), and it is
    # basenamed by the subject: the `-moc` suffix was the kind marker in the FILENAME and the kind
    # is now the DIRECTORY (D5).
    moc = _map_note(wt, "ai-tech")
    assert moc.is_file()
    assert not (wt / "wiki" / "maps" / "ai-tech-moc.md").exists()
    mfm, mbody = frontmatter.parse(moc.read_text(encoding="utf-8"))
    assert mfm["kind"] == "map"
    assert mfm["type"] == "map"
    assert mfm["kb"] == KB_ID
    # A map's OWN `subjects:` is what ADR-0041 D5 reads for the ranking domain filter.
    assert mfm["subjects"] == ["ai-tech"]
    # `children:` frontmatter STAYS [[basename]] (ADR-0014 D3 / Obsidian Properties native).
    assert mfm["children"] == ["[[curator-concurrency]]"]
    # The BODY child bullet keeps the FROZEN grammar (D1.3) — only the relative path moved, since
    # the map and its children now live in SIBLING kind directories.
    assert "- [Curator concurrency model](../concepts/curator-concurrency.md)" in mbody
    # No bare wikilink survives in the map body — the body graph link is markdown-only.
    assert "[[curator-concurrency]]" not in mbody

    # index.md is the ROOT MAP: it stays at the repo root, outside wiki/ and outside maps/ (D1.2).
    index = wt / "index.md"
    assert index.is_file()
    ifm, ibody = frontmatter.parse(index.read_text(encoding="utf-8"))
    assert ifm["kind"] == "index"
    assert ifm["kb"] == KB_ID
    assert ifm["subjects"] == []  # the root map is filed under no subject (D2.2)
    assert ifm["children"] == ["[[ai-tech]]"]
    assert "- [ai-tech map](wiki/maps/ai-tech.md)" in ibody
    assert "[[ai-tech]]" not in ibody


def test_create_theme_result_passes_lint(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))
    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


@pytest.mark.parametrize(
    "title",
    [
        "Edge ] case",  # a bracket terminates _CHILD_BULLET_RE's link-text group early
        "line1\nline2",  # a newline is forbidden in the frozen link-text class
        "[fully] ]bracketed[",  # every bracket char must be sanitized out of the TEXT
    ],
)
def test_create_theme_with_breaking_title_still_round_trips_and_lints(
    tmp_path: Path, title: str
) -> None:
    # REGRESSION (ADR-0014 D3 / ADR-0010 D5 round-trip): a model-decided theme `title` may legally
    # contain `]`, `[` or a newline (all valid YAML scalars), but the FROZEN `_CHILD_BULLET_RE`
    # link-text class `[^\]\r\n]*` forbids them. Emitting such a title RAW into the MOC body bullet
    # would produce a `- [..](themes/<base>.md)` line the curator's OWN L1-6/L1-2 lint can no longer
    # parse, silently dropping the child from `child_bullets` / `body_link_basenames`. `_link_text`
    # sanitizes the TEXT (never the slug-constrained PATH), so emit->parse still recovers the
    # basename and the post-APPLY tree the curator just wrote lints clean.
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme(title=title))
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    # The map body bullet round-trips: emit->parse recovers the concept basename despite the title.
    moc = _map_note(wt, "ai-tech")
    _, mbody = frontmatter.parse(moc.read_text(encoding="utf-8"))
    assert child_bullets(mbody) == {"curator-concurrency"}
    assert body_link_basenames(mbody) == ["curator-concurrency"]

    # And the whole post-APPLY tree lints clean (L1-6 declared==body-bullets, L1-2 no broken links).
    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


def _bare_worktree(tmp_path: Path) -> Path:
    """A schema-2 repo with NO pre-seeded raw/ (the engine writes raw/ itself, ADR-0010 D3)."""
    return _schema2_repo(tmp_path)


# --- ADR-0010 D3: the engine materializes the cited raw/ free-text source -----------------------


# --- ADR-0041 D1.5: the KB identity APPLY stamps into every note --------------------------------


def test_apply_refuses_a_repo_with_no_kb_identity(tmp_path: Path) -> None:
    # `load_kb_identity` returns None for an absent `_meta/kb.yaml` because it cannot know which
    # schema its caller is on. APPLY *is* the schema-2 write path, so for it None is a BROKEN repo:
    # writing anyway would produce notes L1-4 rejects on `kb:` (a whole run discarded at the lint
    # gate) or, worse, notes that name no origin once copied out — the one thing D1.5 exists for.
    layout = RepoLayout(tmp_path)
    emit_schema(layout, taxonomy=TAXONOMY)  # taxonomy + schema doc, but NO _meta/kb.yaml
    with pytest.raises(ApplyError, match="kb.yaml"):
        apply_plan(
            _plan(_create_theme()),
            worktree=tmp_path,
            run_date=RUN_DATE,
            provenance=_provenance("c1", E1),
        )
    assert not (tmp_path / "wiki" / "concepts").exists(), "refused BEFORE any note was written"


def test_drop_only_plan_needs_no_kb_identity(tmp_path: Path) -> None:
    # The identity is resolved once per run and ONLY when the plan materializes something. A plan
    # of pure DROP/NOOP writes no note, so it has no `kb:` to stamp and must not be failed over a
    # file it never needed — the refusal is scoped to the write, not to the call.
    layout = RepoLayout(tmp_path)
    emit_schema(layout, taxonomy=TAXONOMY)
    plan = _plan(
        Disposition(candidate_id="c1", event_ids=(E1,), op="DROP", reason="noise"),
        Disposition(candidate_id="c2", event_ids=(E2,), op="NOOP", reason="dup"),
    )
    assert (
        apply_plan(plan, worktree=tmp_path, run_date=RUN_DATE, provenance=_provenance("c1", E1))
        == {}
    )


# --- ADR-0041 D2.2: the subject left the path, so nothing needs one to have a path --------------


def test_concept_without_a_domain_is_filed_with_empty_subjects(tmp_path: Path) -> None:
    # ADR-0022's `domains[0]` catch-all existed because a v1 note needed a PATH and the path needed
    # a domain, so an unclassifiable fact had to be given a possibly-FALSE subject to land at all.
    # Schema 2 splits the two legs: the concept lands at `wiki/concepts/<slug>.md` regardless, and
    # `subjects: []` asserts nothing while losing nothing (D2.2 legs 1 + 2). This is the test that
    # fails if a future edit "restores" the domain precondition and starts dropping such facts.
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme(domain=None))
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    theme = _concept(wt, "curator-concurrency")
    assert theme.is_file()
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["subjects"] == []
    # No subject means no map to be a child of — an L2 orphan health signal, never a lost fact.
    assert not (wt / "wiki" / "maps").exists()


def test_maps_are_lazy_per_subject_and_the_index_lists_every_map(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(
        _create_theme(),
        _create_theme(
            candidate_id="c2",
            domain="economy",
            basename="cqrs",
            title="CQRS",
            summary="Command/query split.",
            tags=("architecture",),
            aliases=(),
            event_ids=(E2,),
        ),
    )
    prov = {**_provenance("c1", E1, body="a"), **_provenance("c2", E2, body="b")}
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)

    # One map per subject that gained a concept — and none for the declared-but-unused `general`.
    assert sorted(p.name for p in (wt / "wiki" / "maps").glob("*.md")) == [
        "ai-tech.md",
        "economy.md",
    ]
    # A map lists ONLY the concepts whose own `subjects:` name it (D3.2 — frontmatter, not path).
    ai_fm, _ = frontmatter.parse(_map_note(wt, "ai-tech").read_text(encoding="utf-8"))
    ec_fm, _ = frontmatter.parse(_map_note(wt, "economy").read_text(encoding="utf-8"))
    assert ai_fm["children"] == ["[[curator-concurrency]]"]
    assert ec_fm["children"] == ["[[cqrs]]"]

    ifm, ibody = frontmatter.parse((wt / "index.md").read_text(encoding="utf-8"))
    assert ifm["children"] == ["[[ai-tech]]", "[[economy]]"]
    assert "- [ai-tech map](wiki/maps/ai-tech.md)" in ibody
    assert "- [economy map](wiki/maps/economy.md)" in ibody

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


def test_a_concept_in_a_free_subfolder_is_still_a_map_child(tmp_path: Path) -> None:
    # ADR-0041 D1.1: a note may sit at any depth under its kind directory and NO code reads the
    # intermediate segments — a human organising by folder in Obsidian keeps doing so. Membership
    # is read from `subjects:`, so the hand-filed note is a first-class child and its bullet
    # carries the deep relative path while the BASENAME still resolves (ADR-0010 D5).
    wt = _worktree(tmp_path)
    # Filed by a HUMAN, three segments deep. Seeded through the same helper the MERGE targets use,
    # then moved: the note's bytes are a normal schema-2 concept — only its location is unusual.
    _seed_theme(wt, "handbook", sources=[f"raw/ai-tech/{E3}.md"])
    deep = wt / "wiki" / "concepts" / "engineering" / "team" / "handbook.md"
    deep.parent.mkdir(parents=True, exist_ok=True)
    _concept(wt, "handbook").rename(deep)

    apply_plan(
        _plan(_create_theme()), worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1)
    )

    mfm, mbody = frontmatter.parse(_map_note(wt, "ai-tech").read_text(encoding="utf-8"))
    assert mfm["children"] == ["[[curator-concurrency]]", "[[handbook]]"]
    assert "- [handbook](../concepts/engineering/team/handbook.md)" in mbody
    assert child_bullets(mbody) == {"curator-concurrency", "handbook"}

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


def test_create_theme_materializes_cited_raw_source_from_body(tmp_path: Path) -> None:
    # The deterministic engine (APPLY) — never the model — persists the free-text capture at the
    # cited raw/<domain>/<event_id>.md from the provenance tuple's immutable body (ADR-0010 D3), so
    # the curated commit holds raw/ + wiki/ consistently and lint L1-8 passes. raw/ is NOT
    # pre-seeded here: APPLY must create it.
    wt = _bare_worktree(tmp_path)
    plan = _plan(_create_theme())
    body = "One curator advances the branch under a per-repo lock."
    apply_plan(
        plan,
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("c1", E1, body=body),
    )

    theme = _concept(wt, "curator-concurrency")
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    # The theme cites the engine-materialized raw/ path.
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md"]
    # The raw/ artifact was WRITTEN at exactly that path, with the immutable body content.
    raw = wt / "raw" / "ai-tech" / f"{E1}.md"
    assert raw.is_file()
    assert raw.read_text(encoding="utf-8") == body
    # The post-APPLY tree (incl. the materialized raw/) lints clean — L1-8 is satisfied.
    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


def test_materialized_raw_source_is_written_and_recorded_as_exact_bytes(tmp_path: Path) -> None:
    # _materialize_raw_source writes BYTES, not text (write_bytes, never write_text): text mode
    # would translate "\n" to os.linesep on write, so on a platform where that differs from "\n"
    # the file on disk would NOT equal the bytes the §4.0 final-diff gate compares against in
    # `raw_writes` (#85) — a mismatch that cannot be provoked on POSIX (os.linesep == "\n" there),
    # so this asserts the CONTRACT directly rather than relying on a platform-specific repro.
    wt = _bare_worktree(tmp_path)
    plan = _plan(_create_theme())
    body = "line one\nline two\n"
    raw_writes = apply_plan(
        plan,
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("c1", E1, body=body),
    )
    ref = f"raw/ai-tech/{E1}.md"
    assert raw_writes[ref] == (wt / ref).read_bytes() == body.encode("utf-8")


def test_raw_source_is_immutable_not_overwritten(tmp_path: Path) -> None:
    # The raw/ source is immutable: APPLY writes it ONCE and NEVER overwrites a pre-existing file
    # (ADR-0010 D3). A pre-seeded raw/ artifact keeps its original content even when the provenance
    # body differs (e.g. a re-run or a cross-domain merge citing the same ref).
    wt = _bare_worktree(tmp_path)
    raw = wt / "raw" / "ai-tech" / f"{E1}.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("original immutable capture\n", encoding="utf-8")

    plan = _plan(_create_theme())
    apply_plan(
        plan,
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("c1", E1, body="a DIFFERENT body that must not clobber"),
    )
    assert raw.read_text(encoding="utf-8") == "original immutable capture\n"


def test_upload_raw_ref_tuple_cites_ref_and_is_not_written(tmp_path: Path) -> None:
    # A tuple WITH a raw_ref is an UPLOAD already persisted by core.ingest at capture time: APPLY
    # cites that ref verbatim and does NOT (re)write it (only free-text captures without a raw_ref
    # are materialized from the body, ADR-0010 D3). The cited upload path is NOT created by APPLY.
    wt = _bare_worktree(tmp_path)
    upload_ref = "raw/ai-tech/2026-06-13-uploaded-doc.md"
    plan = _plan(_create_theme())
    apply_plan(
        plan,
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("c1", E1, raw_ref=upload_ref, body="ignored when raw_ref present"),
    )

    theme = _concept(wt, "curator-concurrency")
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    # Cites the upload ref, NOT the event-id free-text path.
    assert fm["sources"] == [upload_ref]
    # APPLY did NOT write the upload (core.ingest owns uploads) nor a free-text raw/<event_id>.md.
    assert not (wt / upload_ref).exists()
    assert not (wt / "raw" / "ai-tech" / f"{E1}.md").exists()


def test_bodyless_provenance_cites_path_but_skips_write(tmp_path: Path) -> None:
    # A tuple with neither raw_ref nor body (a hand-authored unit-test fixture) keeps today's
    # behavior: cite the raw/<domain>/<event_id>.md path but skip the file write. raw/ is NOT
    # pre-seeded, so the path is cited but no file is created (the engine has no body to persist).
    wt = _bare_worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    theme = _concept(wt, "curator-concurrency")
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md"]  # path still cited
    assert not (wt / "raw" / "ai-tech" / f"{E1}.md").exists()  # but no file written


def test_two_concepts_one_subject_map_lists_both_sorted(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(
        _create_theme(),
        _create_theme(
            candidate_id="c2",
            basename="cqrs",
            title="CQRS",
            summary="Command/query split.",
            tags=("architecture",),
            aliases=(),
            event_ids=(E2,),
        ),
    )
    prov = {**_provenance("c1", E1), **_provenance("c2", E2)}
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
    moc = _map_note(wt, "ai-tech")
    mfm, _ = frontmatter.parse(moc.read_text(encoding="utf-8"))
    assert mfm["children"] == ["[[cqrs]]", "[[curator-concurrency]]"]
    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


# --- APPEND_DAILY -------------------------------------------------------------------------------


def _append_daily(**overrides: object) -> Disposition:
    # ADR-0041 D2.6: the journal is basenamed by its run_date, repo-wide. APPLY composes BOTH the
    # `<yyyy>/<mm>` shard and the basename from the injected run_date and never parses either back
    # out of `disp.basename` — the PLAN gate is what asserts `basename == run_date`, so the value
    # here is the plan-shaped one, not an input APPLY trusts.
    base: dict[str, object] = {
        "candidate_id": "d1",
        "event_ids": (E1,),
        "op": "APPEND_DAILY",
        "domain": "ai-tech",
        "basename": RUN_DATE,
        "title": f"Daily {RUN_DATE}",
        "summary": "Daily consolidation.",
        "status": "active",
        "tags": (),
        "needs_prose": True,
        "reason": "Capture.",
    }
    base.update(overrides)
    return Disposition(**base)


def test_append_daily_creates_dated_section(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    plan = _plan(_append_daily())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("d1", E1))
    daily = _journal(wt)
    assert daily.is_file()
    fm, body = frontmatter.parse(daily.read_text(encoding="utf-8"))
    assert fm["kind"] == "note"
    assert fm["type"] == "note"
    assert fm["kb"] == KB_ID
    assert fm["subjects"] == ["ai-tech"]
    assert str(fm["date"]) == RUN_DATE
    assert fm["run_id"] == RUN_ID
    assert f"## {RUN_DATE}" in body
    start, end = body_sentinels(region_sentinel_id(RUN_ID, "d1"))
    assert start in body and end in body


def test_two_daily_dispositions_one_file_stable_order(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    # d2's first event (E3) sorts AFTER d1's (E1); §3.1 orders sections by first event_id.
    plan = _plan(
        _append_daily(candidate_id="d2", event_ids=(E3,), summary="second"),
        _append_daily(candidate_id="d1", event_ids=(E1,), summary="first"),
    )
    prov = {**_provenance("d1", E1), **_provenance("d2", E3)}
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
    daily = _journal(wt)
    fm, body = frontmatter.parse(daily.read_text(encoding="utf-8"))
    # Both sentinel regions present; d1's section appears before d2's (E1 < E3).
    start_d1, _ = body_sentinels(region_sentinel_id(RUN_ID, "d1"))
    start_d2, _ = body_sentinels(region_sentinel_id(RUN_ID, "d2"))
    assert body.index(start_d1) < body.index(start_d2)
    # sources unioned across both dispositions.
    assert f"raw/ai-tech/{E1}.md" in fm["sources"]
    assert f"raw/ai-tech/{E3}.md" in fm["sources"]


def test_append_daily_cross_run_restamps_run_id_to_the_touching_run(tmp_path: Path) -> None:
    """A journal a LATER run appends to carries THAT run's ``run_id``, not the first run's.

    The append branch used to preserve the prior value, and under D2.6 that is unpublishable: one
    journal per ``run_date`` means a second ``agora curate`` the same day appends to the file the
    first one wrote, and lint L1-14 then fails the second run on ``run_id ... != injected run_id
    ...`` -- over the very note it just appended to. ``run_id:`` on a note several runs may touch
    reads "the last run that touched this journal"; everything else (prior region, unioned
    sources, bumped ``updated``) is preserved exactly as before.
    """
    wt = _worktree(tmp_path)
    daily = _journal(wt)
    daily.parent.mkdir(parents=True, exist_ok=True)
    prior_start, prior_end = body_sentinels("d0")
    prior_fm = _prior_journal_fm()
    prior_body = f"## {RUN_DATE}\n\n{prior_start}\nprior prose\n{prior_end}"
    daily.write_text(frontmatter.render(prior_fm, prior_body), encoding="utf-8")

    apply_plan(
        _plan(_append_daily(candidate_id="d1", event_ids=(E2,))),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("d1", E2),
    )
    fm, body = frontmatter.parse(daily.read_text(encoding="utf-8"))
    assert fm["run_id"] == RUN_ID, "re-stamped to the run that touched it (L1-14 grades this)"
    assert str(fm["date"]) == RUN_DATE
    assert str(fm["updated"]) == RUN_DATE  # bumped
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]  # unioned
    assert prior_start in body and prior_end in body  # prior region preserved
    new_start, _ = body_sentinels(region_sentinel_id(RUN_ID, "d1"))
    assert new_start in body  # new region appended


def test_append_daily_no_prose_places_no_region_and_no_flag(tmp_path: Path) -> None:
    """#131: a disposition nobody will author must not leave a region nobody will fill.

    APPLY placed the region unconditionally while ``worker._needs_prose_map`` skips
    ``needs_prose=False`` dispositions, so the region was built and then orphaned — a permanent
    ``_summary pending_`` in a published note. The region, the dated heading and the
    ``body_status`` stamp are now ONE decision.
    """
    wt = _worktree(tmp_path)
    plan = _plan(_append_daily(needs_prose=False))
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("d1", E1))

    daily = _journal(wt)
    assert daily.is_file(), "the daily is still created — only its body section is withheld"
    fm, body = frontmatter.parse(daily.read_text(encoding="utf-8"))

    assert "body_status" not in fm
    assert "agora:body" not in body, "no sentinel region"
    assert f"## {RUN_DATE}" not in body, "the dated heading is the section's first line, not a peer"
    assert body.strip() == ""
    # The bytes are the shape _apply_create_theme already writes for a no-prose theme.
    assert daily.read_text(encoding="utf-8").endswith("---\n\n\n")

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


def test_append_daily_no_prose_still_records_provenance(tmp_path: Path) -> None:
    """The PROVENANCE half of the op is unconditional — this is not a silent DROP.

    ``_sources_union`` sits OUTSIDE the ``needs_prose`` gate on purpose: withholding the section
    must never discard the capture. If a future edit "simplifies" a no-prose APPEND_DAILY into a
    DROP, this is the test that fails.
    """
    wt = _worktree(tmp_path)
    plan = _plan(_append_daily(needs_prose=False))
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("d1", E1))

    daily = _journal(wt)
    fm, _ = frontmatter.parse(daily.read_text(encoding="utf-8"))
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md"]
    assert (wt / "raw" / "ai-tech" / f"{E1}.md").is_file()


def test_append_daily_no_prose_append_branch_preserves_the_body_byte_for_byte(
    tmp_path: Path,
) -> None:
    """The append-to-existing branch: a provenance-only append must not touch prior prose.

    Also the trailing-whitespace answer the issue asked for — with no section there is nothing to
    join, so ``prior_body`` is carried through unchanged rather than gaining a blank separator.
    """
    wt = _worktree(tmp_path)
    daily = _journal(wt)
    daily.parent.mkdir(parents=True, exist_ok=True)
    prior_start, prior_end = body_sentinels("d0")
    prior_fm = _prior_journal_fm()
    prior_body = f"## {RUN_DATE}\n\n{prior_start}\nprior prose\n{prior_end}"
    daily.write_text(frontmatter.render(prior_fm, prior_body), encoding="utf-8")

    apply_plan(
        _plan(_append_daily(candidate_id="d1", event_ids=(E2,), needs_prose=False)),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("d1", E2),
    )
    fm, body = frontmatter.parse(daily.read_text(encoding="utf-8"))

    assert body == prior_body, "the existing body is carried through untouched"
    assert "body_status" not in fm, "no flag for a section that was never placed"
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]  # still unioned
    assert str(fm["updated"]) == RUN_DATE  # still bumped
    assert fm["run_id"] == RUN_ID  # still re-stamped to the touching run


def test_append_daily_with_prose_bytes_are_unchanged(tmp_path: Path) -> None:
    """The half that must NOT move: pin the ``needs_prose=True`` bytes exactly (#131 lands
    before the ``v0.1.0b1`` tag precisely because it touches APPLY's output shape)."""
    wt = _worktree(tmp_path)
    apply_plan(
        _plan(_append_daily()),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("d1", E1),
    )
    daily = _journal(wt)
    start, end = body_sentinels(region_sentinel_id(RUN_ID, "d1"))
    _, body = frontmatter.parse(daily.read_text(encoding="utf-8"))
    assert body == f"## {RUN_DATE} \u00b7 ai-tech\n\n{start}\n_summary pending_\n{end}"


def test_one_journal_per_run_date_collects_every_domain(tmp_path: Path) -> None:
    # ADR-0041 D2.6: v1 wrote one daily PER DOMAIN and namespaced the basename `<domain>-DATE`
    # because bare dates would collide across domains. With the domain out of the path that reason
    # is gone, so both dispositions land in ONE file — sections in DOMAIN order (the outer,
    # human-legible key now that the journal collects several), `sources:` unioned, and `subjects:`
    # unioned, which makes the journal the one note schema 2 genuinely makes multi-subject.
    wt = _worktree(tmp_path)
    plan = _plan(
        # `economy` carries the EARLIER event id, so a section order keyed only on the §3.1
        # manifest-event tiebreak would put it first. Domain order is what decides.
        _append_daily(candidate_id="d2", domain="economy", event_ids=(E1,), summary="second"),
        _append_daily(candidate_id="d1", domain="ai-tech", event_ids=(E2,), summary="first"),
    )
    prov = {**_provenance("d1", E2, body="ai"), **_provenance("d2", E1, body="ec")}
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)

    assert list((wt / "wiki" / "notes").rglob("*.md")) == [_journal(wt)]
    fm, body = frontmatter.parse(_journal(wt).read_text(encoding="utf-8"))
    assert fm["subjects"] == ["ai-tech", "economy"]
    assert fm["sources"] == [f"raw/ai-tech/{E2}.md", f"raw/economy/{E1}.md"]
    start_ai, _ = body_sentinels(region_sentinel_id(RUN_ID, "d1"))
    start_ec, _ = body_sentinels(region_sentinel_id(RUN_ID, "d2"))
    assert body.index(start_ai) < body.index(start_ec)

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


# --- MERGE_INTO_THEME ---------------------------------------------------------------------------


def _prior_journal_fm(
    *, date: str = RUN_DATE, title: str | None = None, run_id: str = "prior-run"
) -> dict[str, object]:
    """The schema-2 ``kind: note`` frontmatter of a journal a PRIOR run already committed."""
    return {
        "title": title if title is not None else f"Daily {date}",
        "kind": "note",
        "type": "note",
        "kb": KB_ID,
        "subjects": ["ai-tech"],
        "aliases": [],
        "tags": [],
        "created": date,
        "updated": date,
        "status": "active",
        "summary": "prior consolidation",
        "derived": False,
        "provenance": {"writers": [], "agents": []},
        "date": date,
        "run_id": run_id,
        "sources": [f"raw/ai-tech/{E1}.md"],
    }


def _seed_theme(wt: Path, basename: str, *, sources: list[str], body: str = "seeded") -> None:
    """Seed an EXISTING schema-2 concept — a MERGE_INTO_THEME / MARK_CONTESTED target."""
    fm = {
        "title": basename,
        "kind": "concept",
        "type": "concept",
        "kb": KB_ID,
        "subjects": ["ai-tech"],
        "aliases": [],
        "tags": ["architecture"],
        "created": RUN_DATE,
        "updated": RUN_DATE,
        "status": "active",
        "summary": "seed",
        "derived": False,
        "provenance": {"writers": [], "agents": []},
        "sources": sources,
        "related": [],
        "confidence": "high",
    }
    path = _concept(wt, basename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.render(fm, body), encoding="utf-8")


def test_merge_unions_sources_and_appends_sub_region(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="Existing CQRS prose.")
    disp = Disposition(
        candidate_id="m1",
        event_ids=(E2,),
        op="MERGE_INTO_THEME",
        target_basename="cqrs",
        summary="Adds flock detail.",
        status="active",
        links=(),
        needs_prose=True,
        reason="Overlaps cqrs.",
    )
    apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2))
    theme = _concept(wt, "cqrs")
    fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
    # sources unioned (prior kept, new added).
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]
    # prior prose preserved; a NEW sentinel sub-region appended below it.
    assert "Existing CQRS prose." in body
    start, _ = body_sentinels(region_sentinel_id(RUN_ID, "m1"))
    assert start in body
    assert body.index("Existing CQRS prose.") < body.index(start)


def test_merge_no_prose_only_unions_sources(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="Existing prose.")
    disp = Disposition(
        candidate_id="m1",
        event_ids=(E2,),
        op="MERGE_INTO_THEME",
        target_basename="cqrs",
        summary="corroborate",
        needs_prose=False,
        reason="corroborate",
    )
    apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2))
    theme = _concept(wt, "cqrs")
    fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]
    assert "agora:body:start" not in body  # no augmentation region when no prose


def test_merge_harvest_candidate_stamps_origin(tmp_path: Path) -> None:
    # MERGE_INTO_THEME is the only op a gated/harvested candidate may use to ADD content (§6), so a
    # harvested merge MUST tag origin: harvest:<agent> for loop-prevention (DATA-MODEL §7).
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="Existing prose.")
    disp = Disposition(
        candidate_id="m1",
        event_ids=(E2,),
        op="MERGE_INTO_THEME",
        target_basename="cqrs",
        summary="corroborate",
        needs_prose=False,
        reason="corroborate",
    )
    prov = _provenance("m1", E2, source="harvest:basic-memory")
    apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=prov)
    theme = _concept(wt, "cqrs")
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["origin"] == "harvest:basic-memory"


def test_merge_non_harvest_leaves_origin_untouched(tmp_path: Path) -> None:
    # A non-harvest merge must NOT add an origin tag (origin is present iff a provenance source is
    # harvest:<agent>, ADR-0010); the seed theme has none, so none should appear.
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="Existing prose.")
    disp = Disposition(
        candidate_id="m1",
        event_ids=(E2,),
        op="MERGE_INTO_THEME",
        target_basename="cqrs",
        summary="corroborate",
        needs_prose=False,
        reason="corroborate",
    )
    apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2))
    theme = _concept(wt, "cqrs")
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert "origin" not in fm


def _merge_disp(**overrides: object) -> Disposition:
    base: dict[str, object] = {
        "candidate_id": "m1",
        "event_ids": (E2,),
        "op": "MERGE_INTO_THEME",
        "domain": "ai-tech",
        "target_basename": "cqrs",
        "summary": "corroborate",
        "needs_prose": False,
        "reason": "corroborate",
    }
    base.update(overrides)
    return Disposition(**base)


def test_merge_backfills_the_schema2_base_on_a_note_that_lacks_it(tmp_path: Path) -> None:
    # Every note APPLY TOUCHES must leave the touch carrying schema 2 — otherwise a merge into a
    # note an importer or a human wrote produces a note L1-4 rejects for a missing `kind:`/`kb:`,
    # i.e. a whole run discarded at the gate over a file the curator itself just wrote.
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"])
    bare = _concept(wt, "cqrs")
    fm, body = frontmatter.parse(bare.read_text(encoding="utf-8"))
    for key in ("kind", "kb", "subjects", "derived", "provenance"):
        fm.pop(key)
    fm["type"] = "theme"  # a leftover v1 value: RETIRED as the kind authority, so it is re-mirrored
    bare.write_text(frontmatter.render(fm, body), encoding="utf-8")

    apply_plan(
        _plan(_merge_disp()), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2)
    )
    fm, _ = frontmatter.parse(bare.read_text(encoding="utf-8"))
    assert fm["kind"] == "concept"  # from the DIRECTORY, which is authoritative (D2.1)
    assert fm["type"] == "concept"
    assert fm["kb"] == KB_ID
    assert fm["subjects"] == ["ai-tech"]  # seeded from the disposition's singular domain (OD-9)
    assert fm["derived"] is False
    assert fm["provenance"] == {"writers": [], "agents": ["claude-code"]}

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


def test_merge_never_re_identifies_a_note_that_already_names_a_kb(tmp_path: Path) -> None:
    # `kb:` is BACKFILL-only. A note that already names a knowledge base names the one it came
    # FROM — D1.5's whole point is that "a note copied out still names its origin" — so silently
    # re-stamping it would erase exactly the fact the field exists to carry.
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"])
    foreign = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    path = _concept(wt, "cqrs")
    fm, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    fm["kb"] = foreign
    path.write_text(frontmatter.render(fm, body), encoding="utf-8")

    apply_plan(
        _plan(_merge_disp()), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2)
    )
    fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
    assert fm["kb"] == foreign


def test_merge_does_not_re_file_a_concept_under_a_second_subject(tmp_path: Path) -> None:
    # D2.2: "a curator run writes at most one subject". 0..n subjects are an APPLY-and-human
    # capability, NOT a per-merge side effect — a merge of an `economy` candidate into a concept
    # already filed under `ai-tech` records the claim without quietly re-filing the note.
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"])
    apply_plan(
        _plan(_merge_disp(domain="economy")),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("m1", E2),
    )
    fm, _ = frontmatter.parse(_concept(wt, "cqrs").read_text(encoding="utf-8"))
    assert fm["subjects"] == ["ai-tech"]
    # The raw/ SHARD KEY comes from the TARGET's own subjects (D2.2 leg 3 / D3.2), which is the
    # schema-2 replacement for v1's "read the domain out of the target's path".
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]


def test_merge_into_a_summary_mirrors_the_summary_kind_not_a_hard_coded_concept(
    tmp_path: Path,
) -> None:
    # MERGE/CONTEST resolve among the two SOURCED kinds (ADR-0041 D2 gives `summary` the same
    # `sources:`/`related:`/`confidence:` shape as `concept`). The `kind:` APPLY stamps must
    # therefore come from the resolved DIRECTORY, which is authoritative (D2.1) — a hard-coded
    # `concept` would write `kind: concept` into `wiki/summaries/` and hard-fail L1-11 on a note
    # the curator itself had just written. `wiki/summaries/` ships EMPTY under OD-7, so this is the
    # test that keeps the tier correct before it has a producer to notice with.
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"])
    summary = wt / "wiki" / "summaries" / "cqrs.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    _concept(wt, "cqrs").rename(summary)

    apply_plan(
        _plan(_merge_disp()), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2)
    )
    fm, _ = frontmatter.parse(summary.read_text(encoding="utf-8"))
    assert fm["kind"] == "summary"
    assert fm["type"] == "summary"
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


def test_merge_unions_self_declared_agents_never_dropping_a_prior_one(tmp_path: Path) -> None:
    # D2.3: `agents` is RECORDED, never trusted — and, like `sources:`/`related:`, it is a set
    # UNION, so a note keeps every agent that ever contributed to it.
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"])
    path = _concept(wt, "cqrs")
    fm, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    fm["provenance"] = {"writers": ["dochan"], "agents": ["codex"]}
    path.write_text(frontmatter.render(fm, body), encoding="utf-8")

    apply_plan(
        _plan(_merge_disp()),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("m1", E2, source="harvest:basic-memory"),
    )
    fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
    assert fm["provenance"] == {
        "writers": ["dochan"],
        "agents": ["codex", "harvest:basic-memory"],
    }


def test_merge_rejects_journal_target(tmp_path: Path) -> None:
    # MERGE_INTO_THEME is scoped to the SOURCED kinds (concept/summary, ADR-0041 D2); a
    # target_basename resolving to a `kind: note` journal must raise rather than mutate it (the
    # §4.1 BASENAME check only verifies existence). The kind is read from the DIRECTORY, which is
    # authoritative (D2.1) — so a `kind:` mirror that LIES cannot talk the refusal out of it.
    wt = _worktree(tmp_path)
    daily = _journal(wt, "2026-06-12")
    daily.parent.mkdir(parents=True, exist_ok=True)
    fm = _prior_journal_fm(date="2026-06-12", title="Daily", run_id=RUN_ID)
    fm["kind"] = "concept"  # the lie the directory overrules
    daily.write_text(frontmatter.render(fm, "## 2026-06-12"), encoding="utf-8")
    disp = Disposition(
        candidate_id="m1",
        event_ids=(E2,),
        op="MERGE_INTO_THEME",
        target_basename="2026-06-12",
        summary="merge",
        needs_prose=False,
        reason="merge",
    )
    with pytest.raises(ApplyError):
        apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2))


def test_create_theme_confidence_mirrors_candidate(tmp_path: Path) -> None:
    # confidence is MIRRORED from the candidate's worst-case value (ADR-0011 §2), NOT a literal
    # 'high'. A low-confidence candidate must materialize confidence: low so lint/dashboard surface
    # it (§6); the model can never inflate it because it is not a plan field.
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(
        plan,
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("c1", E1),
        confidence={"c1": "low"},
    )
    theme = _concept(wt, "curator-concurrency")
    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["confidence"] == "low"


# --- MARK_CONTESTED -----------------------------------------------------------------------------


def test_mark_contested_renders_callout_and_frontmatter(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="The original CQRS claim.")
    # A competing note must exist for the [[competing]] link to resolve (lint L1-2).
    _seed_theme(wt, "event-sourcing", sources=[f"raw/ai-tech/{E3}.md"], body="Alt claim.")
    disp = Disposition(
        candidate_id="x1",
        event_ids=(E2,),
        op="MARK_CONTESTED",
        target_basename="cqrs",
        summary="Curator uses two writers, not one.",
        links=("event-sourcing",),
        needs_prose=False,
        reason="Contradiction.",
    )
    apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("x1", E2))
    theme = _concept(wt, "cqrs")
    fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))

    # §2.1 frontmatter shape.
    assert fm["status"] == "contested"
    assert fm["contested_by"] == ["event-sourcing"]
    assert str(fm["contested_at"]) == RUN_DATE
    # >=2 sources (prior + new), kept BOTH.
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]
    # §2.1 callout: assert the EXACT contiguous 3-line block (not just substrings), pinning the
    # recorded-date line, the verbatim claim line, and the competing-link + sources line byte-exact.
    expected_block = (
        f"> [!contested] Competing claim (recorded {RUN_DATE})\n"
        "> Curator uses two writers, not one.\n"
        f"> — see [[event-sourcing]] · sources: raw/ai-tech/{E2}.md"
    )
    assert expected_block in body
    # prior prose preserved, callout appended below it.
    assert "The original CQRS claim." in body
    assert body.index("The original CQRS claim.") < body.index(expected_block)

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f for f in result.findings]


def test_mark_contested_empty_links_raises(tmp_path: Path) -> None:
    # A MARK_CONTESTED with empty links carries no competing basename: contested_by would be empty
    # (un-publishable per lint L1-10) and the callout would self-reference the target. APPLY treats
    # this as a precondition violation and raises rather than fabricating a self-link.
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="The original claim.")
    disp = Disposition(
        candidate_id="x1",
        event_ids=(E2,),
        op="MARK_CONTESTED",
        target_basename="cqrs",
        summary="Contradicts.",
        links=(),
        needs_prose=False,
        reason="Contradiction.",
    )
    with pytest.raises(ApplyError):
        apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("x1", E2))


# --- #169 D18/D20: the derived `source_links:` mirror -------------------------------------------
#
# `sources:` stays the provenance of record (schema §3.4); `source_links:` is APPLY's DERIVED
# rendering of its `raw/` half in the one syntax Obsidian links from inside a list property. Three
# stamping sites (CREATE_THEME · MERGE_INTO_THEME · MARK_CONTESTED — the CLAIM-BEARING kinds, D20),
# two deliberately-unmirrored journal sites, and one relation that makes the key derivable rather
# than authoritative: the mirror is always a SUBSET of `sources:`, never a peer of it.
#
# The CREATE site's exact bytes are pinned by `test_create_theme_produces_exact_bytes` above (the
# whole-file golden), so the byte asserts here cover the two RE-STAMP sites, which are the ones
# that have to place the key correctly in a frontmatter block they did not compose.

#: The mirror's rendered form, single-quoted by the YAML emitter because `[` opens a flow sequence.
_MIRROR_E1 = f"- '[[raw/ai-tech/{E1}.md]]'\n"
_MIRROR_E2 = f"- '[[raw/ai-tech/{E2}.md]]'\n"


def _mirror_of(fm: dict[str, object]) -> list[str]:
    value = fm.get("source_links")
    assert isinstance(value, list), f"source_links is {value!r}, not a list"
    return [v for v in value if isinstance(v, str)]


def test_merge_stamps_the_mirror_onto_a_target_that_had_none(tmp_path: Path) -> None:
    """A note that PREDATES the mirror gains one the next time a merge touches its `sources:`.

    This is why the wiring is a RE-STAMP at the union site rather than a one-shot at creation: every
    concept in an existing KB was written before this key existed, and a mirror that only ever
    appeared on brand-new notes would leave the whole corpus unlinkable in Obsidian forever.
    """
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="Existing CQRS prose.")
    seeded, _ = frontmatter.parse(_concept(wt, "cqrs").read_text(encoding="utf-8"))
    assert "source_links" not in seeded, "the fixture must start WITHOUT a mirror"

    apply_plan(
        _plan(_merge_disp()), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2)
    )

    text = _concept(wt, "cqrs").read_text(encoding="utf-8")
    # EXACT bytes: the mirror follows the unioned `sources:` immediately and in the same order, so
    # the YAML diff of a run that adds one source is one contiguous hunk.
    assert (
        "sources:\n"
        f"- raw/ai-tech/{E1}.md\n"
        f"- raw/ai-tech/{E2}.md\n"
        "source_links:\n" + _MIRROR_E1 + _MIRROR_E2 + "related: []\n"
    ) in text


def test_contested_restamps_a_stale_mirror_and_restores_its_place(tmp_path: Path) -> None:
    """A pre-existing mirror is REPLACED (never extended) and moved back beside `sources:`.

    The stale value here is written LAST, which is where a plain `fm["source_links"] = …` would
    leave it and where a hand edit typically puts it. Both halves are the point: a mirror that is
    appended to rather than recomputed would keep citing an artefact the note no longer sources.
    """
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="The original CQRS claim.")
    # A competing note must exist for the [[competing]] link to resolve (lint L1-2).
    _seed_theme(wt, "event-sourcing", sources=[f"raw/ai-tech/{E3}.md"], body="Alt claim.")
    theme = _concept(wt, "cqrs")
    fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
    fm["source_links"] = ["[[raw/ai-tech/no-longer-sourced.md]]"]
    theme.write_text(frontmatter.render(fm, body), encoding="utf-8")
    assert theme.read_text(encoding="utf-8").index("source_links:") > theme.read_text(
        encoding="utf-8"
    ).index("confidence:"), "the stale mirror starts at the END of the block"

    disp = Disposition(
        candidate_id="x1",
        event_ids=(E2,),
        op="MARK_CONTESTED",
        target_basename="cqrs",
        summary="Curator uses two writers, not one.",
        links=("event-sourcing",),
        needs_prose=False,
        reason="Contradiction.",
    )
    apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("x1", E2))

    text = theme.read_text(encoding="utf-8")
    assert "no-longer-sourced" not in text  # replaced, not extended
    assert (
        "sources:\n"
        f"- raw/ai-tech/{E1}.md\n"
        f"- raw/ai-tech/{E2}.md\n"
        "source_links:\n" + _MIRROR_E1 + _MIRROR_E2 + "related: []\n"
    ) in text


def test_the_mirror_is_a_strict_subset_of_sources(tmp_path: Path) -> None:
    """Only `raw/` entries are mirrored — the blob half of a capture included, a non-path excluded.

    Not every `sources:` string is a repo path: `core.gold` still branches on a `harvest:<agent>`
    shape that resolves nowhere, and lint L1-8 (a bare `exists()`) has never adjudicated which of
    the two is stale (#169 R-9). The mirror takes no position on that — it declines to wrap a
    non-path in `[[ ]]`, which is also what keeps it a PROPER subset of the record it mirrors.
    """
    wt = _worktree(tmp_path)
    blob_rel = "raw/_blob/ab/" + "ab" * 32 + ".pdf"
    blob = wt / blob_rel
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"%PDF-1.7 fixture")
    prov = {
        "c1": [
            # (1) a free-text capture -> raw/<domain>/<event_id>.md, materialized by APPLY;
            {"event_id": E1, "source": "claude-code", "writer": "d", "body": "capture"},
            # (2) the BYTES half of the same kind of capture -> raw/_blob/<ab>/<sha>.<ext>;
            {"event_id": E2, "source": "claude-code", "writer": "d", "raw_ref": blob_rel},
            # (3) a non-path citation shape -> mirrored by NOTHING.
            {"event_id": E3, "source": "claude-code", "writer": "d", "raw_ref": "harvest:bm/f-1"},
        ]
    }
    apply_plan(_plan(_create_theme()), worktree=wt, run_date=RUN_DATE, provenance=prov)

    fm, _ = frontmatter.parse(_concept(wt, "curator-concurrency").read_text(encoding="utf-8"))
    sources = fm["sources"]
    assert sources == [f"raw/ai-tech/{E1}.md", blob_rel, "harvest:bm/f-1"]
    assert _mirror_of(fm) == [f"[[raw/ai-tech/{E1}.md]]", f"[[{blob_rel}]]"]
    mirrored = {link[2:-2] for link in _mirror_of(fm)}
    assert mirrored < set(sources), "a PROPER subset of sources: — never a second record"


@pytest.mark.parametrize(
    "source",
    [
        "raw/general/a|b.md",  # `|` opens the display-alias half -> names `raw/general/a`
        "raw/general/e]]f.md",  # `]]` closes the link early
        "raw/general/n[[m.md",  # `[[` re-opens it
    ],
)
def test_a_raw_source_holding_a_wikilink_metacharacter_is_left_out_of_the_mirror(
    source: str,
) -> None:
    """The mirror never emits a link that names a DIFFERENT artefact than `sources:` records.

    APPLY's own refs (`raw/<domain>/<event_id>.md`, `raw/_blob/<ab>/<sha256>.<ext>`) can never hold
    these characters, but `_sources_union` takes a provenance tuple's `raw_ref` verbatim and a
    converted or hand-made KB can hold such a file. Emitting `[[raw/general/a|b.md]]` would make the
    emitter trip L1-25 — the rule shipped in the same wave to keep it honest — and would make
    Obsidian open the wrong file. The `sources:` row is untouched; only the chip is declined.
    """
    assert _source_links([source]) == []
    # And the reason, stated as the property rather than as a character list: the guard is a
    # round-trip through the SAME reader L1-25 grades the mirror with.
    assert wikilinks(f"[[{source}]]") != [source]


@pytest.mark.parametrize(
    "source",
    [
        "raw/general/h#i.md",  # `#` opens the heading address -> names the file `raw/general/h`
        "raw/general/b#^c.md",  # `#^` opens the block-reference address
        "raw/general/caret^d.md",  # refused WITH the hash, not only in the `#^` pair
    ],
)
def test_a_raw_source_holding_a_wikilink_address_sigil_is_left_out_of_the_mirror(
    source: str,
) -> None:
    """The other half of "names a DIFFERENT artefact" — the one the round trip cannot see.

    `wikilinks()` splits only on `|`, so these paths round-trip through it BYTE-FOR-BYTE while an
    actual wikilink reader splits them at `#`: `[[raw/general/h#i.md]]` addresses the heading
    `i.md` inside the file `raw/general/h`. The round-trip test alone would therefore have emitted
    a chip that opens the wrong file, which is exactly what the guard exists to prevent — so the
    two tests run together and this one asserts the round trip PASSES.

    The refusal is deliberately stricter than L1-25, which drops a `#` address before comparing
    against `sources:` and so tolerates a hand-written `[[raw/x.md#part-2]]`. APPLY declines to MINT
    a chip whose faithfulness it cannot prove; the subset direction is the safe one.
    """
    assert _source_links([source]) == []
    assert wikilinks(f"[[{source}]]") == [source]


def test_the_mirror_round_trips_through_the_reader_that_grades_it() -> None:
    """The positive half: an ordinary `raw/` path (Unicode and a blob ref) does get its chip."""
    ok = ["raw/general/plain.md", "raw/한글/캡처.md", "raw/_blob/ab/" + "d" * 64 + ".pdf"]
    assert _source_links(ok) == [f"[[{s}]]" for s in ok]
    assert [link for entry in _source_links(ok) for link in wikilinks(entry)] == ok


def test_a_concept_with_no_raw_source_carries_no_mirror_key(tmp_path: Path) -> None:
    """An empty mirror is ABSENT, never `source_links: []` — a key nothing can read as a claim."""
    wt = _worktree(tmp_path)
    prov = {
        "c1": [{"event_id": E1, "source": "claude-code", "writer": "d", "raw_ref": "harvest:x"}]
    }
    apply_plan(_plan(_create_theme()), worktree=wt, run_date=RUN_DATE, provenance=prov)

    text = _concept(wt, "curator-concurrency").read_text(encoding="utf-8")
    fm, _ = frontmatter.parse(text)
    assert fm["sources"] == ["harvest:x"]
    assert "source_links" not in fm
    assert "source_links" not in text  # not even an empty list


def test_a_restamp_that_empties_the_mirror_removes_the_key(tmp_path: Path) -> None:
    """The pop half of the contract, at a RE-STAMP site: a stale mirror cannot outlive its sources.

    Reachable today the moment a human edits `sources:` between two curator runs, which is exactly
    the population the mirror is least able to defend itself against.
    """
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=["harvest:bm/f-0"], body="Existing CQRS prose.")
    theme = _concept(wt, "cqrs")
    fm, body = frontmatter.parse(theme.read_text(encoding="utf-8"))
    fm["source_links"] = ["[[raw/ai-tech/no-longer-sourced.md]]"]
    theme.write_text(frontmatter.render(fm, body), encoding="utf-8")

    prov = {
        "m1": [{"event_id": E2, "source": "claude-code", "writer": "d", "raw_ref": "harvest:y"}]
    }
    apply_plan(_plan(_merge_disp()), worktree=wt, run_date=RUN_DATE, provenance=prov)

    fm, _ = frontmatter.parse(theme.read_text(encoding="utf-8"))
    assert fm["sources"] == ["harvest:bm/f-0", "harvest:y"]
    assert "source_links" not in fm


def test_journals_are_never_mirrored_at_either_write_site(tmp_path: Path) -> None:
    """D20 gates the mirror on CLAIM_BEARING_KINDS, and `note` is not one — at BOTH journal sites.

    The second site is the one a future flip would forget: the cross-run union that extends an
    EXISTING journal. A mirror stamped by `_journal_frontmatter` on day 1 and not re-stamped there
    would go stale the next time the same journal gained a source.
    """
    wt = _worktree(tmp_path)
    # Site 1 — the fresh journal composed by `_journal_frontmatter`.
    apply_plan(
        _plan(_append_daily()), worktree=wt, run_date=RUN_DATE, provenance=_provenance("d1", E1)
    )
    fm, _ = frontmatter.parse(_journal(wt).read_text(encoding="utf-8"))
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md"]
    assert "source_links" not in fm

    # Site 2 — the union branch that appends into the journal that now exists.
    apply_plan(
        _plan(_append_daily(candidate_id="d2", event_ids=(E2,))),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("d2", E2),
    )
    text = _journal(wt).read_text(encoding="utf-8")
    fm, _ = frontmatter.parse(text)
    assert fm["sources"] == [f"raw/ai-tech/{E1}.md", f"raw/ai-tech/{E2}.md"]  # the union ran
    assert "source_links" not in text


def test_the_mirror_changes_no_body_bytes_at_the_merge_site(tmp_path: Path) -> None:
    """The mirror is frontmatter-only — the #144 determinism pin depends on it.

    The planning brain's `related/` view is `Wiki.query_lexical` over note BODIES
    (`curator/bundle.py`), and that view chooses MERGE_INTO_THEME targets, so a body-byte change
    here would move a permanent merge decision that no committed test observes. Rendering the SAME
    plan with the mirror's own inputs varied must leave every body byte-identical.

    This drives ONE merge plan, which is what the name says: the property itself is structural
    rather than per-site — `_stamp_source_links` takes the frontmatter mapping alone and has no
    body to reach — and `tests/core/test_rank_neutrality.py` pins that structurally, comparing the
    mirrored and unmirrored renders of the same note byte-for-byte below the frontmatter.
    """
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="Existing CQRS prose.")
    apply_plan(
        _plan(_merge_disp(needs_prose=True)),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("m1", E2),
    )
    mirrored_bodies = {
        p.relative_to(wt).as_posix(): frontmatter.parse(p.read_text(encoding="utf-8"))[1]
        for p in sorted((wt / "wiki").rglob("*.md"))
    }
    assert any("source_links" in p.read_text(encoding="utf-8") for p in (wt / "wiki").rglob("*.md"))

    # The same run over a repo whose only difference is that NOTHING is mirrorable.
    other = _worktree(tmp_path / "other")
    _seed_theme(other, "cqrs", sources=["harvest:bm/f-0"], body="Existing CQRS prose.")
    prov = {
        "m1": [{"event_id": E2, "source": "claude-code", "writer": "d", "raw_ref": "harvest:y"}]
    }
    apply_plan(
        _plan(_merge_disp(needs_prose=True)),
        worktree=other,
        run_date=RUN_DATE,
        provenance=prov,
    )
    unmirrored_bodies = {
        p.relative_to(other).as_posix(): frontmatter.parse(p.read_text(encoding="utf-8"))[1]
        for p in sorted((other / "wiki").rglob("*.md"))
    }
    assert mirrored_bodies == unmirrored_bodies


def test_a_mirrored_worktree_lints_clean_with_no_l1_25(tmp_path: Path) -> None:
    """All three stamped sites in ONE worktree, graded by the REAL schema-2 ruleset.

    L1-25 (#169 D19) grades a citation the note's `sources:` does not carry. The mirror is derived
    FROM `sources:` and is a subset of it, so it can never be the thing that trips the rule — which
    is the property that lets the emitter and the rule ship in the same wave without a repair pass.
    """
    wt = _worktree(tmp_path)
    apply_plan(
        _plan(_create_theme()),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("c1", E1),
    )
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"], body="Existing CQRS prose.")
    apply_plan(
        _plan(_merge_disp()), worktree=wt, run_date=RUN_DATE, provenance=_provenance("m1", E2)
    )
    apply_plan(
        _plan(
            Disposition(
                candidate_id="x1",
                event_ids=(E3,),
                op="MARK_CONTESTED",
                target_basename="cqrs",
                summary="Curator uses two writers, not one.",
                links=("curator-concurrency",),
                needs_prose=False,
                reason="Contradiction.",
            )
        ),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("x1", E3),
    )

    for basename in ("curator-concurrency", "cqrs"):
        fm, _ = frontmatter.parse(_concept(wt, basename).read_text(encoding="utf-8"))
        assert _mirror_of(fm), f"{basename} carries a mirror"

    result = lint(RepoLayout(wt), taxonomy=TAXONOMY, run_date=RUN_DATE, run_id=RUN_ID)
    assert result.ok, [f"{f.code} {f.path}: {f.message}" for f in result.findings]
    assert [f for f in result.findings if f.code == "L1-25"] == []


# --- DROP / NOOP --------------------------------------------------------------------------------


def test_drop_and_noop_write_nothing(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    before = sorted(p.relative_to(wt).as_posix() for p in wt.rglob("*") if p.is_file())
    plan = _plan(
        Disposition(candidate_id="c1", event_ids=(E1,), op="DROP", reason="noise"),
        Disposition(candidate_id="c2", event_ids=(E2,), op="NOOP", reason="dup"),
    )
    prov = {**_provenance("c1", E1), **_provenance("c2", E2)}
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
    after = sorted(p.relative_to(wt).as_posix() for p in wt.rglob("*") if p.is_file())
    assert before == after  # no wiki edit


# --- determinism --------------------------------------------------------------------------------


def test_apply_is_byte_deterministic(tmp_path: Path) -> None:
    plan = _plan(
        _create_theme(),
        _create_theme(
            candidate_id="c2",
            basename="cqrs",
            title="CQRS",
            summary="split.",
            tags=("architecture",),
            aliases=(),
            event_ids=(E2,),
        ),
    )
    prov = {**_provenance("c1", E1), **_provenance("c2", E2)}

    def _run(root: Path) -> dict[str, str]:
        wt = _worktree(root)
        apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=prov)
        return {
            p.relative_to(wt).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(wt.rglob("*.md"))
            if p.is_file()
        }

    a = _run(tmp_path / "a")
    b = _run(tmp_path / "b")
    assert a == b


def test_create_theme_missing_basename_raises(tmp_path: Path) -> None:
    # The basename is the one token a CREATE_THEME still cannot do without: it IS the note's
    # identity (ADR-0010 D5) and the only thing the schema-2 path composer takes. Bypass the §4.1
    # gate via model_construct to feed APPLY a malformed disposition.
    wt = _worktree(tmp_path)
    disp = Disposition.model_construct(
        candidate_id="c1",
        event_ids=(E1,),
        op="CREATE_THEME",
        domain="ai-tech",
        basename=None,
        title="x",
        summary="s",
        status="active",
        aliases=(),
        tags=(),
        links=(),
        needs_prose=True,
        reason="bad",
    )
    with pytest.raises(ApplyError):
        apply_plan(_plan(disp), worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))


# --- §4.6 stray-wikilink stripping --------------------------------------------------------------


def test_strip_stray_wikilinks_strips_unplanned_keeps_planned() -> None:
    text = "See [[planned]] and also [[stray]] plus [[other|Display]]."
    out = strip_stray_wikilinks(text, allowed={"planned"})
    assert "[[planned]]" in out  # planned kept verbatim
    assert "[[stray]]" not in out and "stray" in out  # delimiters dropped, meaning kept
    # stray display token unwrapped to its inner text:
    assert "[[other|Display]]" not in out and "other|Display" in out


def test_strip_stray_wikilinks_keeps_planned_display_token() -> None:
    # Resolution keys off the basename left of '|', matching wikilinks() normalization.
    text = "[[planned|Nice Name]]"
    out = strip_stray_wikilinks(text, allowed={"planned"})
    assert out == "[[planned|Nice Name]]"


def test_strip_stray_wikilinks_is_byte_deterministic() -> None:
    text = "[[a]] [[b]] [[a]]"
    assert strip_stray_wikilinks(text, {"a"}) == strip_stray_wikilinks(text, {"a"})
    assert strip_stray_wikilinks(text, {"a"}) == "[[a]] b [[a]]"


def test_strip_stray_wikilinks_nested_brackets_no_survivor() -> None:
    from agora_kb.schema.notes import wikilinks

    # Doubled delimiters must not SYNTHESIZE a surviving link: a single pass would leave
    # [[victim]] from [[[[victim]]]]. The fixed-point loop guarantees no non-allowed key survives.
    out = strip_stray_wikilinks("[[[[victim]]]]", allowed=set())
    assert not (set(wikilinks(out)) - set())  # no surviving link at all

    out2 = strip_stray_wikilinks("prose [[x[[victim]]]] more", allowed=set())
    assert set(wikilinks(out2)) == set()  # neither 'xvictim' nor 'victim' survives


def test_strip_stray_wikilinks_nested_keeps_allowed() -> None:
    from agora_kb.schema.notes import wikilinks

    # An allowed key nested with a stray must keep ONLY the allowed link, no synthesized stray.
    out = strip_stray_wikilinks("[[stray[[planned]]]]", allowed={"planned"})
    surviving = set(wikilinks(out))
    assert surviving - {"planned"} == set()


# --- §4.2 AUTHOR-diff validation ----------------------------------------------------------------


def _note(fm_block: str, region_body: str, cid: str = "c1") -> str:
    start, end = body_sentinels(cid)
    return f"{fm_block}\n\n{start}\n{region_body}\n{end}\n"


_FM_BLOCK = (
    "---\ntitle: T\ntype: theme\ntags: []\nstatus: active\n"
    "summary: s\ncreated: '2026-06-13'\nupdated: '2026-06-13'\n"
    "sources:\n- raw/ai-tech/e1.md\nrelated: []\nconfidence: high\n"
    "body_status: pending\n---"
)


def test_author_diff_accepts_clean_body_edit() -> None:
    old = _note(_FM_BLOCK, "_summary pending_")
    new = _note(_FM_BLOCK, "The curator holds a per-repo flock while advancing the branch.")
    errors = validate_author_diff(
        changed_paths=["wiki/ai-tech/themes/t.md"],
        per_file_old={"wiki/ai-tech/themes/t.md": old},
        per_file_new={"wiki/ai-tech/themes/t.md": new},
        sentinels={"wiki/ai-tech/themes/t.md": {"c1"}},
    )
    assert errors == []


def test_author_diff_rejects_frontmatter_edit() -> None:
    old = _note(_FM_BLOCK, "_summary pending_")
    tampered_fm = _FM_BLOCK.replace("status: active", "status: deprecated")
    new = _note(tampered_fm, "_summary pending_")
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert any("frontmatter changed" in e for e in errors)


def test_author_diff_rejects_a_pass_2_edit_of_the_source_links_mirror() -> None:
    """#169 D20: the mirror is frozen by the EXISTING check 2 — the wave adds no sixth check.

    APPLY stamps `source_links:` BEFORE the PASS-2 snapshot, so the key is inside the frontmatter
    block check 2 already compares byte-for-byte. A brain that rewrites a citation — the one edit
    that would make a note point at an artefact its `sources:` never named — is rejected with the
    message that already existed. Asserting the WHOLE error list (not `any(...)`) is the point: a
    new, narrower check bolted on for the mirror would show up here as a second entry.
    """
    fm_block = _FM_BLOCK.replace(
        "sources:\n- raw/ai-tech/e1.md\n",
        "sources:\n- raw/ai-tech/e1.md\nsource_links:\n- '[[raw/ai-tech/e1.md]]'\n",
    )
    old = _note(fm_block, "_summary pending_")
    new = _note(fm_block.replace("e1.md]]", "attacker.md]]"), "_summary pending_")
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert errors == ["t.md: frontmatter changed during PASS 2 (frontmatter is owned by APPLY)"]


def test_author_diff_rejects_out_of_sentinel_edit() -> None:
    start, end = body_sentinels("c1")
    old = _note(_FM_BLOCK, "_summary pending_")
    # Inject prose OUTSIDE the sentinel region (after the end marker).
    new = old.rstrip("\n") + "\nrogue prose outside the region\n"
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert any("outside the sentinel" in e for e in errors)


def test_author_diff_rejects_log_md_change() -> None:
    errors = validate_author_diff(
        changed_paths=["log.md"],
        per_file_old={"log.md": "base log\n"},
        per_file_new={"log.md": "base log\ntampered\n"},
        sentinels={},
    )
    assert any("log.md changed" in e for e in errors)


def test_author_diff_rejects_unexpected_file() -> None:
    errors = validate_author_diff(
        changed_paths=["wiki/ai-tech/themes/other.md"],
        per_file_old={"wiki/ai-tech/themes/other.md": "x"},
        per_file_new={"wiki/ai-tech/themes/other.md": "y"},
        sentinels={"wiki/ai-tech/themes/t.md": {"c1"}},
    )
    assert any("not a declared needs_prose note" in e for e in errors)


def test_author_diff_rejects_oversized_body() -> None:
    old = _note(_FM_BLOCK, "_summary pending_")
    new = _note(_FM_BLOCK, "x" * (DEFAULT_MAX_BODY_BYTES + 1))
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert any("exceeds" in e for e in errors)


def test_author_diff_rejects_new_wikilink() -> None:
    old = _note(_FM_BLOCK, "_summary pending_")
    new = _note(_FM_BLOCK, "Now references [[some-other-theme]] which APPLY never linked.")
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert any("new wikilink" in e for e in errors)


def test_author_diff_rejects_sentinel_tampering() -> None:
    old = _note(_FM_BLOCK, "_summary pending_")
    start, end = body_sentinels("c1")
    # Delete the end marker -> unmatched start -> tampering.
    new = old.replace(end + "\n", "")
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert errors  # rejected (tampering or region-set mismatch)


def test_author_diff_rejects_missing_frontmatter() -> None:
    # A declared needs_prose note whose PASS-2 text lacks a proper '---' frontmatter fence is
    # rejected as missing/malformed frontmatter (apply.py _split_frontmatter_and_body branch).
    start, end = body_sentinels("c1")
    no_fm = f"not frontmatter\n\n{start}\n_summary pending_\n{end}\n"
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": no_fm},
        per_file_new={"t.md": no_fm},
        sentinels={"t.md": {"c1"}},
    )
    assert any("missing/malformed frontmatter" in e for e in errors)


def test_author_diff_rejects_embedded_fake_sentinel() -> None:
    # A model writing a NEW agora:body sentinel pair (each marker on its OWN line, the matched
    # grammar) INSIDE its prose region nests a foreign region -> tampering / a foreign region id.
    old = _note(_FM_BLOCK, "_summary pending_")
    fake_start, fake_end = body_sentinels("evil")
    new = _note(_FM_BLOCK, f"prose\n{fake_start}\nsmuggled\n{fake_end}\nmore")
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1"}},
    )
    assert errors  # rejected: an embedded sentinel pair is tampering / a foreign region


def test_author_diff_accepts_two_region_clean_edit() -> None:
    # A note with TWO declared regions {c1,c2}, both edited cleanly and nothing out-of-region
    # touched, validates clean — the multi-region accept path (CREATE wraps c1; a later MERGE
    # appended c2), confirming set(regions) == sentinels[path] holds for prior + this-run regions.
    s1, e1 = body_sentinels("c1")
    s2, e2 = body_sentinels("c2")
    old = f"{_FM_BLOCK}\n\n{s1}\n_summary pending_\n{e1}\n\n{s2}\n_summary pending_\n{e2}\n"
    new = f"{_FM_BLOCK}\n\n{s1}\nFirst region prose.\n{e1}\n\n{s2}\nSecond region prose.\n{e2}\n"
    errors = validate_author_diff(
        changed_paths=["t.md"],
        per_file_old={"t.md": old},
        per_file_new={"t.md": new},
        sentinels={"t.md": {"c1", "c2"}},
    )
    assert errors == []


def test_author_diff_end_to_end_from_apply_output(tmp_path: Path) -> None:
    # APPLY -> AUTHOR contract on REAL bytes: apply a CREATE_THEME(needs_prose), read the produced
    # file as base, simulate a prose edit inside the candidate region, and assert §4.2 accepts it.
    # This guards against APPLY's emitted frontmatter/sentinel format drifting from what the §4.2
    # validator expects (the hand-rolled _FM_BLOCK tests cannot catch such drift).
    wt = _worktree(tmp_path)
    plan = _plan(_create_theme())
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))
    theme = _concept(wt, "curator-concurrency")
    rel = "wiki/concepts/curator-concurrency.md"
    old = theme.read_text(encoding="utf-8")
    new = old.replace(
        "_summary pending_", "The curator holds a per-repo flock while advancing the branch."
    )
    errors = validate_author_diff(
        changed_paths=[rel],
        per_file_old={rel: old},
        per_file_new={rel: new},
        sentinels={rel: {region_sentinel_id(RUN_ID, "c1")}},
    )
    assert errors == []


# --- ADR-0041 D1.3: the lazily-minted map shares ONE basename namespace with every concept -------


def test_apply_refuses_to_mint_a_map_over_an_existing_basename(tmp_path: Path) -> None:
    """The CROSS-RUN wedge, refused at the write with the cause NAMED.

    v1's ``<domain>-moc.md`` suffix made a concept/MOC basename collision impossible; D1.3 drops the
    suffix, so ``wiki/maps/economy.md`` and ``wiki/concepts/economy.md`` are one basename. The map
    is minted LAZILY at the first concept of its subject, so the collision arms itself a run LATER
    than the note that caused it: without this precondition the run fails at the §4.4 lint gate with
    two symmetric ``L1-1`` findings naming neither cause, and every later run touching that subject
    fails identically until a human renames the note.

    The PLAN gate reserves the declared domains against NEW basenames; this covers what the plan
    gate structurally cannot see — a note a human, an importer, or a pre-reservation build already
    put in the tree.
    """
    wt = _worktree(tmp_path)
    _seed_theme(wt, "economy", sources=[f"raw/ai-tech/{E1}.md"])  # a concept named like a domain
    plan = _plan(_create_theme(candidate_id="c2", domain="economy", basename="market-structure"))

    with pytest.raises(ApplyError) as exc:
        apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c2", E1))

    assert "map basename collision" in str(exc.value)
    assert "wiki/concepts/economy.md" in str(exc.value)
    assert not _map_note(wt, "economy").exists(), "refused BEFORE the write"


def test_apply_still_mints_a_map_whose_basename_is_free(tmp_path: Path) -> None:
    """The precondition is a collision check, not a new obstacle: the ordinary path is unchanged."""
    wt = _worktree(tmp_path)
    apply_plan(
        _plan(_create_theme()), worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1)
    )
    assert _map_note(wt, "ai-tech").is_file()


def test_apply_refuses_a_map_that_would_collide_with_the_root_index(tmp_path: Path) -> None:
    """``index.md`` lives OUTSIDE ``wiki/`` (D1.2), so the tree walk alone would miss it."""
    wt = _worktree(tmp_path)
    (wt / "index.md").write_text("---\ntitle: Index\n---\n", encoding="utf-8")
    plan = _plan(_create_theme(domain="index", basename="curator-concurrency"))
    with pytest.raises(ApplyError, match="map basename collision"):
        apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))


def test_a_people_note_never_blocks_a_map(tmp_path: Path) -> None:
    """D3.3: people basenames are outside the global identity space, so they cannot collide."""
    wt = _worktree(tmp_path)
    person = wt / "wiki" / "people" / "hando" / "ai-tech.md"
    person.parent.mkdir(parents=True, exist_ok=True)
    person.write_text("---\ntitle: mine\n---\n", encoding="utf-8")
    apply_plan(
        _plan(_create_theme()), worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1)
    )
    assert _map_note(wt, "ai-tech").is_file()


# --- ADR-0041 D2.2 leg 3: the raw/ shard key is GRADED before it composes a path -----------------


def test_an_escaping_subject_never_steers_the_raw_write(tmp_path: Path) -> None:
    """A MERGE target's ``subjects:`` is arbitrary frontmatter — grade it, or it steers the write.

    v1 read the shard key out of the target's live PATH, a real directory component that cannot
    contain a separator. D3.2 replaces that with the note's own ``subjects:``, which a human edit or
    an import can put anything into — and ``_sources_union`` turns it straight into
    ``raw/<subject>/<event_id>.md``. ``_contained`` only proves the write lands inside the WORKTREE,
    so ``../wiki/concepts`` passes it while landing an unauthored, frontmatter-less file in the wiki
    — outside ``raw/`` and outside the ADR-0010 D3 authorship channel, since git reports the
    NORMALIZED path and the ``raw_writes`` key would keep the escaping one.
    """
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[])
    victim = _concept(wt, "cqrs")
    fm, body = frontmatter.parse(victim.read_text(encoding="utf-8"))
    fm["subjects"] = ["../wiki/concepts"]
    victim.write_text(frontmatter.render(fm, body), encoding="utf-8")

    raw_writes = apply_plan(
        _plan(_merge_disp()),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("m1", E2, body="PLANTED BODY"),
    )

    assert all(ref.startswith("raw/") for ref in raw_writes), raw_writes
    # It degrades to the PLAN-graded domain, so the capture is still written — nothing is lost.
    assert f"raw/ai-tech/{E2}.md" in raw_writes
    assert not (wt / "wiki" / "concepts" / f"{E2}.md").exists()
    merged_fm, _ = frontmatter.parse(victim.read_text(encoding="utf-8"))
    assert all(str(s).startswith("raw/") for s in merged_fm["sources"])


def test_a_reserved_underscore_subject_never_reaches_the_raw_prefix_namespace(
    tmp_path: Path,
) -> None:
    """``raw/_blob`` / ``raw/_pages`` are RESERVED (D1.4) and share ONE namespace with ``raw/``."""
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[])
    victim = _concept(wt, "cqrs")
    fm, body = frontmatter.parse(victim.read_text(encoding="utf-8"))
    fm["subjects"] = ["_blob"]
    victim.write_text(frontmatter.render(fm, body), encoding="utf-8")

    raw_writes = apply_plan(
        _plan(_merge_disp()),
        worktree=wt,
        run_date=RUN_DATE,
        provenance=_provenance("m1", E2, body="capture"),
    )
    assert f"raw/ai-tech/{E2}.md" in raw_writes
    assert not (wt / "raw" / "_blob").exists()


# --- ADR-0041 D2.4: a derived note is never a MERGE_INTO_THEME target ----------------------------


def test_a_derived_note_is_never_a_merge_target(tmp_path: Path) -> None:
    """``derived: true`` marks the PROPOSAL plane; merging would append claims into it."""
    wt = _worktree(tmp_path)
    _seed_theme(wt, "cqrs", sources=[f"raw/ai-tech/{E1}.md"])
    target = _concept(wt, "cqrs")
    fm, body = frontmatter.parse(target.read_text(encoding="utf-8"))
    fm["derived"] = True
    target.write_text(frontmatter.render(fm, body), encoding="utf-8")
    before = target.read_text(encoding="utf-8")

    with pytest.raises(ApplyError, match="not found as a concept/summary"):
        apply_plan(
            _plan(_merge_disp()),
            worktree=wt,
            run_date=RUN_DATE,
            provenance=_provenance("m1", E2),
        )
    assert target.read_text(encoding="utf-8") == before, "refused before any write"


# --- ADR-0041 D2: the bundle root carries the SAME common base as every other note ---------------


def test_the_root_index_carries_the_d2_provenance_block(tmp_path: Path) -> None:
    """The one note APPLY re-renders with no provenance of its own to merge must still carry it."""
    wt = _worktree(tmp_path)
    apply_plan(
        _plan(_create_theme()), worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1)
    )
    fm, _ = frontmatter.parse((wt / "index.md").read_text(encoding="utf-8"))
    assert fm["provenance"] == {"writers": [], "agents": []}
    assert fm["derived"] is False
    assert fm["kind"] == "index"


def test_a_touched_note_missing_the_base_is_backfilled_with_provenance(tmp_path: Path) -> None:
    """The BACKFILL half: a root index an older build (or an importer) wrote lacks the block.

    ``_update_index``, unlike merge/contest, has no provenance of its own to merge, so without the
    seed in the common-base stamp the bundle ROOT would be the one curator-written note whose
    frontmatter is not the shape D2 states — silently, since lint grades ``provenance:`` only when
    it is present.
    """
    wt = _worktree(tmp_path)
    (wt / "index.md").write_text(
        frontmatter.render({"title": "Index", "children": []}, "# KB"), encoding="utf-8"
    )
    apply_plan(
        _plan(_create_theme()), worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1)
    )
    fm, _ = frontmatter.parse((wt / "index.md").read_text(encoding="utf-8"))
    assert fm["provenance"] == {"writers": [], "agents": []}


# --- ADR-0041 D2.6: one journal, several domains -> each section names its contributor -----------


def test_journal_sections_from_two_domains_are_headed_by_their_contributor(
    tmp_path: Path,
) -> None:
    """A bare ``## <run_date>`` repeated per domain is N identical headings and N ambiguous anchors.

    D2.6 pins the section COUNT (one per ``needs_prose`` disposition) and leaves the heading TEXT
    open; the domain is already the outer sort key, so the information exists at write time.
    """
    wt = _worktree(tmp_path)
    daily = {
        "op": "APPEND_DAILY",
        "basename": RUN_DATE,
        "title": f"Daily {RUN_DATE}",
        "summary": "the day",
        "status": "active",
        "tags": (),
        "aliases": (),
        "links": (),
        "needs_prose": True,
        "reason": "daily",
    }
    plan = _plan(
        Disposition(candidate_id="d1", event_ids=(E1,), domain="ai-tech", **daily),
        Disposition(candidate_id="d2", event_ids=(E2,), domain="general", **daily),
    )
    apply_plan(
        plan,
        worktree=wt,
        run_date=RUN_DATE,
        provenance={
            **_provenance("d1", E1),
            **_provenance("d2", E2),
        },
    )
    _, body = frontmatter.parse(_journal(wt).read_text(encoding="utf-8"))
    assert f"## {RUN_DATE} · ai-tech" in body
    assert f"## {RUN_DATE} · general" in body
    assert body.count(f"## {RUN_DATE}\n") == 0, "no bare, ambiguous heading survives"
