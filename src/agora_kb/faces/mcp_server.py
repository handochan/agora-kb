"""MCP server face — the 7 agent tools over the core API (DESIGN §5.1).

This is the **agent-facing** face: a single FastMCP registration that exposes ``kb_remember`` /
``kb_query`` / ``kb_read`` / ``kb_neighbors`` / ``kb_context`` / ``kb_status`` / ``kb_curate`` to
every MCP client (Claude Code, Codex, Qwen, Hermes, …), plus the ``agora://gold/{pack}`` resource
and the ``gold_context`` prompt (the ADR-0027 Phase-C gold consumption channels, #40). It is a thin
face: it adds no concurrency, provenance, or access-control logic of its own — those are properties
of the core (DESIGN §2.1). Each tool delegates straight to the core API (or an already-shipped read
handler below):

- ``kb_remember``  → :meth:`agora_kb.core.inbox.Inbox.write` (the only write path; non-blocking).
- ``kb_query``     → :meth:`agora_kb.core.wiki.Wiki.query` (the deterministic, model-free read
  path).
- ``kb_read``      → :meth:`AgoraHandlers.note` (the web face's per-note read payload, reused
  verbatim — body + frontmatter + outgoing links; #58).
- ``kb_neighbors`` → :meth:`AgoraHandlers.graph` (the ADR-0021 local ego-graph around a note,
  ``depth`` clamped by the existing cap; #58).
- ``kb_context``   → :meth:`AgoraHandlers.gold_pack` (the built ADR-0027 gold pack, served
  byte-identically from ``_kb/gold/``; the same handler backs the ``agora://gold/{pack}`` resource,
  the ``gold_context`` prompt, and the web ``GET /api/gold/{pack}``; #40).
- ``kb_status``    → :class:`agora_kb.core.state.StateStore` + live inbox/failed counts (meta
  face).
- ``kb_curate``    → :func:`agora_kb.curator.worker.recover` + :func:`agora_kb.curator.worker.run`
  (a real consolidation run against the configured ``adapters.yaml`` backend; see its note below).

**The agentic navigation loop (#58).** ``kb_query`` returns flat hits (path/line/excerpt) —
citations, not synthesis. ``kb_read`` / ``kb_neighbors`` are its read-side companions: an MCP-only
agent (no filesystem access) can now OPEN a cited note and WALK its link neighborhood, closing
DESIGN's "index → ``[[links]]`` → grep" navigation model over pure MCP. The intended loop —
broad ``kb_query`` → ``kb_read`` a hit → ``kb_neighbors`` to follow links → re-query with sharper
terms — is spelled out in each tool description so agents learn it from the tool surface itself.
Wiring only: both delegate to handlers the web face already ships, the core is untouched, and the
``QueryResult`` / ``SearchHit`` shapes stay frozen (``kb_query`` unchanged).

**Testability split (the architecture this module is built around).** The cognitive part — turning
a tool call into a core-API call and shaping a JSON-serializable result — lives in
:class:`AgoraHandlers`, which has no dependency on a live MCP transport and is unit-testable
directly. :func:`build_server` is the only transport-aware part: it constructs the handlers and
registers seven thin tool wrappers (plus the gold resource/prompt) that call them — one-line
delegation, except that ``kb_read`` / ``kb_neighbors`` keep their not-found shaping in the closure
(#58) and the gold resource/prompt shape their not-built outcome in the closure (#40). So the
handler logic is exercised by ordinary unit tests with no server I/O, the wiring by a single smoke
test, and the closure-only shaping by real-Client tests (``tests/faces/test_mcp_read_tools.py``,
``tests/faces/test_gold_consumption.py``).

The ``writer`` identity is **server configuration** (the connecting agent's identity), not a tool
argument — a client cannot spoof another writer's inbox namespace (tenant isolation, invariant 5).
For the local stdio MVP it defaults to ``"local"``.
"""

from __future__ import annotations

import argparse
import posixpath
from pathlib import Path
from typing import TYPE_CHECKING

from agora_kb.config import (
    MAX_SUPPORTED_KB_SCHEMA_VERSION,
    guard_repo_schema_version,
    load_backend_registry,
    load_repo_config,
)
from agora_kb.core import Inbox, Repo, StateStore, Wiki, failed_event_count, rawstore
from agora_kb.core.inbox import assert_writable_repo_schema
from agora_kb.core.layout import CLAIM_BEARING_KINDS, SIDECAR_SUFFIX, RepoLayout
from agora_kb.core.wiki import MAX_HITS
from agora_kb.curator.constants import DEFAULT_BODY_BYTE_BOUND
from agora_kb.curator.subprocess_backend import (
    RoutedBackend,
    SubprocessBackend,
    build_routed_backend,
)
from agora_kb.curator.worker import recover, run

if TYPE_CHECKING:
    from datetime import datetime

    from fastmcp import FastMCP

__all__ = ["AgoraHandlers", "build_server", "main"]

# Default writer identity for the local stdio MVP. In team mode this comes from the authenticated
# connection, never from a tool argument (DESIGN §5.1 / §7).
DEFAULT_WRITER = "local"
# The MCP source tag used for captures that arrive without a more specific origin. ``manual`` is one
# of the FIXED_SOURCES the core accepts (DATA-MODEL §1). An agent that wants its capture attributed
# to ITSELF passes ``agent:<name>`` instead (issue #147) — the parametric form that needs no core
# change and no blessed name; the fixed names stay for back-compat with events already on disk.
DEFAULT_SOURCE = "manual"

# Knowledge-graph viz bounds (faces/web /graph seam — ADR-0019 §7 / graph-plan). The global graph is
# soft-capped so a very large KB stays responsive in the browser; when the kept node set exceeds the
# cap it is truncated to the first MAX_GRAPH_NODES by sorted rel_path and ``truncated`` is set (the
# honest signal — node_total still reports the true pre-cap count, never silently dropped). These
# the CONFIGURABLE DEFAULTS (ADR-0025): the web face's :class:`WebConfig` graph caps override them
# per-call via ``graph(max_nodes=…, max_depth=…)``; the default is raised LARGE so a sizable KB
# renders out of the box (the honest-truncation behaviour is unchanged).
MAX_GRAPH_NODES = 10_000  # soft-cap default for the global graph; honest truncated flag (ADR-0025)
MAX_GRAPH_DEPTH = 3  # clamp default for the local ego-graph depth (ADR-0025)

# The residue bucket of :meth:`AgoraHandlers.health`'s kind census: a note whose DERIVED kind is
# outside the ADR-0041 vocabulary — a schema-2 note under an unknown `wiki/<dir>/` or directly
# under `wiki/` (the L1-22 population), or a schema-1 note with an unrecognised `type:`. It exists
# so the census sums to `note_total`; the panel that shows a count must not quietly drop the very
# notes the closed directory vocabulary was introduced to surface.
UNKNOWN_KIND = "unknown"


