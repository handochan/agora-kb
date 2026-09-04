"""``raw/_blob/`` — APPLY materialises a capture's ORIGINAL BYTES (ADR-0041 D1.4 / D4.2, #153).

The write half of the capture channel, end to end and with ZERO real model: a face stages an
artefact's bytes beside the inbox event that summarises it, the claim carries both into the run, and
the deterministic APPLY pass — the sole writer of ``raw/`` (ADR-0020 decision 3) — copies the bytes
into ``raw/_blob/<ab>/<sha256>.<ext>``, writes the ``.meta.yaml`` capture sidecar, and cites the
BLOB (never the sidecar) in the note's ``sources:`` beside the ``raw/<domain>/<event_id>.md`` text
evidence.

Four properties this file exists to pin, because each one is a way the channel could quietly go
wrong instead of loudly failing:

* **The bytes survive verbatim.** The fixture artefact is deliberately not text — a NUL, a lone
  ``0xff``, a CRLF — so any accidental text-mode round trip, newline translation or decode attempt
  corrupts it visibly rather than passing.
* **Authorship still gates admission.** Content-addressing is an INTEGRITY self-check and never an
  authorship one (D1.4, normative): a brain-planted blob whose name correctly hashes its own bytes
  is still refused, and a PASS-2 overwrite of a blob the engine DID write is refused too.
* **Immutability.** A second capture of identical bytes cites the existing blob and rewrites
  nothing — not the blob, not the sidecar, which still names the FIRST event that delivered them.
* **The brain never sees bytes.** ``candidates.json`` carries a text summary (filename, media type,
  size, digest); the bundle tree the backend reads contains the artefact nowhere.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from agora_kb.config import KbIdentity, write_kb_identity
from agora_kb.core import frontmatter
from agora_kb.core.inbox import Inbox
from agora_kb.core.layout import RepoLayout
from agora_kb.core.repo import Repo
from agora_kb.core.state import StateStore
from agora_kb.curator.apply import region_sentinel_id
from agora_kb.curator.worker import AuthorRegion, Backend, FakeBackend, RunReport, run
from agora_kb.schema.emit import Taxonomy, emit_schema
from agora_kb.schema.lint import lint

NOW = datetime(2026, 6, 13, 3, 0, 0, tzinfo=UTC)
RUN_DATE = "2026-06-13"
KB_ID = "01J8ZQ3M4N5P6Q7R8S9T0V1W2X"

TAXONOMY = Taxonomy(
    schema_version=2,
    taxonomy_policy="open",
    allowed_tags=("curator", "concurrency"),
    domains=("ai-tech", "general"),
)

#: The fixture artefact: NOT text under any codec. A NUL, a lone ``0xff`` (invalid UTF-8 in every
#: position), and a CRLF — so a text-mode write, a universal-newline read or a decode attempt
#: anywhere on the path corrupts it into a visible test failure instead of passing by luck.
BLOB_BYTES = b"%PDF-1.7\r\n\x00\xff\xfe binary payload \x00\r\nnot text\n"
BLOB_SHA = hashlib.sha256(BLOB_BYTES).hexdigest()
BLOB_REF = f"raw/_blob/{BLOB_SHA[:2]}/{BLOB_SHA}.pdf"
SIDECAR_REF = f"{BLOB_REF}.meta.yaml"

#: The OTHER dangerous artefact shape: CRLF and NOT ONE NUL, so git classifies it TEXT and — under
#: ``core.autocrlf`` — normalises it to LF on commit. That rewrite would leave the published blob no
#: longer hashing to its own basename, breaking D1.4's integrity property permanently.
#: ``BLOB_BYTES`` cannot catch it (its NUL makes git call it binary and skip the conversion).
CRLF_BYTES = b"name,value\r\nalpha,1\r\nbeta,2\r\n"
CRLF_SHA = hashlib.sha256(CRLF_BYTES).hexdigest()
CRLF_REF = f"raw/_blob/{CRLF_SHA[:2]}/{CRLF_SHA}.csv"


# --- fixtures -----------------------------------------------------------------------------------


def _init_repo(tmp_path: Path) -> Repo:
    """Init a git SCHEMA-2 knowledge repo committed at the curated tip (mirrors test_worker.py)."""
    layout = RepoLayout(tmp_path)
    layout.root.mkdir(parents=True, exist_ok=True)
    write_kb_identity(layout, KbIdentity(kb_id=KB_ID, name="agora-fixture"))
    for directory in ("concepts", "summaries", "notes", "maps", "entities", "people"):
        (layout.wiki_dir / directory).mkdir(parents=True, exist_ok=True)
        (layout.wiki_dir / directory / ".gitkeep").write_text("", encoding="utf-8")
    repo = Repo(layout)
    repo.init(when=datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC), schema_version=2, kb_id=KB_ID)
    emit_schema(layout, taxonomy=TAXONOMY, schema_version=2)
    repo.commit_worktree(
        repo.root, "chore: emit schema", when=datetime(2026, 6, 12, 1, 0, 0, tzinfo=UTC)
    )
    return repo


def _capture_with_attachment(
    inbox: Inbox,
    *,
    text: str,
    second: int,
    data: bytes = BLOB_BYTES,
    filename: str | None = "2026-q3-report.pdf",
    media_type: str | None = "application/pdf",
) -> str:
    """Write one inbox capture carrying ``data`` as its original-bytes attachment."""
    now = datetime(2026, 6, 13, 2, 40, second, tzinfo=UTC)
    return inbox.write(
        text=text,
        writer="dochan",
        source="web:dochan",
        domain="ai-tech",
        attachments=[(filename, media_type, data)],
        now=now,
    ).id


def _create_theme_plan(candidate_id: str, event_id: str, *, basename: str) -> str:
    """A canned single-CREATE_THEME ``plan.json`` over one candidate."""
    return json.dumps(
        {
            "schema_version": 1,
            "run_id": "ignored",
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": candidate_id,
                    "event_ids": [event_id],
                    "op": "CREATE_THEME",
                    "domain": "ai-tech",
                    "basename": basename,
                    "title": "Curator concurrency model",
                    "summary": "One curator advances the curated branch under a per-repo lock.",
                    "status": "active",
                    "tags": ["curator", "concurrency"],
                    "aliases": [],
                    "links": [],
                    "needs_prose": True,
                    "reason": "New concept; no related note above threshold.",
                }
            ],
        }
    )


def _prose(candidate_id: str = "c1") -> dict[str, str]:
    """The run-scoped prose map FakeBackend is keyed by (``{plan.run_id}--{candidate_id}``)."""
    return {region_sentinel_id("ignored", candidate_id): "The single curator holds a flock."}


def _run(repo: Repo, backend: Backend, *, now: datetime = NOW) -> RunReport:
    return run(
        repo, backend=backend, state_store=StateStore(repo.layout), now=now, taxonomy=TAXONOMY
    )


def _published_sources(repo: Repo, commit: str, note_rel: str) -> list[str]:
    """The ``sources:`` list of one note in the PUBLISHED tree (the CAS moved only the ref)."""
    with repo.worktree(at=commit) as published:
        fm, _ = frontmatter.parse((published / note_rel).read_text(encoding="utf-8"))
    sources = fm.get("sources")
    assert isinstance(sources, list)
    return [s for s in sources if isinstance(s, str)]


class _BlobForgingBackend(FakeBackend):
    """A backend that OVERWRITES an engine-written file with forged bytes during PASS 2."""

    def __init__(self, plan_text: str, *, forge_ref: str, forged: bytes, **kw: object) -> None:
        super().__init__(plan_text, **kw)  # type: ignore[arg-type]
        self._forge_ref = forge_ref
        self._forged = forged

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, AuthorRegion],
    ) -> None:
        super().author(worktree, needs_prose, context)
        forged = worktree / self._forge_ref
        forged.parent.mkdir(parents=True, exist_ok=True)
        forged.write_bytes(self._forged)


class _BundleSnoopingBackend(FakeBackend):
    """A backend that records every file in the bundle it is handed, path AND exact bytes.

    The bundle lives under ``processing/<run-id>/bundle/`` and is ``rmtree``d when the run
    finalizes, so the only place to observe it is where the backend does: inside ``plan()``.
    """

    def __init__(self, plan_text: str, **kw: object) -> None:
        super().__init__(plan_text, **kw)  # type: ignore[arg-type]
        self.seen: dict[str, bytes] = {}

    def plan(self, bundle_dir: Path) -> str:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_file():
                self.seen[path.relative_to(bundle_dir).as_posix()] = path.read_bytes()
        return super().plan(bundle_dir)


# --- (1) the happy path: bytes in, blob + sidecar + citation out --------------------------------


def test_apply_materialises_the_blob_and_the_note_cites_it(tmp_path: Path) -> None:
    """A full run over an event with a BINARY attachment publishes the artefact under
    ``raw/_blob/`` byte-for-byte, writes its closed-key sidecar, and cites the blob beside the text
    evidence in ``sources:`` — never the sidecar (lint L1-8b), and the published tree lints clean
    (L1-8: every cited source resolves)."""
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _capture_with_attachment(
        inbox, text="One curator advances the branch under a lock.", second=10
    )
    # The staging half (B1) really put the bytes in the writer's namespace, unmodified.
    staged = layout.inbox_attachment_path("dochan", BLOB_SHA, "pdf")
    assert staged.read_bytes() == BLOB_BYTES

    backend = FakeBackend(
        _create_theme_plan("c1", e1, basename="curator-concurrency"), prose=_prose()
    )
    report = _run(repo, backend)

    assert report.status == "published"
    assert report.published_commit is not None

    with repo.worktree(at=report.published_commit) as published:
        blob = published / BLOB_REF
        # Byte-for-byte, through a rename-free copy: the NUL, the lone 0xff and the CRLF all
        # survive, and the file hashes to the name it is filed under (the D1.4 self-check).
        assert blob.read_bytes() == BLOB_BYTES
        assert hashlib.sha256(blob.read_bytes()).hexdigest() == blob.stem

        sidecar = published / SIDECAR_REF
        doc = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
        # The CLOSED key set (DATA-MODEL §2) — and, just as load-bearing, NOT the extracted text.
        assert set(doc) == {
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
        assert doc["sha256"] == BLOB_SHA
        assert doc["ext"] == "pdf"
        assert doc["media_type"] == "application/pdf"
        assert doc["bytes"] == len(BLOB_BYTES)
        assert doc["filename"] == "2026-q3-report.pdf"
        assert doc["captured_at"] == "2026-06-13T02:40:10Z"
        assert doc["writer"] == "dochan"
        assert doc["source"] == "web:dochan"
        assert doc["event_id"] == e1
        assert "One curator advances" not in sidecar.read_text(encoding="utf-8")

        # The published tree lints clean: L1-8 resolves the raw/_blob/ citation like any other
        # raw/ path, so the blob shape needs no lint change to be citable.
        assert lint(RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE).ok

    sources = _published_sources(
        repo, report.published_commit, "wiki/concepts/curator-concurrency.md"
    )
    # BOTH artefacts of one capture, text first: the extracted knowledge and the bytes it came
    # from. Citing only one of them loses half the provenance.
    assert sources == [f"raw/ai-tech/{e1}.md", BLOB_REF]
    assert SIDECAR_REF not in sources  # L1-8b: cite the artefact, never its sidecar


def test_a_crlf_text_artefact_still_hashes_to_its_own_basename_after_commit(
    tmp_path: Path,
) -> None:
    """git must never translate the EOLs of a content-addressed original (ADR-0041 D1.4).

    ``core.autocrlf=input`` is set here at the REPO-LOCAL scope on purpose: it is the one scope the
    hermetic commit env (``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` → devnull) cannot neutralise.
    With a CRLF, NUL-free artefact — git classifies it TEXT — an unguarded ``git add -A`` rewrites
    the bytes to LF on commit; the published blob then no longer hashes to its own filename, and
    every later run that re-cites that digest fails in ``_materialize_one_blob``'s re-verification.
    The two guards under test are the seeded ``.gitattributes`` (``raw/_blob/** -text``) and the
    ``-c core.autocrlf=false`` argv pin on every engine git call.
    """
    repo = _init_repo(tmp_path)
    subprocess.run(  # noqa: S603,S607 - argv list, no shell
        ["git", "config", "core.autocrlf", "input"], cwd=repo.root, check=True
    )
    e1 = _capture_with_attachment(
        Inbox(repo.layout),
        text="Quarterly rows, exported.",
        second=10,
        data=CRLF_BYTES,
        filename="rows.csv",
        media_type="text/csv",
    )

    backend = FakeBackend(
        _create_theme_plan("c1", e1, basename="curator-concurrency"), prose=_prose()
    )
    report = _run(repo, backend)

    assert report.status == "published"
    assert report.published_commit is not None
    with repo.worktree(at=report.published_commit) as published:
        blob = published / CRLF_REF
        assert blob.read_bytes() == CRLF_BYTES  # the CRLFs survived
        assert hashlib.sha256(blob.read_bytes()).hexdigest() == blob.stem
    # And the second capture of the same artefact re-cites it instead of failing re-verification —
    # the permanent failure a rewritten byte would have caused.
    e2 = _capture_with_attachment(
        Inbox(repo.layout),
        text="The same rows, captured again.",
        second=20,
        data=CRLF_BYTES,
        filename="rows.csv",
        media_type="text/csv",
    )
    second = _run(
        repo,
        FakeBackend(_create_theme_plan("c1", e2, basename="curator-lock"), prose=_prose()),
        now=datetime(2026, 6, 13, 4, 0, 0, tzinfo=UTC),
    )
    assert second.status == "published"


def test_a_dropped_capture_materialises_no_blob_and_keeps_the_bytes_in_the_spool(
    tmp_path: Path,
) -> None:
    """A DROP writes no note, so nothing cites the artefact and APPLY materialises nothing.

    Pinned rather than left incidental, because it is the outcome the docs must not overstate: the
    bytes are NOT destroyed — they drain to ``_kb/processed/<date>/_attach/`` with their event and
    are never pruned — but ``_kb/`` is git-ignored, so a DROPped capture's artefact never enters
    the committed tree and ``agora sync`` never pushes it. The parity with a DROPped free-text
    capture (which writes no ``raw/<domain>/<event_id>.md`` either) is the reason: an UNCITED blob
    would be a file the final diff admits that lint L1-8 can never account for.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    e1 = _capture_with_attachment(Inbox(layout), text="A marginal capture.", second=10)
    plan = json.dumps(
        {
            "schema_version": 1,
            "run_id": "ignored",
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": "c1",
                    "event_ids": [e1],
                    "op": "DROP",
                    "needs_prose": False,
                    "reason": "Too thin to be worth a note.",
                }
            ],
        }
    )

    report = _run(repo, FakeBackend(plan, prose={}))

    assert report.status == "published"
    assert report.counts.get("DROP") == 1
    with repo.worktree(at=report.published_commit or "HEAD") as published:
        assert not (published / "raw" / "_blob").exists()
        assert not (published / f"{BLOB_REF}.meta.yaml").exists()
    # Not destroyed: the archival copy drained to the (git-ignored) processed spool with its event.
    processed = layout.processed_dir / RUN_DATE
    assert (processed / f"{e1}.md").is_file()
    assert (processed / "_attach" / f"{BLOB_SHA}.pdf").read_bytes() == BLOB_BYTES
    assert not layout.inbox_attachment_path("dochan", BLOB_SHA, "pdf").exists()


def test_the_staged_attachment_drains_with_its_event(tmp_path: Path) -> None:
    """The staged bytes follow their event through the spool and out to ``processed/``.

    The event and its bytes are ONE delivery (ADR-0041 D4.2). If the drain moved only the event,
    the artefact's staging copy would be destroyed with the ``processing/`` tree — recoverable
    afterwards only from git, and not at all for a run that failed before publishing.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _capture_with_attachment(
        inbox, text="One curator advances the branch under a lock.", second=10
    )
    report = _run(
        repo,
        FakeBackend(_create_theme_plan("c1", e1, basename="curator-concurrency"), prose=_prose()),
    )
    assert report.status == "published"

    processed = layout.processed_dir / RUN_DATE
    assert (processed / f"{e1}.md").is_file()
    assert (processed / "_attach" / f"{BLOB_SHA}.pdf").read_bytes() == BLOB_BYTES
    # Nothing left staged in the writer's inbox, and the run's events tree is empty of both the
    # event and its bytes (the manifest stays behind by design; the payload does not).
    assert not layout.inbox_attachment_path("dochan", BLOB_SHA, "pdf").exists()
    events_dir = layout.processing_dir / report.run_id / "events"
    assert [p for p in events_dir.rglob("*") if p.is_file()] == []
    assert inbox.depth() == 0


# --- (2) authorship still gates admission -------------------------------------------------------


def test_a_pass2_overwrite_of_the_engine_blob_fails_the_final_diff(tmp_path: Path) -> None:
    """A brain that rewrites the blob APPLY materialised fails the run; nothing is published.

    The blob is in ``raw_writes`` (the engine wrote it this run), so it clears the AUTHORSHIP half
    of the gate — and is still refused, because its bytes no longer equal what the engine recorded.
    Without the second half a brain could forge the immutable verification baseline at a path the
    engine had legitimately opened.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _capture_with_attachment(
        inbox, text="One curator advances the branch under a lock.", second=10
    )
    base = repo.head_commit()

    backend = _BlobForgingBackend(
        _create_theme_plan("c1", e1, basename="curator-concurrency"),
        forge_ref=BLOB_REF,
        forged=b"\x00\xff FORGED ARTEFACT \x00\n",
        prose=_prose(),
    )
    report = _run(repo, backend)

    assert report.status == "failed"
    assert repo.branch_commit() == base
    checks = json.loads(
        next(iter(layout.failed_dir.rglob("error.json"))).read_text(encoding="utf-8")
    )["failed_checks"]
    assert any("FINAL-DIFF" in c and BLOB_SHA in c for c in checks)


def test_a_pass2_overwrite_of_the_sidecar_fails_the_final_diff(tmp_path: Path) -> None:
    """The SIDECAR is engine-written too, and is graded exactly like the blob.

    Its own gate entry matters: the sidecar is the capture record an operator reads to learn who
    delivered these bytes and when, so a brain that could rewrite it could relabel any artefact's
    provenance while leaving the (correctly hashed) bytes alone.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _capture_with_attachment(
        inbox, text="One curator advances the branch under a lock.", second=10
    )
    base = repo.head_commit()

    backend = _BlobForgingBackend(
        _create_theme_plan("c1", e1, basename="curator-concurrency"),
        forge_ref=SIDECAR_REF,
        forged=b"sha256: 0\nwriter: attacker\n",
        prose=_prose(),
    )
    report = _run(repo, backend)

    assert report.status == "failed"
    assert repo.branch_commit() == base


def test_a_missing_staged_attachment_fails_the_run_cleanly(tmp_path: Path) -> None:
    """An event citing bytes that are not on disk fails the run — as a REJECTION, not a traceback.

    The failure direction the channel is built around: bytes with no event are inert, an event with
    no bytes is a broken citation. APPLY refuses rather than publishing a note whose ``sources:``
    names a blob nobody wrote (which lint L1-8 would then reject on every subsequent run).
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _capture_with_attachment(
        inbox, text="One curator advances the branch under a lock.", second=10
    )
    base = repo.head_commit()
    # Remove the staged bytes AFTER the event was accepted (a hand-cleaned `_kb/`, a partial
    # restore): the event still names them, so the run must fail loudly.
    layout.inbox_attachment_path("dochan", BLOB_SHA, "pdf").unlink()

    report = _run(
        repo,
        FakeBackend(_create_theme_plan("c1", e1, basename="curator-concurrency"), prose=_prose()),
    )

    assert report.status == "failed"
    assert repo.branch_commit() == base
    checks = json.loads(
        next(iter(layout.failed_dir.rglob("error.json"))).read_text(encoding="utf-8")
    )["failed_checks"]
    assert any("BLOB" in c for c in checks)


# --- (3) immutability: identical bytes are re-cited, never rewritten ----------------------------


def test_a_second_capture_of_the_same_bytes_recites_the_existing_blob(tmp_path: Path) -> None:
    """Identical bytes are ONE artefact: the second run cites the existing blob and writes nothing.

    The sidecar is the observable: it still names the FIRST event, so the second run demonstrably
    did not rewrite it. Content-addressing is what makes this safe — the bytes cannot have changed
    under a name that is their digest — and immutability is what makes the citation stable.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _capture_with_attachment(
        inbox, text="One curator advances the branch under a lock.", second=10
    )
    first = _run(
        repo,
        FakeBackend(_create_theme_plan("c1", e1, basename="curator-concurrency"), prose=_prose()),
    )
    assert first.status == "published"
    assert first.published_commit is not None
    with repo.worktree(at=first.published_commit) as published:
        sidecar_after_first = (published / SIDECAR_REF).read_bytes()

    # A DIFFERENT capture (different text ⇒ a distinct tier-2 candidate) carrying the SAME artefact.
    e2 = _capture_with_attachment(
        inbox, text="The inbox is append-only and per-writer namespaced.", second=20
    )
    second = _run(
        repo,
        FakeBackend(_create_theme_plan("c1", e2, basename="inbox-append-only"), prose=_prose()),
        now=datetime(2026, 6, 13, 4, 0, 0, tzinfo=UTC),
    )
    assert second.status == "published"
    assert second.published_commit is not None

    with repo.worktree(at=second.published_commit) as published:
        assert (published / BLOB_REF).read_bytes() == BLOB_BYTES
        # NOT rewritten: still the first capture's record, byte for byte.
        assert (published / SIDECAR_REF).read_bytes() == sidecar_after_first
        assert yaml.safe_load((published / SIDECAR_REF).read_text(encoding="utf-8"))[
            "event_id"
        ] == (e1)
    # The second note cites the SAME blob — one artefact, two notes.
    assert BLOB_REF in _published_sources(
        repo, second.published_commit, "wiki/concepts/inbox-append-only.md"
    )


def test_two_events_with_one_artefact_in_a_single_run_write_one_blob(tmp_path: Path) -> None:
    """Two events in ONE run carrying identical bytes produce one blob, cited by both.

    The intra-run half of the same rule: the second event finds the blob already materialised by
    the first (the run has not committed yet), so it re-cites without a second write, and the
    ``sources:`` union collapses the duplicate ref.
    """
    repo = _init_repo(tmp_path)
    layout = repo.layout
    inbox = Inbox(layout)

    e1 = _capture_with_attachment(
        inbox, text="One curator advances the branch under a lock.", second=10
    )
    e2 = _capture_with_attachment(
        inbox, text="The inbox is append-only and per-writer namespaced.", second=11
    )
    plan = json.dumps(
        {
            "schema_version": 1,
            "run_id": "ignored",
            "finished": True,
            "dispositions": [
                json.loads(_create_theme_plan("c1", e1, basename="curator-concurrency"))[
                    "dispositions"
                ][0],
                json.loads(_create_theme_plan("c2", e2, basename="inbox-append-only"))[
                    "dispositions"
                ][0],
            ],
        }
    )
    report = _run(repo, FakeBackend(plan, prose={**_prose("c1"), **_prose("c2")}))

    assert report.status == "published"
    assert report.published_commit is not None
    with repo.worktree(at=report.published_commit) as published:
        blobs = sorted(p.name for p in (published / "raw" / "_blob").rglob("*") if p.is_file())
        assert blobs == [f"{BLOB_SHA}.pdf", f"{BLOB_SHA}.pdf.meta.yaml"]
    for note in ("curator-concurrency", "inbox-append-only"):
        sources = _published_sources(repo, report.published_commit, f"wiki/concepts/{note}.md")
        assert sources.count(BLOB_REF) == 1


# --- (4) the brain never sees bytes -------------------------------------------------------------


def test_the_bundle_carries_a_text_summary_and_never_the_bytes(tmp_path: Path) -> None:
    """``candidates.json`` summarises the attachment; the bundle tree contains no artefact bytes.

    The bundle is the sandboxed backend's entire world (ADR-0011 §1), and an opaque binary has no
    business in a prompt: it cannot be reasoned about, it is exactly the shape a prompt-injection
    payload hides in, and shipping it would put the artefact somewhere the redaction paths never
    look. The model gets the FACTS about the capture — name, media type, size, digest — and no way
    to reach the file.
    """
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)

    e1 = _capture_with_attachment(
        inbox, text="One curator advances the branch under a lock.", second=10
    )
    backend = _BundleSnoopingBackend(
        _create_theme_plan("c1", e1, basename="curator-concurrency"), prose=_prose()
    )
    report = _run(repo, backend)
    assert report.status == "published"

    assert backend.seen, "the backend was never handed a bundle"
    # No file in the bundle IS the artefact, and none CONTAINS it — a summary that embedded the
    # bytes (base64 or otherwise) would defeat the point as thoroughly as a copy would.
    for rel, data in backend.seen.items():
        assert data != BLOB_BYTES, rel
        assert BLOB_BYTES not in data, rel
        assert b"\x00\xff\xfe binary payload" not in data, rel

    doc = json.loads(backend.seen["candidates.json"])
    (attachment,) = doc["candidates"][0]["provenance"][0]["attachments"]
    assert attachment["sha256"] == BLOB_SHA
    assert attachment["filename"] == "2026-q3-report.pdf"
    assert attachment["media_type"] == "application/pdf"
    assert attachment["bytes"] == len(BLOB_BYTES)


def test_an_attachment_free_run_leaves_the_bundle_and_the_tree_untouched(tmp_path: Path) -> None:
    """A capture with no attachment produces no ``attachments`` key and no ``raw/_blob/`` at all.

    The channel is strictly additive: every existing repo, bundle and note keeps the shape it had.
    """
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)

    now = datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC)
    e1 = inbox.write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=now,
    ).id
    backend = _BundleSnoopingBackend(
        _create_theme_plan("c1", e1, basename="curator-concurrency"), prose=_prose()
    )
    report = _run(repo, backend)

    assert report.status == "published"
    assert report.published_commit is not None
    doc = json.loads(backend.seen["candidates.json"])
    assert "attachments" not in doc["candidates"][0]["provenance"][0]
    with repo.worktree(at=report.published_commit) as published:
        assert not (published / "raw" / "_blob").exists()
    assert _published_sources(
        repo, report.published_commit, "wiki/concepts/curator-concurrency.md"
    ) == [f"raw/ai-tech/{e1}.md"]


# --- (5) lint grades the blob citation shape ----------------------------------------------------


@pytest.mark.parametrize(
    ("cited", "expect_check"),
    [
        (BLOB_REF, None),  # L1-8: an existing blob resolves like any other raw/ artefact
        (SIDECAR_REF, "L1-8b"),  # cite the artefact, never its sidecar
        (f"raw/_blob/00/{'0' * 64}.pdf", "L1-8"),  # a blob nobody wrote does not exist
    ],
)
def test_lint_grades_blob_citations(tmp_path: Path, cited: str, expect_check: str | None) -> None:
    """L1-8/L1-8b need no change to grade ``raw/_blob/`` citations — that is the point of the
    ``<sha256>.<ext>.meta.yaml`` sidecar naming rule (ADR-0041 D1.4). Locked here because a future
    ``<sha256>.meta.yaml`` spelling would make the sidecar indistinguishable from an artefact whose
    extension merely happens to be ``yaml``, and L1-8b is a pure suffix test."""
    repo = _init_repo(tmp_path)
    inbox = Inbox(repo.layout)

    e1 = _capture_with_attachment(
        inbox, text="One curator advances the branch under a lock.", second=10
    )
    report = _run(
        repo,
        FakeBackend(_create_theme_plan("c1", e1, basename="curator-concurrency"), prose=_prose()),
    )
    assert report.status == "published"
    assert report.published_commit is not None

    with repo.worktree(at=report.published_commit) as published:
        note = published / "wiki" / "concepts" / "curator-concurrency.md"
        fm, body = frontmatter.parse(note.read_text(encoding="utf-8"))
        fm["sources"] = [f"raw/ai-tech/{e1}.md", cited]
        note.write_text(frontmatter.render(fm, body), encoding="utf-8")
        result = lint(RepoLayout(published), taxonomy=TAXONOMY, run_date=RUN_DATE)
        checks = {f.code for f in result.findings if f.severity == "error"}

    if expect_check is None:
        assert result.ok, checks
    else:
        assert expect_check in checks
