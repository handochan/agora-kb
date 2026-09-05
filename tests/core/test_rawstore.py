"""Tests for the ``raw/`` read primitive (:mod:`agora_kb.core.rawstore`, #169 / DRILLDOWN-169 A1).

The module composes a filesystem path out of an UNTRUSTED string — that is the whole reason it
exists and the whole reason it is dangerous — so most of this file is the refusal table. Each of
the three gates gets a case that ONLY it can catch, because the point of keeping them separate is
that no one of them is sufficient:

* gate 1 (prefix allowlist) — ``raw/../wiki/…`` and ``wiki/…`` never reach the filesystem at all;
* gate 2 (containment against ``raw_dir``) — a symlink out of the repo, and a symlink into the
  human-owned ``wiki/people/`` tree that a ROOT-relative containment check would have admitted;
* gate 3 (symlink identity) — a symlink whose target is a perfectly ordinary file INSIDE ``raw/``,
  which passes gate 2 and is still refused, because the curator never wrote it.

``raw/`` is byte-identical on schema 1 and schema 2 (ADR-0041 D1.4), so the last test asserts the
two behave the same rather than branching on a version anywhere in the module.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
import yaml

from agora_kb.core import rawstore
from agora_kb.core.layout import BLOB_PREFIX, SIDECAR_SUFFIX, RepoLayout, blob_ref
from tests.support.kb_builder import NoteSpec, build_kb

#: Real symlinks require an elevated privilege on Windows (the ``schema/test_emit.py`` posture).
posix_symlinks = pytest.mark.skipif(
    os.name != "posix", reason="real symlinks require privilege on Windows"
)

TEXT_SOURCE = "raw/ai-tech/e1.md"

#: The fixture artefact, copied in shape from ``tests/curator/test_blob_capture.py``: NOT text under
#: any codec. A NUL, a lone ``0xff`` and a CRLF, so a text-mode read or a decode attempt anywhere on
#: the path corrupts it into a visible failure instead of passing by luck.
BLOB_BYTES = b"%PDF-1.7\r\n\x00\xff\xfe binary payload \x00\r\nnot text\n"
BLOB_SHA = hashlib.sha256(BLOB_BYTES).hexdigest()
BLOB_REF = blob_ref(BLOB_SHA, "pdf")
SIDECAR_REF = f"{BLOB_REF}{SIDECAR_SUFFIX}"


# --- fixtures ------------------------------------------------------------------------------------


def _build(root: Path, *, schema_version: int = 2) -> RepoLayout:
    """A lint-clean KB whose one concept cites :data:`TEXT_SOURCE` (the builder materializes it)."""
    build_kb(
        root,
        [
            NoteSpec(
                kind="theme",
                domain="ai-tech",
                title="Retrieval Augmented Generation",
                body="Retrieval augmented generation grounds an answer in retrieved documents.",
                sources=[TEXT_SOURCE],
            )
        ],
        schema_version=schema_version,
        domains=["ai-tech", "general"],
    )
    return RepoLayout(root)


def _write_blob(layout: RepoLayout, *, sidecar: dict[str, object] | None = None) -> None:
    """Write the blob + its sidecar BY HAND, in APPLY's shape (``curator/apply.py`` §BLOB).

    Deliberately not through the curator: this suite tests the reader against the bytes-on-disk
    contract, and a fixture that needs a whole run to produce one file is a fixture nobody extends.
    The sidecar's key set is APPLY's closed one, with absent optionals OMITTED rather than empty.
    """
    blob = layout.root / BLOB_REF
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(BLOB_BYTES)
    doc = sidecar if sidecar is not None else _sidecar_doc()
    (layout.root / SIDECAR_REF).write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def _sidecar_doc() -> dict[str, object]:
    return {
        "sha256": BLOB_SHA,
        "ext": "pdf",
        "media_type": "application/pdf",
        "bytes": len(BLOB_BYTES),
        "filename": "2026-q3-report.pdf",
        "captured_at": "2026-06-13T02:40:10Z",
        "writer": "dochan",
        "source": "web:dochan",
        "event_id": "01J8ZQ3M4N5P6Q7R8S9T0V1W2X",
    }


# --- the happy paths -----------------------------------------------------------------------------


def test_resolve_reads_a_text_artifact(tmp_path: Path) -> None:
    """The ordinary case: a cited ``raw/<domain>/<event>.md`` resolves and reads back verbatim."""
    layout = _build(tmp_path)
    on_disk = (layout.root / TEXT_SOURCE).read_text(encoding="utf-8")

    ref = rawstore.resolve(layout, TEXT_SOURCE)

    assert ref is not None
    assert ref.kind == "text"
    assert ref.rel_path == TEXT_SOURCE  # the citation string, unmangled (ADR-0041 D3.4)
    assert ref.path == layout.root / TEXT_SOURCE
    assert ref.size_bytes == len(on_disk.encode("utf-8"))
    assert ref.sidecar_rel_path is None

    text, truncated = rawstore.read_text(ref)
    assert text == on_disk
    assert truncated is False


def test_resolve_classifies_a_blob_and_reads_its_sidecar(tmp_path: Path) -> None:
    """A ``raw/_blob/`` artefact is ``kind='blob'`` and knows where its capture record lives."""
    layout = _build(tmp_path)
    _write_blob(layout)

    ref = rawstore.resolve(layout, BLOB_REF)

    assert ref is not None
    assert ref.kind == "blob"
    assert ref.rel_path == BLOB_REF
    assert ref.rel_path.startswith(f"{BLOB_PREFIX}/{BLOB_SHA[:2]}/")
    assert ref.size_bytes == len(BLOB_BYTES)
    assert ref.sidecar_rel_path == SIDECAR_REF

    doc = rawstore.read_sidecar(layout, ref)
    assert doc == _sidecar_doc()
    # The CLOSED capture key set (DATA-MODEL §2) reaches the reader intact — nine keys, no more.
    assert set(doc or {}) == {
        "sha256",
        "ext",
        "media_type",
        "bytes",
        "filename",
        "captured_at",
        "writer",
        "source",
        "event_id",
    }


def test_a_sidecar_path_resolves_as_its_own_kind(tmp_path: Path) -> None:
    """``<blob>.meta.yaml`` is ``kind='sidecar'``, not ``'blob'`` — it too sits under ``raw/_blob``.

    Resolvable on purpose: the faces need "that is a sidecar" to be distinguishable from "that does
    not exist" so they can answer with lint L1-8b's rule — cite the artefact, not its sidecar
    (DRILLDOWN-169 D9) — instead of a bare dead end.
    """
    layout = _build(tmp_path)
    _write_blob(layout)

    ref = rawstore.resolve(layout, SIDECAR_REF)

    assert ref is not None
    assert ref.kind == "sidecar"
    assert ref.sidecar_rel_path is None
    # And read_sidecar refuses to treat it as a blob's record: there is only one way in.
    assert rawstore.read_sidecar(layout, ref) is None


def test_read_sidecar_tolerates_an_absent_or_corrupt_record(tmp_path: Path) -> None:
    """Losing the DESCRIPTION of an artefact must never cost a caller the artefact itself."""
    layout = _build(tmp_path)
    _write_blob(layout)
    ref = rawstore.resolve(layout, BLOB_REF)
    assert ref is not None

    (layout.root / SIDECAR_REF).write_text("key: [unclosed\n", encoding="utf-8")
    assert rawstore.read_sidecar(layout, ref) is None

    (layout.root / SIDECAR_REF).write_text("just a scalar\n", encoding="utf-8")
    assert rawstore.read_sidecar(layout, ref) is None  # not a mapping

    (layout.root / SIDECAR_REF).unlink()
    assert rawstore.read_sidecar(layout, ref) is None
    assert rawstore.resolve(layout, BLOB_REF) is not None  # the blob is untouched by any of it


# --- the refusal table ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path",
    [
        pytest.param("../../etc/passwd", id="traversal-out-of-repo"),
        pytest.param("/etc/passwd", id="absolute"),
        pytest.param("raw/../wiki/concepts/x.md", id="raw-prefixed-escape-into-wiki"),
        pytest.param("raw/./ai-tech/../../index.md", id="dot-and-dotdot-mix"),
        pytest.param("wiki/concepts/x.md", id="not-under-raw"),
        pytest.param("_kb/gold/default.md", id="operational-spool"),
        pytest.param(f"{BLOB_PREFIX}/../../etc/passwd", id="blob-prefixed-escape"),
        pytest.param("raw//ai-tech/e1.md", id="double-slash"),
        pytest.param("raw/ai-tech/e1.md/", id="trailing-slash"),
        pytest.param("raw", id="the-directory-itself"),
        pytest.param("", id="empty"),
        # An embedded NUL is the one byte that makes CPython's `realpath` RAISE rather than fail:
        # `posixpath.normpath` leaves it untouched and the `raw/` prefix survives it, so gate 1 has
        # to reject it textually or gate 2 throws `ValueError` straight out of a function whose
        # contract is "every failure returns None" — a 500 on two unauthenticated read routes and
        # on the pre-existing /note page, which resolves every `sources:` entry.
        pytest.param("raw/a\x00b.md", id="embedded-nul"),
        pytest.param(f"{TEXT_SOURCE}\x00.png", id="nul-suffixed-real-file"),
        pytest.param("raw/\x00", id="bare-nul-component"),
    ],
)
def test_adversarial_paths_all_resolve_to_none(tmp_path: Path, rel_path: str) -> None:
    """Every refusal is the SAME ``None`` — no exception, no message, no stat disclosure.

    A caller that could tell "refused" from "does not exist" could walk the filesystem outside the
    repo one probe at a time, which is why the module has no error channel to leak through.
    """
    layout = _build(tmp_path)
    assert rawstore.resolve(layout, rel_path) is None


def test_a_missing_file_under_raw_is_none(tmp_path: Path) -> None:
    layout = _build(tmp_path)
    assert rawstore.resolve(layout, "raw/ai-tech/nope.md") is None


def test_a_directory_is_not_an_artifact(tmp_path: Path) -> None:
    """``is_file()`` is required: a directory is a real path under ``raw/`` and still not a read."""
    layout = _build(tmp_path)
    assert (layout.root / "raw/ai-tech").is_dir()
    assert rawstore.resolve(layout, "raw/ai-tech") is None


@posix_symlinks
def test_a_symlink_out_of_the_repo_is_refused_and_leaks_nothing(tmp_path: Path) -> None:
    """Gate 2: a link inside ``raw/`` pointing at an outside file resolves outside ``raw_dir``."""
    layout = _build(tmp_path)
    secret = tmp_path.parent / "outside-secret.md"
    secret.write_text("TOP SECRET OUTSIDE THE REPO\n", encoding="utf-8")
    link = layout.root / "raw/ai-tech/link.md"
    link.symlink_to(secret)

    ref = rawstore.resolve(layout, "raw/ai-tech/link.md")

    assert ref is None  # and, structurally, no RawRef exists for anything to read the target with


@posix_symlinks
def test_a_symlink_into_the_people_namespace_is_refused(tmp_path: Path) -> None:
    """Gate 2 keyed on ``raw_dir``, NOT the worktree root — the case root-containment would admit.

    ``curator/apply.py``'s ``_contained`` concedes in its own docstring that ``raw/../wiki/…``
    resolves in-tree and passes it. Reused verbatim here, this link would launder the human-owned
    ``wiki/people/**`` namespace (ADR-0041 D3.3) out through a ``raw/`` sentence.
    """
    layout = _build(tmp_path)
    person = layout.root / "wiki/people/hando/secret.md"
    person.parent.mkdir(parents=True, exist_ok=True)
    person.write_text("# private\n\nHuman-owned, never curated.\n", encoding="utf-8")
    (layout.root / "raw/ai-tech/person.md").symlink_to(person)

    assert rawstore.resolve(layout, "raw/ai-tech/person.md") is None


@posix_symlinks
def test_a_symlink_to_an_in_raw_file_is_still_refused(tmp_path: Path) -> None:
    """Gate 3: identity, not resolution. Gate 2 passes here and the link is refused anyway.

    The target is an ordinary artefact the curator DID write; the link beside it is not, and the
    final-diff allowlist never admitted it. ``schema/notes.py`` takes the same posture when it
    enumerates notes — a symlink is skipped for WHAT IT IS, never graded on where it points.
    """
    layout = _build(tmp_path)
    (layout.root / "raw/ai-tech/alias.md").symlink_to(layout.root / TEXT_SOURCE)

    assert rawstore.resolve(layout, "raw/ai-tech/alias.md") is None
    assert rawstore.resolve(layout, TEXT_SOURCE) is not None  # the real one still reads


@posix_symlinks
def test_a_symlinked_ancestor_directory_is_refused(tmp_path: Path) -> None:
    """Gate 3 walks EVERY component below ``raw/``, not just the leaf."""
    layout = _build(tmp_path)
    outside = tmp_path.parent / "outside-tree"
    (outside / "nested").mkdir(parents=True, exist_ok=True)
    (outside / "nested" / "e9.md").write_text("outside\n", encoding="utf-8")
    (layout.root / "raw/linkdir").symlink_to(outside, target_is_directory=True)

    assert rawstore.resolve(layout, "raw/linkdir/nested/e9.md") is None


@posix_symlinks
def test_the_raw_directory_itself_being_a_symlink_is_refused(tmp_path: Path) -> None:
    """Gate 3 grades ``raw_dir``'s OWN identity — the one component gate 2 structurally cannot.

    If ``raw/`` is a symlink, ``raw_dir.resolve()`` follows the same link, so both sides of the
    containment test move together and every path under the target tree "contains". Git stores
    symlinks, so this shape arrives through a clone, an import or a push to a hub repo an operator
    later serves — and without this check both read faces become an arbitrary-file server for the
    target tree (§4 T3). Only ``raw_dir`` itself is graded: its ANCESTORS stay unchecked, because a
    symlinked worktree is legitimate repo shape and is not something a repo's contents can choose.
    """
    layout = _build(tmp_path)
    outside = tmp_path.parent / "outside-raw-target"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "passwd").write_text("SECRET-OUTSIDE-TREE\n", encoding="utf-8")

    real_raw = layout.root / "raw"
    for path in sorted(real_raw.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    real_raw.rmdir()
    real_raw.symlink_to(outside, target_is_directory=True)

    assert rawstore.resolve(layout, "raw/passwd") is None
    # And no other door opens: the composed twin a caller might reach for is refused too.
    assert rawstore.resolve(layout, "raw/passwd.meta.yaml") is None


# --- classification is spelling-insensitive, because the filesystem may be ------------------------


@pytest.mark.parametrize("prefix", ["raw/_blob", "raw/_BLOB", "raw/_Blob"])
def test_a_blob_is_classified_blob_however_the_prefix_is_spelled(
    tmp_path: Path, prefix: str
) -> None:
    """On APFS/NTFS the gates resolve a case-flipped ``_blob`` to the real file. Kind must follow.

    A case-SENSITIVE classifier calls that file ``"text"``, and the text branch is the one that
    reads the bytes into a payload field — a direct bypass of D5 ("bytes are not served over MCP",
    normative) and of D8's hardened attachment download path. Casefolding is a strict tightening:
    on a case-sensitive filesystem the flipped spelling does not resolve at all, so the assertion
    is conditional on a ref coming back, and NO ref is ever allowed to come back as ``"text"``.
    """
    layout = _build(tmp_path)
    _write_blob(layout)
    spelled = BLOB_REF.replace(BLOB_PREFIX, prefix, 1)

    ref = rawstore.resolve(layout, spelled)

    if ref is not None:
        assert ref.kind == "blob"
        assert ref.sidecar_rel_path == f"{spelled}{SIDECAR_SUFFIX}"  # composed VERBATIM
        assert rawstore.read_sidecar(layout, ref) == _sidecar_doc()


@pytest.mark.parametrize("suffix", [".meta.yaml", ".META.YAML", ".Meta.Yaml"])
def test_a_sidecar_is_classified_sidecar_however_the_suffix_is_spelled(
    tmp_path: Path, suffix: str
) -> None:
    """D9's "a sidecar is not a citable artefact" must not be spelling-dependent either.

    Classified ``"blob"``, a case-flipped sidecar becomes directly SERVABLE — contradicting lint
    L1-8b's citation space, which the URL space is required to mirror.
    """
    layout = _build(tmp_path)
    _write_blob(layout)
    spelled = f"{BLOB_REF}{suffix}"

    ref = rawstore.resolve(layout, spelled)

    if ref is not None:
        assert ref.kind == "sidecar"
        assert ref.sidecar_rel_path is None


# --- the one URL conversion site (D6) -------------------------------------------------------------


def test_web_href_drops_exactly_one_prefix_segment_and_escapes_the_rest() -> None:
    """D6 lives HERE so no face can grow a second spelling of it (``_raw_href`` is its alias)."""
    assert rawstore.web_href("raw/ai-tech/e1.md") == "/raw/ai-tech/e1.md"
    assert rawstore.web_href(BLOB_REF) == f"/{BLOB_REF}"
    # Segments stay segments; everything a URL would otherwise reinterpret is escaped.
    assert rawstore.web_href("raw/general/보고서 2026.md").startswith("/raw/general/")
    assert " " not in rawstore.web_href("raw/general/보고서 2026.md")
    assert rawstore.web_href("raw/general/a.md?v=2#x") == "/raw/general/a.md%3Fv%3D2%23x"


# --- resource bounds -----------------------------------------------------------------------------


def test_oversize_text_is_truncated_and_says_so(tmp_path: Path) -> None:
    """§4 T9: the 25 MiB cap is a WRITE control; the read side has :data:`MAX_RAW_TEXT_BYTES`."""
    layout = _build(tmp_path)
    big = layout.root / "raw/general/big.md"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_text("x" * (rawstore.MAX_RAW_TEXT_BYTES + 4096), encoding="utf-8")

    ref = rawstore.resolve(layout, "raw/general/big.md")
    assert ref is not None

    text, truncated = rawstore.read_text(ref)
    assert truncated is True
    assert len(text) <= rawstore.MAX_RAW_TEXT_BYTES

    # An artefact exactly AT the bound is whole, not truncated (the boundary is inclusive).
    exact = layout.root / "raw/general/exact.md"
    exact.write_text("y" * 32, encoding="utf-8")
    at_bound = rawstore.resolve(layout, "raw/general/exact.md")
    assert at_bound is not None
    assert rawstore.read_text(at_bound, max_bytes=32) == ("y" * 32, False)


def test_read_text_replaces_undecodable_bytes_instead_of_raising(tmp_path: Path) -> None:
    """ADR-0014 D1 tolerant consumer: one bad byte degrades a file, it does not fail the read."""
    layout = _build(tmp_path)
    bad = layout.root / "raw/general/bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"caf\xe9 \xff not utf-8\n")

    ref = rawstore.resolve(layout, "raw/general/bad.md")
    assert ref is not None
    text, truncated = rawstore.read_text(ref)
    assert "�" in text
    assert truncated is False


def test_read_sidecar_refuses_an_oversize_record(tmp_path: Path) -> None:
    """A hand-editable git-tracked YAML file is not allowed to be a memory bomb."""
    layout = _build(tmp_path)
    _write_blob(layout)
    (layout.root / SIDECAR_REF).write_text(
        "sha256: " + "a" * (rawstore.MAX_RAW_TEXT_BYTES + 1) + "\n", encoding="utf-8"
    )
    ref = rawstore.resolve(layout, BLOB_REF)
    assert ref is not None
    assert rawstore.read_sidecar(layout, ref) is None


# --- schema independence -------------------------------------------------------------------------


def test_a_schema_1_repo_behaves_identically(tmp_path: Path) -> None:
    """No ``schema_version`` branching exists in the module, and this is what holds it that way.

    ``raw/`` is byte-identical across the flip (ADR-0041 D1.4 never moves it) — which is exactly
    what keeps every stored ``sources:`` string resolvable after a repo converts.
    """
    v1 = _build(tmp_path / "v1", schema_version=1)
    v2 = _build(tmp_path / "v2", schema_version=2)
    _write_blob(v1)
    _write_blob(v2)

    for layout in (v1, v2):
        text_ref = rawstore.resolve(layout, TEXT_SOURCE)
        blob = rawstore.resolve(layout, BLOB_REF)
        assert text_ref is not None and text_ref.kind == "text"
        assert blob is not None and blob.kind == "blob"
        assert rawstore.read_sidecar(layout, blob) == _sidecar_doc()
        assert rawstore.resolve(layout, "raw/../wiki/concepts/x.md") is None

    v1_ref = rawstore.resolve(v1, TEXT_SOURCE)
    v2_ref = rawstore.resolve(v2, TEXT_SOURCE)
    assert v1_ref is not None and v2_ref is not None
    assert (v1_ref.kind, v1_ref.rel_path) == (v2_ref.kind, v2_ref.rel_path)


# --- the module creates nothing ------------------------------------------------------------------


def test_nothing_is_ever_created_on_disk(tmp_path: Path) -> None:
    """Invariant 2: this is reader-class code. Not one refusal or read may leave a trace.

    Includes the refused paths — a gate that ``mkdir(parents=True)``s on its way to saying no is a
    gate that writes outside the repo.
    """
    layout = _build(tmp_path)
    _write_blob(layout)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))

    for candidate in (
        TEXT_SOURCE,
        BLOB_REF,
        SIDECAR_REF,
        "raw/ai-tech/nope.md",
        "raw/../wiki/concepts/x.md",
        "raw/brand/new/deep/tree.md",
        "/etc/passwd",
    ):
        ref = rawstore.resolve(layout, candidate)
        if ref is not None:
            rawstore.read_text(ref)
            rawstore.read_sidecar(layout, ref)

    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before