class AgoraHandlers:
    """Transport-free handlers for the 7 MCP tools (DESIGN §5.1).

    Construct with the repo and the connecting writer identity; each method maps a tool call to a
    core-API call and returns a plain, JSON-serializable ``dict``. No FastMCP / transport types
    appear here, so these methods are unit-testable without a running server.
    """

    def __init__(self, repo: Repo, writer: str = DEFAULT_WRITER) -> None:
        self._repo = repo
        self._writer = writer
        self._inbox = Inbox(repo.layout)
        self._wiki = Wiki(repo.layout)
        self._state = StateStore(repo.layout)

    @property
    def writer(self) -> str:
        return self._writer

    # --- write ----------------------------------------------------------------------------------
    def remember(
        self,
        text: str,
        *,
        target: str = "personal",
        domain: str | None = None,
        tags: list[str] | None = None,
        source: str = DEFAULT_SOURCE,
    ) -> dict[str, object]:
        """``kb_remember``: append one immutable inbox event; return ``{id, queued, inbox_depth}``.

        Non-blocking — it only appends to the per-writer inbox (CQRS write side, DESIGN §2.2); the
        captured item becomes queryable only after the curator consolidates (eventual consistency).
        The ``writer`` is fixed by server config, never taken from the caller.
        """
        receipt = self._inbox.write(
            text=text,
            writer=self._writer,
            source=source,
            target=target,
            domain=domain,
            tags=tags,
        )
        return receipt.as_dict()

    # --- read -----------------------------------------------------------------------------------
    def query(self, question: str, *, limit: int = MAX_HITS) -> dict[str, object]:
        """``kb_query``: deterministic evidence search; render the :class:`QueryResult` faithfully.

        Returns ``{query, status, hits}`` where ``status`` is ``'ok'`` or ``'not_found'`` and each
        hit carries ``{repo, path, anchor, line, excerpt, match_reason, score}`` citations.

        ``limit`` is passed straight through to :meth:`agora_kb.core.wiki.Wiki.query` and defaults
        to :data:`~agora_kb.core.wiki.MAX_HITS` — the SAME default the core applies, so this kwarg
        is purely additive: every existing caller (the ``kb_query`` tool, the web face) keeps the
        exact result size it had. It exists for the CLI read verb's ``--limit`` (DRILLDOWN-169 A5),
        which must not re-implement a second query path to vary one number. Changing the default
        here would silently change how much every face returns.
        """
        result = self._wiki.query(question, limit=limit)
        return {
            "query": result.query,
            "status": result.status,
            "hits": [
                {
                    "repo": hit.repo,
                    "path": hit.path,
                    "anchor": hit.anchor,
                    "line": hit.line,
                    "excerpt": hit.excerpt,
                    "match_reason": hit.match_reason,
                    "score": hit.score,
                }
                for hit in result.hits
            ],
        }

    # --- browse (read; web face, ADR-0003/0019) -------------------------------------------------
    def browse(self) -> dict[str, object]:
        """List every wiki note for the browse face; return ``{notes: [...], subjects: [...]}``.

        A thin shaping of :meth:`agora_kb.core.wiki.Wiki.list_notes` into JSON-serializable dicts —
        one ``{rel_path, basename, kind, title, status, tags, subjects}`` per note, plus the sorted
        union of every subject any note declares. ``title`` falls back to ``basename``. Notes are
        sorted by ``rel_path`` for a deterministic listing. The body markdown is intentionally NOT
        included here — that is the per-note :meth:`note` payload.

        **``domains`` became ``subjects`` and ``type`` became ``kind`` (ADR-0041 D2/D3.2), and the
        old keys are GONE rather than mirrored.** The subject is now a LIST (0..n) read from
        ``subjects:``, so a scalar ``domain`` could not carry it without silently picking one; and
        ``type`` is retired as the kind authority, so echoing it would re-publish the axis this
        wave flipped. Both values are derived per-schema by
        :func:`~agora_kb.schema.notes.parse_all_notes`, so a schema-1 repo browses correctly too:
        its single path domain arrives as a one-element ``subjects`` and its ``type:`` as the
        mapped ``kind`` (D2.5). ``wiki/people/**`` IS listed — read is first class (D3.3).
        """
        rows = [self._note_summary(note) for note in self._wiki.list_notes()]
        rows.sort(key=lambda r: r["rel_path"])  # type: ignore[arg-type, return-value]
        subject_values: set[str] = set()
        for row in rows:
            row_subjects = row["subjects"]
            if isinstance(row_subjects, list):
                subject_values.update(s for s in row_subjects if isinstance(s, str))
        subjects = sorted(subject_values)
        return {"notes": rows, "subjects": subjects}

    def note(self, rel_path: str) -> dict[str, object] | None:
        """Return one note's full read payload by ``rel_path``, or ``None`` if not tracked.

        Shapes :meth:`agora_kb.core.wiki.Wiki.get_note` into the browse summary fields plus the raw
        ``frontmatter``, the raw markdown ``body`` (rendering to HTML is the web layer's job, never
        the core/face's), and ``links`` — the body graph-edge basenames via
        :func:`agora_kb.schema.notes.body_link_basenames`. Path-safety is the core's: an untracked
        or traversal path resolves to no note, so this returns ``None``.
        """
        note = self._wiki.get_note(rel_path)
        if note is None:
            return None
        from agora_kb.schema.notes import body_link_basenames

        payload = self._note_summary(note)
        # ``kind`` is KEPT here (v1's ``type`` was popped, because it was already in
        # ``frontmatter``): the schema-2 kind is DERIVED from the directory (ADR-0041 D2.1) and on
        # a schema-1 repo it is derived from ``type:``, so it is genuinely not a duplicate of any
        # frontmatter key the caller can read for itself.
        payload["frontmatter"] = note.frontmatter
        payload["body"] = note.body
        payload["links"] = body_link_basenames(note.body)
        return payload

    @staticmethod
    def _note_summary(note: object) -> dict[str, object]:
        """Derive the shared browse summary fields from a :class:`~agora_kb.schema.notes.Note`.

        ``title`` ← ``frontmatter["title"]`` (else ``basename``); ``status`` ←
        ``frontmatter.status`` (may be ``None``); ``tags`` ← ``frontmatter.tags`` (``[]`` when
        absent/non-list); ``kind`` ← the note's derived kind (the DIRECTORY on schema 2, the frozen
        ``type:`` table on schema 1 — ADR-0041 D2.1/D2.5, ``None`` when neither yields one);
        ``subjects`` ← the note's derived subjects as a plain list (``subjects:`` on schema 2, the
        single path domain on schema 1 — D2.2; ``[]`` is legal and honest).

        The two schema-2 fields are read straight off the :class:`~agora_kb.schema.notes.Note`
        rather than re-derived from the path here: ADR-0041 D3.2 leaves exactly one place a
        subject is recorded, and a face computing its own would be a second one.
        """
        fm = note.frontmatter  # type: ignore[attr-defined]
        raw_title = fm.get("title")
        title = raw_title if isinstance(raw_title, str) and raw_title else note.basename  # type: ignore[attr-defined]
        raw_tags = fm.get("tags")
        tags = list(raw_tags) if isinstance(raw_tags, list) else []
        return {
            "rel_path": note.rel_path,  # type: ignore[attr-defined]
            "basename": note.basename,  # type: ignore[attr-defined]
            "kind": note.kind,  # type: ignore[attr-defined]
            "title": title,
            "status": fm.get("status"),
            "tags": tags,
            "subjects": list(note.subjects),  # type: ignore[attr-defined]
        }

    # --- raw/ captures (read; the provenance drill-down — #169) ---------------------------------
    def raw(self, rel_path: str) -> dict[str, object]:
        """Serve ONE ``raw/`` artefact by its citation string. Three statuses, never an exception.

        This is the shared seam every face uses to follow a note's ``sources:`` down to the capture
        it was written from — the MCP ``kb_read`` bridge, the web ``/raw`` + ``/api/raw`` routes and
        the ``agora read`` verb all wrap THIS method, so provenance cannot mean one thing on one
        face and something else on another (DRILLDOWN-169 D3). The shape is
        :meth:`gold_pack`'s, deliberately: a status dict rather than ``None``, because a caller has
        to be able to tell ``../../etc/passwd`` (refused) from ``raw/gone.md`` (absent), and because
        a ``None`` would put the not-found sentence in three closures instead of one.

        ``rel_path`` is the citation VERBATIM (``raw/<domain>/<event-id>.md``,
        ``raw/_blob/<ab>/<sha256>.<ext>``) — never trimmed, never re-spelled. Path safety is
        :func:`agora_kb.core.rawstore.resolve`'s three gates, not this method's; the textual test
        below only decides WHICH refusal to name.

        Statuses:

        - ``ok`` + ``raw_kind: "text"`` — ``text`` (tolerantly decoded, capped at
          :data:`~agora_kb.core.rawstore.MAX_RAW_TEXT_BYTES` with ``truncated`` saying so) and
          ``bytes``, the artefact's true on-disk size, so a truncated read is legible as one.
        - ``ok`` + ``raw_kind: "blob"`` — the capture sidecar's facts as ``meta`` and NO bytes
          (D5, argued at the call site below). ``meta`` is ``None`` when the sidecar is absent or
          unreadable, the same way :meth:`gold_pack` treats its own advisory sidecar.
        - ``not_found`` — no such artefact, a refused path that still LOOKED like a ``raw/``
          citation (a symlink, an escape that resolves out of ``raw/``), an unreadable file, or a
          ``*.meta.yaml`` asked for directly (D9: a sidecar is not a citable artefact — lint L1-8b
          — so the note teaches the rule instead of dead-ending).
        - ``invalid_path`` — the argument is not a repo-relative ``raw/`` path at all.

        **ADR-0027 §8 note.** This is the FOURTH Agora→agent emission path, after ``kb_query`` /
        ``kb_read`` / ``kb_neighbors``, and the first one that serves content which passed through
        NONE of the curator's PLAN/APPLY grading: everything under ``wiki/`` at least survived the
        lint ruleset, while ``raw/`` is whatever an extractor, an upload, a harvest connector or a
        hand-run capture produced. Nor is it uniformly redacted — only the ``session:`` connector
        redacts on the way in (§4 T5). The control that would gate this is the undesigned people /
        egress control (residual R1, #166); until it exists the operator's controls are the web
        face's ``raw_enabled`` kill switch and not exposing the face.
        """
        echo = rel_path if isinstance(rel_path, str) else ""
        # A textual restatement of rawstore's gate 1, for MESSAGING ONLY: it decides which refusal
        # to name, never whether to serve. The refusal itself is resolve()'s, and a path that passes
        # here can still (and does) come back None from it — reported as not_found, with no hint of
        # why, so a caller cannot walk the filesystem one status at a time (D2/D3).
        if not _is_raw_citation(echo):
            return {
                "status": "invalid_path",
                "resource": "raw",
                "path": echo,
                "note": (
                    f"{echo!r} is not a raw/ citation: pass a repo-relative path exactly as it "
                    "appears in a note's `sources:` (e.g. 'raw/<domain>/<event-id>.md' or "
                    "'raw/_blob/<ab>/<sha256>.<ext>')."
                ),
            }

        layout = self._repo.layout
        ref = rawstore.resolve(layout, echo)
        if ref is None:
            return {
                "status": "not_found",
                "resource": "raw",
                "path": echo,
                "note": (
                    f"no readable artefact at {echo!r}: a raw/ citation resolves only to a real "
                    "file inside raw/. Open the citing note with kb_read to check the "
                    "`sources:` spelling."
                ),
            }

        if ref.kind == "sidecar":
            # D9 / lint L1-8b: the sidecar is a record ABOUT an artefact, and the citation space
            # excludes it, so the URL and tool space must exclude it too — but the dead end teaches
            # the rule and names the artefact instead of merely refusing.
            artefact = echo[: -len(SIDECAR_SUFFIX)]
            return {
                "status": "not_found",
                "resource": "raw",
                "path": echo,
                "note": (
                    "a sidecar is not a citable artefact (lint L1-8b); read the artefact at "
                    f"{artefact}"
                ),
            }

        if ref.kind == "blob":
            # D5 IS NORMATIVE: no bytes and no base64 leave over MCP. Four reasons, so a later
            # "just add the bytes" change has to argue with all of them: (1) a 25 MiB PDF is ~33
            # MiB of base64 in a model's context — 4/3 the token cost for content no LLM can read;
            # (2) the sidecar already carries everything an agent needs to DECIDE (digest, type,
            # size, filename, capture provenance), and the web face serves the bytes to a human;
            # (3) `grep -rn b64encode src/` is 0 hits — this codebase has never had a byte channel,
            # and opening one makes the undesigned egress control (R1/#166) a content-type matrix
            # instead of one surface; (4) blob bytes are the ONLY repo content that passed neither
            # the curator, nor the ADR-0007 candidate gate, nor ADR-0023 redaction.
            # Reversing this requires citing D5 and retiring it explicitly.
            meta = rawstore.read_sidecar(layout, ref)
            return {
                "status": "ok",
                "resource": "raw",
                "raw_kind": "blob",
                "path": ref.rel_path,
                "bytes": ref.size_bytes,
                # Exactly the keys the sidecar HAS: APPLY omits an absent optional rather than
                # emitting it empty, and inventing one here would report a capture fact nobody
                # recorded. No top-level `sha256` either — echoing the basename back as an
                # integrity claim is a tautology (ADR-0041 D1.4); real digest verification is a
                # later unit.
                "meta": meta,
                # The URL is composed by `rawstore.web_href`, the ONE site that knows D6's prefix
                # rule — never by re-spelling it here. This seam is shared (`/api/raw` and `agora
                # read` return this same note), and a second hand-rolled "/{rel_path}" agrees with
                # `_raw_href` only by luck: it skips the percent-encoding, so the two drift the
                # first time a capture path needs escaping. Hence also "not in this payload":
                # the sentence has to stay true for a caller who is not on the MCP face.
                "note": (
                    "bytes are not served over MCP (DRILLDOWN-169 D5) and are not in this "
                    "payload; the capture facts are in 'meta' and the bytes download from the "
                    f"web face at {rawstore.web_href(ref.rel_path)}"
                ),
            }

        try:
            text, truncated = rawstore.read_text(ref)
        except OSError as exc:
            # rawstore lets I/O errors out on purpose (an unreadable file must not render as an
            # empty one); this face owes its caller a status dict, so it is the layer that wraps.
            return {
                "status": "not_found",
                "resource": "raw",
                "path": ref.rel_path,
                # `strerror` alone ("Permission denied"), never str(exc): the latter appends the
                # ABSOLUTE path, and a face's error text is not the place to publish the host's
                # directory layout.
                "note": (
                    f"raw/ artefact at {ref.rel_path} could not be read: "
                    f"{exc.strerror or type(exc).__name__}"
                ),
            }
        return {
            "status": "ok",
            "resource": "raw",
            "raw_kind": "text",
            "path": ref.rel_path,
            "text": text,
            # The artefact's on-disk size, NOT len(text): with `truncated` beside it that pair says
            # how much was left behind, where a post-truncation length would just agree with itself.
            "bytes": ref.size_bytes,
            "truncated": truncated,
        }

    # --- meta -----------------------------------------------------------------------------------
    def status(self) -> dict[str, object]:
        """``kb_status``: the meta face — backlog + last consolidation + cumulative counters.

        Returns the documented §5.1 surface (``inbox_depth``, ``last_consolidation``,
        ``processed_today``, ``failed``) plus the richer ``last_commit`` / ``counters`` extras. All
        of it already exists in ``_kb/state.json`` and the live ``inbox/`` / ``processed/`` /
        ``failed/`` dirs (DESIGN §5.3), so this is a pure read with no curator interaction.
        """
        state = self._state.load()
        return {
            "inbox_depth": self._inbox.depth(),
            "last_consolidation": (None if state.last_run is None else _iso_z(state.last_run)),
            "processed_today": self._processed_today_count(),
            "last_commit": state.last_commit,
            "counters": {
                "ingested": state.counters.ingested,
                "merged": state.counters.merged,
                "dropped": state.counters.dropped,
                "failed": state.counters.failed,
            },
            "failed": self._failed_count(),
            # #60 / ADR-0024 §3: the last published run's claim/bundle shape (None ⇒ never run or a
            # pre-#60 state.json) — events claimed, tier-2 candidates, the max_candidates_per_run
            # cap in effect, and the queue depth left right after the claim.
            "last_batch": (
                None if state.last_batch is None else state.last_batch.model_dump(mode="json")
            ),
            "gold": self._gold_meta_row(),
        }

    def _gold_meta_row(self) -> dict[str, object]:
        """The ADR-0027 gold-pack row for :meth:`status` — a CHEAP meta-sidecar read, NO git.

        Reads only ``_kb/gold/<default>.meta.json`` (never assembles a pack, never resolves the
        curated tip), so ``kb_status`` and the metrics scrape stay cheap. ``present=False`` when the
        pack has never been built; freshness-vs-tip is intentionally NOT computed here (that needs a
        git call) — the dashboard panel (:meth:`gold_status`) and ``agora gold status`` do that.
        """
        from agora_kb.core.gold import DEFAULT_PACK, read_meta

        meta = read_meta(self._repo.layout, DEFAULT_PACK)
        if meta is None:
            return {"pack": DEFAULT_PACK, "present": False}
        return {
            "pack": meta.pack,
            "present": True,
            "note_count": meta.note_count,
            "est_tokens": meta.est_tokens,
            "budget_tokens": meta.budget_tokens,
            "generated_at": meta.generated_at,
            "curated_sha": meta.curated_sha,
            "harvest_derived_share": meta.harvest_derived_share,
        }

    # --- dashboard (meta; read-only — DESIGN §5.3 / ADR-0003) -----------------------------------
    # The three panels below are a pure READ aggregation over already-existing metadata: the wiki
    # notes (browse seam → core.wiki), the deterministic lint() (the SAME health-signal code path
    # the curator runs, so dashboard and curator can never disagree — DESIGN §5.3), the curator
    # state.json, log.md, and the harvester cursors. They add NO write path and touch no
    # integrity/curator/inbox code; every value is JSON-serializable, consistent with status().
    def health(self) -> dict[str, object]:
        """KB-health panel (DESIGN §5.3): note counts, status/tag distribution, lint signals.

        Counts are derived from :meth:`agora_kb.core.wiki.Wiki.list_notes` (the same core.wiki seam
        :meth:`browse` uses) — a ``by_kind`` census over the ADR-0041 kind vocabulary plus an
        :data:`UNKNOWN_KIND` residue bucket, so it is TOTAL (``sum(by_kind.values()) ==
        note_total``) and an off-layout note is surfaced rather than silently dropped (with
        ``concepts`` / ``journals`` lifted out as the two headline scalars that replace v1's
        ``themes`` / ``dailies``), status split over the frozen vocabulary, and a tag-frequency
        map. ``broken_links`` and ``lint_findings`` come from the
        deterministic :func:`agora_kb.schema.lint.lint` reused VERBATIM (called WITHOUT ``run_date``
        — dashboard mode, so no historical note is flagged as a future date), so this never
        reimplements a health check. ``orphans`` is a small read-time link-graph derivation (L2-1:
        a claim-bearing note nothing links TO — lint() emits no orphan finding by default, so it is
        NOT a lint signal). ``unmanaged_notes`` counts the notes the curator did NOT write
        (issue #152): read, indexed and linkable, but outside the producer lint's subject and never
        a MERGE/CONTEST target — ``wiki/people/**`` is excluded from it, because a human-owned note
        is unmanaged BY DESIGN (D3.3) and counting it would turn the anomaly signal into noise;
        the people population is reported instead as ``by_kind['person']``.
        ``contested`` counts notes whose frontmatter ``status == 'contested'``;
        ``last_consolidation`` is from :meth:`status`. ``kb_schema_version`` /
        ``writable_schema`` / ``writable_schema_version`` are the ADR-0041 D6 WRITE verdict, read
        from the canonical declaration ``Inbox.write`` itself consults so the panel and the write
        path cannot disagree: a repo on the older layout reads and lints perfectly while refusing
        every capture, and a health panel that reported only ``lint_ok`` showed it as green.
        This panel runs lint() + a full note scan, so it is the heavy one — refreshed on load + a
        manual button, not a poll.
        """
        from agora_kb.config import load_repo_config
        from agora_kb.curator.worker import is_curator_written
        from agora_kb.schema.lint import lint
        from agora_kb.schema.notes import SCHEMA2_KINDS, body_link_basenames, wikilinks

        notes = self._wiki.list_notes()
        note_total = len(notes)
        # The kind census is TOTAL over `notes`: `sum(by_kind.values()) == note_total`, always.
        # A note whose derived kind is None — a schema-2 note under an unknown `wiki/<dir>/` or
        # directly under `wiki/` (the L1-22 population), or a schema-1 note with an unrecognised
        # `type:` — lands in `unknown` rather than being dropped. Dropping it made the panel
        # under-report next to its own "Notes" total, and hid precisely the anomaly the closed
        # directory vocabulary exists to catch.
        by_kind = {kind: 0 for kind in sorted(SCHEMA2_KINDS)}
        by_kind[UNKNOWN_KIND] = 0
        for n in notes:
            by_kind[n.kind if n.kind in by_kind else UNKNOWN_KIND] += 1
        concepts = by_kind["concept"]
        journals = by_kind["note"]

        by_status = {"active": 0, "stub": 0, "contested": 0, "deprecated": 0}
        tag_distribution: dict[str, int] = {}
        for note in notes:
            fm = note.frontmatter
            status = fm.get("status")
            if isinstance(status, str) and status in by_status:
                by_status[status] += 1
            raw_tags = fm.get("tags")
            if isinstance(raw_tags, list):
                for tag in raw_tags:
                    if isinstance(tag, str):
                        tag_distribution[tag] = tag_distribution.get(tag, 0) + 1
        contested = by_status["contested"]

        # Reuse the curator's deterministic L1 lint VERBATIM (the dashboard health-signal source,
        # DESIGN §5.3). Dashboard mode = no run_date (the no-future-date half is a curator-run gate;
        # outside a run there is no canonical "today" to compare historical notes against).
        taxonomy = load_repo_config(self._repo.layout).taxonomy
        result = lint(self._repo.layout, taxonomy=taxonomy)
        lint_findings = len(result.findings)
        # Two DISTINCT health signals (DESIGN §5.3), in opposite link directions:
        #  - broken_links: dangling OUTBOUND references — L1-2 findings (a link whose target note
        #    does not exist). A hard-gate signal, counted straight from lint().
        #  - orphans: claim-bearing notes nothing links TO (L2-1) — read-time derived (lint() emits
        #    NO orphan finding unless curator.lint.max_orphans is set). A note is an orphan when
        #    its basename is referenced by no other note's body markdown link nor any frontmatter
        #    related:/children: [[ ]]; journals, maps/index roots, entities and people are exempt.
        broken_links = sum(1 for f in result.findings if f.code == "L1-2")
        referenced: set[str] = set()
        for n in notes:
            # Links OUT of a people note are UNGRADED (ADR-0041 D3.3), so they do not feed the
            # reference universe — otherwise one human file linking a concept would silently
            # suppress that concept's orphan count, giving a human-owned tree a vote on the signal
            # that gates a curator run. This mirrors `schema.lint`'s L2-1 derivation exactly, which
            # is the whole point: the dashboard and the gate must never disagree about the number.
            if _is_ungraded_people_note(n):
                continue
            referenced.update(body_link_basenames(n.body))
            for fkey in ("related", "children"):
                fval = n.frontmatter.get(fkey)
                for item in fval if isinstance(fval, list) else [fval]:
                    if isinstance(item, str):
                        referenced.update(wikilinks(item))
        orphans = sum(1 for n in notes if _is_orphan(n, referenced))
        # Issue #152: how many of those notes the curator does NOT own — no curator stamp, so they
        # are read, indexed and linkable but never graded by the producer lint and never a
        # MERGE/CONTEST target. The SAME predicate the curator classifies its live tree with, so the
        # dashboard and the run can never disagree about who wrote what. Some of the `lint_findings`
        # above may therefore belong to notes the curator will not act on — the dashboard reports
        # the whole tree honestly (it is a health panel, not the gate); this number is what tells an
        # operator why a finding is not being fixed by the next run.
        unmanaged_notes = sum(
            1 for n in notes if not _is_ungraded_people_note(n) and not is_curator_written(n)
        )

        # The WRITABILITY verdict, keyed on the canonical declaration `Inbox.write` itself consults
        # (`read_canonical_kb_schema_version`), so the panel and the write path provably cannot
        # disagree. Without it the dashboard showed a green KB — `lint_ok: true`, zero findings —
        # whose every upload comes back as a receipt error, the same true-statement/false-impression
        # the CLI's READ-ONLY line exists to prevent (ADR-0041 D6). `None` for a directory that
        # declares nothing determinable; `writable_schema` is then False on the same honest ground
        # `agora doctor` fails an unreadable version on.
        kb_schema_version = _canonical_schema_version(self._repo.layout)
        return {
            "kb_schema_version": kb_schema_version,
            "writable_schema": kb_schema_version == MAX_SUPPORTED_KB_SCHEMA_VERSION,
            "writable_schema_version": MAX_SUPPORTED_KB_SCHEMA_VERSION,
            "note_total": note_total,
            "by_kind": by_kind,
            "concepts": concepts,
            "journals": journals,
            "by_status": by_status,
            "tag_distribution": tag_distribution,
            "orphans": orphans,
            "unmanaged_notes": unmanaged_notes,
            "broken_links": broken_links,
            "contested": contested,
            "lint_ok": result.ok,
            "lint_findings": lint_findings,
            "last_consolidation": self.status()["last_consolidation"],
        }

    def curator_status(self) -> dict[str, object]:
        """Curator panel (DESIGN §5.3): queue depth, throughput, active backend, work-log timeline.

        Reuses :meth:`status` for the inbox/consolidation meta and the cumulative ``counters``, adds
        the resolved ``active_backend`` label (the brain that runs the AUTHOR act — see
        :meth:`_active_backend`, which mirrors ``agora doctor``'s routing resolution), and tails
        ``log.md`` for the work-log timeline (:meth:`_recent_log`). All read-only — no curator
        interaction, the moving values (queue depth) make this a cheap, pollable panel.
        """
        base = self.status()
        return {
            "inbox_depth": base["inbox_depth"],
            "last_consolidation": base["last_consolidation"],
            "last_commit": base["last_commit"],
            "processed_today": base["processed_today"],
            "failed": base["failed"],
            "counters": base["counters"],
            "last_batch": base["last_batch"],
            "active_backend": self._active_backend(),
            "recent_log": self._recent_log(),
        }

    def harvester_status(self) -> dict[str, object]:
        """Harvester panel (DESIGN §5.3): connectors enabled + per-source scan / candidate tally.

        From :func:`agora_kb.config.load_harvest_policy` (``enabled``) and
        :func:`agora_kb.config.load_connector_specs` + :class:`agora_kb.harvester.CursorStore` per
        connector. ``accepted`` / ``rejected`` are the curator-owned cursor counters DEFERRED to
        ADR-0017 §7 — they are CURRENTLY 0 and are rendered as-is (never faked); they light up later
        without a redesign here. ``format`` is the effective transcript grammar of a ``session:``
        connector (issue #147; ``None`` for any other kind, as ``follow_links`` is for a session
        one). Tolerant: an unreadable ``repo.yaml`` / ``adapters.yaml`` degrades to
        ``enabled=False`` / no connectors rather than crashing the read-only panel.
        """
        from agora_kb.config import (
            ConfigError,
            load_connector_specs,
            load_harvest_policy,
        )
        from agora_kb.harvester import CursorStore
        from agora_kb.harvester.session_sources import DEFAULT_SESSION_FORMAT

        try:
            policy = load_harvest_policy(self._repo.layout)
            enabled = policy.enabled
        except (ConfigError, ValueError):
            enabled = False

        adapters_path = self._repo.layout.root / "adapters.yaml"
        try:
            specs = load_connector_specs(adapters_path)
        except (ConfigError, ValueError):
            specs = None

        store = CursorStore(self._repo.layout)
        connectors: list[dict[str, object]] = []
        for spec in specs or []:
            cursor = store.load(spec.name)
            connectors.append(
                {
                    "name": spec.name,
                    "path": spec.path,
                    "scope": spec.scope,
                    # `follow_links` is a file:-connector concern and `format` a session: one; each
                    # is None on the other kind rather than a default that cannot take effect (the
                    # same "a key that cannot take effect must not look accepted" rule
                    # load_connector_specs enforces). `format` is the EFFECTIVE transcript grammar
                    # (issue #147), mirroring the `agora doctor` line, so the dashboard can answer
                    # "which grammar is reading my transcripts" too.
                    "follow_links": (
                        None if spec.name.startswith("session:") else spec.follow_links
                    ),
                    "format": (
                        (spec.format or DEFAULT_SESSION_FORMAT)
                        if spec.name.startswith("session:")
                        else None
                    ),
                    "last_scan": (None if cursor.last_scan is None else _iso_z(cursor.last_scan)),
                    "proposed": cursor.proposed,
                    "accepted": cursor.accepted,
                    "rejected": cursor.rejected,
                    # ADR-0023 §6: {class: count} facts-with-redaction, the source the Prometheus
                    # agora_harvester_redacted{connector,class} family reads (metadata-only — a
                    # class name + count, never the secret). {} for one that does not redact.
                    "redacted": dict(cursor.redacted),
                }
            )
        return {"enabled": enabled, "connectors": connectors}

    def gold_status(self) -> dict[str, object]:
        """Gold panel (DESIGN §5.3 medallion): the derived pack tier + its freshness vs silver.

        The bronze/silver/gold panel's gold row: reads the ADR-0027 pack meta sidecar and resolves
        the curated tip to mark the pack FRESH (``curated_sha`` == the live tip) or STALE. Unlike
        the cheap :meth:`status` gold row (meta-only, no git), this panel may do one ``git
        rev-parse``. Tolerant: an absent pack → ``present=False``; a git failure →
        ``fresh=None`` (unknown) rather than crashing the read-only panel. A pack whose sidecar
        declares a different :data:`~agora_kb.core.gold.GOLD_SCHEMA_VERSION` is also
        ``present=False`` — this build will not serve it — but carries ``stale_schema`` so the
        panel can say WHY instead of implying nothing was ever built. ``bronze`` carries the
        cheap tier context (inbox backlog already in :meth:`status`; silver lives in the KB-health
        panel)."""
        from agora_kb.core.gold import (
            DEFAULT_PACK,
            GOLD_SCHEMA_VERSION,
            read_meta,
            read_meta_schema_version,
        )

        base = self.status()
        meta = read_meta(self._repo.layout, DEFAULT_PACK)
        gold: dict[str, object] = {"present": meta is not None, "pack": DEFAULT_PACK}
        if meta is None:
            # `present: false` is the right answer for a pack this build will not serve, but on its
            # own it says "nothing was ever built" — misleading for the one case where a pack file
            # plainly exists. `stale_schema` names the version that IS on disk, so the panel and
            # :meth:`gold_pack`'s refusal tell the operator the same story.
            declared = read_meta_schema_version(self._repo.layout, DEFAULT_PACK)
            if declared is not None and declared != GOLD_SCHEMA_VERSION:
                gold["stale_schema"] = declared
        if meta is not None:
            try:
                commit: str | None = self._repo.branch_commit()
            except Exception:  # noqa: BLE001 — a git failure leaves freshness unknown, not fatal.
                commit = None
            gold.update(
                {
                    "pack": meta.pack,
                    "fresh": None if commit is None else meta.curated_sha == commit,
                    "note_count": meta.note_count,
                    "est_tokens": meta.est_tokens,
                    "budget_tokens": meta.budget_tokens,
                    "generated_at": meta.generated_at,
                    "curated_sha": meta.curated_sha[:12],
                    "harvest_derived_share": meta.harvest_derived_share,
                }
            )
        return {
            "bronze": {
                "inbox_depth": base["inbox_depth"],
                "processed_today": base["processed_today"],
            },
            "gold": gold,
        }

    # --- gold consumption (read; ADR-0027 Phase C, issue #40) -----------------------------------
    def gold_pack(self, pack: str = "default") -> dict[str, object]:
        """Serve one BUILT gold context pack — the single shared Phase-C consumption handler.

        The ADR-0019/0021 two-face lock: the MCP channels (``kb_context`` tool,
        ``agora://gold/{pack}`` resource, ``gold_context`` prompt) and the web face
        (``GET /api/gold/{pack}``) all wrap THIS method — no face ships its own read. It serves the
        built artifact at ``_kb/gold/<pack>.md`` **byte-identically** and never (re)assembles a
        pack: assembly stays with the producer (``agora gold build`` + the worker finalize rebuild,
        ADR-0027 decisions 2/3/7 ROLE RULE) so the byte-identical / prompt-cache contract holds on
        the serve path. Pull-only, on-request — nothing is auto-injected (the injection opt-in
        posture, ADR-0027 decision 7 / #40).

        Three statuses, never an exception:

        - ``ok``: ``text`` is the exact pack file content; ``meta`` carries the sidecar fields
          (``None`` if the sidecar is absent/corrupt — the pack bytes are the contract, the meta is
          advisory).
        - ``not_built``: no built pack file — ``note`` says how to build one and ``packs`` lists
          the packs that DO exist. A pack whose sidecar declares a DIFFERENT
          :data:`~agora_kb.core.gold.GOLD_SCHEMA_VERSION` reports the same status, carries the
          declared version as ``stale_schema``, and is NOT served: its bytes were selected under
          eligibility rules this build has invalidated. A corrupt (version-less) sidecar is a
          different case and still serves — see the note on ``meta`` above.
        - ``invalid_name``: ``pack`` is not a safe single path component (the same
          :func:`~agora_kb.core.layout.safe_path_component` traversal guard the layout uses), so
          nothing outside ``_kb/gold/`` is ever read.
        """
        from agora_kb.core.gold import GOLD_SCHEMA_VERSION, read_meta, read_meta_schema_version
        from agora_kb.core.layout import InvalidWriterError

        try:
            pack_path = self._repo.layout.gold_pack_path(pack)
        except InvalidWriterError:
            return {
                "status": "invalid_name",
                "pack": pack,
                "packs": self._built_packs(),
                "note": (
                    f"invalid pack name {pack!r}: a pack name is a single safe filename token "
                    "(alphanumeric, then alphanumerics/._-), never a path. Built packs: see "
                    "'packs'."
                ),
            }
        try:
            # read_bytes().decode(), NOT read_text(): Path.read_text (3.12: no newline= param)
            # applies universal-newline translation, which would silently rewrite \r\n / \r in an
            # out-of-band-written pack and break the byte-identical serving contract above.
            text = pack_path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return {
                "status": "not_built",
                "pack": pack,
                "packs": self._built_packs(),
                "note": (
                    f"gold pack {pack!r} has not been built (or is unreadable): run "
                    "'agora gold build' — or let the next curator run rebuild it — then retry "
                    "(ADR-0027)."
                ),
            }
        # A sidecar declaring a DIFFERENT gold schema means these bytes were assembled under a
        # different eligibility rule, which is the one thing a GOLD_SCHEMA_VERSION bump exists to
        # invalidate (1 → 2 moved eligibility to the kind axis and added the `wiki/people/**` and
        # `derived: true` exclusions). Serving them anyway made the bump blind on the surface it
        # was meant to protect: `gold_status` reported `present: false` off the same mismatch while
        # `kb_context` kept serving the superseded selection, so the operator's panel and the
        # agents' standing context disagreed. `_kb/` is git-ignored, so a pack built on the
        # previous branch survives a checkout and lands exactly here.
        #
        # The refusal is keyed on the version FIELD specifically, never on "no usable meta": a
        # CORRUPT sidecar still serves its bytes (the pack bytes are the contract, the meta is
        # advisory — `test_handler_tolerates_corrupt_meta_sidecar` pins that), and
        # `read_meta_schema_version` returns None for it precisely so the two stay separable.
        declared = read_meta_schema_version(self._repo.layout, pack)
        if declared is not None and declared != GOLD_SCHEMA_VERSION:
            return {
                "status": "not_built",
                "pack": pack,
                "packs": self._built_packs(),
                "stale_schema": declared,
                "note": (
                    f"gold pack {pack!r} was built by an older gold schema "
                    f"(v{declared}, this build assembles v{GOLD_SCHEMA_VERSION}) and its contents "
                    "no longer match the current eligibility rules: run 'agora gold build' — or "
                    "let the next curator run rebuild it — then retry (ADR-0027 / ADR-0041 D3.3)."
                ),
            }
        meta = read_meta(self._repo.layout, pack)
        meta_row: dict[str, object] | None = None
        if meta is not None:
            meta_row = {
                "curated_sha": meta.curated_sha,
                "spec_hash": meta.spec_hash,
                "generated_at": meta.generated_at,
                "estimator": meta.estimator,
                "note_count": meta.note_count,
                "est_tokens": meta.est_tokens,
                "budget_tokens": meta.budget_tokens,
                "harvest_derived_share": meta.harvest_derived_share,
            }
        return {"status": "ok", "pack": pack, "text": text, "meta": meta_row}

    def _built_packs(self) -> list[str]:
        """Sorted stems of the packs that exist under ``_kb/gold/`` (``[]`` when none/absent)."""
        gold_dir = self._repo.layout.gold_dir
        if not gold_dir.is_dir():
            return []
        return sorted(p.stem for p in gold_dir.glob("*.md"))

    # --- graph (read; web face /graph viz — ADR-0003/0019 §7 / graph-plan) -----------------------
    def graph(
        self,
        *,
        center: str | None = None,
        depth: int = 1,
        subject: str | None = None,
        max_nodes: int | None = None,
        max_depth: int | None = None,
    ) -> dict[str, object]:
        """Knowledge-graph data for the web ``/graph`` viz (DESIGN §5.3 / ADR-0019 §7, graph-plan).

        A thin, deterministic READ aggregation over :meth:`agora_kb.core.wiki.Wiki.list_notes` (the
        same core.wiki seam :meth:`browse` / :meth:`health` use) into a JSON-serializable
        node/edge graph — it adds NO write path and touches no integrity/curator/inbox code
        (ADR-0003 thin-face). It is the data seam the graph plan and ADR-0019 §7 prescribe; the
        rendering (force-graph layout) is the web layer's job, never this face's.

        **Nodes.** One per note via :meth:`_note_summary` (reused verbatim, NOT duplicated) for
        ``title`` / ``subjects`` / ``status`` / ``kind``, with ``id == rel_path`` — the canonical
        unique identity, which is exactly what ``/note/<id>`` navigates to — plus an ``orphan``
        flag. ``kind`` and ``subjects`` replace v1's ``type`` and ``domain`` (ADR-0041 D2/D3.2);
        the graph viz colours by the FIRST subject, which is the web layer's presentation choice
        and not a claim that a note has only one.

        **Orphan.** Reuses :meth:`health`'s EXACT derivation, through the SHARED
        :func:`_is_orphan`: a referenced set is built over the FULL note set — minus the ungraded
        ``wiki/people/**`` sources (D3.3) — as the union of
        :func:`agora_kb.schema.notes.body_link_basenames` of each body plus, for ``related`` /
        ``children``, the :func:`agora_kb.schema.notes.wikilinks` of each str item (list-or-scalar
        tolerant, exactly as ``health()`` iterates). A node is an orphan iff its kind is in
        :data:`ORPHAN_KINDS` and its basename is referenced by nothing. Orphan is a GLOBAL
        property, so it is computed over every note regardless of the ``center`` / ``subject``
        filter (so the count matches ``health()["orphans"]``).

        **Edges.** Directed, deduped, no self-loops, both endpoints real nodes. A ``by_basename``
        map (basename → rel_path, ``setdefault`` over notes sorted by ``rel_path`` for determinism)
        resolves each source note's body links + ``related`` / ``children`` wikilinks to a target
        rel_path; an unresolved (dangling) basename is NOT an edge, and self-loops are dropped.
        Emitted as ``{"source", "target"}``, sorted by ``(source, target)``.

        **Scope.** GLOBAL (``center is None``): all notes, optionally filtered to one ``subject``
        (kept iff that subject is in the note's ``subjects``, which is a MEMBERSHIP test now that a
        note may declare 0..n of them — D2.2); ``edge_total`` / ``node_total`` are the
        kept counts BEFORE the cap. If the kept node count exceeds :data:`MAX_GRAPH_NODES` the kept
        set is truncated to the first ``MAX_GRAPH_NODES`` by sorted rel_path, edges are recomputed
        on the survivors, and ``truncated`` is ``True`` — but ``node_total`` still reports the true
        pre-cap count (no silent truncation). LOCAL (``center`` is a rel_path): ``depth`` is clamped
        to ``[1, MAX_GRAPH_DEPTH]``; an unknown center returns an empty graph with ``center=None``;
        otherwise a BFS over the UNDIRECTED adjacency of the global edge set (both directions) from
        the center out to ``depth`` hops yields the reached notes (``subject`` is ignored for
        local), with edges induced on that reached set and ``truncated`` always ``False``.

        Returns ``{nodes, edges, node_total, edge_total, truncated, center, depth}`` — ``center``
        echoes the resolved center rel_path (``None`` for global or an unknown requested center) and
        ``depth`` echoes the clamped value; every value is JSON-serializable.
        """
        from agora_kb.schema.notes import body_link_basenames, wikilinks

        # ADR-0025: resolve the EFFECTIVE caps — a caller-supplied value (the web face threads its
        # WebConfig graph caps) wins, else the module default. Backward-compatible: a graph() call
        # with no caps (MCP/tests) keeps the module-default behaviour.
        eff_max_nodes = max_nodes if max_nodes is not None else MAX_GRAPH_NODES
        eff_max_depth = max_depth if max_depth is not None else MAX_GRAPH_DEPTH

        notes = self._wiki.list_notes()

        # Orphan derivation — reused VERBATIM from health() (a GLOBAL property over every note,
        # computed before any center/subject filtering so the count matches health()["orphans"]).
        # The `wiki/people/**` source skip is part of that "verbatim": drop it here and a human
        # note linking a concept would un-orphan it in the graph while lint and the dashboard still
        # count it — the same number disagreeing with itself across two panels.
        referenced: set[str] = set()
        for n in notes:
            if _is_ungraded_people_note(n):
                continue
            referenced.update(body_link_basenames(n.body))
            for fkey in ("related", "children"):
                fval = n.frontmatter.get(fkey)
                for item in fval if isinstance(fval, list) else [fval]:
                    if isinstance(item, str):
                        referenced.update(wikilinks(item))

        # Stable basename → rel_path resolver (setdefault over notes sorted by rel_path so a
        # duplicate basename deterministically resolves to the first path). `wiki/people/**` is
        # OUTSIDE this identity space (D3.3: "a people note is addressed by path, never by
        # [[basename]]") — people notes stay NODES and stay readable by path, they are just never
        # what a link RESOLVES to. Without the skip a human note captures every edge drawn to a
        # curated note of the same name, and `wiki/people/...` sorts BEFORE `wiki/summaries/...`,
        # so it would win the setdefault for every summary basename. lint's graded `by_basename`
        # already drops `people_paths`; this makes the face's identity space equal to it.
        sorted_notes = sorted(notes, key=lambda n: n.rel_path)
        by_basename: dict[str, str] = {}
        for n in sorted_notes:
            if _is_ungraded_people_note(n):
                continue
            by_basename.setdefault(n.basename, n.rel_path)

        def _node(n: object) -> dict[str, object]:
            summary = self._note_summary(n)
            return {
                "id": summary["rel_path"],
                "title": summary["title"],
                "subjects": summary["subjects"],
                "status": summary["status"],
                "kind": summary["kind"],
                "orphan": _is_orphan(n, referenced),
            }

        # The full directed, deduped, self-loop-free edge set over every real node — the basis for
        # both the global induced edges and the local undirected BFS adjacency.
        all_node_ids = {n.rel_path for n in notes}
        edge_pairs: set[tuple[str, str]] = set()
        for n in sorted_notes:
            targets = list(body_link_basenames(n.body))
            for fkey in ("related", "children"):
                fval = n.frontmatter.get(fkey)
                for item in fval if isinstance(fval, list) else [fval]:
                    if isinstance(item, str):
                        targets.extend(wikilinks(item))
            for base in targets:
                target_id = by_basename.get(base)
                if target_id is None or target_id == n.rel_path:
                    continue  # dangling link → not an edge; self-loop dropped
                edge_pairs.add((n.rel_path, target_id))

        def _induced_edges(node_ids: set[str]) -> list[dict[str, object]]:
            kept = sorted((s, t) for (s, t) in edge_pairs if s in node_ids and t in node_ids)
            return [{"source": s, "target": t} for (s, t) in kept]

        if center is None:
            # GLOBAL scope — all notes, optionally filtered to one subject.
            if subject is None:
                kept_notes = sorted_notes
            else:
                kept_notes = [n for n in sorted_notes if subject in n.subjects]
            kept_ids = {n.rel_path for n in kept_notes}
            node_total = len(kept_notes)
            edge_total = len(_induced_edges(kept_ids))
            truncated = node_total > eff_max_nodes
            if truncated:
                kept_notes = kept_notes[:eff_max_nodes]  # first N by sorted rel_path
                kept_ids = {n.rel_path for n in kept_notes}
            nodes = [_node(n) for n in kept_notes]
            edges = _induced_edges(kept_ids)
            return {
                "nodes": nodes,
                "edges": edges,
                "node_total": node_total,  # honest pre-cap count
                "edge_total": edge_total,
                "truncated": truncated,
                "center": None,
                "depth": depth,
            }

        # LOCAL scope — ego-graph around `center` out to a clamped depth.
        clamped_depth = max(1, min(depth, eff_max_depth))
        if center not in all_node_ids:
            # Unknown center → empty graph, center echoed as None (no node to seed the BFS).
            return {
                "nodes": [],
                "edges": [],
                "node_total": 0,
                "edge_total": 0,
                "truncated": False,
                "center": None,
                "depth": clamped_depth,
            }

        adjacency: dict[str, set[str]] = {}
        for s, t in edge_pairs:
            adjacency.setdefault(s, set()).add(t)
            adjacency.setdefault(t, set()).add(s)

        reached: set[str] = {center}
        frontier: set[str] = {center}
        for _ in range(clamped_depth):
            nxt: set[str] = set()
            for node_id in frontier:
                for neighbor in adjacency.get(node_id, ()):
                    if neighbor not in reached:
                        nxt.add(neighbor)
            reached.update(nxt)
            frontier = nxt
            if not frontier:
                break

        by_path = {n.rel_path: n for n in sorted_notes}
        local_notes = [by_path[rel] for rel in sorted(reached) if rel in by_path]
        nodes = [_node(n) for n in local_notes]
        edges = _induced_edges(reached)
        return {
            "nodes": nodes,
            "edges": edges,
            "node_total": len(nodes),
            "edge_total": len(edges),
            "truncated": False,
            "center": center,
            "depth": clamped_depth,
        }

    def _active_backend(self) -> str | None:
        """Resolve the label of the brain that runs the AUTHOR act (the prose writer), or ``None``.

        Mirrors ``agora doctor``'s routing resolution (cli.py ``_doctor_routing``):
        ``load_backend_registry(adapters.yaml).routed_backends(default=repo.yaml curator.backend)``
        under the ADR-0015 precedence (``routing[act]`` → repo default → adapters default). The
        AUTHOR act is the brain that actually materializes wiki prose, so it is the meaningful
        "active backend + model" the dashboard names (DESIGN §5.3). ``None`` when no
        ``adapters.yaml`` is configured (or it is unreadable) — the same "no backend" signal
        :meth:`curate` surfaces, never a crash on the read-only panel.
        """
        from agora_kb.config import load_backend_registry, load_repo_config

        adapters_path = self._repo.layout.root / "adapters.yaml"
        try:
            registry = load_backend_registry(adapters_path)
        except ValueError:
            return None
        if registry is None:
            return None
        default_backend = load_repo_config(self._repo.layout).default_backend
        routed = registry.routed_backends(default=default_backend)
        return routed.get("author") or default_backend

    def _recent_log(self, limit: int = 10) -> list[dict[str, object]]:
        """Tail the last ``limit`` structured ``log.md`` entries into a work-log timeline.

        Matches the EXACT format the curator's ``worker._append_log`` writes (one ``## <run_id>``
        section per run, followed by ``- base: \\`<base>\\``` / ``- dispositions: <op>=<n>, …`` and
        optional ``- contested:`` / ``- dropped:`` / ``- pending-body:`` lines). Each parsed entry
        is ``{run_id, base, ops:{op:count}, contested:[…], dropped:[…], pending_body:[…]}``, newest
        first. A pure read of the git-tracked ``log.md`` (the curator alone writes it, ADR-0002); a
        missing file yields ``[]``. Parsing is tolerant — a ``- dispositions: no-op`` line or an
        unrecognized bullet simply yields an empty ``ops`` / is ignored rather than raising.
        """
        log_path = self._repo.layout.log_file
        if not log_path.is_file():
            return []
        text = log_path.read_text(encoding="utf-8")
        entries: list[dict[str, object]] = []
        current: dict[str, object] | None = None
        for line in text.splitlines():
            if line.startswith("## "):
                current = {
                    "run_id": line[3:].strip(),
                    "base": None,
                    "ops": {},
                    "contested": [],
                    "dropped": [],
                    "pending_body": [],
                }
                entries.append(current)
                continue
            if current is None:
                continue
            stripped = line.strip()
            if stripped.startswith("- base:"):
                current["base"] = stripped[len("- base:") :].strip().strip("`") or None
            elif stripped.startswith("- dispositions:"):
                current["ops"] = _parse_ops(stripped[len("- dispositions:") :].strip())
            elif stripped.startswith("- contested:"):
                current["contested"] = _split_csv(stripped[len("- contested:") :])
            elif stripped.startswith("- dropped:"):
                current["dropped"] = _split_csv(stripped[len("- dropped:") :])
            elif stripped.startswith("- pending-body:"):
                current["pending_body"] = _split_csv(stripped[len("- pending-body:") :])
        # Newest first; cap at `limit`.
        entries.reverse()
        return entries[:limit]

    def _failed_count(self) -> int:
        """The ``_kb/failed/`` event count behind ``kb_status.failed`` — see
        :func:`agora_kb.core.failed_event_count`.

        A one-line delegate since #96 crit 8: the CLI's ``agora status`` prints the SAME number as
        ``failed_events``, and the two must agree BY CONSTRUCTION rather than by two copies of the
        same recursive glob drifting apart. The count itself is byte-identical to what this method
        computed inline before the promotion.
        """
        return failed_event_count(self._repo.layout)

    def _processed_today_count(self) -> int:
        """Number of items consolidated today under ``_kb/processed/<today-UTC>/`` (0 if absent).

        The processed spool is partitioned by UTC date (DESIGN §5.3: ``processed/<date>/``), so
        "today" is the current UTC calendar day, matching how the curator finalizes events.
        """
        today_dir = self._repo.layout.processed_dir / _utc_today()
        if not today_dir.is_dir():
            return 0
        return sum(1 for _ in today_dir.glob("*.md"))

    # --- admin ----------------------------------------------------------------------------------
    def curate(self, *, target: str = "personal", force: bool = False) -> dict[str, object]:
        """``kb_curate``: run ONE consolidation against the configured ``adapters.yaml`` backend.

        Order (ADR-0011 §9 then §0): :func:`agora_kb.curator.worker.recover` finalizes/returns any
        in-flight run first, then :func:`agora_kb.curator.worker.run` executes the transactional run
        (claim → bundle → PASS-1 PLAN → APPLY → PASS-2 AUTHOR → validate → commit → CAS publish).
        The backend is OUTSIDE the integrity boundary (ADR-0008/0011 §7): the deterministic gates
        decide success, so this face adds none of its own logic — it only loads the configured brain
        and shapes the :class:`~agora_kb.curator.worker.RunReport` into a JSON-serializable dict.

        Returns ``{status, published_commit, counts, ...}``. When no ``adapters.yaml`` backend is
        configured (or the configured brain is unknown), it returns ``{status: "no_backend", note:
        ...}`` rather than raising — a clear, actionable signal to the caller. ``force`` is accepted
        for signature stability (DESIGN §5.1 ``kb_curate(target, force?)``); the worker itself
        no-ops an empty/all-deduped inbox, so a forced call over an empty inbox simply reports
        ``noop``. ``target`` defaults to ``"personal"`` (the only repo until multi-tenancy lands).

        RAISES :class:`~agora_kb.config.ReadOnlySchemaVersionError` on a repo whose KB wiki schema
        this build will not write (ADR-0041 D6) — one of D6's exhaustive write-path call
        sites. It is a RAISE rather than a ``{"status": ...}`` dict on purpose: the
        ``no_backend`` shape reports a repo that is fine but unconfigured, whereas this is a
        refusal to touch the repo at all, and the FastMCP tool error carries the one remedy
        (``agora import --from-kb``) to the calling agent verbatim. It sits BEFORE the backend
        build so a schema-1 repo refuses identically whether or not a brain is wired — the verdict
        is about the repo, never about ``adapters.yaml``. The server-construction guard
        (:func:`~agora_kb.config.guard_repo_schema_version`, ``build_server``) cannot cover this:
        with ``SUPPORTED_KB_SCHEMA_VERSIONS`` widened to ``{1, 2}`` it passes for a schema-1 repo
        by design, so ``kb_query`` keeps working on the same server object that must refuse here.
        The predicate is the SAME one ``Inbox.write`` applies to ``kb_remember`` on this very
        server, imported rather than restated so the two admin/capture surfaces cannot disagree
        about which repos are writable.
        """
        assert_writable_repo_schema(self._repo.layout)
        cfg = load_repo_config(self._repo.layout)
        backend = self._build_backend(
            default_backend=cfg.default_backend,
            allow_reduced_isolation=cfg.allow_reduced_isolation,
            body_byte_bound=cfg.body_byte_bound,
            language=cfg.language,
        )
        if backend is None:
            return {
                "status": "no_backend",
                "published_commit": None,
                "counts": {},
                "inbox_depth": self._inbox.depth(),
                "note": (
                    f"no backend configured for target={target!r}: create an adapters.yaml with a "
                    "'backends:' mapping and 'default_backend' (DATA-MODEL §8) so curator.worker "
                    "can run consolidation."
                ),
            }

        recovered = [
            {"run_id": r.run_id, "status": r.status, "counts": r.counts}
            for r in recover(self._repo, state_store=self._state)
        ]
        report = run(
            self._repo,
            backend=backend,
            state_store=self._state,
            now=_now(),
            taxonomy=cfg.taxonomy,
            max_attempts=cfg.max_attempts,
            related_k=cfg.related_k,
            max_candidates=cfg.max_candidates_per_run,
            max_orphans=cfg.max_orphans,
        )
        return {
            "status": report.status,
            "published_commit": report.published_commit,
            "counts": report.counts,
            # Non-fatal diagnostics for a run that published anyway — chiefly "PASS 2 authored no
            # prose, the bodies are placeholders" (#115). An agent reading only ``status`` would
            # otherwise be told the consolidation succeeded while every new note is empty.
            "warnings": report.warnings,
            # #96: the fatal counterpart to ``warnings``. An agent that sees ``status: "failed"``
            # and nothing else is in exactly the position the human operator was in — it now gets
            # the bounded reason echo plus the repo-relative path to the lossless record.
            "failure": (
                None
                if report.failure is None
                else {
                    "run_id": report.failure.run_id,
                    "phase": report.failure.phase,
                    "reasons": list(report.failure.reasons),
                    "record_path": report.failure.record_path,
                    "cas_conflict": report.failure.cas_conflict,
                }
            ),
            "recovered": recovered,
        }

    def _build_backend(
        self,
        *,
        default_backend: str | None = None,
        allow_reduced_isolation: bool = False,
        body_byte_bound: int = DEFAULT_BODY_BYTE_BOUND,
        language: str | None = None,
    ) -> RoutedBackend | SubprocessBackend | None:
        """Resolve the configured WRITE-adapter(s) into a worker backend, or ``None``.

        Loads ``adapters.yaml`` (DATA-MODEL §8) and delegates to the shared
        :func:`~agora_kb.curator.subprocess_backend.build_routed_backend`, honoring the optional
        per-act ``routing`` table (ADR-0015): ``plan`` and ``author`` may run on different brains.
        ``None`` (an absent file, or a ``network: 'none'`` act with no usable OS sandbox under
        ``allow_reduced_isolation=False``) is the caller's "no backend configured" signal — the face
        stays SILENT (``report=None``), unlike the CLI which prints to stderr. A missing executable
        surfaces later at invocation as a clear error from the backend itself.
        """
        adapters_path = self._repo.layout.root / "adapters.yaml"
        try:
            registry = load_backend_registry(adapters_path)
        except ValueError:
            # A malformed adapters.yaml (incl. an invalid ADR-0015 ``routing:`` block) is unusable;
            # the silent face reports it as "no_backend" rather than raising to the MCP client.
            return None
        if registry is None:
            return None
        return build_routed_backend(
            registry,
            allow_reduced_isolation=allow_reduced_isolation,
            default_backend=default_backend,
            body_byte_bound=body_byte_bound,
            language=language,
            report=None,
        )


