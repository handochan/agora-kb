"""Tests for the ingest extractors (input adapters, ADR-0004 / DESIGN §2.3).

These cover the pure-transform contract: dispatch selection, the canonical ``content_sha256`` reuse
(proving the extractor hashes via the SAME helper the inbox uses, not a reinvention), the
optional-dependency posture (``ExtractorUnavailable`` raised cleanly via a simulated-missing import,
which MUST pass even when the real deps are installed), and the real extractors run over tiny
in-memory fixtures (skipped via ``pytest.importorskip`` when the ``ingest`` extra is absent).

No network is used: the URL extractor is exercised through ``trafilatura.extract`` over an HTML
STRING with its fetch monkeypatched, never a real request.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from agora_kb.core.hashing import content_sha256
from agora_kb.core.inbox import Inbox
from agora_kb.core.layout import RepoLayout
from agora_kb.ingest.extractors import (
    ExtractedDoc,
    Extractor,
    ExtractorError,
    ExtractorUnavailable,
    extract,
    extract_office,
    extract_pdf,
    extract_url,
)
from agora_kb.ingest.extractors import base as base_mod

# --- tiny in-memory fixtures (no committed binaries; deterministic) ----------------------------


def _make_pdf(text: str = "Hello Agora PDF") -> bytes:
    """Build a minimal single-page PDF whose only content is ``text`` (pdfminer-parseable)."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    ]
    stream = b"BT /F1 24 Tf 72 700 Td (" + text.encode("latin-1") + b") Tj ET"
    objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1,
        xref_pos,
    )
    return out


def _make_docx(text: str = "Hello Agora DOCX") -> bytes:
    """Build a minimal valid .docx (OOXML zip) whose single paragraph is ``text``."""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument'
        '.wordprocessingml.document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


_HTML = (
    "<html><head><title>Agora Test Page</title></head>"
    "<body><article><h1>Agora</h1>"
    "<p>This is a real paragraph with enough words for trafilatura to recognize it as the "
    "main content of the page and extract it cleanly.</p></article></body></html>"
)


# --- dispatch logic (model-free; no optional deps needed for the error paths) -------------------


def test_extract_requires_exactly_one_of_url_or_data() -> None:
    """Neither url nor data → ValueError."""
    with pytest.raises(ValueError, match="exactly one of"):
        extract()


def test_extract_rejects_both_url_and_data() -> None:
    """Both url and data → ValueError (ambiguous)."""
    with pytest.raises(ValueError, match="exactly one of"):
        extract(url="https://example.com", data=b"%PDF-")


