"""Tests for inbox attachment staging — the ADR-0041 D4.2 transport for original bytes.

The event and its bytes are ONE delivery: the bytes land inside the writer's own namespace at
``_kb/inbox/<writer>/_attach/<sha256>.<ext>`` before the event that names them, they are
content-addressed and immutable, and they travel with the event through the spool. What is pinned
here is every refusal that keeps those three sentences true.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agora_kb.core.frontmatter import parse
from agora_kb.core.inbox import (
    AttachmentContainmentError,
    AttachmentError,
    AttachmentIntegrityError,
    AttachmentTooLargeError,
    Inbox,
    attachment_sha256,
    carry_attachments,
    default_attachment_byte_cap,
    event_attachments,
    failed_event_count,
    iter_failed_events,
    parse_attachments,
    read_attachment,
    return_event_to_inbox,
)
from agora_kb.core.layout import (
    DEFAULT_ATTACHMENT_EXT,
    InvalidAttachmentExtError,
    RepoLayout,
    attachment_dir,
)

PDF = b"%PDF-1.4\nnot really a pdf\n"
PDF_SHA = hashlib.sha256(PDF).hexdigest()


@pytest.fixture()
def inbox(tmp_path: Path) -> Inbox:
    return Inbox(RepoLayout(tmp_path))


def _read(path: Path) -> tuple[dict, str]:
    return parse(path.read_text(encoding="utf-8"))


# --- the round trip -----------------------------------------------------------------------------
def test_write_stages_bytes_beside_the_event_and_records_them(inbox: Inbox) -> None:
    receipt = inbox.write(
        text="a summary of the pdf",
        writer="dochan",
        source="web:alice",
        attachments=[("report.pdf", "application/pdf", PDF)],
    )
    event = inbox.layout.inbox_item_path("dochan", receipt.id)
    staged = inbox.layout.inbox_attachment_path("dochan", PDF_SHA, "pdf")

    assert staged.is_file()
    assert staged.read_bytes() == PDF
    assert staged.parent == inbox.layout.inbox_writer_dir("dochan") / "_attach"

    fm, body = _read(event)
    assert body == "a summary of the pdf"
    assert fm["attachments"] == [
        {
            "sha256": PDF_SHA,
            "ext": "pdf",
            "filename": "report.pdf",
            "media_type": "application/pdf",
            "bytes": len(PDF),
        }
    ]
    # ...and `attachments` is LAST, so every pre-existing key keeps its DATA-MODEL §1 position.
    assert list(fm)[-1] == "attachments"


def test_read_back_resolves_the_staged_path_and_verifies_the_digest(inbox: Inbox) -> None:
    receipt = inbox.write(
        text="summary", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    event = inbox.layout.inbox_item_path("dochan", receipt.id)

    (staged,) = event_attachments(event)
    assert staged.record.sha256 == PDF_SHA
    assert staged.record.media_type is None  # unknown stays absent, never guessed
    assert staged.exists
    assert read_attachment(staged) == PDF


def test_read_refuses_bytes_that_no_longer_match_their_content_address(inbox: Inbox) -> None:
    receipt = inbox.write(
        text="summary", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    event = inbox.layout.inbox_item_path("dochan", receipt.id)
    (staged,) = event_attachments(event)
    staged.path.write_bytes(b"swapped")  # tamper: the name no longer describes the content

    with pytest.raises(AttachmentIntegrityError):
        read_attachment(staged)


def test_no_attachments_leaves_the_event_byte_identical(inbox: Inbox) -> None:
    receipt = inbox.write(text="plain capture", writer="dochan", source="manual")
    fm, _ = _read(inbox.layout.inbox_item_path("dochan", receipt.id))

    assert "attachments" not in fm
    assert not (inbox.layout.inbox_writer_dir("dochan") / "_attach").exists()


def test_attachment_digest_is_of_the_raw_bytes_not_the_normalised_text(inbox: Inbox) -> None:
    """CRLF bytes must hash as themselves — `content_sha256` would normalise them away."""
    from agora_kb.core.hashing import content_sha256

    data = b"line one\r\nline two   \r\n\r\n"
    receipt = inbox.write(
        text="summary", writer="dochan", source="manual", attachments=[("a.txt", None, data)]
    )
    (staged,) = event_attachments(inbox.layout.inbox_item_path("dochan", receipt.id))

    assert staged.record.sha256 == hashlib.sha256(data).hexdigest()
    assert staged.record.sha256 != content_sha256(data.decode())
    assert attachment_sha256(data) == staged.record.sha256


# --- refusals -----------------------------------------------------------------------------------
def test_cap_refusal_uses_the_upload_cap_and_writes_nothing(inbox: Inbox) -> None:
    cap = default_attachment_byte_cap()
    assert cap == 25 * 1024 * 1024  # the ONE per-file upload bound (config.WebUploadConfig)

    with pytest.raises(AttachmentTooLargeError) as exc:
        inbox.write(
            text="summary",
            writer="dochan",
            source="manual",
            attachments=[("big.bin", None, b"x" * 11)],
            max_attachment_bytes=10,
        )

    assert "11 bytes" in str(exc.value)
    # A refused attachment yields NO event and NO bytes: the capture is loud, never half-delivered.
    assert inbox.depth() == 0
    assert not inbox.layout.inbox_writer_dir("dochan").exists()


def test_cap_is_refused_even_for_an_idempotent_redelivery(inbox: Inbox) -> None:
    """A refusal must not depend on retry timing (the check runs before the event_key lookup)."""
    inbox.write(text="first", writer="dochan", source="manual", event_key="k1")

    with pytest.raises(AttachmentTooLargeError):
        inbox.write(
            text="first",
            writer="dochan",
            source="manual",
            event_key="k1",
            attachments=[("big.bin", None, b"x" * 11)],
            max_attachment_bytes=10,
        )


def test_a_zero_byte_attachment_is_admitted(inbox: Inbox) -> None:
    receipt = inbox.write(
        text="summary", writer="dochan", source="manual", attachments=[("empty.txt", None, b"")]
    )
    (staged,) = event_attachments(inbox.layout.inbox_item_path("dochan", receipt.id))

    assert staged.record.bytes == 0
    assert read_attachment(staged) == b""


def test_write_refuses_a_non_bytes_payload(inbox: Inbox) -> None:
    with pytest.raises(AttachmentError):
        inbox.write(
            text="summary",
            writer="dochan",
            source="manual",
            attachments=[("a.pdf", None, "not bytes")],  # type: ignore[list-item]
        )


def test_staging_refuses_an_occupied_content_address_holding_other_bytes(inbox: Inbox) -> None:
    """A content-addressed file whose content is not what its name says is never reused."""
    planted = inbox.layout.inbox_attachment_path("dochan", PDF_SHA, "pdf")
    planted.parent.mkdir(parents=True)
    planted.write_bytes(b"different bytes under the right name")

    with pytest.raises(AttachmentIntegrityError):
        inbox.write(
            text="summary", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
        )
    assert inbox.depth() == 0


# --- extensions ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("report.pdf", "pdf"),
        ("REPORT.PDF", "pdf"),
        ("archive.tar.gz", "gz"),  # the LAST component only — never a dotted compound
        ("noext", DEFAULT_ATTACHMENT_EXT),
        ("trailing.", DEFAULT_ATTACHMENT_EXT),
        ("weird.c++", DEFAULT_ATTACHMENT_EXT),
        ("sidecar.meta", DEFAULT_ATTACHMENT_EXT),  # `meta` is reserved (ADR-0041 D1.4)
        ("long.abcdefghijklmnopq", DEFAULT_ATTACHMENT_EXT),  # 17 chars > the 16-char grammar
    ],
)
def test_extension_is_derived_under_the_d1_4_grammar(
    inbox: Inbox, filename: str, expected: str
) -> None:
    receipt = inbox.write(
        text="summary", writer="dochan", source="manual", attachments=[(filename, None, PDF)]
    )
    (staged,) = event_attachments(inbox.layout.inbox_item_path("dochan", receipt.id))

    assert staged.record.ext == expected
    assert staged.path.name == f"{PDF_SHA}.{expected}"
    assert not staged.path.name.endswith(".meta.yaml")  # lint L1-8b stays a valid sidecar test


def test_identical_bytes_under_two_extensions_stay_two_attachments(inbox: Inbox) -> None:
    receipt = inbox.write(
        text="summary",
        writer="dochan",
        source="manual",
        attachments=[("a.pdf", None, PDF), ("a.txt", None, PDF)],
    )
    staged = event_attachments(inbox.layout.inbox_item_path("dochan", receipt.id))

    assert sorted(item.record.ext for item in staged) == ["pdf", "txt"]
    assert all(item.exists for item in staged)


def test_a_repeated_payload_collapses_to_one_record_and_one_file(inbox: Inbox) -> None:
    receipt = inbox.write(
        text="summary",
        writer="dochan",
        source="manual",
        attachments=[("a.pdf", None, PDF), ("a-copy.pdf", None, PDF)],
    )
    staged = event_attachments(inbox.layout.inbox_item_path("dochan", receipt.id))

    assert len(staged) == 1
    assert list(attachment_dir(inbox.layout.inbox_writer_dir("dochan")).iterdir()) == [
        staged[0].path
    ]


def test_re_staging_the_same_bytes_across_two_events_is_a_no_op(inbox: Inbox) -> None:
    first = inbox.write(
        text="one", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    staged_path = inbox.layout.inbox_attachment_path("dochan", PDF_SHA, "pdf")
    before = staged_path.stat().st_mtime_ns

    second = inbox.write(
        text="two", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )

    assert first.id != second.id
    assert staged_path.stat().st_mtime_ns == before  # immutable: reused, never rewritten
    assert len(list(attachment_dir(inbox.layout.inbox_writer_dir("dochan")).iterdir())) == 1


# --- containment (invariant 5) ------------------------------------------------------------------
@pytest.mark.parametrize(
    "hostile",
    ["../../evil.pdf", "a/../../../etc/passwd", "..\\..\\evil.pdf", "x.pd/f", "‮exe.pdf"],
)
def test_a_hostile_filename_can_never_address_anything_outside_the_writer_namespace(
    inbox: Inbox, tmp_path: Path, hostile: str
) -> None:
    receipt = inbox.write(
        text="summary", writer="dochan", source="manual", attachments=[(hostile, None, PDF)]
    )
    (staged,) = event_attachments(inbox.layout.inbox_item_path("dochan", receipt.id))

    assert staged.path.parent == inbox.layout.inbox_writer_dir("dochan") / "_attach"
    assert staged.path.name == f"{PDF_SHA}.{staged.record.ext}"
    # Every file this write created is inside the repo.
    for path in tmp_path.rglob("*"):
        assert path.resolve().is_relative_to(tmp_path.resolve())


def test_a_symlinked_attach_directory_is_refused(inbox: Inbox, tmp_path: Path) -> None:
    """Character rules cannot see an inode — containment is checked against the RESOLVED parent."""
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    writer_dir = inbox.layout.inbox_writer_dir("dochan")
    writer_dir.mkdir(parents=True)
    (writer_dir / "_attach").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AttachmentContainmentError):
        inbox.write(
            text="summary", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
        )
    assert list(outside.iterdir()) == []  # nothing was written through the link
    assert inbox.depth() == 0


def test_an_invalid_writer_is_refused_before_any_attachment_is_staged(inbox: Inbox) -> None:
    from agora_kb.core.layout import InvalidWriterError

    with pytest.raises(InvalidWriterError):
        inbox.write(
            text="summary", writer="../evil", source="manual", attachments=[("a.pdf", None, PDF)]
        )
    assert not inbox.layout.inbox_dir.exists()


def test_a_read_only_schema_refuses_before_any_attachment_is_staged(
    inbox: Inbox, tmp_path: Path
) -> None:
    from agora_kb.config import ReadOnlySchemaVersionError

    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "taxonomy.yaml").write_text("schema_version: 1\n", encoding="utf-8")

    with pytest.raises(ReadOnlySchemaVersionError):
        inbox.write(
            text="summary", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
        )
    assert not inbox.layout.inbox_dir.exists()


# --- the staging directory is not an event namespace --------------------------------------------
def test_an_invalid_source_is_refused_before_any_attachment_is_staged(inbox: Inbox) -> None:
    """An ordinary argument typo must not orphan a full-size payload in the staging directory.

    `source`/`target`/`domain`/`tags` are validated by the `InboxItem` model, and the model is now
    built BEFORE the staging loop. Staging first meant `agora capture --file big.pdf --source
    manuel` refused loudly AND left 25 MiB behind that no event cites and nothing ever sweeps
    (`carry_attachments` only moves files an EVENT names).
    """
    with pytest.raises(ValueError):
        inbox.write(
            text="a capture",
            writer="dochan",
            source="not a valid source!",
            attachments=[("x.pdf", "application/pdf", PDF)],
        )

    assert not inbox.layout.inbox_attachment_dir("dochan").exists()
    assert inbox.depth() == 0


def test_a_non_kebab_tag_is_refused_before_any_attachment_is_staged(inbox: Inbox) -> None:
    """The same rule for every other field the model validates, not just `source`."""
    with pytest.raises(ValueError):
        inbox.write(
            text="a capture",
            writer="dochan",
            source="manual",
            tags=["Not Kebab"],
            attachments=[("x.pdf", "application/pdf", PDF)],
        )

    assert not inbox.layout.inbox_attachment_dir("dochan").exists()
    assert inbox.depth() == 0


def test_a_markdown_attachment_is_not_counted_as_a_pending_event(inbox: Inbox) -> None:
    receipt = inbox.write(
        text="summary", writer="dochan", source="manual", attachments=[("notes.md", None, PDF)]
    )
    (staged,) = event_attachments(inbox.layout.inbox_item_path("dochan", receipt.id))

    assert staged.path.suffix == ".md"
    assert inbox.depth() == 1  # the event, not the bytes
    assert inbox.last_write() is not None


def test_a_markdown_attachment_under_failed_is_not_a_failed_event(
    inbox: Inbox, tmp_path: Path
) -> None:
    """`_kb/failed/` is the one place events are addressed by a RECURSIVE glob."""
    run_dir = inbox.layout.failed_dir / "2026-09-04" / "run-1"
    (run_dir / "_attach").mkdir(parents=True)
    (run_dir / "2026-09-04T00-00-00.000Z--aaaaaa.md").write_text("---\nid: x\n---\n\nbody\n")
    (run_dir / "_attach" / f"{PDF_SHA}.md").write_bytes(PDF)

    assert failed_event_count(inbox.layout) == 1
    assert [p.name for p in iter_failed_events(inbox.layout)] == [
        "2026-09-04T00-00-00.000Z--aaaaaa.md"
    ]


# --- the bytes travel with the event ------------------------------------------------------------
def _claim_like_move(event: Path, dest_dir: Path) -> Path:
    """The one move every spool mover performs: rename the event, then carry its bytes."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / event.name
    source_dir = event.parent
    event.replace(dest)
    carry_attachments(dest, source_dir=source_dir)
    return dest