#: The kinds the L2-1 orphan signal is derived over — the claim-bearing tiers (ADR-0041 D2.5).
#:
#: One schema-agnostic predicate replaces the v1 ``type == "theme"`` test, and it is EQUIVALENT on
#: schema 1 rather than merely similar: :attr:`Note.kind <agora_kb.schema.notes.Note.kind>` is
#: derived on both schemas, ``type: theme`` maps to ``concept`` (D2.5), and schema 1 has no
#: ``summary`` antecedent — so on a v1 repo this set matches exactly the v1 themes. It is the
#: read-side twin of ``schema.lint._V2_SOURCED_KINDS`` and MUST stay equal to it: lint's L2-1
#: warning (``curator.lint.max_orphans``, ADR-0022) gates a curator run while this number is shown
#: on the dashboard, and an operator cannot act on two different answers to one question.
#: ``entity`` and ``person`` are exempt by construction — an entity may not be a map child (D1.3)
#: and the curator may not link into ``people/`` at all (D3.3), so both are orphans by design and
#: counting them would make the signal noise.
#: It IS :data:`~agora_kb.core.layout.CLAIM_BEARING_KINDS` (the same object, not a copy of its
#: members), so "MUST stay equal" above is now structural rather than a promise a future edit can
#: quietly break;
#: ``tests/core/test_layout.py::test_the_claim_bearing_kind_set_is_one_object_in_all_three_modules``
#: pins the three-way equality as well.
ORPHAN_KINDS: frozenset[str] = CLAIM_BEARING_KINDS