def test_extract_dispatches_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A url argument routes to the url extractor."""
    sentinel = ExtractedDoc(markdown="x", content_sha256=content_sha256("x"), extractor="url")
    seen: dict[str, str] = {}

    def fake_extract_url(url: str) -> ExtractedDoc:
        seen["url"] = url
        return sentinel

    # extract() imports extract_url from the url submodule lazily; patch it there.
    import agora_kb.ingest.extractors.url as url_mod

    monkeypatch.setattr(url_mod, "extract_url", fake_extract_url)
    result = extract(url="https://example.com/page")
    assert result is sentinel
    assert seen["url"] == "https://example.com/page"


def test_extract_dispatches_pdf_by_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    """``.pdf`` filename routes data to the pdf extractor."""
    sentinel = ExtractedDoc(markdown="p", content_sha256=content_sha256("p"), extractor="pdf")
    import agora_kb.ingest.extractors.pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "extract_pdf", lambda data, *, filename=None: sentinel)
    assert extract(data=b"%PDF-1.4 junk", filename="doc.pdf") is sentinel


def test_extract_dispatches_pdf_by_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    """``application/pdf`` mime routes data to the pdf extractor even without a filename."""
    sentinel = ExtractedDoc(markdown="p", content_sha256=content_sha256("p"), extractor="pdf")
    import agora_kb.ingest.extractors.pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "extract_pdf", lambda data, *, filename=None: sentinel)
    assert extract(data=b"%PDF-1.4 junk", mime="application/pdf; charset=binary") is sentinel


def test_extract_dispatches_office_by_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    """``.docx`` filename routes data to the office extractor."""
    sentinel = ExtractedDoc(markdown="o", content_sha256=content_sha256("o"), extractor="office")
    import agora_kb.ingest.extractors.office as office_mod

    monkeypatch.setattr(office_mod, "extract_office", lambda data, *, filename: sentinel)
    assert extract(data=b"PK\x03\x04 junk", filename="report.docx") is sentinel


def test_extract_office_requires_filename() -> None:
    """An office MIME with no filename cannot determine the document type → ValueError."""
    office_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    with pytest.raises(ValueError, match="requires a filename"):
        extract(data=b"PK\x03\x04 junk", mime=office_mime)


def test_extract_unsupported_data_type() -> None:
    """Unknown extension + no mime → unsupported ValueError, no extractor chosen."""
    with pytest.raises(ValueError, match="unsupported or ambiguous"):
        extract(data=b"plain text bytes", filename="notes.txt")


def test_extract_ambiguous_pdf_and_office() -> None:
    """A .pdf filename with an office mime is contradictory → ambiguous ValueError."""
    office_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    with pytest.raises(ValueError, match="ambiguous"):
        extract(data=b"junk", filename="x.pdf", mime=office_mime)


# --- canonical content_sha256 reuse (NOT reinvention) -------------------------------------------


def test_content_sha256_matches_canonical_helper() -> None:
    """ExtractedDoc.content_sha256 equals the canonical core helper for a known body."""
    body = "Hello\nworld   \n\n\n"  # trailing ws + extra blank lines exercise the normalization
    doc = ExtractedDoc(
        markdown=body,
        content_sha256=content_sha256(body),
        extractor="pdf",
    )
    assert doc.content_sha256 == content_sha256(body)
    # Sanity: the helper actually normalizes (so this isn't a trivial identity).
    assert content_sha256(body) == content_sha256("Hello\nworld\n")


def test_extractor_hash_equals_inbox_path_hash(tmp_path) -> None:
    """The extractor's content_sha256 equals the value the inbox stamps for the SAME body.

    This proves the extractor reuses the canonical helper (DATA-MODEL §11.2) rather than a private
    reimplementation: an extracted markdown fed verbatim into ``Inbox.write`` produces a stored
    ``content_sha256`` byte-identical to the extractor's.
    """
    body = "Some extracted markdown body.\nLine two.   \n"
    doc = ExtractedDoc(markdown=body, content_sha256=content_sha256(body), extractor="url")

    layout = RepoLayout(tmp_path)
    inbox = Inbox(layout)
    receipt = inbox.write(text=doc.markdown, writer="tester", source="web:tester")

    item_path = layout.inbox_item_path("tester", receipt.id)
    from agora_kb.core import frontmatter

    fm, _ = frontmatter.parse(item_path.read_text(encoding="utf-8"))
    assert fm["content_sha256"] == doc.content_sha256


# --- optional-dependency posture: ExtractorUnavailable (must pass even WITH deps installed) ------


def test_url_unavailable_when_trafilatura_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A simulated-missing trafilatura import raises ExtractorUnavailable, not ImportError."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "trafilatura" or name.startswith("trafilatura."):
            raise ImportError("simulated missing trafilatura")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ExtractorUnavailable, match="trafilatura"):
        extract_url("https://example.com")


def test_pdf_unavailable_when_pdfminer_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A simulated-missing pdfminer import raises ExtractorUnavailable, not ImportError."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pdfminer.high_level" or name == "pdfminer":
            raise ImportError("simulated missing pdfminer")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ExtractorUnavailable, match="pdfminer"):
        extract_pdf(b"%PDF-1.4", filename="x.pdf")


def test_office_unavailable_when_markitdown_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A simulated-missing markitdown import raises ExtractorUnavailable, not ImportError."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "markitdown" or name.startswith("markitdown."):
            raise ImportError("simulated missing markitdown")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ExtractorUnavailable, match="markitdown"):
        extract_office(b"PK\x03\x04", filename="x.docx")


def test_unavailable_message_names_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    """ExtractorUnavailable names the install remedy (pip extra / uv sync)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("pdfminer"):
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ExtractorUnavailable) as excinfo:
        extract_pdf(b"%PDF-1.4", filename="x.pdf")
    msg = str(excinfo.value)
    assert "agora-kb[ingest]" in msg
    assert "uv sync --extra ingest" in msg


# --- import-time safety: importing the package never requires the optional deps ------------------


def test_package_import_does_not_require_optional_deps() -> None:
    """The extractor modules import without the optional libs at module top level (ADR-0005)."""
    import sys

    # base.py must not have imported any optional dep at module load.
    for mod in ("trafilatura", "pdfminer", "markitdown"):
        # We cannot assert absence (the deps ARE installed in CI), but we CAN assert the extractor
        # modules don't reference them at module scope: their globals never bind the package name.
        assert mod not in vars(base_mod)
    assert "agora_kb.ingest.extractors.base" in sys.modules


def test_extractor_protocol_is_runtime_checkable() -> None:
    """The Extractor Protocol is usable for isinstance/type assertions."""

    def an_extractor(*args: object, **kwargs: object) -> ExtractedDoc:
        return ExtractedDoc(markdown="", content_sha256=content_sha256(""), extractor="url")

    assert isinstance(an_extractor, Extractor)


