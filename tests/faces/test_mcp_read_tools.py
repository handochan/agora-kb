"""Tests for the read-side MCP tools ``kb_read`` / ``kb_neighbors`` (#58) and its ``raw/`` bridge.

Both tools are pure WIRING in :func:`build_server` — ``kb_read`` delegates to the already-tested
:meth:`AgoraHandlers.note`, then to :meth:`AgoraHandlers.raw` (the DRILLDOWN-169 provenance
drill-down), and ``kb_neighbors`` to :meth:`AgoraHandlers.graph` (the ADR-0021 ego-graph) — so
these tests drive them through a REAL ``fastmcp.Client`` over the protocol, the only place the
wrapper logic (not-found shaping, the raw bridge, description text) lives. The note corpus is the
graph test fixture reused verbatim (index + MOC + two themes; see ``test_mcp_server_graph``); the
``raw/`` corpus is a ``build_kb`` fixture, because only the builder writes a ``raw/_blob/``
artefact in APPLY's shape (sidecar key set and order included) without hand-copied YAML.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
from fastmcp import Client

from agora_kb.core import Repo
from agora_kb.core.layout import SIDECAR_SUFFIX, blob_ref
from agora_kb.core.wiki import MAX_HITS
from agora_kb.faces import mcp_server
from agora_kb.faces.mcp_server import AgoraHandlers, build_server
from tests.faces.test_mcp_server_graph import _init_repo, _write_wiki_notes
from tests.support.kb_builder import NoteSpec, build_kb

_CENTER = "wiki/concepts/curator-concurrency.md"
_INBOX = "wiki/concepts/inbox-design.md"
_MAP = "wiki/maps/ai-tech.md"

#: Real symlinks require an elevated privilege on Windows (the ``tests/core/test_rawstore.py``
#: posture).
posix_symlinks = pytest.mark.skipif(
    os.name != "posix", reason="real symlinks require privilege on Windows"
)

# --- the raw/ corpus ----------------------------------------------------------------------------
_RAW_TEXT_SOURCE = "raw/ai-tech/e1.md"
_RAW_TEXT = "# Extracted\n\nA curator advances one repo at a time.\n"

#: A raw text capture carrying a LIVE gold-pack sentinel span — a pack that was harvested back into
#: the KB is exactly how one gets there (``curator/apply.py`` materialises the event body verbatim
#: and strips nothing).
_RAW_SENTINEL_SOURCE = "raw/ai-tech/e2.md"
_RAW_SENTINEL_TEXT = (
    "<!-- agora:pack default v1 sha=abc -->\n"
    "# Standing context\n\nA pack body that came back in through a capture.\n"
    "<!-- agora:pack:end default -->\n"
)

#: NOT text under any codec: a NUL, a lone ``0xff`` and a CRLF, so a text-mode read or a decode
#: attempt anywhere on the path corrupts it into a visible failure instead of passing by luck.
_BLOB_BYTES = b"%PDF-1.7\r\n\x00\xff\xfe binary payload \x00\r\nnot text\n"
_BLOB_SHA = hashlib.sha256(_BLOB_BYTES).hexdigest()
_BLOB_REF = blob_ref(_BLOB_SHA, "pdf")
_SIDECAR_REF = f"{_BLOB_REF}{SIDECAR_SUFFIX}"

#: The capture facts APPLY records beside the bytes, minus the three it derives from the blob
#: itself (``sha256`` / ``ext`` / ``bytes``, which the builder writes).
_SIDECAR_FACTS: dict[str, object] = {
    "media_type": "application/pdf",
    "filename": "2026-q3-report.pdf",
    "captured_at": "2026-06-13T02:40:10Z",
    "writer": "dochan",
    "source": "web:dochan",
    "event_id": "01J8ZQ3M4N5P6Q7R8S9T0V1W2X",
}


def _server(tmp_path: Path):
    # `_init_repo` (shared with the graph tests) is what declares KB wiki schema 2 in
    # `_meta/taxonomy.yaml` — the corpus below is kind-first, so it must be read as such.
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    return build_server(repo_path=tmp_path, writer="local")


def _call(server: object, tool: str, args: dict[str, object]) -> dict[str, object]:
    async def _run() -> dict[str, object]:
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            return result.data

    return asyncio.run(_run())


def _raw_repo(tmp_path: Path, *, schema_version: int = 2) -> Path:
    """A KB with the whole ``raw/`` capture tier on disk: two text artefacts and one blob.

    Built by ``build_kb`` rather than by hand so the blob and its ``.meta.yaml`` carry APPLY's own
    shape — the closed key set, in APPLY's emission order, with the derived keys taken from the
    bytes actually written. ``raw/`` is byte-identical across both schemas (ADR-0041 D1.4), which is
    what lets ``schema_version`` be a parameter here rather than a second fixture.
    """
    build_kb(
        tmp_path,
        [
            NoteSpec(
                kind="theme",
                domain="ai-tech",
                title="Retrieval Augmented Generation",
                body="Retrieval augmented generation grounds an answer in retrieved documents.",
                sources=[_RAW_TEXT_SOURCE],
            )
        ],
        schema_version=schema_version,
        domains=["ai-tech", "general"],
        blobs=[(_BLOB_SHA, "pdf", _BLOB_BYTES, _SIDECAR_FACTS)],
    )
    # Overwrite the builder's synthetic evidence with content these tests assert on. The builder
    # only materializes a cited source that does not exist yet, so this is the artefact's content
    # from every reader's point of view.
    (tmp_path / _RAW_TEXT_SOURCE).write_text(_RAW_TEXT, encoding="utf-8", newline="\n")
    (tmp_path / _RAW_SENTINEL_SOURCE).write_text(_RAW_SENTINEL_TEXT, encoding="utf-8", newline="\n")
    return tmp_path


def _raw_server(tmp_path: Path, *, schema_version: int = 2):
    return build_server(
        repo_path=_raw_repo(tmp_path, schema_version=schema_version), writer="local"
    )


# --- kb_read ------------------------------------------------------------------------------------
def test_kb_read_returns_note_payload(tmp_path: Path) -> None:
    """Happy path: kb_read renders the EXACT note() payload — same data the web face serves."""
    data = _call(_server(tmp_path), "kb_read", {"path": _CENTER})

    assert set(data) == {
        "rel_path",
        "basename",
        "kind",
        "title",
        "status",
        "tags",
        "subjects",
        "frontmatter",
        "body",
        "links",
    }
    assert data["rel_path"] == _CENTER
    assert data["basename"] == "curator-concurrency"
    assert data["title"] == "Curator Concurrency Model"  # frontmatter title wins over basename
    assert data["status"] == "active"
    assert data["kind"] == "concept"
    assert data["subjects"] == ["ai-tech"]
    # Body stays RAW markdown (rendering is the consumer's job), with its H1 intact.
    assert data["body"].startswith("# Curator Concurrency")
    assert isinstance(data["frontmatter"], dict)
    # Links are the body graph-edge BASENAMES, not rel_paths: follow them via kb_neighbors on
    # this note's rel_path — its node ids (rel_paths) are what feed kb_read.
    assert data["links"] == ["inbox-design"]


def test_kb_read_not_found_and_traversal_safe(tmp_path: Path) -> None:
    """An unknown path AND every escape shape yield the clear not-found shape, never contents.

    The ``raw/``-prefixed vectors matter as much as the bare ones: the bridge added below composes
    a filesystem path out of this argument (``AgoraHandlers.note`` never did — it compares against
    notes it already enumerated), so a path that merely LOOKS like a citation is the new attack
    surface. All of them land on the SAME sentence a bad note path gets: kb_read has one not-found
    wording, and a caller cannot tell "outside the repo" from "not on disk" (DRILLDOWN-169 D2/D3).
    """
    server = _raw_server(tmp_path)
    # The wiki note the escape vectors are trying to reach through raw/, and a sentence only its
    # body contains — so "no contents" is asserted against real bytes, not against an absence.
    secret = "Retrieval augmented generation grounds"

    for path in (
        "wiki/concepts/nope.md",
        "../../etc/passwd",
        "/etc/passwd",
        "raw/ai-tech/nope.md",
        "raw/",  # the directory itself is not an artefact
        "raw/ai-tech",  # nor is a subdirectory
        "raw/../wiki/concepts/retrieval-augmented-generation.md",
        "raw/./ai-tech/../../index.md",
        "raw/../../etc/passwd",
        f"raw/_blob/../../{_RAW_TEXT_SOURCE}",
        "raw/_blob/../../etc/passwd",
        "wiki/concepts/retrieval-augmented-generation.md/../../../etc/passwd",
        # An embedded NUL makes CPython's realpath RAISE. Unless rawstore's gate 1 rejects it
        # textually, the ValueError escapes AgoraHandlers.raw() — breaking its own "three
        # statuses, never an exception" contract (D3) — and kb_read raises instead of answering.
        "raw/a\x00b.md",
        f"{_RAW_TEXT_SOURCE}\x00.png",
    ):
        data = _call(server, "kb_read", {"path": path})
        assert data["error"] == "not_found", path
        assert data["path"] == path
        assert "kb_query" in data["note"]  # actionable: points back to the hit source
        assert "body" not in data
        assert "text" not in data
        assert secret not in json.dumps(data)


def test_kb_read_serves_a_people_note(tmp_path: Path) -> None:
    """ADR-0041 D3.3: read is FIRST CLASS over ``wiki/people/**`` — kb_read opens one on demand.

    This is deliberately WIDER than the push surface: the same content is excluded from gold packs
    and therefore from ``kb_context`` (D3.3 day-1 exclusion), because a pull-shaped, agent-initiated
    read of a note a human filed in a shared repo is a different risk from a standing pack assembled
    without a prompt. ADR-0027 §8's scope names the read tools as an emission path whose control is
    distinct and still undesigned (residual R1) — so this test pins the CURRENT, decided behaviour,
    not an accident.
    """
    _init_repo(tmp_path)
    _write_wiki_notes(tmp_path)
    people = tmp_path / "wiki" / "people" / "hando"
    people.mkdir(parents=True, exist_ok=True)
    (people / "desk.md").write_text(
        "---\ntitle: Desk\nstatus: active\n---\n# Desk\n\nMy own notes.\n", encoding="utf-8"
    )
    server = build_server(repo_path=tmp_path, writer="local")

    data = _call(server, "kb_read", {"path": "wiki/people/hando/desk.md"})
    assert data["kind"] == "person"  # DERIVED from the directory; the note declares no kind
    assert data["body"].startswith("# Desk")
    assert data["subjects"] == []


# --- kb_read: the raw/ bridge (DRILLDOWN-169 §2 A2) ----------------------------------------------
def test_kb_read_serves_a_raw_text_artifact(tmp_path: Path) -> None:
    """A ``sources:`` string opens: the capture behind a curated claim, over the SAME tool.

    The payload's discriminator is ``resource: "raw"`` — not ``kind``, which stays the closed
    ADR-0041 note vocabulary lint/gold/graph/browse all key on (D4).
    """
    data = _call(_raw_server(tmp_path), "kb_read", {"path": _RAW_TEXT_SOURCE})

    assert set(data) == {
        "status",
        "resource",
        "raw_kind",
        "path",
        "text",
        "bytes",
        "truncated",
    }
    assert data["status"] == "ok"
    assert data["resource"] == "raw"
    assert data["raw_kind"] == "text"
    # The citation string comes back VERBATIM: the stored identity (ADR-0041 D3.4) is what makes
    # the note's `sources:` entry and this payload's `path` the same string on every face.
    assert data["path"] == _RAW_TEXT_SOURCE
    assert data["text"] == _RAW_TEXT
    # `bytes` is the artefact's size on disk, not len(text) — with `truncated` beside it that pair
    # says how much was left behind rather than agreeing with itself.
    assert data["bytes"] == len(_RAW_TEXT.encode("utf-8"))
    assert data["truncated"] is False


def test_kb_read_blob_returns_sidecar_and_never_bytes(tmp_path: Path) -> None:
    """D5 IS NORMATIVE: a ``raw/_blob/`` read returns capture FACTS and no bytes, ever.

    Four reasons, recorded here so a future "just add the bytes" change has to argue with all of
    them rather than with an omission:

    1. a 25 MiB PDF is ~33 MiB of base64 — 4/3 the token cost, for content no LLM can read;
    2. the sidecar already carries everything an agent needs to DECIDE (digest, media type, size,
       filename, capture provenance), and the web face serves the bytes to a human;
    3. ``grep -rn 'b64encode' src/`` is 0 hits: this codebase has never had a byte channel, and
       opening one turns the undesigned egress control (residual R1 / #166) from one surface into a
       content-type matrix;
    4. blob bytes are the ONLY repo content that passed neither the curator, nor the ADR-0007
       candidate gate, nor ADR-0023 redaction.

    Reversing this requires citing D5 and retiring it explicitly.
    """
    data = _call(_raw_server(tmp_path), "kb_read", {"path": _BLOB_REF})

    assert set(data) == {"status", "resource", "raw_kind", "path", "bytes", "meta", "note"}
    assert data["status"] == "ok"
    assert data["resource"] == "raw"
    assert data["raw_kind"] == "blob"
    assert data["path"] == _BLOB_REF
    assert data["bytes"] == len(_BLOB_BYTES)
    # No body, no text, no base64 field under ANY spelling — and no value anywhere in the payload
    # decodes to the blob's bytes, which is the assertion a smuggled field would have to survive.
    assert "body" not in data
    assert "text" not in data
    assert "content" not in data
    assert "base64" not in json.dumps(data).lower()
    for value in json.loads(json.dumps(data)).values():
        if isinstance(value, str):
            try:
                decoded = base64.b64decode(value, validate=True)
            except (ValueError, TypeError):
                continue
            assert decoded != _BLOB_BYTES
    # The capture record: APPLY's closed key set, reported as recorded and never invented.
    assert data["meta"] == {
        "sha256": _BLOB_SHA,
        "ext": "pdf",
        "bytes": len(_BLOB_BYTES),
        **_SIDECAR_FACTS,
    }
    # No top-level `sha256`: echoing the basename back as an integrity claim is a tautology
    # (ADR-0041 D1.4). The digest is a capture FACT inside `meta`, where it was recorded.
    assert "sha256" not in data
    assert "bytes are not served over MCP" in data["note"]


def test_kb_read_blob_without_a_sidecar_still_serves_the_artifact(tmp_path: Path) -> None:
    """A missing/corrupt capture record costs the caller the DESCRIPTION, never the artefact.

    ``meta: None`` is :meth:`AgoraHandlers.gold_pack`'s posture for its own advisory sidecar — the
    honest answer, distinguishable from "recorded as empty".
    """
    root = _raw_repo(tmp_path)
    (root / _SIDECAR_REF).unlink()

    data = _call(build_server(repo_path=root, writer="local"), "kb_read", {"path": _BLOB_REF})
    assert data["status"] == "ok"
    assert data["raw_kind"] == "blob"
    assert data["bytes"] == len(_BLOB_BYTES)
    assert data["meta"] is None


def test_kb_read_returns_raw_text_verbatim_including_a_live_sentinel(tmp_path: Path) -> None:
    """OD-3: a raw capture is served BYTE-FOR-BYTE, live ``agora:pack`` sentinel opener included.

    A gold pack that was harvested back into the KB sits in ``raw/`` verbatim (nothing on the way
    in strips a span), so this artefact really can carry one. The drill-down's whole purpose is
    "this is the byte the claim came from", and a display-time transform would make the served text
    disagree with the file — the one thing a provenance surface may not do.

    This does NOT weaken the ADR-0027 §8 loop-break contract, which lives on the WRITE side: the
    duty is the CONSUMER's (``core/sentinel.strip_sentinel_spans``, run by the harvester when text
    comes back IN as candidate facts), never the reader's. A reader that silently deleted a span
    would be answering a different question than the one it was asked.
    """
    data = _call(_raw_server(tmp_path), "kb_read", {"path": _RAW_SENTINEL_SOURCE})

    assert data["status"] == "ok"
    assert data["text"] == _RAW_SENTINEL_TEXT
    assert "<!-- agora:pack default v1 sha=abc -->" in data["text"]
    assert "<!-- agora:pack:end default -->" in data["text"]


def test_kb_read_sidecar_path_redirects(tmp_path: Path) -> None:
    """D9: ``*.meta.yaml`` is not a citable artefact — the dead end TEACHES lint L1-8b's rule.

    The handler answers with the rule and the artefact's path; ``kb_read`` keeps its single
    not-found wording for everything that did not resolve, so the tool surface gains no second
    vocabulary for "nothing here" (the wiring the brief pins).
    """
    root = _raw_repo(tmp_path)
    payload = AgoraHandlers(Repo.resolve(root), writer="local").raw(_SIDECAR_REF)

    assert set(payload) == {"status", "resource", "path", "note"}
    assert payload["status"] == "not_found"
    assert payload["resource"] == "raw"
    assert payload["path"] == _SIDECAR_REF
    assert "L1-8b" in payload["note"]
    assert _BLOB_REF in payload["note"]  # names the artefact to read instead
    # The sidecar's own fields are NOT lost — they are reachable, on the blob's payload, where the
    # citation space says they belong.
    assert payload["note"].endswith(_BLOB_REF)

    data = _call(build_server(repo_path=root, writer="local"), "kb_read", {"path": _SIDECAR_REF})
    assert data["error"] == "not_found"
    assert data["path"] == _SIDECAR_REF
    # The raw seam's D9 guidance LEADS (a kb_query hit never names a raw/ path, so the wiki
    # sentence alone would misdirect), and the wiki sentence still follows — one composer.
    assert data["note"].startswith("a sidecar is not a citable artefact (lint L1-8b)")
    assert _BLOB_REF in data["note"]
    assert "kb_query" in data["note"]


def test_kb_read_missing_raw_path_carries_the_raw_seam_guidance(tmp_path: Path) -> None:
    """A raw/-shaped miss explains itself in raw/ terms, then points at the wiki protocol."""
    root = _raw_repo(tmp_path)
    data = _call(
        build_server(repo_path=root, writer="local"), "kb_read", {"path": "raw/general/nope.md"}
    )
    assert data["error"] == "not_found" and data["path"] == "raw/general/nope.md"
    assert data["note"].startswith("no readable artefact at 'raw/general/nope.md'")
    assert "`sources:`" in data["note"] and "kb_query" in data["note"]
    # A wiki-shaped miss is unchanged: the wiki sentence alone, byte-identical to before.
    wiki = _call(
        build_server(repo_path=root, writer="local"), "kb_read", {"path": "wiki/concepts/nope.md"}
    )
    assert wiki["note"].startswith("no tracked note at path='wiki/concepts/nope.md'")


def test_the_resource_key_marks_raw_payloads_only(tmp_path: Path) -> None:
    """D4: the discriminator is ``resource``, and it appears on raw payloads and NOWHERE else.

    Putting it on a note payload would break that payload's key-set equality — the shape #58
    shipped and the web face shares — for a key a note reader has no use for.
    """
    server = _raw_server(tmp_path)

    note = _call(server, "kb_read", {"path": "wiki/concepts/retrieval-augmented-generation.md"})
    assert "resource" not in note
    assert note["kind"] == "concept"

    raw = _call(server, "kb_read", {"path": _RAW_TEXT_SOURCE})
    assert raw["resource"] == "raw"
    assert "kind" not in raw  # `raw_kind`, so the closed note vocabulary is never overloaded


def test_kb_read_invalid_and_missing_raw_paths_are_distinguishable_to_the_handler(
    tmp_path: Path,
) -> None:
    """D3: the handler separates ``invalid_path`` from ``not_found``; kb_read folds both.

    ``../../etc/passwd`` and ``raw/ai-tech/gone.md`` are different events for a caller that can act
    on them (a CLI exit code, a web 404 vs 400), which is why the shared seam is a status dict and
    not ``None``. A path that is SPELLED as a citation but refused by the filesystem gates is
    ``not_found`` with no explanation — the disclosure a status-per-reason would create.
    """
    handlers = AgoraHandlers(Repo.resolve(_raw_repo(tmp_path)), writer="local")

    assert handlers.raw("../../etc/passwd")["status"] == "invalid_path"
    assert handlers.raw("/etc/passwd")["status"] == "invalid_path"
    assert handlers.raw("raw/../wiki/concepts/x.md")["status"] == "invalid_path"
    assert handlers.raw("wiki/concepts/x.md")["status"] == "invalid_path"
    assert handlers.raw("")["status"] == "invalid_path"
    assert handlers.raw("raw/ai-tech/gone.md")["status"] == "not_found"
    assert handlers.raw(_RAW_TEXT_SOURCE)["status"] == "ok"


@posix_symlinks
def test_kb_read_refuses_a_symlink_inside_raw_and_leaks_nothing(tmp_path: Path) -> None:
    """A symlink under ``raw/`` is refused by IDENTITY — pointing back INSIDE the repo is no help.

    Both directions are covered: out of the repo entirely, and at ``wiki/people/**``, the
    human-owned namespace ADR-0041 D3.3 keeps out of the curated wiki. Laundering either one
    through a ``raw/`` sentence is the hole a root-relative containment test leaves open.
    """
    root = _raw_repo(tmp_path)
    outside = tmp_path.parent / "outside.md"
    outside.write_text("OUTSIDE-SECRET\n", encoding="utf-8")
    people = root / "wiki" / "people" / "hando"
    people.mkdir(parents=True, exist_ok=True)
    (people / "desk.md").write_text("PEOPLE-SECRET\n", encoding="utf-8")
    (root / "raw" / "ai-tech" / "escape.md").symlink_to(outside)
    (root / "raw" / "ai-tech" / "person.md").symlink_to(people / "desk.md")

    server = build_server(repo_path=root, writer="local")
    for path in ("raw/ai-tech/escape.md", "raw/ai-tech/person.md"):
        data = _call(server, "kb_read", {"path": path})
        assert data["error"] == "not_found", path
        blob = json.dumps(data)
        assert "OUTSIDE-SECRET" not in blob
        assert "PEOPLE-SECRET" not in blob


def test_kb_read_serves_raw_on_a_schema_1_repo(tmp_path: Path) -> None:
    """``raw/`` is byte-identical across both schemas (ADR-0041 D1.4), so the bridge is too.

    A schema-1 repo is READ-ONLY in this build, not unreadable — the drill-down has to work there
    or the provenance loop closes only for repos written after the flip.
    """
    data = _call(_raw_server(tmp_path, schema_version=1), "kb_read", {"path": _RAW_TEXT_SOURCE})
    assert data["status"] == "ok"
    assert data["text"] == _RAW_TEXT


# --- query: the additive limit kwarg (A5 needs it; the default must not move) --------------------
def test_query_limit_is_additive_and_defaults_to_max_hits(tmp_path: Path) -> None:
    """``limit`` reaches the core, and its DEFAULT is the core's own — so no face's size changed.

    The kwarg exists for the CLI read verb (``agora query --limit``), which must not grow a second
    query path to vary one number. The default is asserted against
    :data:`~agora_kb.core.wiki.MAX_HITS` rather than a literal: a moved default would silently
    change how much MCP and the web return.
    """
    import inspect

    handlers = AgoraHandlers(Repo.resolve(_raw_repo(tmp_path)), writer="local")
    assert inspect.signature(handlers.query).parameters["limit"].default == MAX_HITS

    everything = handlers.query("retrieval augmented generation")
    assert everything["status"] == "ok"
    assert len(everything["hits"]) >= 1
    assert len(handlers.query("retrieval augmented generation", limit=1)["hits"]) == 1


# --- kb_neighbors -------------------------------------------------------------------------------
def test_kb_neighbors_depth1_returns_ego_graph(tmp_path: Path) -> None:
    """depth=1 around a theme: undirected 1-hop reach + induced edges, center/depth echoed."""
    data = _call(_server(tmp_path), "kb_neighbors", {"path": _CENTER})

    assert data["center"] == _CENTER
    assert data["depth"] == 1
    assert data["truncated"] is False
    ids = {n["id"] for n in data["nodes"]}
    # 1 hop from curator-concurrency: the MOC links TO it, and it links to inbox-design.
    assert ids == {
        _CENTER,
        _MAP,
        _INBOX,
    }
    assert data["node_total"] == 3
    # Induced directed edges among the reached set (moc→cc, moc→inbox, cc→inbox).
    edges = {(e["source"], e["target"]) for e in data["edges"]}
    assert edges == {
        (_MAP, _CENTER),
        (_MAP, _INBOX),
        (_CENTER, _INBOX),
    }
    # Every node carries the id (rel_path — feeds kb_read) + title label.
    for node in data["nodes"]:
        assert isinstance(node["id"], str) and node["id"]
        assert isinstance(node["title"], str) and node["title"]


def test_kb_neighbors_depth_is_clamped_by_existing_cap(tmp_path: Path) -> None:
    """An oversized depth is clamped to graph()'s existing MAX_GRAPH_DEPTH cap and echoed."""
    data = _call(_server(tmp_path), "kb_neighbors", {"path": _CENTER, "depth": 99})

    assert data["depth"] == mcp_server.MAX_GRAPH_DEPTH
    # At the clamped depth the 2-hop index.md is reachable too — the whole 4-note corpus.
    ids = {n["id"] for n in data["nodes"]}
    assert "index.md" in ids
    assert data["node_total"] == 4


def test_kb_neighbors_unknown_path_returns_empty_graph_with_note(tmp_path: Path) -> None:
    """An unknown center is SAFE: empty graph, center=null, plus the actionable not-found note."""
    data = _call(_server(tmp_path), "kb_neighbors", {"path": "wiki/concepts/ghost.md"})

    assert data["center"] is None
    assert data["nodes"] == []
    assert data["edges"] == []
    assert data["node_total"] == 0
    assert "kb_query" in data["note"]


# --- descriptions carry the navigation protocol (#58) -------------------------------------------
def test_read_tool_descriptions_carry_navigation_protocol(tmp_path: Path) -> None:
    """Both new tools teach the query → read → neighbors → re-query loop in their descriptions."""
    server = _server(tmp_path)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}

    read_desc = tools["kb_read"].description or ""
    neighbors_desc = tools["kb_neighbors"].description or ""
    for desc in (read_desc, neighbors_desc):
        assert "Navigation protocol" in desc
        assert "kb_query" in desc  # the loop's entry and re-query step
    assert "kb_neighbors" in read_desc  # kb_read points at the next step…
    assert "kb_read" in neighbors_desc  # …and kb_neighbors points back into reading
    # The drill-down hop is learnable from the tool surface itself, or agents never take it: a
    # `sources:` string is only a dead label until the description says it is also a kb_read path.
    assert "sources" in read_desc
    assert "raw/" in read_desc