def _canonical_schema_version(layout: RepoLayout) -> int | None:
    """The repo's CANONICAL declared KB wiki schema, or ``None`` when it is not determinable.

    The narrow read — ``_meta/taxonomy.yaml`` alone, through
    :func:`~agora_kb.config.read_canonical_kb_schema_version` — that
    :func:`~agora_kb.core.inbox.assert_writable_repo_schema` makes before it refuses a capture, and
    that ``agora doctor``'s write verdict is keyed on. Deliberately NOT the broader
    ``read_kb_schema_version``, which falls back to the git-ignored ``_kb/repo.yaml`` mirror and
    defaults a bare directory to ``1``: a writability claim on a read surface must be true of the
    write refusal, not of a lookalike. Never raises — an unreadable or unparseable declaration is
    reported as ``None`` (and therefore as not writable), which is what it is.
    """
    from agora_kb.config import read_canonical_kb_schema_version

    try:
        return read_canonical_kb_schema_version(layout)
    except (OSError, ValueError):
        return None


def _is_orphan(note: object, referenced: set[str]) -> bool:
    """True iff ``note`` is a claim-bearing note whose basename nothing references (L2-1).

    Shared by :meth:`AgoraHandlers.health` (the count) and :meth:`AgoraHandlers.graph` (the
    per-node flag) so the dashboard's number and the graph's highlighting can never disagree. A
    ``person`` note is excluded by :data:`ORPHAN_KINDS` alone — no extra path test is needed, and
    adding one would make this DISAGREE with lint on a schema-1 repo that happens to have a
    ``wiki/people/`` directory (there, ``skip_people`` is ``False``).
    """
    return (
        note.kind in ORPHAN_KINDS  # type: ignore[attr-defined]
        and note.basename not in referenced  # type: ignore[attr-defined]
    )