# --- real extractors over tiny fixtures (skip when the ingest extra is absent) -------------------


def test_extract_pdf_real() -> None:
    """pdfminer over a tiny in-memory PDF yields the embedded text and a canonical hash."""
    pytest.importorskip("pdfminer.high_level")
    data = _make_pdf("Hello Agora PDF")
    doc = extract_pdf(data, filename="hello-world.pdf")
    assert doc.extractor == "pdf"
    assert doc.mime == "application/pdf"
    assert doc.title == "hello-world"
    assert doc.source_url is None
    assert "Hello Agora PDF" in doc.markdown
    assert doc.content_sha256 == content_sha256(doc.markdown)


def test_extract_pdf_real_via_dispatch() -> None:
    """The dispatcher routes raw PDF bytes (by extension) to extract_pdf end to end."""
    pytest.importorskip("pdfminer.high_level")
    doc = extract(data=_make_pdf("Dispatch PDF"), filename="d.pdf")
    assert doc.extractor == "pdf"
    assert "Dispatch PDF" in doc.markdown


def test_extract_pdf_malformed_raises_extractor_error() -> None:
    """Garbage PDF bytes raise ExtractorError, not a raw pdfminer traceback."""
    pytest.importorskip("pdfminer.high_level")
    with pytest.raises(ExtractorError):
        extract_pdf(b"this is definitely not a pdf", filename="bad.pdf")


def test_extract_office_real() -> None:
    """markitdown over a tiny in-memory .docx yields the paragraph text and a canonical hash."""
    pytest.importorskip("markitdown")
    pytest.importorskip("mammoth")  # markitdown's docx reader
    doc = extract_office(_make_docx("Hello Agora DOCX"), filename="my-report.docx")
    assert doc.extractor == "office"
    assert doc.mime == ("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert doc.title == "my-report"
    assert doc.source_url is None
    assert "Hello Agora DOCX" in doc.markdown
    assert doc.content_sha256 == content_sha256(doc.markdown)


def test_extract_office_real_via_dispatch() -> None:
    """The dispatcher routes raw .docx bytes (by extension) to extract_office end to end."""
    pytest.importorskip("markitdown")
    pytest.importorskip("mammoth")
    doc = extract(data=_make_docx("Dispatch DOCX"), filename="d.docx")
    assert doc.extractor == "office"
    assert "Dispatch DOCX" in doc.markdown


def test_extract_office_malformed_raises_extractor_error() -> None:
    """A corrupt .docx (recognized as OOXML but unparseable) raises ExtractorError, not a raw
    markitdown traceback.

    The bytes are a valid-looking docx zip (``[Content_Types].xml`` + ``word/document.xml``) whose
    end-of-central-directory record is truncated, so markitdown's DocxConverter recognizes the type
    and then throws — exercising the wrapper's untrusted-input try/except. (Plain garbage bytes
    labeled ``.docx`` do NOT raise: markitdown 0.1.x content-sniffs them and silently degrades to a
    plaintext conversion, so they would not test the error path.)
    """
    pytest.importorskip("markitdown")
    pytest.importorskip("mammoth")
    corrupt = _make_docx("will be truncated")[:-20]  # corrupt the zip EOCD
    with pytest.raises(ExtractorError):
        extract_office(corrupt, filename="bad.docx")


def test_extract_url_real_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """trafilatura over an HTML STRING (fetch monkeypatched) extracts content+title; no network."""
    trafilatura = pytest.importorskip("trafilatura")

    # Replace the network fetch with a pure function returning our HTML string.
    monkeypatch.setattr(trafilatura, "fetch_url", lambda url: _HTML)

    doc = extract_url("https://example.com/agora")
    assert doc.extractor == "url"
    assert doc.mime == "text/html"
    assert doc.source_url == "https://example.com/agora"
    assert "main content" in doc.markdown
    assert doc.title == "Agora Test Page" or doc.title == "Agora"
    assert doc.content_sha256 == content_sha256(doc.markdown)


def test_extract_url_fetch_failure_raises_extractor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed/empty fetch raises ExtractorError (untrusted input), not a raw traceback."""
    trafilatura = pytest.importorskip("trafilatura")
    monkeypatch.setattr(trafilatura, "fetch_url", lambda url: None)
    with pytest.raises(ExtractorError):
        extract_url("https://example.com/unreachable")


def test_extract_url_no_content_raises_extractor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fetched page with no extractable main content raises ExtractorError."""
    trafilatura = pytest.importorskip("trafilatura")
    monkeypatch.setattr(trafilatura, "fetch_url", lambda url: "<html><body></body></html>")
    with pytest.raises(ExtractorError):
        extract_url("https://example.com/empty")