def test_carry_moves_the_bytes_when_no_other_event_cites_them(inbox: Inbox, tmp_path: Path) -> None:
    receipt = inbox.write(
        text="summary", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    event = inbox.layout.inbox_item_path("dochan", receipt.id)

    moved = _claim_like_move(event, tmp_path / "_kb" / "processing" / "run-1" / "events")

    (staged,) = event_attachments(moved)
    assert staged.path == attachment_dir(moved.parent) / f"{PDF_SHA}.pdf"
    assert read_attachment(staged) == PDF
    assert not inbox.layout.inbox_attachment_path("dochan", PDF_SHA, "pdf").exists()


def test_carry_copies_while_another_event_still_cites_the_same_bytes(
    inbox: Inbox, tmp_path: Path
) -> None:
    first = inbox.write(
        text="one", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    inbox.write(text="two", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)])
    event = inbox.layout.inbox_item_path("dochan", first.id)

    moved = _claim_like_move(event, tmp_path / "_kb" / "processing" / "run-1" / "events")

    # The claimed event has its bytes AND the event left behind still has its own.
    assert read_attachment(event_attachments(moved)[0]) == PDF
    assert inbox.layout.inbox_attachment_path("dochan", PDF_SHA, "pdf").read_bytes() == PDF


def test_carrying_the_LAST_of_two_co_citing_events_leaves_no_orphan(
    inbox: Inbox, tmp_path: Path
) -> None:
    """Both events gone from the writer dir ⇒ the staging dir is empty (no permanent orphan).

    The copy-vs-move rule only ever fires on the FIRST carry (a sibling still cites the file); the
    LAST carry finds the destination already populated and would short-circuit before the move, so
    without the reclaim the staged bytes stayed in ``_kb/inbox/<writer>/_attach/`` forever with no
    event citing them and nothing anywhere that sweeps the directory.
    """
    first = inbox.write(
        text="one", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    second = inbox.write(
        text="two", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    events_dir = tmp_path / "_kb" / "processing" / "run-1" / "events"

    moved_first = _claim_like_move(inbox.layout.inbox_item_path("dochan", first.id), events_dir)
    # After the first carry the file is still cited by the event left behind, so it is COPIED.
    assert inbox.layout.inbox_attachment_path("dochan", PDF_SHA, "pdf").is_file()
    moved_second = _claim_like_move(inbox.layout.inbox_item_path("dochan", second.id), events_dir)

    assert read_attachment(event_attachments(moved_first)[0]) == PDF
    assert read_attachment(event_attachments(moved_second)[0]) == PDF
    assert list(inbox.layout.inbox_attachment_dir("dochan").iterdir()) == []


def test_the_reclaim_keeps_the_source_when_the_destination_is_corrupt(
    inbox: Inbox, tmp_path: Path
) -> None:
    """A destination that no longer hashes to its name means the SOURCE is the only good copy."""
    first = inbox.write(
        text="one", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    second = inbox.write(
        text="two", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    events_dir = tmp_path / "_kb" / "processing" / "run-1" / "events"
    _claim_like_move(inbox.layout.inbox_item_path("dochan", first.id), events_dir)
    (attachment_dir(events_dir) / f"{PDF_SHA}.pdf").write_bytes(b"tampered")

    _claim_like_move(inbox.layout.inbox_item_path("dochan", second.id), events_dir)

    assert inbox.layout.inbox_attachment_path("dochan", PDF_SHA, "pdf").read_bytes() == PDF


def test_failed_events_are_visible_from_a_repo_nested_under_an_attach_directory(
    tmp_path: Path,
) -> None:
    """The `_attach/` test is asked of the path RELATIVE to `failed/`, never of the absolute one.

    A repo may legally live under a directory called ``_attach``; testing the absolute components
    there classified EVERY terminally-failed event as bytes, making the whole of ``_kb/failed/``
    invisible to ``agora status``, ``kb_status.failed`` and ``agora requeue`` at once.
    """
    layout = RepoLayout(tmp_path / "_attach" / "kb")
    run_dir = layout.failed_dir / "2026-09-04" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "2026-09-04T00-00-00.000Z--aaaaaa.md").write_text("---\nid: x\n---\n\nbody\n")

    assert failed_event_count(layout) == 1
    assert [p.name for p in iter_failed_events(layout)] == ["2026-09-04T00-00-00.000Z--aaaaaa.md"]


def test_carry_reports_missing_bytes_instead_of_raising(inbox: Inbox, tmp_path: Path) -> None:
    receipt = inbox.write(
        text="summary", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    event = inbox.layout.inbox_item_path("dochan", receipt.id)
    inbox.layout.inbox_attachment_path("dochan", PDF_SHA, "pdf").unlink()

    dest_dir = tmp_path / "_kb" / "processing" / "run-1" / "events"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / event.name
    source_dir = event.parent
    event.replace(dest)
    carry = carry_attachments(dest, source_dir=source_dir)

    assert not carry.ok
    assert carry.missing == (f"{PDF_SHA}.pdf",)
    assert carry.errors == ()


def test_carry_is_idempotent_and_a_no_op_without_attachments(inbox: Inbox, tmp_path: Path) -> None:
    receipt = inbox.write(
        text="summary", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    event = inbox.layout.inbox_item_path("dochan", receipt.id)
    moved = _claim_like_move(event, tmp_path / "_kb" / "processing" / "run-1" / "events")

    again = carry_attachments(moved, source_dir=inbox.layout.inbox_writer_dir("dochan"))
    assert again.ok
    assert again.carried == (attachment_dir(moved.parent) / f"{PDF_SHA}.pdf",)

    plain = inbox.write(text="no bytes", writer="dochan", source="manual")
    plain_path = inbox.layout.inbox_item_path("dochan", plain.id)
    assert carry_attachments(plain_path, source_dir=plain_path.parent).ok


def test_the_back_edge_brings_the_bytes_home(inbox: Inbox, tmp_path: Path) -> None:
    """`agora requeue` / the curator retry path must not strand an event's bytes (#99, #124)."""
    receipt = inbox.write(
        text="summary", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    event = inbox.layout.inbox_item_path("dochan", receipt.id)
    failed = _claim_like_move(event, inbox.layout.failed_dir / "2026-09-04" / "run-1")

    verdict = return_event_to_inbox(inbox.layout, failed)

    assert verdict.ok
    returned = inbox.layout.inbox_item_path("dochan", receipt.id)
    assert returned.is_file()
    assert read_attachment(event_attachments(returned)[0]) == PDF
    assert not (attachment_dir(failed.parent) / f"{PDF_SHA}.pdf").exists()


# --- frontmatter parsing ------------------------------------------------------------------------
def test_parse_attachments_is_fail_loud_on_a_malformed_list() -> None:
    assert parse_attachments({}) == ()
    for bad in (
        {"attachments": "not-a-list"},
        {"attachments": ["not-a-mapping"]},
        {"attachments": [{"sha256": "short", "ext": "pdf", "bytes": 1}]},
        {"attachments": [{"sha256": PDF_SHA, "ext": "pdf", "bytes": 1, "surprise": "x"}]},
        {"attachments": [{"sha256": PDF_SHA, "ext": "../../evil", "bytes": 1}]},
    ):
        with pytest.raises(AttachmentError):
            parse_attachments(bad)


def test_a_hand_edited_traversal_digest_cannot_reach_a_path(inbox: Inbox) -> None:
    receipt = inbox.write(
        text="summary", writer="dochan", source="manual", attachments=[("a.pdf", None, PDF)]
    )
    event = inbox.layout.inbox_item_path("dochan", receipt.id)
    event.write_text(
        event.read_text(encoding="utf-8").replace(PDF_SHA, "../../../../wiki/PWNED"),
        encoding="utf-8",
    )

    with pytest.raises(AttachmentError):
        event_attachments(event)


def test_the_ext_grammar_is_enforced_at_the_model_boundary() -> None:
    """(pydantic v2 wraps the validator's raise, and ValidationError subclasses ValueError.)"""
    from agora_kb.core.layout import validate_attachment_ext
    from agora_kb.core.models import Attachment

    with pytest.raises(InvalidAttachmentExtError):
        validate_attachment_ext("meta")
    with pytest.raises(ValueError, match="reserved"):
        Attachment(sha256=PDF_SHA, ext="meta", bytes=1)