def _is_ungraded_people_note(note: object) -> bool:
    """True iff ``note`` is a schema-2 ``wiki/people/**`` note — ungraded, human-owned (D3.3).

    A thin alias for :func:`agora_kb.schema.notes.is_ungraded_people_note`, kept as this module's
    local name because it reads at three call sites here. The schema test is not decoration:
    ``schema.lint`` computes its people exclusion as ``skip_people = version >= 2``, so an
    unconditional path test would diverge from the gate on a schema-1 repo that merely owns a
    ``people`` DOMAIN. One question, one answer — and now literally one function.
    """
    from agora_kb.schema.notes import is_ungraded_people_note

    return is_ungraded_people_note(note)  # type: ignore[arg-type]


def _is_raw_citation(rel_path: str) -> bool:
    """True iff ``rel_path`` is SPELLED as a repo-relative ``raw/`` citation (#169 D3).

    A messaging predicate, not a security one. It restates rawstore's textual gate 1 — normalized,
    relative, ``raw/``-prefixed — so :meth:`AgoraHandlers.raw` can answer ``invalid_path`` for an
    argument that is not a ``raw/`` path at all and ``not_found`` for one that is but does not
    resolve. Passing it grants nothing: :func:`agora_kb.core.rawstore.resolve` still runs all three
    gates, and the paths it refuses there are reported as ``not_found`` with no explanation, so no
    caller can tell "outside the repo" from "not on disk" and probe the filesystem one status at a
    time. Keeping the two statements separate is deliberate — the safe answer to a path this
    function accepts is still "no".
    """
    if not rel_path or rel_path.startswith("/") or posixpath.isabs(rel_path):
        return False
    if posixpath.normpath(rel_path) != rel_path:
        return False
    return rel_path.startswith("raw/")


