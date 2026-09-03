"""Tests for repo layout + writer validation (tenant isolation, DESIGN §7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.core.layout import (
    KIND_DIRECTORIES,
    WIKI_KINDS,
    InvalidNoteBasenameError,
    InvalidWriterError,
    RepoLayout,
    validate_writer,
)
from agora_kb.core.pathsafe import is_safe_component


def test_paths(tmp_path: Path) -> None:
    lo = RepoLayout(tmp_path)
    assert lo.kb_dir == tmp_path / "_kb"
    assert lo.inbox_dir == tmp_path / "_kb" / "inbox"
    assert lo.state_file == tmp_path / "_kb" / "state.json"
    assert lo.lock_file == tmp_path / "_kb" / "curator.lock"
    assert lo.inbox_writer_dir("dochan") == tmp_path / "_kb" / "inbox" / "dochan"
    assert (
        lo.inbox_item_path("dochan", "2026-06-13T10-22-33.481Z--a1b2c3")
        == tmp_path / "_kb" / "inbox" / "dochan" / "2026-06-13T10-22-33.481Z--a1b2c3.md"
    )


def test_root_is_absolute(tmp_path: Path) -> None:
    lo = RepoLayout(Path("."))
    assert lo.root.is_absolute()


@pytest.mark.parametrize("good", ["dochan", "claude-code", "web_user", "a", "A1.b-c"])
def test_valid_writers(good: str) -> None:
    assert validate_writer(good) == good


@pytest.mark.parametrize("bad", ["..", ".", "../evil", "a/b", "/abs", "", ".hidden", "a b"])
def test_invalid_writers_rejected(bad: str) -> None:
    with pytest.raises(InvalidWriterError):
        validate_writer(bad)


def test_path_traversal_blocked_in_layout(tmp_path: Path) -> None:
    lo = RepoLayout(tmp_path)
    with pytest.raises(InvalidWriterError):
        lo.inbox_writer_dir("../../etc")


def test_requeued_record_path_guards_its_components(tmp_path: Path) -> None:
    """(#99) The ``_kb/requeued/`` twin is built from directory names read off an editable tree.

    ``agora requeue --reset-attempts`` derives ``date``/``run_id`` from the ``_kb/failed/`` path it
    is archiving, and ``_kb/failed/`` is operator-editable, so both components go through
    :func:`safe_path_component` before they are interpolated (DESIGN §7). The archive ALSO has to
    live outside ``failed_dir``: the retry budget is ``failed_dir.rglob("error.json")`` and
    ``rglob`` descends into dotted directories, so an in-tree archive would still be counted.
    """
    lo = RepoLayout(tmp_path)
    run_id = "2026-06-13T03-00-00.000Z--04e370"

    assert lo.requeued_dir == tmp_path / "_kb" / "requeued"
    assert lo.failed_dir not in lo.requeued_dir.parents
    assert lo.requeued_record_path(date="2026-06-13", run_id=run_id) == (
        tmp_path / "_kb" / "requeued" / "2026-06-13" / run_id / "error.json"
    )

    for date, bad_run in (("..", run_id), ("2026-06-13", "../../etc"), ("/abs", run_id)):
        with pytest.raises(InvalidWriterError):
            lo.requeued_record_path(date=date, run_id=bad_run)


# --- KB wiki schema 2: the kind-first layout (ADR-0041 D1) --------------------------------------


def test_schema_2_kind_directories_are_the_adr_d1_tree(tmp_path: Path) -> None:
    """Every schema-2 accessor, pinned against the D1 layout block verbatim."""
    lo = RepoLayout(tmp_path)

    assert lo.concepts_dir == tmp_path / "wiki" / "concepts"
    assert lo.summaries_dir == tmp_path / "wiki" / "summaries"
    assert lo.notes_dir == tmp_path / "wiki" / "notes"
    assert lo.maps_dir == tmp_path / "wiki" / "maps"
    assert lo.entities_dir == tmp_path / "wiki" / "entities"
    assert lo.people_dir == tmp_path / "wiki" / "people"
    # raw/ NEVER MOVES (D3.4): the two new prefixes live INSIDE it.
    assert lo.blob_dir == tmp_path / "raw" / "_blob"
    assert lo.pages_dir == tmp_path / "raw" / "_pages"
    assert lo.meta_dir == tmp_path / "_meta"
    assert lo.kb_meta_file == tmp_path / "_meta" / "kb.yaml"


def test_schema_2_accessors_create_nothing(tmp_path: Path) -> None:
    """This module computes paths only — naming a directory must not bring it into existence."""
    lo = RepoLayout(tmp_path)
    for path in (lo.concepts_dir, lo.notes_dir, lo.blob_dir, lo.kb_meta_file, lo.meta_dir):
        assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_schema_1_accessors_are_untouched(tmp_path: Path) -> None:
    """W2.1 is ADDITIVE: nothing a schema-1 repo reads moved (`raw/`, `wiki/`, `index.md`)."""
    lo = RepoLayout(tmp_path)
    assert lo.raw_dir == tmp_path / "raw"
    assert lo.wiki_dir == tmp_path / "wiki"
    assert lo.index_file == tmp_path / "index.md"
    assert lo.log_file == tmp_path / "log.md"
    assert lo.schema_file == tmp_path / "AGENTS.md"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("concept", "wiki/concepts/foo.md"),
        ("summary", "wiki/summaries/foo.md"),
        ("map", "wiki/maps/foo.md"),
        ("entity", "wiki/entities/foo.md"),
    ],
)
def test_note_path_for_flat_kinds(tmp_path: Path, kind: str, expected: str) -> None:
    """The subject is GONE from the path: a concept lands at wiki/concepts/<slug>.md whatever its
    subjects are (D2.2 leg 1 — nothing can be dropped for lack of a domain)."""
    lo = RepoLayout(tmp_path)
    assert lo.note_path_for(kind, "foo") == tmp_path / Path(expected)


def test_note_path_for_note_is_date_sharded(tmp_path: Path) -> None:
    """D1.1/D2.6: one journal per run_date, under the <yyyy>/<mm> shard derived FROM run_date."""
    lo = RepoLayout(tmp_path)
    assert (
        lo.note_path_for("note", "2026-09-04", run_date="2026-09-04")
        == tmp_path / "wiki" / "notes" / "2026" / "09" / "2026-09-04.md"
    )


def test_note_path_for_note_requires_a_run_date(tmp_path: Path) -> None:
    """The shard is composed from an injected deterministic fact, never parsed out of a basename.

    D2.6 states the inversion this prevents: parsing ``<yyyy>/<mm>`` back out of a model-supplied
    basename would make a curator-owned path segment a function of model output.
    """
    lo = RepoLayout(tmp_path)
    with pytest.raises(ValueError, match="run_date"):
        lo.note_path_for("note", "2026-09-04")


@pytest.mark.parametrize("bad", ["2026-9-4", "20260904", "2026/09/04", "", "not-a-date"])
def test_note_path_for_rejects_a_malformed_run_date(tmp_path: Path, bad: str) -> None:
    lo = RepoLayout(tmp_path)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        lo.note_path_for("note", "2026-09-04", run_date=bad)


def test_note_path_for_flat_kinds_ignore_a_supplied_run_date(tmp_path: Path) -> None:
    """A caller with a run date in scope need not branch: a flat kind has no shard for it."""
    lo = RepoLayout(tmp_path)
    assert lo.note_path_for("concept", "foo", run_date="2026-09-04") == (
        tmp_path / "wiki" / "concepts" / "foo.md"
    )


def test_note_path_for_index_is_the_root_map(tmp_path: Path) -> None:
    """D1.2: index.md sits at the repo ROOT, not under wiki/maps/ — it is the root OF the tier."""
    lo = RepoLayout(tmp_path)
    assert lo.note_path_for("index", "index") == lo.index_file
    with pytest.raises(InvalidNoteBasenameError, match="basenamed 'index'"):
        lo.note_path_for("index", "something-else")


def test_note_path_for_refuses_to_compose_a_person_path(tmp_path: Path) -> None:
    """D3.3: wiki/people/** is human-owned — the curator never writes it, so nothing composes it."""
    lo = RepoLayout(tmp_path)
    with pytest.raises(ValueError, match="human-owned"):
        lo.note_path_for("person", "hando")


@pytest.mark.parametrize("kind", ["theme", "daily", "moc", "concepts", "", "Concept"])
def test_note_path_for_rejects_unknown_kinds(tmp_path: Path, kind: str) -> None:
    """The kind vocabulary is CLOSED (D3.1) — including the RETIRED v1 `type:` values (D2.5)."""
    lo = RepoLayout(tmp_path)
    with pytest.raises(ValueError, match="unknown note kind"):
        lo.note_path_for(kind, "foo")


def test_note_path_for_rejects_a_leading_underscore_basename(tmp_path: Path) -> None:
    """ADR-0041 D4.4, NORMATIVE: pathsafe ALLOWS a leading `_`, so the composer must reject it.

    The v1 ASCII ``_SAFE_TOKEN_RE_PATTERN`` excluded ``_`` by its leading character class, and
    that exclusion was the ONLY thing stopping a plan token named ``_blob``. ``pathsafe`` puts
    ``_`` in its allowed extras, so the swap is a LOOSENING on exactly this character — which is
    why the reservation is enforced here, at the composition site.
    """
    lo = RepoLayout(tmp_path)
    # The precondition the rule exists for: pathsafe itself is happy with these.
    assert is_safe_component("_blob")
    for reserved in ("_blob", "_pages", "_kb", "_anything"):
        with pytest.raises(InvalidNoteBasenameError, match="reserved"):
            lo.note_path_for("concept", reserved)


@pytest.mark.parametrize(
    "bad",
    ["a/b", "../evil", "..", ".", ".hidden", "", "CON", "a\x00b", "foo.md", "a⁄b"],
)
def test_note_path_for_rejects_unsafe_basenames(tmp_path: Path, bad: str) -> None:
    lo = RepoLayout(tmp_path)
    with pytest.raises(InvalidNoteBasenameError):
        lo.note_path_for("concept", bad)


def test_note_path_for_composed_path_never_escapes_the_repo(tmp_path: Path) -> None:
    """The containment property the basename guard exists for, asserted on the result."""
    lo = RepoLayout(tmp_path)
    for kind, basename, kwargs in (
        ("concept", "ok-name", {}),
        ("map", "ok-name", {}),
        # A `note` is basenamed by its run_date (D2.6), so the containment case uses that basename.
        ("note", "2026-09-04", {"run_date": "2026-09-04"}),
    ):
        path = lo.note_path_for(kind, basename, **kwargs)
        assert path.resolve().is_relative_to(lo.root.resolve())


@pytest.mark.parametrize(
    "basename", ["finance-2026-01-12", "2026-01-13", "2026-02-12", "journal", "2026-01"]
)
def test_note_path_for_asserts_the_note_basename_IS_the_run_date(
    tmp_path: Path, basename: str
) -> None:
    """D2.6: one journal per ``run_date``, repo-wide, basenamed that date.

    The composer must not silently return a path lint L1-14 hard-rejects (``basename is not
    YYYY-MM-DD``, ``date`` != basename, or the shard in the wrong month). A caller-side mismatch is
    a refusal HERE, not a failed run at the gate — the shard is DERIVED from the curator-owned
    ``run_date``, never reconciled with model-supplied output afterwards.
    """
    lo = RepoLayout(tmp_path)
    with pytest.raises(InvalidNoteBasenameError, match="run_date"):
        lo.note_path_for("note", basename, run_date="2026-01-12")


def test_kind_directory_mapping_agrees_across_the_modules_that_hold_a_copy() -> None:
    """D3.1's kind vocabulary is CLOSED at the directory level, so the copies must not drift.

    ``core.layout.KIND_DIRECTORIES`` composes paths, ``schema.notes.KIND_BY_DIRECTORY`` is what
    lint reads for L1-22, and ``schema.lint._V2_NOTES_DIR`` builds L1-14's expected path. They
    cannot be ONE constant (``core.layout`` may not import ``schema``, or the dependency cycles),
    so the relation is pinned by a test instead of by an import.
    """
    from agora_kb.schema.lint import _V2_NOTES_DIR
    from agora_kb.schema.notes import DIRECTORY_BY_KIND, KIND_BY_DIRECTORY, SCHEMA2_KINDS

    # `person` is the only kind with no composable path: wiki/people/** is human-owned (D3.3).
    assert {k: v for k, v in DIRECTORY_BY_KIND.items() if k != "person"} == KIND_DIRECTORIES
    assert set(KIND_BY_DIRECTORY.values()) == WIKI_KINDS
    # WIKI_KINDS is the set as it appears UNDER wiki/; SCHEMA2_KINDS adds the root map's `index`.
    assert SCHEMA2_KINDS - WIKI_KINDS == {"index"}
    assert _V2_NOTES_DIR == f"wiki/{DIRECTORY_BY_KIND['note']}"
    assert RepoLayout(Path("/x")).people_dir.name == DIRECTORY_BY_KIND["person"]


def test_note_path_for_keeps_a_unicode_basename(tmp_path: Path) -> None:
    """D4.4: a Korean title now yields a Korean component instead of the `note-<sha8>` floor."""
    lo = RepoLayout(tmp_path)
    assert lo.note_path_for("concept", "한국어-노트") == (
        tmp_path / "wiki" / "concepts" / "한국어-노트.md"
    )


def test_kind_directories_mapping_matches_the_d2_5_table() -> None:
    """The frozen v1 type → schema-2 kind table (D2.5), minus the two rows with no directory."""
    assert KIND_DIRECTORIES == {
        "concept": "concepts",
        "summary": "summaries",
        "note": "notes",
        "map": "maps",
        "entity": "entities",
    }
    # `index` is not a directory (D1.2) and `person` is not composable (D3.3), but `person` IS a
    # kind that appears under wiki/.
    assert "index" not in KIND_DIRECTORIES
    assert "person" not in KIND_DIRECTORIES
    assert WIKI_KINDS == frozenset({*KIND_DIRECTORIES, "person"})