#: The ONE wording for "the read verb found nothing", as a ``str.format`` template.
#:
#: Hoisted out of the ``kb_read`` closure (DRILLDOWN-169 A5/D14) because the CLI read verbs are the
#: THIRD consumer of this sentence: ``agora read`` runs kb_read's exact algorithm (note, then the
#: ``raw/`` bridge, then this) and must not invent a second remedy that sends an operator somewhere
#: else than the tool does. It is a template rather than a rendered string so both call sites format
#: the SAME bytes, path repr included.
_KB_READ_NOT_FOUND_NOTE = (
    "no tracked note at path={path!r}: pass a `path` exactly as returned by a "
    "kb_query hit or a kb_neighbors node id (e.g. 'wiki/concepts/<name>.md')."
)


def _kb_read_not_found(path: str, *, raw_note: str | None = None) -> dict[str, object]:
    """The shared ``kb_read`` not-found payload — one shape, one sentence, two faces.

    Module-private but deliberately imported by :mod:`agora_kb.cli` (in-package): ``agora read
    --json`` prints the handler payload VERBATIM, so composing the dict here rather than twice is
    what makes the CLI's JSON byte-identical to what an agent gets over MCP.

    ``raw_note`` is the ``raw/`` seam's own explanation (:meth:`AgoraHandlers.raw` ``note``) and
    is used ONLY when ``path`` is ``raw/``-shaped: a ``kb_query`` hit never names a ``raw/`` path,
    so the wiki sentence alone would send the caller to the wrong tool. The raw sentence leads
    (it names the L1-8b sidecar rule or the ``sources:`` spelling to check) and the wiki sentence
    follows, so there is still exactly one place composing "kb_read found nothing".
    """
    note = _KB_READ_NOT_FOUND_NOTE.format(path=path)
    if raw_note and path.startswith("raw/"):
        note = f"{raw_note} (kb_read also opens wiki notes: {note})"
    return {
        "error": "not_found",
        "path": path,
        "note": note,
    }


def _parse_ops(value: str) -> dict[str, int]:
    """Parse a ``log.md`` ``dispositions`` value (``CREATE_THEME=2, DROP=1`` | ``no-op``) → counts.

    Mirrors the ``", ".join(f"{op}={n}" …)`` form ``worker._append_log`` writes. ``no-op`` (the
    empty-counts marker) and any malformed ``op=n`` token (a non-integer count) is skipped, so the
    dashboard tail never raises on a hand-edited or future log line.
    """
    ops: dict[str, int] = {}
    if not value or value == "no-op":
        return ops
    for token in value.split(","):
        op, sep, count = token.strip().partition("=")
        if not sep:
            continue
        try:
            ops[op.strip()] = int(count.strip())
        except ValueError:
            continue
    return ops


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated ``log.md`` list bullet (``contested``/``dropped``/…) into items."""
    return [item.strip() for item in value.split(",") if item.strip()]


def _now() -> datetime:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _utc_today() -> str:
    """Current UTC calendar day as ``YYYY-MM-DD`` — the partition key for ``processed/<date>/``."""
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")


def _iso_z(value: datetime) -> str:
    """Render a UTC instant as ``2026-06-13T03:00:12Z`` (matches the state-file form)."""
    from datetime import UTC

    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_server(*, repo_path: Path, writer: str = DEFAULT_WRITER) -> FastMCP:
    """Construct the FastMCP server: build handlers over ``repo_path`` and register the 7 tools.

    The returned :class:`fastmcp.FastMCP` instance has ``kb_remember`` / ``kb_query`` /
    ``kb_read`` / ``kb_neighbors`` / ``kb_context`` / ``kb_status`` / ``kb_curate`` registered,
    plus the ``agora://gold/{pack}`` resource and the ``gold_context`` prompt (all three gold
    channels wrap the SAME :meth:`AgoraHandlers.gold_pack` — ADR-0027 Phase C, #40). Each tool
    delegates to an :class:`AgoraHandlers` method that holds the transport-free, unit-testable
    logic; the ``kb_read`` / ``kb_neighbors`` wrappers additionally shape their not-found responses
    in the closure (#58), covered over a real Client by ``tests/faces/test_mcp_read_tools.py``, and
    the gold resource/prompt shape their not-built outcome in the closure (#40, covered by
    ``tests/faces/test_gold_consumption.py``).
    """
    from fastmcp import FastMCP
    from fastmcp.exceptions import ResourceError

    repo = Repo.resolve(repo_path)
    # #98 / DESIGN §10 V9: refuse to stand up a face over a repo whose KB schema this build does not
    # understand. `agora serve` already stops at the CLI dispatch guard, so this fires for the OTHER
    # callers — a programmatic embedder, a supervisor importing build_server directly — where a
    # silent misread would put an old binary's writes into a newer repo. Raises
    # UnsupportedSchemaVersionError; a repo that cannot be read at all passes through untouched.
    guard_repo_schema_version(repo.layout)
    handlers = AgoraHandlers(repo, writer=writer)
    mcp: FastMCP = FastMCP(name="agora-kb")

    @mcp.tool
    def kb_remember(
        text: str,
        target: str = "personal",
        domain: str | None = None,
        tags: list[str] | None = None,
        source: str = DEFAULT_SOURCE,
    ) -> dict[str, object]:
        """Capture knowledge: append it to the inbox (non-blocking). Returns id/queued/inbox_depth.

        The writer identity is the connecting agent (server config), not an argument.

        ``source`` says what KIND of thing produced the capture. Pass ``agent:<your-name>`` (e.g.
        ``agent:aelix``) to capture first-class under your own identity — any name matching
        ``[A-Za-z0-9][A-Za-z0-9._-]*`` is accepted, no core change needed. Also valid: the fixed
        names ``claude-code``/``codex``/``qwen``/``gemini``/``opencode``/``hermes``/``manual`` (the
        default) and ``web:<user>``. A BARE name without the ``agent:`` prefix is rejected.
        """
        return handlers.remember(text, target=target, domain=domain, tags=tags, source=source)

    @mcp.tool
    def kb_query(question: str) -> dict[str, object]:
        """Search the wiki for evidence. Returns {query, status, hits[...]} with path/anchor cites.

        Hits are ordered citations into ``wiki/`` markdown — navigation, not synthesis.
        """
        return handlers.query(question)

    @mcp.tool
    def kb_read(path: str) -> dict[str, object]:
        """Open one wiki note OR one cited ``raw/`` source artifact by path.

        A wiki note comes back as raw markdown body + frontmatter + outgoing link basenames. A
        ``raw/`` path — one of the strings in a note's ``sources:`` — comes back as the captured
        evidence itself: ``resource: "raw"`` plus the extracted ``text`` for a text capture, or the
        capture facts (``meta``: digest, media type, size, filename, provenance) for a
        ``raw/_blob/`` binary, whose BYTES are never served over MCP.

        Navigation protocol: kb_query (broad search) -> kb_read (open a hit's ``path``) ->
        kb_neighbors (follow that note's links) -> kb_query again with sharper terms; repeat —
        re-querying erases vocabulary mismatch. To check a claim against its evidence, read the
        note's ``sources:`` entries with kb_read too (kb_read("raw/...")) — that is the hop from a
        curated sentence down to the capture it was written from. ``path`` is a kb_query hit's
        ``path``, a kb_neighbors node ``id``, or a ``sources:`` string. An unknown or out-of-repo
        path returns ``{error: "not_found", ...}`` — never file contents outside the wiki.

        Reads are FIRST CLASS over the whole tree, ``wiki/people/**`` included (ADR-0041 D3.3): a
        human-owned note is indexed, readable and navigable here. That is deliberately WIDER than
        the standing-context channel — ``kb_context`` serves gold packs, which exclude ``people/``
        by construction — because a pull-shaped, agent-initiated read is a different risk from a
        push-shaped pack assembled without a prompt. ADR-0027 §8's scope names the read tools as an
        emission path whose control is distinct and still undesigned (residual R1) — which now
        covers the ``raw/`` captures this tool reaches as well, content no curator run ever graded.
        """
        # Note first, then raw/: the wiki is the curated answer and stays the fast path, and the
        # two namespaces cannot collide (a tracked note is never under raw/). The DISCRIMINATOR a
        # caller reads is the `resource` key, present ONLY on a raw payload — not `kind`, which is
        # the closed ADR-0041 note vocabulary that lint, gold, graph and browse all key on
        # (DRILLDOWN-169 D4). Extending kb_read rather than adding an eighth tool keeps the
        # client-facing tool count at seven, which is the expensive thing to reverse.
        payload = handlers.note(path)
        if payload is not None:
            return payload
        raw = handlers.raw(path)
        if raw["status"] == "ok":
            return raw
        # A raw/-shaped path that did not resolve falls through to the SAME not-found shape a bad
        # note path gets, composed in ONE place (`agora read` is the third face that owes the
        # caller that sentence); the raw seam's own explanation leads for raw/-shaped paths.
        return _kb_read_not_found(path, raw_note=str(raw.get("note", "")))

    @mcp.tool
    def kb_neighbors(path: str, depth: int = 1) -> dict[str, object]:
        """List a note's link neighborhood (ego-graph): nodes {id, title, ...} + directed edges.

        Navigation protocol: kb_query (broad search) -> kb_read (open a hit's ``path``) ->
        kb_neighbors (follow links from here) -> kb_query again with sharper terms; repeat. Each
        node ``id`` is a rel_path that feeds straight back into kb_read. ``depth`` is hops from
        ``path`` (default 1; clamped server-side — the echoed ``depth`` is the effective value).
        An unknown ``path`` returns an empty graph with ``center: null`` plus a not-found note.
        """
        result = handlers.graph(center=path, depth=depth)
        if result["center"] is None:
            result["note"] = (
                f"no tracked note at path={path!r} — empty graph. Pass a `path` exactly as "
                "returned by a kb_query hit or a kb_neighbors node id."
            )
        return result

    @mcp.tool
    def kb_context(pack: str = "default") -> dict[str, object]:
        """Fetch the standing gold context pack: a small, token-budgeted, byte-stable slice of the
        curated wiki, assembled at a curated commit (ADR-0027).

        Complementary to the retrieval tools: kb_context is STANDING context — broad, stable,
        prompt-cache-friendly, suited to session start — while kb_query answers a specific question
        with cited evidence and kb_read opens one cited note. The ``text`` is served byte-identical
        to the built ``_kb/gold/<pack>.md`` artifact and is wrapped in trusted
        ``<!-- agora:pack ... -->`` / ``<!-- agora:pack:end ... -->`` sentinel markers — keep them
        intact when injecting: they are the ADR-0027 §8 loop-break contract that lets harvesters
        span-drop a re-ingested pack. Pull-only, on-request — nothing is ever auto-injected.
        Returns ``status: "not_built"`` with build guidance (and the packs that DO exist) when the
        pack has not been built yet. A ``scopes`` parameter is reserved as a future additive
        extension (federation, reserved ADR-0030) and is not accepted in v0.1.

        ``wiki/people/**`` is absent from every pack BY CONSTRUCTION, not by a filter here: this
        channel serves built pack bytes and the assembler drops human-owned notes from the
        population before scoring (ADR-0041 D3.3 day-1 exclusion, ``core.gold``). Nothing in this
        face can re-admit them, which is the point of serving the artifact rather than assembling.
        """
        return handlers.gold_pack(pack)

    @mcp.resource("agora://gold/{pack}")
    def gold_pack_resource(pack: str) -> str:
        """One gold context pack, byte-identical to the built ``_kb/gold/<pack>.md`` (ADR-0027).

        The resource form of ``kb_context`` for clients that attach context as resources — the same
        :meth:`AgoraHandlers.gold_pack` read. A not-built (or invalid) pack raises a clear
        :class:`~fastmcp.exceptions.ResourceError` carrying the build guidance rather than serving
        placeholder bytes (the resource contract is the pack text itself, byte-identical).
        """
        payload = handlers.gold_pack(pack)
        if payload["status"] != "ok":
            raise ResourceError(str(payload["note"]))
        return str(payload["text"])

    @mcp.prompt
    def gold_context(pack: str = "default") -> str:
        """Inject the gold context pack into the conversation (the ADR-0027 prompt channel).

        Returns the built pack text VERBATIM (byte-identical, sentinel markers intact) so invoking
        the prompt is exactly the opt-in, human-triggered injection ADR-0027 decision 7 describes.
        When the pack is not built, the prompt returns the actionable build-guidance note instead
        (a prompt is a conversation aid — a hard error would be less useful than the remedy).
        """
        payload = handlers.gold_pack(pack)
        return str(payload["text"] if payload["status"] == "ok" else payload["note"])

    @mcp.tool
    def kb_status() -> dict[str, object]:
        """Report backlog, last consolidation, processed-today/failed counts, and counters."""
        return handlers.status()

    @mcp.tool
    def kb_curate(target: str = "personal", force: bool = False) -> dict[str, object]:
        """Run one consolidation against the configured backend. Returns status/commit/counts.

        Recovers any in-flight run, then runs ``curator.worker`` over the ``adapters.yaml`` brain.
        ``target`` defaults to ``"personal"`` — the only repo until multi-tenancy lands. Returns
        ``status: "no_backend"`` (not an error) when no backend is configured.
        """
        return handlers.curate(target=target, force=force)

    return mcp


def main(argv: list[str] | None = None) -> int:
    """Build the server for a repo path and run it over stdio (the local MVP entry point)."""
    parser = argparse.ArgumentParser(prog="agora-mcp", description="Agora MCP server (stdio).")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Path to the knowledge repo (default: current directory).",
    )
    parser.add_argument(
        "--writer",
        default=DEFAULT_WRITER,
        help=f"Writer identity for captures (default: {DEFAULT_WRITER!r}).",
    )
    args = parser.parse_args(argv)
    server = build_server(repo_path=args.repo, writer=args.writer)
    server.run(transport="stdio")
    return 0
